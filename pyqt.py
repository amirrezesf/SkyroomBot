"""
Skyroom Bot - PyQt6 GUI

Run:
    python pyqt.py
    python pyqt.py --json users.json --auto-start

Persian text is rendered natively by Qt (no arabic_reshaper / bidi needed).
All settings, the last opened JSON file, and window layout are persisted
via QSettings (~/.config/SkyroomBot/SkyroomBotGUI.conf on Linux,
%APPDATA%/SkyroomBot/SkyroomBotGUI.ini on Windows).
"""

import json
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    from PyQt6.QtCore import (
        Qt, QTimer, pyqtSignal, QObject, QSize, QSettings, QUrl
    )
    from PyQt6.QtGui import (
        QAction, QFont, QColor, QTextCharFormat, QTextCursor,
        QFontDatabase, QIcon, QDesktopServices
    )
    from PyQt6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QGridLayout, QSplitter, QTreeWidget, QTreeWidgetItem, QPushButton,
        QLabel, QTabWidget, QLineEdit, QSpinBox, QCheckBox, QComboBox,
        QRadioButton, QButtonGroup, QPlainTextEdit, QMessageBox,
        QFileDialog, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
        QMenu, QStatusBar, QFrame, QSizePolicy, QScrollArea, QTextEdit,
        QListWidget, QListWidgetItem, QAbstractItemView,
    )
except ImportError as e:
    print("PyQt6 is not installed. Run:\n    pip install PyQt6")
    print(f"(import error: {e})")
    sys.exit(1)

from skyroom_core import (
    RuntimeConfig,
    SkyroomBot,
    TEHRAN_TZ,
    parse_time_string,
    PERSIAN_DAYS,
    next_occurrence,
)


# ============================================================
# QSettings keys
# ============================================================
SETTINGS_ORG = "SkyroomBot"
SETTINGS_APP = "SkyroomBotGUI"

# ============================================================
# Project metadata (also used in the About dialog and links)
# ============================================================
APP_NAME         = "ربات اسکای‌روم"
APP_VERSION      = "2.0"
GITHUB_URL       = "https://github.com/amirrezesf/SkyroomBot"
GITHUB_ISSUES    = f"{GITHUB_URL}/issues"
GITHUB_RELEASES  = f"{GITHUB_URL}/releases/latest"

# ============================================================
# Platform detection (for sleep/wake)
# ============================================================
IS_WINDOWS = platform.system() == "Windows"
IS_LINUX   = platform.system() == "Linux"
IS_MAC     = platform.system() == "Darwin"

# Name of the Windows Task Scheduler task used for wake
WINDOWS_TASK_NAME = "SkyroomBotWake"


# ============================================================
# Windows one-time setup PowerShell script (self-elevating)
#
# Exit codes:
#   0 - success
#   1 - setup failed (details printed to console)
#   2 - UAC prompt cancelled
# ============================================================
_WINDOWS_SETUP_PS1 = r'''
# skyroom_setup.ps1 - self-elevating setup for SkyroomBot
$ErrorActionPreference = "Stop"

# ---- self-elevate (with -Wait so the parent waits for us) ----
if (-NOT ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    try {
        $proc = Start-Process powershell -Verb RunAs -Wait -PassThru -ArgumentList @(
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", "`"$PSCommandPath`""
        )
        exit $proc.ExitCode
    } catch {
        Write-Host ""
        Write-Host "Elevation was cancelled or denied."
        Write-Host "Setup cannot continue without Administrator rights."
        Start-Sleep -Seconds 3
        exit 2
    }
}

# ---- elevated: do the real work ----
try {
    Write-Host ""
    Write-Host "=== SkyroomBot setup ==="
    Write-Host ""

    # 1. Hibernation
    Write-Host "[1/3] Enabling hibernation..."
    try {
        $hibStatus = (powercfg /a 2>&1 | Out-String)
        if ($hibStatus -match "Hibernate") {
            Write-Host "       Already available."
        } else {
            powercfg /hibernate on
            Write-Host "       Enabled."
        }
    } catch {
        Write-Host "       WARNING: could not enable hibernation: $_"
    }

    # 2. Wake timers
    Write-Host "[2/3] Allowing wake timers..."
    powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
    powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
    powercfg /setactive SCHEME_CURRENT
    Write-Host "       Enabled for AC and battery."

    # 3. Pre-register the wake task (disabled placeholder)
    Write-Host "[3/3] Registering wake task (placeholder)..."

    $xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>SkyroomBot class wake timer (managed by SkyroomBot app).</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>2030-01-01T00:00:00</StartBoundary>
      <Enabled>false</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>true</WakeToRun>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>cmd.exe</Command>
      <Arguments>/c exit</Arguments>
    </Exec>
  </Actions>
</Task>
"@

    $tmp = [System.IO.Path]::GetTempFileName()
    [System.IO.File]::WriteAllText($tmp, $xml, [System.Text.Encoding]::Unicode)

    $existing = schtasks /Query /TN SkyroomBotWake 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "       Task exists - updating."
    } else {
        Write-Host "       Creating new task."
    }
    schtasks /Create /TN SkyroomBotWake /XML $tmp /F | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "schtasks failed with code $LASTEXITCODE"
    }
    Remove-Item $tmp -ErrorAction SilentlyContinue
    Write-Host "       Task 'SkyroomBotWake' is registered."

    Write-Host ""
    Write-Host "=== Setup complete ==="
    Write-Host ""
    Write-Host "You can close this window and use 'Arm Wake & Sleep' in the app."
    Write-Host ""
    Write-Host "Press any key to close..."
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    exit 0
}
catch {
    Write-Host ""
    Write-Host "ERROR: $_" -ForegroundColor Red
    Write-Host ""
    Write-Host "Press any key to close..."
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    exit 1
}
'''


def settings() -> QSettings:
    return QSettings(SETTINGS_ORG, SETTINGS_APP)


# ============================================================
# Fonts & colours
# ============================================================
def _pick_persian_font() -> str:
    families = QFontDatabase.families()
    for name in ("Vazirmatn", "Vazir", "Sahel", "IRANSans",
                 "Tahoma", "Noto Sans Arabic", "DejaVu Sans"):
        if name in families:
            return name
    return ""


def _pick_mono_font() -> str:
    families = QFontDatabase.families()
    for name in ("JetBrains Mono", "Fira Code", "Cascadia Mono",
                 "Source Code Pro", "DejaVu Sans Mono", "Courier New"):
        if name in families:
            return name
    return "monospace"


LOG_COLORS = {
    "INFO":    QColor("#d0e8ff"),
    "WARNING": QColor("#ffcc66"),
    "ERROR":   QColor("#ff8080"),
    "DEBUG":   QColor("#a0a0a0"),
}

_RTL_ALIGN = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter


# ============================================================
# Button styling helper
# ============================================================
_BUTTON_PALETTES = {
    "start": {
        "bg": "#d9ead3", "bg_hover": "#c3ddb9",
        "bg_press": "#a8c79c", "bg_disabled": "#3a3a3a",
        "fg": "#1a1a1a", "fg_disabled": "#666666",
    },
    "stop": {
        "bg": "#f4cccc", "bg_hover": "#e8b8b8",
        "bg_press": "#d9a3a3", "bg_disabled": "#3a3a3a",
        "fg": "#1a1a1a", "fg_disabled": "#666666",
    },
    "sleep": {
        "bg": "#ffe599", "bg_hover": "#ffd966",
        "bg_press": "#f0c94a", "bg_disabled": "#3a3a3a",
        "fg": "#1a1a1a", "fg_disabled": "#666666",
    },
    "neutral": {
        "bg": "#e6e6e6", "bg_hover": "#d4d4d4",
        "bg_press": "#bfbfbf", "bg_disabled": "#3a3a3a",
        "fg": "#1a1a1a", "fg_disabled": "#666666",
    },
}


def apply_button_style(button, kind: str = "neutral", big: bool = False):
    p = _BUTTON_PALETTES.get(kind, _BUTTON_PALETTES["neutral"])
    size = "font-size: 14px;" if big else ""
    button.setStyleSheet(f"""
        QPushButton {{
            background: {p['bg']};
            color: {p['fg']};
            border-radius: 6px;
            font-weight: bold;
            {size}
            padding: 4px 12px;
        }}
        QPushButton:hover:enabled {{
            background: {p['bg_hover']};
        }}
        QPushButton:pressed:enabled {{
            background: {p['bg_press']};
        }}
        QPushButton:focus {{ outline: none; }}
        QPushButton:disabled {{
            background: {p['bg_disabled']};
            color: {p['fg_disabled']};
        }}
    """)


# ============================================================
# Windows wake helpers
# ============================================================
def _windows_wake_task_xml(wake_local_iso: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Wakes the machine for a scheduled Skyroom class.</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>{wake_local_iso}</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>true</WakeToRun>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>cmd.exe</Command>
      <Arguments>/c exit</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _windows_arm_and_sleep(mode: str, wake_at) -> tuple[int, str]:
    try:
        local_wake = wake_at.astimezone()
    except Exception:
        local_wake = wake_at
    wake_iso = local_wake.strftime("%Y-%m-%dT%H:%M:%S")

    xml = _windows_wake_task_xml(wake_iso)

    tmp_dir = Path(os.environ.get("TEMP", "."))
    tmp_file = tmp_dir / f"skyroom_wake_{int(time.time() * 1000)}.xml"
    try:
        tmp_file.write_text(xml, encoding="utf-16")
    except Exception as e:
        return 1, f"could not write task XML: {e}"

    cf = 0
    if IS_WINDOWS:
        cf = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    try:
        create = subprocess.run(
            ["schtasks", "/Create",
             "/TN", WINDOWS_TASK_NAME,
             "/XML", str(tmp_file),
             "/F"],
            capture_output=True, text=True, creationflags=cf,
        )
    except Exception as e:
        return 1, f"schtasks /Create failed: {e}"
    finally:
        try:
            tmp_file.unlink()
        except OSError:
            pass

    if create.returncode != 0:
        err = (create.stderr or create.stdout or "").strip()
        low = err.lower()
        if "access is denied" in low or "denied" in low:
            err += ("\n\nThis operation needs Administrator privileges. "
                    "Right-click the app (or its shortcut) and choose "
                    "'Run as administrator' once to create the task.")
        return create.returncode, err

    if mode == "mem":
        sleep_cmd = ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]
    else:
        sleep_cmd = ["shutdown", "/h"]

    try:
        sleep_proc = subprocess.run(
            sleep_cmd, capture_output=True, text=True, creationflags=cf,
        )
    except Exception as e:
        subprocess.run(["schtasks", "/Delete", "/TN", WINDOWS_TASK_NAME, "/F"],
                       capture_output=True, text=True, creationflags=cf)
        return 1, f"sleep command failed to launch: {e}"

    if sleep_proc.returncode != 0:
        err = (sleep_proc.stderr or sleep_proc.stdout or "").strip()
        low = err.lower()
        if "hibernate" in low or "not enabled" in low:
            err += ("\n\nHibernation is not enabled. Run this once as "
                    "Administrator:\n    powercfg /hibernate on")
        subprocess.run(["schtasks", "/Delete", "/TN", WINDOWS_TASK_NAME, "/F"],
                       capture_output=True, text=True, creationflags=cf)
        return sleep_proc.returncode, err

    subprocess.run(["schtasks", "/Delete", "/TN", WINDOWS_TASK_NAME, "/F"],
                   capture_output=True, text=True, creationflags=cf)
    return 0, ""


# ============================================================
# Signals bridge
# ============================================================
class BotBridge(QObject):
    log = pyqtSignal(str, str)
    finished = pyqtSignal()


# ============================================================
# Dialogs
# ============================================================
class UserDialog(QDialog):
    def __init__(self, parent=None, user: dict = None):
        super().__init__(parent)
        self.setWindowTitle("ویرایش کاربر" if user else "افزودن کاربر")
        self.setModal(True)
        self.result_value: str | None = None

        self.name_edit = QLineEdit(user.get("user_name", "") if user else "")
        self.name_edit.setLayoutDirection(Qt.LayoutDirection.RightToLeft)

        form = QFormLayout(self)
        form.addRow("نام کاربر:", self.name_edit)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("تایید")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("انصراف")
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

        self.resize(360, 120)

    def _ok(self):
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "خطا", "نام کاربر نمی‌تواند خالی باشد")
            return
        self.result_value = name
        self.accept()


class ClassDialog(QDialog):
    DAYS = ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه",
            "چهارشنبه", "پنجشنبه", "جمعه"]

    def __init__(self, parent=None, cls: dict = None):
        super().__init__(parent)
        self.setWindowTitle("ویرایش کلاس" if cls else "افزودن کلاس")
        self.setModal(True)
        self.result_value: dict | None = None

        cls = cls or {}

        self.name_edit = QLineEdit(cls.get("name", ""))
        self.name_edit.setLayoutDirection(Qt.LayoutDirection.RightToLeft)

        self.url_edit = QLineEdit(cls.get("url", ""))
        self.url_edit.setLayoutDirection(Qt.LayoutDirection.LeftToRight)

        self.day_combo = QComboBox()
        self.day_combo.addItems(self.DAYS)
        if cls.get("day") in self.DAYS:
            self.day_combo.setCurrentText(cls["day"])

        self.start_edit = QLineEdit(cls.get("time", "10:00am"))
        self.start_edit.setLayoutDirection(Qt.LayoutDirection.LeftToRight)

        self.end_edit = QLineEdit(cls.get("end_time", "12:00pm"))
        self.end_edit.setLayoutDirection(Qt.LayoutDirection.LeftToRight)

        form = QFormLayout(self)
        form.addRow("نام کلاس:", self.name_edit)
        form.addRow("آدرس:", self.url_edit)
        form.addRow("روز:", self.day_combo)
        form.addRow("زمان شروع:", self.start_edit)
        form.addRow("زمان پایان:", self.end_edit)

        hint = QLabel("قالب زمان: 10:00am / 2:30pm / 22:30")
        hint.setStyleSheet("color: gray; font-size: 11px;")
        form.addRow("", hint)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("تایید")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("انصراف")
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

        self.resize(480, 260)

    def _ok(self):
        name = self.name_edit.text().strip()
        url = self.url_edit.text().strip()
        day = self.day_combo.currentText().strip()
        time_str = self.start_edit.text().strip()
        end_str = self.end_edit.text().strip()

        if not name:
            QMessageBox.warning(self, "خطا", "نام کلاس نمی‌تواند خالی باشد")
            return
        if not url:
            QMessageBox.warning(self, "خطا", "آدرس نمی‌تواند خالی باشد")
            return
        if day not in PERSIAN_DAYS:
            QMessageBox.warning(self, "خطا", f"روز نامعتبر: {day}")
            return
        try:
            start_t = parse_time_string(time_str)
        except ValueError as e:
            QMessageBox.warning(self, "خطا", f"زمان شروع: {e}")
            return
        try:
            end_t = parse_time_string(end_str)
        except ValueError as e:
            QMessageBox.warning(self, "خطا", f"زمان پایان: {e}")
            return
        if end_t <= start_t:
            QMessageBox.warning(self, "خطا",
                                "زمان پایان باید بعد از زمان شروع باشد")
            return

        self.result_value = {
            "name": name,
            "url": url,
            "day": day,
            "time": time_str,
            "end_time": end_str,
        }
        self.accept()


# ============================================================
# Main window
# ============================================================
class SkyroomGUI(QMainWindow):

    SLEEP_MODES = [
        ("حالت خواب (Suspend S3) — بازگشت سریع، مصرف کم باتری", "mem"),
        ("هایبرنیت (Hibernate S4) — بدون مصرف باتری، بازگشت کندتر", "disk"),
        ("خاموش کامل (Power off S5) — نیاز به پشتیبانی BIOS", "off"),
    ]

    def __init__(self):
        super().__init__()
        self.setWindowTitle("مدیریت ربات اسکای‌روم")
        self.resize(1300, 820)

        self.users: list = []
        self.json_path: Path | None = None
        self.bot: SkyroomBot | None = None
        self.bot_thread: threading.Thread | None = None

        self.bridge = BotBridge()
        self.bridge.log.connect(self._append_log)
        self.bridge.finished.connect(self._on_bot_finished)

        self.cfg = self._load_config_from_settings()

        self._build_ui()
        self._apply_debug_toggle()
        self._apply_send_message_toggle()
        self._refresh_tree()
        self.refresh_next_wake()

        self._restore_ui_state()

        self._tick = QTimer(self)
        self._tick.timeout.connect(self.refresh_next_wake)
        self._tick.start(30_000)

        QTimer.singleShot(4000, self._maybe_show_star_hint)

    # =========================================================
    # Persistence
    # =========================================================
    def _load_config_from_settings(self) -> RuntimeConfig:
        s = settings()
        return RuntimeConfig(
            send_message=s.value("send_message", True, type=bool),
            chat_message=s.value("chat_message", "سلام", type=str),
            page_load_timeout=s.value("page_load_timeout", 60, type=int),
            element_timeout=s.value("element_timeout", 30, type=int),
            ws_check_interval=s.value("ws_check_interval", 5, type=int),
            ws_dead_grace=s.value("ws_dead_grace", 15, type=int),
            max_restarts=s.value("max_restarts", 30, type=int),
            min_delay_min=s.value("min_delay_min", 10, type=int),
            max_delay_min=s.value("max_delay_min", 15, type=int),
            debug_mode=s.value("debug_mode", False, type=bool),
            debug_run_now=s.value("debug_run_now", False, type=bool),
            debug_disable_random_delay=s.value(
                "debug_disable_random_delay", False, type=bool),
            debug_disable_ws_check=s.value(
                "debug_disable_ws_check", False, type=bool),
        )

    def _restore_ui_state(self):
        s = settings()

        geo = s.value("window/geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        wstate = s.value("window/state")
        if wstate is not None:
            self.restoreState(wstate)

        splitter_state = s.value("window/splitter")
        if splitter_state is not None and hasattr(self, "_splitter"):
            self._splitter.restoreState(splitter_state)

        tab = s.value("window/active_tab", 0, type=int)
        if 0 <= tab < self.tabs.count():
            self.tabs.setCurrentIndex(tab)

        self.v_wake_lead.setValue(s.value("wake/lead_min", 3, type=int))
        self.v_auto_after_wake.setChecked(
            s.value("wake/auto_start", True, type=bool))
        mode = s.value("wake/sleep_mode", "disk", type=str)
        if mode in self._sleep_radio_by_value:
            self._sleep_radio_by_value[mode].setChecked(True)

        if self.json_path is None:
            last = s.value("files/last_json", "", type=str)
            if last:
                p = Path(last)
                if p.is_file():
                    try:
                        self._load_json_from_path(p)
                    except Exception:
                        pass

    def _save_all_state(self):
        s = settings()
        cfg = self._collect_config()

        s.setValue("send_message", cfg.send_message)
        s.setValue("chat_message", cfg.chat_message)
        s.setValue("page_load_timeout", cfg.page_load_timeout)
        s.setValue("element_timeout", cfg.element_timeout)
        s.setValue("ws_check_interval", cfg.ws_check_interval)
        s.setValue("ws_dead_grace", cfg.ws_dead_grace)
        s.setValue("max_restarts", cfg.max_restarts)
        s.setValue("min_delay_min", cfg.min_delay_min)
        s.setValue("max_delay_min", cfg.max_delay_min)

        s.setValue("debug_mode", cfg.debug_mode)
        s.setValue("debug_run_now", cfg.debug_run_now)
        s.setValue("debug_disable_random_delay",
                   cfg.debug_disable_random_delay)
        s.setValue("debug_disable_ws_check", cfg.debug_disable_ws_check)

        s.setValue("wake/lead_min", self.v_wake_lead.value())
        s.setValue("wake/auto_start", self.v_auto_after_wake.isChecked())
        for val, rb in self._sleep_radio_by_value.items():
            if rb.isChecked():
                s.setValue("wake/sleep_mode", val)
                break

        if self.json_path is not None:
            s.setValue("files/last_json", str(self.json_path))

        s.setValue("window/geometry", self.saveGeometry())
        s.setValue("window/state", self.saveState())
        if hasattr(self, "_splitter"):
            s.setValue("window/splitter", self._splitter.saveState())
        s.setValue("window/active_tab", self.tabs.currentIndex())

        s.sync()

    def _reset_settings(self):
        r = QMessageBox.question(
            self, "بازنشانی تنظیمات",
            "تمام تنظیمات ذخیره‌شده (شامل مسیر فایل JSON و چیدمان پنجره) "
            "بازنشانی شود؟\n\n"
            "برنامه بسته و دوباره باز نخواهد شد — تغییرات با راه‌اندازی بعدی "
            "اعمال می‌شوند.",
            QMessageBox.StandardButton.Yes |
            QMessageBox.StandardButton.No,
        )
        if r != QMessageBox.StandardButton.Yes:
            return
        settings().clear()
        settings().sync()
        self._append_log("WARNING", "تنظیمات بازنشانی شد — "
                                    "برای اعمال کامل، برنامه را دوباره باز کنید")

    def _forget_json(self):
        s = settings()
        s.remove("files/last_json")
        s.sync()
        self.json_path = None
        self.v_json_path.setText("(فایلی بارگذاری نشده)")
        self._append_log("INFO", "مسیر فایل JSON ذخیره‌شده فراموش شد")

    # ---------------------------------------------------------
    # UI
    # ---------------------------------------------------------
    def _build_ui(self):
        self._build_menu()

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(6, 6, 6, 6)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(self._splitter, 1)

        self._splitter.addWidget(self._build_left_panel())
        self._splitter.addWidget(self._build_right_panel())
        self._splitter.setStretchFactor(0, 3)
        self._splitter.setStretchFactor(1, 2)
        self._splitter.setSizes([720, 560])

        outer.addWidget(self._build_action_bar())

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("آماده")

        repo_short = GITHUB_URL.replace("https://", "")
        link = QLabel(
            f'<a href="{GITHUB_URL}" style="color: #7a8ba8; '
            f'text-decoration: none;">⭐ {repo_short}</a>'
        )
        link.setOpenExternalLinks(True)
        link.setToolTip("باز کردن پروژه در گیت‌هاب")
        link.setContentsMargins(0, 0, 8, 0)
        self.status.addPermanentWidget(link)

    def _build_menu(self):
        menubar = self.menuBar()

        file_menu = menubar.addMenu("&پرونده")
        act_load = QAction("بارگذاری فایل JSON…", self)
        act_load.triggered.connect(self.load_json)
        file_menu.addAction(act_load)

        act_save = QAction("ذخیره", self)
        act_save.triggered.connect(self.save_json)
        file_menu.addAction(act_save)

        act_save_as = QAction("ذخیره در…", self)
        act_save_as.triggered.connect(self.save_json_as)
        file_menu.addAction(act_save_as)

        file_menu.addSeparator()

        act_forget = QAction("فراموش کردن فایل JSON ذخیره‌شده", self)
        act_forget.triggered.connect(self._forget_json)
        file_menu.addAction(act_forget)

        act_reset = QAction("بازنشانی تنظیمات…", self)
        act_reset.triggered.connect(self._reset_settings)
        file_menu.addAction(act_reset)

        file_menu.addSeparator()
        act_quit = QAction("خروج", self)
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

        help_menu = menubar.addMenu("&راهنما")

        act_github = QAction("مشاهده در گیت‌هاب", self)
        act_github.setShortcut("F1")
        act_github.triggered.connect(
            lambda: QDesktopServices.openUrl(QUrl(GITHUB_URL))
        )
        help_menu.addAction(act_github)

        act_releases = QAction("بررسی به‌روزرسانی…", self)
        act_releases.triggered.connect(
            lambda: QDesktopServices.openUrl(QUrl(GITHUB_RELEASES))
        )
        help_menu.addAction(act_releases)

        act_issues = QAction("گزارش مشکل…", self)
        act_issues.triggered.connect(
            lambda: QDesktopServices.openUrl(QUrl(GITHUB_ISSUES))
        )
        help_menu.addAction(act_issues)

        help_menu.addSeparator()

        act_about = QAction("درباره", self)
        act_about.triggered.connect(self._about)
        help_menu.addAction(act_about)

    def _build_left_panel(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)

        title = QLabel("کاربران و کلاس‌ها")
        title.setStyleSheet("font-weight: bold; font-size: 13px;")
        v.addWidget(title)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["کاربر / کلاس", "نام کلاس",
                                    "روز", "شروع", "پایان"])
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_tree_menu)
        self.tree.itemDoubleClicked.connect(self._on_tree_double)
        self.tree.itemSelectionChanged.connect(self._on_tree_selection)
        self.tree.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.tree.header().setDefaultAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.tree.setColumnWidth(0, 200)
        self.tree.setColumnWidth(1, 220)
        self.tree.setColumnWidth(2, 90)
        self.tree.setColumnWidth(3, 80)
        self.tree.setColumnWidth(4, 80)
        v.addWidget(self.tree, 1)

        btn_row = QHBoxLayout()
        b_add_user = QPushButton("افزودن کاربر")
        b_add_class = QPushButton("افزودن کلاس")
        b_edit = QPushButton("ویرایش")
        b_del = QPushButton("حذف")
        for b in (b_add_user, b_add_class, b_edit, b_del):
            apply_button_style(b, "neutral")
        b_add_user.clicked.connect(self.add_user)
        b_add_class.clicked.connect(self.add_class)
        b_edit.clicked.connect(self.edit_selected)
        b_del.clicked.connect(self.delete_selected)
        for b in (b_add_user, b_add_class, b_edit, b_del):
            btn_row.addWidget(b)
        v.addLayout(btn_row)

        return w

    def _build_right_panel(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_general_tab(), "عمومی")
        self.tabs.addTab(self._build_wake_tab(), "خواب و بیدارباش")
        self.tabs.addTab(self._build_debug_tab(), "اشکال‌زدایی")
        self.tabs.addTab(self._build_log_tab(), "گزارش")
        v.addWidget(self.tabs)

        return w

    def _build_general_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(12, 12, 12, 12)
        self.v_json_path = QLineEdit()
        self.v_json_path.setReadOnly(True)
        self.v_json_path.setPlaceholderText("فایلی بارگذاری نشده")
        self.v_json_path.setLayoutDirection(Qt.LayoutDirection.LeftToRight)

        form.addRow("فایل کاربران:", self.v_json_path)

        self.v_send_message = QCheckBox("ارسال پیام در هنگام ورود")
        self.v_send_message.setChecked(self.cfg.send_message)
        self.v_send_message.stateChanged.connect(
            self._apply_send_message_toggle)
        form.addRow("پیام چت:", self.v_send_message)

        self.v_chat_message = QLineEdit(self.cfg.chat_message)
        self.v_chat_message.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        form.addRow("متن پیام:", self.v_chat_message)

        def _spin(val, lo=0, hi=100000):
            s = QSpinBox()
            s.setRange(lo, hi)
            s.setValue(val)
            return s

        self.v_page_timeout = _spin(self.cfg.page_load_timeout, 5, 600)
        self.v_elem_timeout = _spin(self.cfg.element_timeout, 5, 600)
        self.v_ws_interval = _spin(self.cfg.ws_check_interval, 1, 300)
        self.v_ws_grace = _spin(self.cfg.ws_dead_grace, 0, 300)
        self.v_max_restarts = _spin(self.cfg.max_restarts, 1, 500)
        self.v_min_delay = _spin(self.cfg.min_delay_min, 0, 240)
        self.v_max_delay = _spin(self.cfg.max_delay_min, 0, 240)

        form.addRow("مهلت بارگذاری صفحه (ثانیه):", self.v_page_timeout)
        form.addRow("مهلت یافتن عنصر (ثانیه):", self.v_elem_timeout)
        form.addRow("فاصله بررسی WS (ثانیه):", self.v_ws_interval)
        form.addRow("مهلت قطع WS (ثانیه):", self.v_ws_grace)
        form.addRow("حداکثر تلاش مجدد:", self.v_max_restarts)
        form.addRow("حداقل تاخیر تصادفی (دقیقه):", self.v_min_delay)
        form.addRow("حداکثر تاخیر تصادفی (دقیقه):", self.v_max_delay)

        hint = QLabel(
            "تمام زمان‌ها بر اساس منطقه زمانی آسیا/تهران تفسیر می‌شوند.\n"
            "اگر دکمه «شروع» در میانه کلاس زده شود، ربات بلافاصله وارد می‌شود."
        )
        hint.setStyleSheet("color: gray; font-size: 11px;")
        hint.setWordWrap(True)
        form.addRow("", hint)

        scroll.setWidget(w)
        return scroll

    def _build_wake_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(12, 12, 12, 12)

        title = QLabel("زمان‌بندی خواب و بیدارباش")
        title.setStyleSheet("font-weight: bold; font-size: 13px;")
        v.addWidget(title)

        desc = QLabel(
            "زمان بیداری RTC را فعال می‌کند و لپ‌تاپ را به خواب می‌برد. "
            "پس از بیدار شدن، همان برنامه ادامه می‌یابد و (در صورت فعال بودن) "
            "ربات به صورت خودکار شروع می‌کند.\n\n"
            "اولین‌بار که روی دکمهٔ پایین کلیک کنید، برنامه به‌طور خودکار "
            "دسترسی لازم را از شما درخواست می‌کند."
        )
        desc.setStyleSheet("color: gray; font-size: 11px;")
        desc.setWordWrap(True)
        v.addWidget(desc)

        mode_box = QGroupBox("حالت خواب")
        mode_layout = QVBoxLayout(mode_box)
        self.v_sleep_mode = QButtonGroup(self)
        self._sleep_radio_by_value = {}
        for i, (text, val) in enumerate(self.SLEEP_MODES):
            rb = QRadioButton(text)
            rb.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
            self.v_sleep_mode.addButton(rb, i)
            self._sleep_radio_by_value[val] = rb
            mode_layout.addWidget(rb)
            if val == "disk":
                rb.setChecked(True)
        v.addWidget(mode_box)

        lead_row = QHBoxLayout()
        lead_row.addWidget(QLabel("بیدار شدن چند دقیقه قبل از کلاس:"))
        self.v_wake_lead = QSpinBox()
        self.v_wake_lead.setRange(0, 60)
        self.v_wake_lead.setValue(3)
        self.v_wake_lead.valueChanged.connect(self.refresh_next_wake)
        lead_row.addWidget(self.v_wake_lead)
        lead_row.addStretch(1)
        v.addLayout(lead_row)

        v.addWidget(QLabel("زمان بیداری بعدی:"))
        self.v_next_wake = QLabel("(برنامه‌ای بارگذاری نشده)")
        self.v_next_wake.setStyleSheet(
            "color: #2a7f42; font-size: 15px; font-weight: bold;")
        self.v_next_wake.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        v.addWidget(self.v_next_wake)

        refresh_btn = QPushButton("به‌روزرسانی")
        apply_button_style(refresh_btn, "neutral")
        refresh_btn.clicked.connect(self.refresh_next_wake)
        v.addWidget(refresh_btn, alignment=Qt.AlignmentFlag.AlignLeft)

        self.v_auto_after_wake = QCheckBox(
            "پس از بیداری، ربات به صورت خودکار شروع شود")
        self.v_auto_after_wake.setChecked(True)
        v.addWidget(self.v_auto_after_wake)

        self.btn_arm_sleep = QPushButton("💤  خواب و بیدارباش")
        self.btn_arm_sleep.setMinimumHeight(52)
        apply_button_style(self.btn_arm_sleep, "sleep", big=True)
        self.btn_arm_sleep.clicked.connect(self.arm_wake_and_sleep)
        v.addWidget(self.btn_arm_sleep)

        # NOTE: the standalone "Setup permission" button was removed.
        # arm_wake_and_sleep() now detects missing permissions on its own
        # and prompts the user at the right moment.

        v.addStretch(1)

        scroll.setWidget(w)
        return scroll

    def _build_debug_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(12, 12, 12, 12)

        title = QLabel("کلید اصلی اشکال‌زدایی")
        title.setStyleSheet("font-weight: bold; font-size: 13px;")
        v.addWidget(title)

        self.v_debug_mode = QCheckBox("فعال‌سازی حالت اشکال‌زدایی")
        self.v_debug_mode.setStyleSheet("font-weight: bold;")
        self.v_debug_mode.setChecked(self.cfg.debug_mode)
        self.v_debug_mode.stateChanged.connect(self._apply_debug_toggle)
        v.addWidget(self.v_debug_mode)

        hint = QLabel(
            "وقتی غیرفعال است، تمام گزینه‌های زیر نادیده گرفته می‌شوند."
        )
        hint.setStyleSheet("color: gray; font-size: 11px;")
        hint.setWordWrap(True)
        v.addWidget(hint)

        self.cb_run_now = QCheckBox("اجرای فوری (نادیده گرفتن زمان‌بندی)")
        self.cb_run_now.setChecked(self.cfg.debug_run_now)
        self.cb_no_delay = QCheckBox("غیرفعال کردن تاخیر تصادفی")
        self.cb_no_delay.setChecked(self.cfg.debug_disable_random_delay)
        self.cb_no_ws = QCheckBox("غیرفعال کردن نظارت بر WebSocket")
        self.cb_no_ws.setChecked(self.cfg.debug_disable_ws_check)
        v.addWidget(self.cb_run_now)
        v.addWidget(self.cb_no_delay)
        v.addWidget(self.cb_no_ws)

        v.addStretch(1)
        return w

    def _build_log_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)

        bar = QHBoxLayout()
        clear_btn = QPushButton("پاک کردن")
        apply_button_style(clear_btn, "neutral")
        clear_btn.clicked.connect(self._clear_log)
        bar.addWidget(clear_btn)

        self.v_autoscroll = QCheckBox("اسکرول خودکار")
        self.v_autoscroll.setChecked(True)
        bar.addWidget(self.v_autoscroll)
        bar.addStretch(1)

        wrapper = QWidget()
        wrapper.setLayout(bar)
        v.addWidget(wrapper)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet(
            "QPlainTextEdit { background: #111; color: #eee; }"
        )
        mono = _pick_mono_font()
        if mono:
            self.log_view.setFont(QFont(mono, 10))
        v.addWidget(self.log_view, 1)

        return w

    def _build_action_bar(self) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(4, 4, 4, 4)

        self.btn_start = QPushButton("▶  شروع")
        self.btn_start.setMinimumHeight(38)
        apply_button_style(self.btn_start, "start")
        self.btn_start.clicked.connect(self.start_bot)

        self.btn_stop = QPushButton("■  توقف")
        self.btn_stop.setMinimumHeight(38)
        apply_button_style(self.btn_stop, "stop")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_bot)

        h.addWidget(self.btn_start)
        h.addWidget(self.btn_stop)
        h.addStretch(1)

        self.v_status = QLabel("آماده")
        self.v_status.setStyleSheet("color: #888;")
        h.addWidget(self.v_status)

        return w

    # ---------------------------------------------------------
    # Config <-> UI
    # ---------------------------------------------------------
    def _apply_debug_toggle(self):
        enabled = self.v_debug_mode.isChecked()
        for cb in (self.cb_run_now, self.cb_no_delay, self.cb_no_ws):
            cb.setEnabled(enabled)

    def _apply_send_message_toggle(self):
        self.v_chat_message.setEnabled(self.v_send_message.isChecked())

    def _collect_config(self) -> RuntimeConfig:
        return RuntimeConfig(
            send_message=self.v_send_message.isChecked(),
            chat_message=self.v_chat_message.text().strip() or "سلام",
            page_load_timeout=self.v_page_timeout.value(),
            element_timeout=self.v_elem_timeout.value(),
            ws_check_interval=self.v_ws_interval.value(),
            ws_dead_grace=self.v_ws_grace.value(),
            max_restarts=self.v_max_restarts.value(),
            min_delay_min=self.v_min_delay.value(),
            max_delay_min=self.v_max_delay.value(),
            debug_mode=self.v_debug_mode.isChecked(),
            debug_run_now=self.cb_run_now.isChecked(),
            debug_disable_random_delay=self.cb_no_delay.isChecked(),
            debug_disable_ws_check=self.cb_no_ws.isChecked(),
        )

    # ---------------------------------------------------------
    # JSON I/O
    # ---------------------------------------------------------
    def load_json(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "انتخاب فایل کاربران", "",
            "JSON files (*.json);;All files (*)")
        if not p:
            return
        self._load_json_from_path(Path(p))

    def _load_json_from_path(self, path: Path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            QMessageBox.critical(self, "خطا", f"خواندن فایل ناموفق:\n{e}")
            return
        if not isinstance(data, list):
            QMessageBox.critical(self, "خطا",
                                 "ساختار JSON باید یک لیست از کاربران باشد")
            return
        for u in data:
            if not isinstance(u, dict) or "user_name" not in u:
                QMessageBox.critical(self, "خطا",
                                     "هر کاربر باید کلید user_name داشته باشد")
                return
            u.setdefault("classes", [])
        self.users = data
        self.json_path = path
        self.v_json_path.setText(str(path))
        self._refresh_tree()
        self.refresh_next_wake()
        self._append_log("INFO", f"بارگذاری {len(self.users)} کاربر از {path}")

    def save_json(self):
        if self.json_path is None:
            return self.save_json_as()
        try:
            self.json_path.write_text(
                json.dumps(self.users, ensure_ascii=False, indent=4),
                encoding="utf-8",
            )
            self._append_log("INFO", f"ذخیره در {self.json_path}")
        except Exception as e:
            QMessageBox.critical(self, "خطا", f"ذخیره ناموفق:\n{e}")

    def save_json_as(self):
        p, _ = QFileDialog.getSaveFileName(
            self, "ذخیره فایل کاربران", "users.json",
            "JSON files (*.json);;All files (*)")
        if not p:
            return
        self.json_path = Path(p)
        self.v_json_path.setText(str(self.json_path))
        self.save_json()

    # ---------------------------------------------------------
    # Tree
    # ---------------------------------------------------------
    def _refresh_tree(self):
        self.tree.clear()
        self._tree_map = {}

        for user in self.users:
            uid = QTreeWidgetItem([user.get("user_name", "?"), "", "", "", ""])
            for col in range(5):
                uid.setTextAlignment(col, _RTL_ALIGN)
            self.tree.addTopLevelItem(uid)
            self._tree_map[id(uid)] = ("user", user, None)

            for cls in user.get("classes", []):
                cid = QTreeWidgetItem([
                    "",
                    cls.get("name", ""),
                    cls.get("day", ""),
                    cls.get("time", ""),
                    cls.get("end_time", ""),
                ])
                for col in range(5):
                    cid.setTextAlignment(col, _RTL_ALIGN)
                uid.addChild(cid)
                self._tree_map[id(cid)] = ("class", user, cls)

        self.tree.expandAll()

    def _selected(self):
        items = self.tree.selectedItems()
        if not items:
            return None
        it = items[0]
        return it, self._tree_map.get(id(it))

    def _on_tree_double(self, item, _col):
        self.edit_selected()

    def _on_tree_selection(self):
        pass

    def _on_tree_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None:
            return
        self.tree.setCurrentItem(item)
        info = self._tree_map.get(id(item))
        if not info:
            return
        kind, _user, _cls = info

        menu = QMenu(self)
        if kind == "user":
            menu.addAction("افزودن کلاس به این کاربر", self.add_class)
        menu.addAction("ویرایش", self.edit_selected)
        menu.addAction("حذف", self.delete_selected)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def add_user(self):
        dlg = UserDialog(self)
        if dlg.exec() and dlg.result_value:
            self.users.append({"user_name": dlg.result_value, "classes": []})
            self._refresh_tree()
            self._append_log("INFO", f"کاربر «{dlg.result_value}» اضافه شد")

    def add_class(self):
        sel = self._selected()
        if not sel:
            QMessageBox.information(self, "توجه", "ابتدا یک کاربر انتخاب کنید")
            return
        kind, user, _cls = sel[1]
        if kind != "user":
            user = sel[1][1]

        dlg = ClassDialog(self)
        if dlg.exec() and dlg.result_value:
            user.setdefault("classes", []).append(dlg.result_value)
            self._refresh_tree()
            self.refresh_next_wake()
            self._append_log(
                "INFO",
                f"کلاس «{dlg.result_value['name']}» برای "
                f"«{user['user_name']}» اضافه شد",
            )

    def edit_selected(self):
        sel = self._selected()
        if not sel:
            return
        _item, (kind, user, cls) = sel

        if kind == "user":
            dlg = UserDialog(self, user=user)
            if dlg.exec() and dlg.result_value:
                old = user["user_name"]
                user["user_name"] = dlg.result_value
                self._refresh_tree()
                self._append_log(
                    "INFO", f"نام «{old}» به «{dlg.result_value}» تغییر یافت")
        else:
            dlg = ClassDialog(self, cls=cls)
            if dlg.exec() and dlg.result_value:
                cls.update(dlg.result_value)
                self._refresh_tree()
                self.refresh_next_wake()
                self._append_log("INFO", f"کلاس «{cls['name']}» ویرایش شد")

    def delete_selected(self):
        sel = self._selected()
        if not sel:
            return
        _item, (kind, user, cls) = sel

        if kind == "user":
            r = QMessageBox.question(
                self, "تایید",
                f"کاربر «{user['user_name']}» و تمام کلاس‌هایش حذف شود؟",
                QMessageBox.StandardButton.Yes |
                QMessageBox.StandardButton.No,
            )
            if r != QMessageBox.StandardButton.Yes:
                return
            self.users.remove(user)
            self._append_log("INFO", f"کاربر «{user['user_name']}» حذف شد")
        else:
            r = QMessageBox.question(
                self, "تایید",
                f"کلاس «{cls['name']}» حذف شود؟",
                QMessageBox.StandardButton.Yes |
                QMessageBox.StandardButton.No,
            )
            if r != QMessageBox.StandardButton.Yes:
                return
            user["classes"].remove(cls)
            self._append_log("INFO", f"کلاس «{cls['name']}» حذف شد")

        self._refresh_tree()
        self.refresh_next_wake()

    # ---------------------------------------------------------
    # Bot control
    # ---------------------------------------------------------
    def start_bot(self):
        if self.bot is not None:
            QMessageBox.information(self, "توجه", "ربات در حال اجراست")
            return
        if not self.users:
            QMessageBox.information(self, "توجه",
                                    "ابتدا فایل کاربران را بارگذاری کنید")
            return

        cfg = self._collect_config().effective()

        self._append_log(
            "INFO",
            "درایور مرورگر به‌صورت خودکار انتخاب می‌شود "
            "(درایور داخلی، سپس نسخه سازگار — نیاز به اینترنت فقط بار اول)",
        )

        self.bot = SkyroomBot(cfg, self.users, self._emit_log)

        try:
            tasks = self.bot.start()
        except Exception as e:
            QMessageBox.critical(self, "خطا", f"شروع ربات ناموفق:\n{e}")
            self.bot = None
            return

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.v_status.setText(f"در حال اجرا: {len(tasks)} وظیفه")
        self._append_log("INFO", f"=== ربات با {len(tasks)} وظیفه شروع شد ===")
        self._append_log(
            "INFO",
            f"    پیام چت: "
            f"{'فعال' if cfg.send_message else 'غیرفعال'}"
        )
        for t in tasks:
            self._append_log(
                "INFO",
                f"  • {t.user_name}@{t.class_name}  "
                f"کلاس {t.scheduled_time:%a %Y-%m-%d %H:%M}"
                f"-{t.end_time:%H:%M} تهران",
            )

        self.bot_thread = threading.Thread(
            target=self._watch_bot, daemon=True)
        self.bot_thread.start()

    def _watch_bot(self):
        if self.bot is None:
            return
        for t in list(self.bot.threads):
            t.join()
        self.bridge.finished.emit()

    def _on_bot_finished(self):
        self.bot = None
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.v_status.setText("آماده")
        self._append_log("INFO", "=== تمام وظایف ربات پایان یافت ===")

    def stop_bot(self):
        if self.bot is None:
            return
        self.v_status.setText("در حال توقف…")
        self._append_log("WARNING", "درخواست توقف — بستن مرورگرها…")
        bot = self.bot

        def _bg():
            try:
                bot.stop()
            finally:
                self.bridge.finished.emit()
                self._emit_log("INFO", "ربات توسط کاربر متوقف شد")

        threading.Thread(target=_bg, daemon=True).start()

    # ---------------------------------------------------------
    # Wake scheduling
    # ---------------------------------------------------------
    def _compute_next_wake(self):
        if not self.users:
            return None, None
        soonest = None
        for u in self.users:
            for cls in u.get("classes", []):
                try:
                    dt = next_occurrence(cls["day"], cls["time"])
                except Exception:
                    continue
                if soonest is None or dt < soonest:
                    soonest = dt
        if soonest is None:
            return None, None
        wake_at = soonest - timedelta(minutes=self.v_wake_lead.value())
        return soonest, wake_at

    def refresh_next_wake(self):
        class_start, wake_at = self._compute_next_wake()
        if class_start is None:
            self.v_next_wake.setText("(کلاس معتبری در برنامه نیست)")
            return
        now = datetime.now(TEHRAN_TZ)
        if wake_at <= now:
            self.v_next_wake.setText(
                f"{wake_at:%Y-%m-%d %H:%M} — گذشته "
                f"(کلاس بعدی {class_start:%H:%M})"
            )
            return
        delta = wake_at - now
        h = int(delta.total_seconds() // 3600)
        m = int((delta.total_seconds() % 3600) // 60)
        self.v_next_wake.setText(
            f"{wake_at:%Y-%m-%d %H:%M:%S}  (تا {h} ساعت و {m} دقیقه دیگر)"
        )

    # ---------------------------------------------------------
    # Permission check + setup flow
    # ---------------------------------------------------------
    def _check_wake_permissions(self) -> bool:
        """
        Returns True if the current user can arm a wake without any
        additional prompts.

        Linux:  sudoers must have NOPASSWD for /usr/bin/rtcwake
        Windows: the SkyroomBotWake task must already exist
        """
        if IS_WINDOWS:
            cf = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            try:
                r = subprocess.run(
                    ["schtasks", "/Query", "/TN", WINDOWS_TASK_NAME],
                    capture_output=True, text=True,
                    creationflags=cf, timeout=8,
                )
                return r.returncode == 0
            except Exception:
                return False

        # ---- Linux / macOS ----
        try:
            r = subprocess.run(
                ["sudo", "-n", "-l", "/usr/bin/rtcwake"],
                capture_output=True, text=True, timeout=6,
            )
        except Exception:
            return False
        if r.returncode != 0:
            return False
        out = r.stdout or ""
        return "NOPASSWD" in out and "rtcwake" in out

    def _show_setup_prompt(self) -> bool:
        """
        Show the platform-appropriate permission setup dialog.
        Returns True if the setup succeeded, False if the user cancelled
        or the setup failed.
        """
        if IS_WINDOWS:
            return self._windows_setup_flow()
        else:
            return self._linux_setup_flow()

    def _linux_setup_flow(self) -> bool:
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or "$USER"
        cmd = (f"echo '{user} ALL=(ALL) NOPASSWD: /usr/bin/rtcwake' | "
               f"sudo tee /etc/sudoers.d/skyroom-rtcwake && "
               f"sudo chmod 440 /etc/sudoers.d/skyroom-rtcwake && "
               f"sudo -k")

        result = {"ok": False}

        dlg = QDialog(self)
        dlg.setWindowTitle("نیاز به دسترسی برای خواب و بیدارباش")
        dlg.resize(780, 340)
        v = QVBoxLayout(dlg)

        info = QLabel(
            "برای اجرای بیدارباش، برنامه به اجازهٔ اجرای rtcwake بدون "
            "رمز عبور نیاز دارد.\n\n"
            "این دستور را یک‌بار در ترمینال اجرا کنید، سپس روی "
            "«بررسی مجدد» بزنید:"
        )
        info.setWordWrap(True)
        v.addWidget(info)

        text = QTextEdit()
        text.setPlainText(cmd)
        text.setReadOnly(True)
        text.setFont(QFont(_pick_mono_font(), 10))
        text.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        text.setFixedHeight(80)
        v.addWidget(text)

        status = QLabel("")
        status.setWordWrap(True)
        v.addWidget(status)

        h = QHBoxLayout()
        copy_btn = QPushButton("کپی در حافظه")
        apply_button_style(copy_btn, "neutral")
        recheck_btn = QPushButton("بررسی مجدد")
        apply_button_style(recheck_btn, "start")
        cancel_btn = QPushButton("لغو")
        apply_button_style(cancel_btn, "stop")
        h.addWidget(copy_btn)
        h.addWidget(recheck_btn)
        h.addWidget(cancel_btn)
        h.addStretch(1)
        v.addLayout(h)

        def _do_copy():
            QApplication.clipboard().setText(cmd)
            status.setText("دستور در کلیپ‌بورد کپی شد.")
            status.setStyleSheet("color: #7a8ba8; font-size: 12px;")

        def _do_recheck():
            if self._check_wake_permissions():
                result["ok"] = True
                status.setText("✅ دسترسی تایید شد.")
                status.setStyleSheet(
                    "color: #2a7f42; font-size: 12px; font-weight: bold;")
                self._append_log("INFO", "دسترسی rtcwake تایید شد")
                QTimer.singleShot(600, dlg.accept)
            else:
                status.setText(
                    "❌ هنوز تنظیم نشده. مطمئن شوید دستور را در ترمینال "
                    "اجرا کرده‌اید.")
                status.setStyleSheet(
                    "color: #c0392b; font-size: 12px; font-weight: bold;")

        copy_btn.clicked.connect(_do_copy)
        recheck_btn.clicked.connect(_do_recheck)
        cancel_btn.clicked.connect(dlg.reject)

        dlg.exec()
        return result["ok"]

    def _windows_setup_flow(self) -> bool:
        result = {"ok": False}
        state = {"running": False}

        dlg = QDialog(self)
        dlg.setWindowTitle("نیاز به دسترسی برای خواب و بیدارباش")
        dlg.resize(740, 340)
        v = QVBoxLayout(dlg)

        info = QLabel(
            "برای فعال‌سازی خواب و بیدارباش خودکار، ویندوز به یک‌بار "
            "دسترسی Administrator نیاز دارد.\n\n"
            "با کلیک روی «اجرای راه‌اندازی»، برنامه مراحل زیر را "
            "خودکار انجام می‌دهد:\n"
            "  ۱) فعال‌سازی هایبرنیت\n"
            "  ۲) اجازهٔ Wake Timers\n"
            "  ۳) ثبت تسک بیداری\n\n"
            "ویندوز یک پنجرهٔ UAC نمایش می‌دهد. روی «Yes» بزنید."
        )
        info.setWordWrap(True)
        v.addWidget(info)

        status = QLabel("آماده برای اجرا.")
        status.setStyleSheet("color: gray; font-size: 12px;")
        status.setWordWrap(True)
        v.addWidget(status)

        h = QHBoxLayout()
        run_btn = QPushButton("اجرای راه‌اندازی")
        apply_button_style(run_btn, "start")
        copy_btn = QPushButton("کپی اسکریپت")
        apply_button_style(copy_btn, "neutral")
        cancel_btn = QPushButton("لغو")
        apply_button_style(cancel_btn, "stop")
        h.addWidget(run_btn)
        h.addWidget(copy_btn)
        h.addWidget(cancel_btn)
        h.addStretch(1)
        v.addLayout(h)

        def _do_copy():
            QApplication.clipboard().setText(_WINDOWS_SETUP_PS1)
            status.setText("اسکریپت در کلیپ‌بورد کپی شد.")
            status.setStyleSheet("color: #7a8ba8; font-size: 12px;")

        def _do_run():
            if state["running"]:
                return
            state["running"] = True
            run_btn.setEnabled(False)
            copy_btn.setEnabled(False)
            cancel_btn.setEnabled(False)
            status.setText("⏳ در حال اجرا… پنجرهٔ UAC را تایید کنید.")
            status.setStyleSheet("color: #7a8ba8; font-size: 12px;")
            threading.Thread(target=_bg_run, daemon=True).start()

        def _bg_run():
            try:
                tmp = Path(tempfile.gettempdir()) / "skyroom_setup.ps1"
                tmp.write_text(_WINDOWS_SETUP_PS1, encoding="utf-8-sig")
                cf = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                proc = subprocess.run(
                    ["powershell.exe",
                     "-NoProfile",
                     "-ExecutionPolicy", "Bypass",
                     "-File", str(tmp)],
                    capture_output=True, text=True, creationflags=cf,
                )
                rc = proc.returncode
                out = (proc.stdout or "").strip()
                err = (proc.stderr or "").strip()
                QTimer.singleShot(0, lambda: _on_done(rc, out, err))
            except Exception as e:
                QTimer.singleShot(0, lambda er=str(e): _on_done(1, "", er))

        def _on_done(rc, out, err):
            state["running"] = False
            run_btn.setEnabled(True)
            copy_btn.setEnabled(True)
            cancel_btn.setEnabled(True)

            if rc == 0:
                result["ok"] = True
                status.setText("✅ راه‌اندازی با موفقیت انجام شد.")
                status.setStyleSheet(
                    "color: #2a7f42; font-size: 12px; font-weight: bold;")
                self._append_log("INFO",
                                 "راه‌اندازی ویندوز با موفقیت انجام شد")
                QTimer.singleShot(700, dlg.accept)
            elif rc == 2:
                status.setText("⚠️ درخواست UAC لغو شد. راه‌اندازی انجام نشد.")
                status.setStyleSheet(
                    "color: #c98a00; font-size: 12px; font-weight: bold;")
                self._append_log("WARNING", "کاربر درخواست UAC را لغو کرد")
            else:
                detail = (err or out or "").strip()[:500]
                status.setText(
                    f"❌ راه‌اندازی ناموفق (کد {rc}). جزئیات در گزارش.")
                status.setStyleSheet(
                    "color: #c0392b; font-size: 12px; font-weight: bold;")
                self._append_log(
                    "ERROR",
                    f"راه‌اندازی ویندوز ناموفق (کد {rc}): {detail}")

        run_btn.clicked.connect(_do_run)
        copy_btn.clicked.connect(_do_copy)
        cancel_btn.clicked.connect(dlg.reject)

        dlg.exec()
        return result["ok"]

    def arm_wake_and_sleep(self):
        if not self.users:
            QMessageBox.information(self, "توجه",
                                    "ابتدا فایل کاربران را بارگذاری کنید")
            return

        class_start, wake_at = self._compute_next_wake()
        if wake_at is None:
            QMessageBox.critical(self, "خطا", "کلاس معتبری یافت نشد")
            return
        now = datetime.now(TEHRAN_TZ)
        if wake_at <= now:
            QMessageBox.critical(
                self, "خطا",
                f"زمان بیداری محاسبه شده در گذشته است:\n"
                f"{wake_at:%Y-%m-%d %H:%M:%S}"
            )
            return

        if self.bot is not None:
            r = QMessageBox.question(
                self, "ربات در حال اجراست",
                "ربات متوقف شود و سپس لپ‌تاپ به خواب برود؟",
                QMessageBox.StandardButton.Yes |
                QMessageBox.StandardButton.No,
            )
            if r != QMessageBox.StandardButton.Yes:
                return
            self.stop_bot()
            for _ in range(40):
                if self.bot is None:
                    break
                time.sleep(0.25)

        # ---- permission gate: check, then prompt if needed ----
        if not self._check_wake_permissions():
            self._append_log("INFO",
                             "دسترسی خواب/بیداری تنظیم نشده — درخواست از کاربر")
            if not self._show_setup_prompt():
                self._append_log("INFO", "کاربر راه‌اندازی را لغو کرد")
                return
            if not self._check_wake_permissions():
                QMessageBox.critical(
                    self, "راه‌اندازی کامل نشد",
                    "به نظر می‌رسد راه‌اندازی اولیه به درستی انجام نشده است.\n"
                    "دوباره تلاش کنید یا دستور را در ترمینال اجرا کنید."
                )
                return

        # ---- mode selection and confirmation ----
        mode = "disk"
        for val, rb in self._sleep_radio_by_value.items():
            if rb.isChecked():
                mode = val
                break

        if IS_WINDOWS:
            mode_name = {
                "mem":  "خواب (Suspend)",
                "disk": "هایبرنیت (Hibernate)",
                "off":  "هایبرنیت (Hibernate) — «خاموش کامل» روی ویندوز پشتیبانی نمی‌شود",
            }[mode]
        else:
            mode_name = {
                "mem":  "خواب (suspend)",
                "disk": "هایبرنیت (hibernate)",
                "off":  "خاموش کامل (power off)",
            }[mode]

        warn_off = ""
        if mode == "off" and IS_LINUX:
            warn_off = ("\n\nتوجه: خاموش کامل باعث می‌شود برنامه "
                        "پس از روشن شدن دوباره باز نشود.")
        elif mode == "off" and IS_WINDOWS:
            warn_off = ("\n\nتوجه: ویندوز از «خاموش کامل + بیداری خودکار» "
                        "پشتیبانی نمی‌کند. به جای آن هایبرنیت انجام می‌شود.")

        r = QMessageBox.question(
            self, "تایید",
            f"دستگاه {mode_name} شود.\n\n"
            f"شروع کلاس: {class_start:%Y-%m-%d %H:%M:%S} تهران\n"
            f"بیداری در: {wake_at:%Y-%m-%d %H:%M:%S} تهران\n\n"
            f"ادامه؟{warn_off}",
            QMessageBox.StandardButton.Yes |
            QMessageBox.StandardButton.No,
        )
        if r != QMessageBox.StandardButton.Yes:
            return

        self._save_all_state()

        self._append_log(
            "INFO",
            f"در حال تنظیم بیداری برای {wake_at:%Y-%m-%d %H:%M:%S} "
            f"و رفتن به حالت {mode_name}"
        )

        self.btn_arm_sleep.setEnabled(False)
        threading.Thread(
            target=self._do_arm_and_sleep,
            args=(mode, wake_at), daemon=True
        ).start()

    def _do_arm_and_sleep(self, mode: str, wake_at):
        try:
            if IS_WINDOWS:
                rc, err = _windows_arm_and_sleep(mode, wake_at)
            else:
                epoch = int(wake_at.timestamp())
                cmd = ["sudo", "-n", "rtcwake", "-m", mode, "-t", str(epoch)]
                self._emit_log("INFO",
                               "اجرای دستور: " + " ".join(cmd))
                proc = subprocess.run(cmd, capture_output=True, text=True)
                rc = proc.returncode
                err = (proc.stderr or proc.stdout or "").strip()
        except Exception as e:
            rc, err = 1, str(e)

        if rc != 0:
            self._emit_log("ERROR", f"خواب/بیداری ناموفق: {err}")
            QTimer.singleShot(0, lambda e=err: self._on_wake_error(e))
            return

        QTimer.singleShot(0, self._on_woke_up)

    def _on_wake_error(self, msg: str):
        self.btn_arm_sleep.setEnabled(True)
        low = (msg or "").lower()

        if IS_WINDOWS:
            if ("access is denied" in low or "denied" in low
                    or "administrator" in low):
                QMessageBox.critical(
                    self, "نیاز به دسترسی Administrator",
                    "برای ساخت تسک بیداری، برنامه باید یک‌بار با دسترسی "
                    "Administrator اجرا شود.\n\n"
                    "روی آیکن برنامه راست‌کلیک کنید و "
                    "«Run as administrator» را انتخاب کنید، یا از تب "
                    "«خواب و بیدارباش» دوباره تلاش کنید تا درخواست UAC "
                    "نمایش داده شود.\n\n"
                    "جزئیات خطا:\n" + msg
                )
            elif "hibernate" in low or "not enabled" in low:
                QMessageBox.critical(
                    self, "هایبرنیت فعال نیست",
                    "هایبرنیت روی این سیستم فعال نیست.\n\n"
                    "روی «خواب و بیدارباش» بزنید تا دوباره راه‌اندازی "
                    "خودکار اجرا شود."
                )
            else:
                QMessageBox.critical(self, "خواب/بیداری ناموفق",
                                     msg or "(بدون خروجی)")
        else:
            if ("sudo" in low or "password" in low or
                    "a terminal is required" in low or
                    "no tty present" in low):
                QMessageBox.critical(
                    self, "نیاز به دسترسی",
                    "برای اجرای rtcwake نیاز به دسترسی بدون رمز است.\n\n"
                    "دوباره روی «خواب و بیدارباش» بزنید تا مراحل "
                    "راه‌اندازی نمایش داده شود."
                )
            else:
                QMessageBox.critical(self, "خواب/بیداری ناموفق",
                                     msg or "(بدون خروجی)")

    def _on_woke_up(self):
        self.btn_arm_sleep.setEnabled(True)
        self._append_log("INFO", "=== بازگشت از خواب ===")
        self.refresh_next_wake()
        if self.v_auto_after_wake.isChecked():
            self._append_log("INFO", "شروع خودکار ربات در ۵ ثانیه…")
            QTimer.singleShot(5000, self._auto_start_after_wake)
        else:
            self._append_log("INFO", "شروع خودکار پس از بیداری غیرفعال است")

    def _auto_start_after_wake(self):
        if self.bot is not None:
            self._append_log("INFO", "ربات در حال اجراست؛ صرف‌نظر از شروع خودکار")
            return
        if not self.users:
            self._append_log("WARNING", "برنامه‌ای بارگذاری نشده")
            return
        self._append_log("INFO", "شروع خودکار ربات")
        self.start_bot()

    # ---------------------------------------------------------
    # Logging
    # ---------------------------------------------------------
    def _emit_log(self, level: str, message: str):
        self.bridge.log.emit(level, message)

    def _append_log(self, level: str, message: str):
        ts = time.strftime("%H:%M:%S")
        prefix = f"{ts} | {level:<7} | "
        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        fmt_prefix = QTextCharFormat()
        fmt_prefix.setForeground(QColor("#888888"))
        cursor.insertText(prefix, fmt_prefix)

        fmt_msg = QTextCharFormat()
        fmt_msg.setForeground(LOG_COLORS.get(level, QColor("#eee")))
        cursor.insertText(message + "\n", fmt_msg)

        if self.v_autoscroll.isChecked():
            self.log_view.verticalScrollBar().setValue(
                self.log_view.verticalScrollBar().maximum()
            )

    def _clear_log(self):
        self.log_view.clear()

    # ---------------------------------------------------------
    # درباره / معرفی پروژه
    # ---------------------------------------------------------
    def _about(self):
        s = settings()
        path = s.fileName()

        html = f"""
        <h2 style="margin: 0 0 4px 0;">{APP_NAME}</h2>
        <p style="color: #888; margin: 0 0 12px 0;">
          نسخه {APP_VERSION}
        </p>

        <p>
          ورود خودکار به کلاس‌های آنلاین اسکای‌روم با مرورگر ناشناس (incognito).
          ربات اتصال زنده را زیر نظر می‌گیرد و می‌تواند بین کلاس‌ها
          لپ‌تاپ را به خواب ببرد و در زمان مقرر بیدار کند.
        </p>

        <p>
          <b>صفحه پروژه:</b>
          <a href="{GITHUB_URL}">{GITHUB_URL}</a><br/>
          <b>آخرین نسخه:</b>
          <a href="{GITHUB_RELEASES}">{GITHUB_RELEASES}</a><br/>
          <b>گزارش مشکل:</b>
          <a href="{GITHUB_ISSUES}">ثبت یک issue جدید</a>
        </p>

        <p style="color: #888; font-size: 11px;">
          فایل تنظیمات:<br/>
          <code>{path}</code>
        </p>
        """

        box = QMessageBox(self)
        box.setWindowTitle(f"درباره {APP_NAME}")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(html)
        for label in box.findChildren(QLabel):
            label.setOpenExternalLinks(True)
            label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextBrowserInteraction
            )
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.button(QMessageBox.StandardButton.Ok).setText("تایید")
        box.exec()

    def _maybe_show_star_hint(self):
        s = settings()
        if s.value("promo/star_hint_shown", False, type=bool):
            return

        first_run = s.value("promo/first_run", "", type=str)
        if not first_run:
            s.setValue("promo/first_run",
                       datetime.now(TEHRAN_TZ).isoformat())
            s.sync()
            return

        s.setValue("promo/star_hint_shown", True)
        s.sync()

        r = QMessageBox.question(
            self,
            f"از {APP_NAME} راضی هستید؟",
            "اگر این برنامه باعث می‌شود خودتان دستی وارد کلاس‌ها نشوید، "
            "لطفاً با ستاره دادن به پروژه در گیت‌هاب از آن حمایت کنید. "
            "این کار به دیده‌شدن آن توسط بقیه دانشجوها کمک می‌کند.\n\n"
            "الان مخزن را باز کنید؟",
            QMessageBox.StandardButton.Yes |
            QMessageBox.StandardButton.No,
        )
        if r == QMessageBox.StandardButton.Yes:
            QDesktopServices.openUrl(QUrl(GITHUB_URL))

    def closeEvent(self, event):
        if self.bot is not None:
            r = QMessageBox.question(
                self, "خروج",
                "ربات در حال اجراست. متوقف و خارج شود؟",
                QMessageBox.StandardButton.Yes |
                QMessageBox.StandardButton.No,
            )
            if r != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.bot.stop(join_timeout=5)

        self._save_all_state()
        event.accept()


# ============================================================
# Entry point
# ============================================================
def main():
    app = QApplication(sys.argv)

    persian_font = _pick_persian_font()
    if persian_font:
        app.setFont(QFont(persian_font, 10))

    app.setApplicationName("Skyroom Bot")
    app.setOrganizationName(SETTINGS_ORG)
    app.setStyle("Fusion")

    if getattr(sys, 'frozen', False):
        icon_path = Path(sys._MEIPASS) / 'assets' / 'icon-512.png'
    else:
        icon_path = Path(__file__).parent / 'assets' / 'icon-512.png'
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    window = SkyroomGUI()

    if "--json" in sys.argv:
        idx = sys.argv.index("--json")
        if idx + 1 < len(sys.argv):
            p = Path(sys.argv[idx + 1])
            if p.is_file():
                window._load_json_from_path(p)

    window.show()

    if "--auto-start" in sys.argv and window.users:
        QTimer.singleShot(1000, window.start_bot)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()