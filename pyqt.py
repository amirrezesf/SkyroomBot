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
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    from PyQt6.QtCore import (
        Qt, QTimer, QEventLoop, pyqtSignal, QObject, QSize, QSettings, QUrl
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

from jarvis_integration import JARVIS_AVAILABLE, JarvisRuntime
from skyroom_core import (
    RuntimeConfig,
    SkyroomBot,
    TEHRAN_TZ,
    parse_time_string,
    PERSIAN_DAYS,
    next_occurrence_range,
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

# Absolute path of the rtcwake binary.  The sudoers rule written by the
# setup flow grants NOPASSWD for this exact path, so probing, granting and
# executing must all use the same string — running a bare "rtcwake" only
# works by accident of sudo's secure_path.
RTCWAKE_PATH = "/usr/bin/rtcwake"

# Where the one-time sudoers rule is installed. Must live in sudoers.d/, be
# owned by root and be mode 0440 or sudo ignores it.
LINUX_SUDOERS_FILE = "/etc/sudoers.d/skyroom-rtcwake"

# visudo is in sbin, which is typically NOT on a GUI app's PATH, so resolve it
# explicitly — otherwise sudoers validation silently doesn't happen.
VISUDO_PATH = shutil.which("visudo") or "/usr/sbin/visudo"


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
    # No "press any key": the parent waits on this process, so blocking here
    # would leave the app showing "in progress" until the user noticed a
    # console window they were never told about.
    exit 0
}
catch {
    Write-Host ""
    Write-Host "ERROR: $_" -ForegroundColor Red
    Write-Host ""
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


# ============================================================
# One-time elevation: ask the OS for the permission, don't ask the
# user to type commands.
#
# Linux  -> polkit (pkexec) shows its own password prompt, exactly like
#           "Run as administrator" on Windows. Used to install a single
#           sudoers rule so later rtcwake calls need no prompt at all.
# Windows-> the self-elevating PowerShell in _WINDOWS_SETUP_PS1 (UAC).
#
# Both paths are one-shot: after the rule exists, _check_wake_permissions()
# returns True and none of this runs again.
# ============================================================


def _linux_elevated_setup() -> tuple[int, str]:
    """
    Install the NOPASSWD rule for rtcwake, asking for a password via polkit.

    Returns (rc, error). rc 0 means the rule is in place.

    Deliberately does NOT use `sudo` with a pipe: `sudo tee` needs a TTY, and
    `sudo sh -c '...'` would let any string become root. Instead a tiny helper
    script is installed once and run through pkexec, and the rule is validated
    with `visudo -cf` before it is ever used.
    """
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if not user:
        return 1, "could not determine the current user name"

    if not shutil.which("pkexec"):
        return 2, "pkexec not available"      # caller falls back to manual

    # A private 0700 directory, not a fixed /tmp path: /tmp is world-writable,
    # so a predictable filename could be swapped for another script between
    # the moment we write it and the moment pkexec runs it as root.
    workdir = tempfile.mkdtemp(prefix="skyroom-elevate-")
    helper = Path(workdir) / "install-rule.sh"
    rule = f"{user} ALL=(ALL) NOPASSWD: {RTCWAKE_PATH}"
    # Write the rule to a temp file, install it, then validate with visudo.
    # A malformed sudoers file can lock a user out of sudo entirely, so it is
    # never written blind — and if visudo rejects it, remove the bad file.
    helper.write_text(
        "#!/bin/sh\n"
        "set -e\n"
        f"printf '%s\\n' {shlex.quote(rule)} > {LINUX_SUDOERS_FILE}\n"
        "chmod 440 " + LINUX_SUDOERS_FILE + "\n"
        "chown root:root " + LINUX_SUDOERS_FILE + "\n"
        f"if ! {VISUDO_PATH} -cf {LINUX_SUDOERS_FILE}; then\n"
        f"  rm -f {LINUX_SUDOERS_FILE}\n"
        "  exit 1\n"
        "fi\n",
        encoding="utf-8",
    )
    helper.chmod(0o700)

    try:
        proc = subprocess.run(
            ["pkexec", str(helper)],
            capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired:
        return 1, "the permission request timed out"
    except Exception as e:
        return 1, f"could not run pkexec: {e}"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if proc.returncode == 126 or proc.returncode == 127:
        # polkit's "dialog was dismissed" codes
        return 3, "permission request was cancelled"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return 1, err or f"pkexec failed with code {proc.returncode}"
    return 0, ""


def _windows_suspend_ps(hibernate: bool) -> list:
    """Command list that calls the documented SetSuspendState API.

    The old `rundll32 powrprof.dll,SetSuspendState 0,1,0` line is
    undocumented and hibernates whenever hibernation is enabled system-wide
    — which this app's own setup script does. P/Invoking the API directly is
    the only way "Suspend" actually suspends.
    """
    flag = "$true" if hibernate else "$false"
    script = (
        "Add-Type -Namespace SkyroomBot -Name Power -MemberDefinition "
        "'[DllImport(\"powrprof.dll\", SetLastError=true)] public static "
        "extern bool SetSuspendState(bool hibernate, bool force, "
        "bool disabled);'; "
        f"if ([SkyroomBot.Power]::SetSuspendState({flag}, $true, $false)) "
        "{ exit 0 } else { exit 1 }"
    )
    return ["powershell.exe", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-Command", script]


def _windows_sleep_command(mode: str) -> list:
    """Sleep command for `mode`, per platform semantics.

    Linux distinguishes mem/disk/off; Windows only really offers hibernate
    and suspend, so "off" falls back to hibernate (the GUI disables that
    radio on Windows).
    """
    if mode == "disk" or mode == "off":
        return ["shutdown", "/h"]

    ps_cmd = _windows_suspend_ps(hibernate=False)
    if shutil.which(ps_cmd[0]):
        return ps_cmd
    # P/Invoke unavailable (no powershell.exe?) — old behaviour.
    return ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]


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

    sleep_cmd = _windows_sleep_command(mode)

    # NOTE: the scheduled task is deliberately NOT deleted after the sleep
    # request. `shutdown /h` and SetSuspendState return the moment Windows
    # *accepts* the request — they do not block until resume — so deleting
    # here removed the wake alarm while the machine was going down, and the
    # machine never woke. The task is a one-shot time trigger: once it has
    # fired it cannot fire again, and the next arm overwrites it via
    # `schtasks /Create /F`.
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

    return 0, ""


# ============================================================
# Signals bridge
# ============================================================
class BotBridge(QObject):
    log = pyqtSignal(str, str)
    # Carries the SkyroomBot that ended so a late duplicate from a previous
    # run cannot clear a newer one (see _on_bot_finished).
    finished = pyqtSignal(object)
    arm_result = pyqtSignal(int, str)         # (rc, stderr) from the sleep helper
    setup_done = pyqtSignal(int, str, str)    # (rc, stdout, stderr) from setup


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

class JarvisBridge(QObject):
    status = pyqtSignal(str)
    transcript = pyqtSignal(str, float)
    decision = pyqtSignal(object)
    pending_added = pyqtSignal(int, str)
    pending_removed = pyqtSignal(int)

# ============================================================
# Jarvis settings dialog
# ============================================================
JARVIS_CONFIG_PATH = Path.home() / ".jarvis" / "config.json"

_ACTION_CHOICES = ["type_number", "send_chat", "notify_me"]


class JarvisConfigDialog(QDialog):
    """Edit ~/.jarvis/config.json from inside the GUI."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("تنظیمات جارویس")
        self.setModal(True)
        self.resize(720, 620)

        self._data: dict = {}
        self._widgets: dict = {}

        self._load()
        self._build_ui()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        from jarvis.core import config_loader
        from jarvis import config as jcfg

        if not JARVIS_CONFIG_PATH.exists():
            config_loader.load(jcfg.__dict__)

        try:
            self._data = json.loads(
                JARVIS_CONFIG_PATH.read_text(encoding="utf-8")
            )
        except Exception as e:
            QMessageBox.warning(self, "جارویس",
                                f"خواندن فایل ناموفق:\n{e}")
            self._data = {}

    # ------------------------------------------------------------------
    def _line(self, key: str, hint: str = "") -> QLineEdit:
        w = QLineEdit(str(self._data.get(key, "")))
        w.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        if hint:
            w.setPlaceholderText(hint)
        self._widgets[key] = w
        return w

    def _password(self, key: str) -> QLineEdit:
        w = QLineEdit(str(self._data.get(key, "")))
        w.setEchoMode(QLineEdit.EchoMode.Password)
        w.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self._widgets[key] = w
        return w

    def _check(self, key: str) -> QCheckBox:
        w = QCheckBox()
        w.setChecked(bool(self._data.get(key, False)))
        self._widgets[key] = w
        return w
    
    def _monitor_combo(self, key: str) -> QWidget:
        """Editable combo populated from pactl, with a refresh button."""
        try:
            from jarvis.audio.monitor import list_monitor_sources
            sources = list_monitor_sources()
        except Exception:
            sources = []

        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        combo = QComboBox()
        combo.setEditable(True)
        combo.setLayoutDirection(Qt.LayoutDirection.LeftToRight)

        current = str(self._data.get(key, ""))
        if current and current not in sources:
            combo.addItem(current)
        for s in sources:
            combo.addItem(s)
        if current:
            combo.setCurrentText(current)

        self._widgets[key] = combo
        layout.addWidget(combo, 1)

        def refresh():
            try:
                from jarvis.audio.monitor import list_monitor_sources as lm
                new_sources = lm()
            except Exception:
                new_sources = []
            text = combo.currentText()
            combo.clear()
            if text and text not in new_sources:
                combo.addItem(text)
            for s in new_sources:
                combo.addItem(s)
            combo.setCurrentText(text)

        btn = QPushButton("↻")
        btn.setToolTip("به‌روزرسانی لیست دستگاه‌ها")
        btn.setFixedWidth(32)
        apply_button_style(btn, "neutral")
        btn.clicked.connect(refresh)
        layout.addWidget(btn)

        return box
    def _combo(self, key: str, choices: list) -> QComboBox:
        w = QComboBox()
        for c in choices:
            w.addItem(c)
        current = str(self._data.get(key, choices[0] if choices else ""))
        if current in choices:
            w.setCurrentText(current)
        self._widgets[key] = w
        return w

    def _checkgroup(self, key: str) -> dict:
        """Multi-checkbox for a list-of-strings field."""
        current = set(self._data.get(key, []) or [])
        group: dict = {}
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        for choice in _ACTION_CHOICES:
            cb = QCheckBox(choice)
            cb.setChecked(choice in current)
            layout.addWidget(cb)
            group[choice] = cb
        layout.addStretch(1)
        self._widgets[key] = group
        return {"widget": box, "group": group}

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        tabs = QTabWidget()
        tabs.addTab(self._tab_models(), "مدل و صوت")
        tabs.addTab(self._tab_extraction(), "استخراج")
        tabs.addTab(self._tab_actions(), "اکشن‌ها و هشدار")
        outer.addWidget(tabs, 1)

        # footer
        footer = QHBoxLayout()
        open_raw = QPushButton("باز کردن فایل خام")
        apply_button_style(open_raw, "neutral")
        open_raw.clicked.connect(self._open_raw)
        footer.addWidget(open_raw)

        reset = QPushButton("بازگردانی به پیش‌فرض")
        apply_button_style(reset, "neutral")
        reset.clicked.connect(self._reset_defaults)
        footer.addWidget(reset)
        footer.addStretch(1)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save |
            QDialogButtonBox.StandardButton.Cancel
        )
        bb.button(QDialogButtonBox.StandardButton.Save).setText("ذخیره")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("انصراف")
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        footer.addWidget(bb)

        outer.addLayout(footer)

    # ------------------------------------------------------------------
    def _tab_models(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(12, 12, 12, 12)

        form.addRow("مدل ویشپر:",
                    self._line("WHISPER_MODEL",
                               "large-v3-turbo یا مسیر کامل"))
        form.addRow("دستگاه:",
                    self._combo("DEVICE", ["cuda", "cpu"]))
        form.addRow("نوع محاسبه:",
                    self._combo("COMPUTE_TYPE",
                                ["float16", "int8_float16", "int8",
                                 "float32"]))
        form.addRow("زبان:", self._line("LANGUAGE", "fa / en / خالی"))
        form.addRow("دستگاه ضبط سیستم:", self._monitor_combo("LOOPBACK_DEVICE"))
        hint = QLabel(
            "برای تغییر مدل ویشپر به مسیر کامل فایل‌های CTranslate2 یا "
            "نام مدل هاگینگ‌فیس نیاز است. تغییر زبان تنها روی مدل‌های "
            "چندزبانه اثر دارد.\n\n"
            "دستگاه ضبط سیستم از میان منابع monitor موجود انتخاب می‌شود. "
            "دکمهٔ ↻ فهرست را دوباره می‌خواند. تغییر آن فقط پس از "
            "راه‌اندازی مجدد جارویس (شروع کلاس بعدی) اعمال می‌شود."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray; font-size: 11px;")
        form.addRow("", hint)

        scroll.setWidget(w)
        return scroll

    def _tab_extraction(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(12, 12, 12, 12)

        form.addRow("بک‌اند:",
                    self._combo("EXTRACTION_BACKEND", ["local", "cloud"]))

        form.addRow(QLabel("<b>محلی (Ollama)</b>"))
        form.addRow("آدرس Ollama:",
                    self._line("LOCAL_LLM_URL",
                               "http://localhost:11434/v1"))
        form.addRow("مدل محلی:",
                    self._line("LOCAL_EXTRACTION_MODEL",
                               "qwen2.5:3b"))

        form.addRow(QLabel("<b>ابری (9Router)</b>"))
        form.addRow("آدرس 9Router:",
                    self._line("NINEROUTER_URL",
                               "http://localhost:20128/v1"))
        form.addRow("کلید API:", self._password("NINEROUTER_KEY"))
        form.addRow("نام مدل ابری:", self._line("EXTRACTION_MODEL"))
        form.addRow("کومبو:", self._line("NINEROUTER_COMBO_NAME"))

        scroll.setWidget(w)
        return scroll

    def _tab_actions(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(12, 12, 12, 12)

        form.addRow("اجرای اکشن‌ها فعال:",
                    self._check("ACTION_EXECUTION_ENABLED"))
        form.addRow("حالت آزمایشی (Dry run):",
                    self._check("ACTION_DRY_RUN"))

        box_req = self._checkgroup("ACTION_REQUIRE_CONFIRM")
        form.addRow("نیاز به تایید برای:", box_req["widget"])

        box_dis = self._checkgroup("ACTION_DISABLED")
        form.addRow("اکشن‌های غیرفعال:", box_dis["widget"])

        form.addRow(QLabel("<b>هشدار صوتی</b>"))
        form.addRow("فعال:", self._check("ALARM_ENABLED"))
        form.addRow("فایل صدا:",
                    self._line("ALARM_SOUND_PATH",
                               "default یا مسیر کامل"))

        hint = QLabel(
            "«default» یعنی از صدای همراه پکیج استفاده شود. "
            "اگر «نیاز به تایید» خالی باشد، اکشن‌ها بدون تأیید اجرا "
            "می‌شوند."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray; font-size: 11px;")
        form.addRow("", hint)

        scroll.setWidget(w)
        return scroll

    # ------------------------------------------------------------------
    def _collect(self) -> dict:
        out = {}
        for key, w in self._widgets.items():
            if isinstance(w, QLineEdit):
                out[key] = w.text().strip()
            elif isinstance(w, QCheckBox):
                out[key] = w.isChecked()
            elif isinstance(w, QComboBox):
                out[key] = w.currentText()
            elif isinstance(w, dict):
                out[key] = [c for c, cb in w.items() if cb.isChecked()]
        return out

    def _save(self) -> None:
        values = self._collect()

        # Merge with the file already on disk, in case a field exists
        # there that the dialog doesn't render.
        merged = dict(self._data)
        merged.update(values)

        try:
            JARVIS_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            JARVIS_CONFIG_PATH.write_text(
                json.dumps(merged, ensure_ascii=False, indent=4) + "\n",
                encoding="utf-8",
            )
        except Exception as e:
            QMessageBox.critical(self, "جارویس",
                                 f"ذخیره ناموفق:\n{e}")
            return

        # Reload into the running process so the next segment sees it.
        try:
            from jarvis.core import config_loader
            from jarvis import config as jcfg
            config_loader.reload_into(jcfg.__dict__)
        except Exception as e:
            QMessageBox.warning(
                self, "جارویس",
                f"فایل ذخیره شد ولی بارگذاری مجدد ناموفق بود:\n{e}\n\n"
                "برای اعمال کامل، برنامه را دوباره باز کنید."
            )

        self.accept()

    def _open_raw(self) -> None:
        if not JARVIS_CONFIG_PATH.exists():
            QMessageBox.information(self, "جارویس",
                                    "فایل تنظیمات هنوز ساخته نشده است.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(JARVIS_CONFIG_PATH)))

    def _reset_defaults(self) -> None:
        r = QMessageBox.question(
            self, "بازگردانی",
            "فایل تنظیمات جارویس حذف و مقادیر پیش‌فرض بازسازی شود؟",
            QMessageBox.StandardButton.Yes |
            QMessageBox.StandardButton.No,
        )
        if r != QMessageBox.StandardButton.Yes:
            return
        try:
            JARVIS_CONFIG_PATH.unlink(missing_ok=True)
        except Exception as e:
            QMessageBox.critical(self, "جارویس", f"حذف ناموفق:\n{e}")
            return
        self._load()
        # Rebuild the UI with the new values
        for i in reversed(range(self.layout().count())):
            item = self.layout().itemAt(i)
            if item and item.widget():
                item.widget().deleteLater()
        while self.layout().count():
            self.layout().takeAt(0)
        self._widgets.clear()
        self._build_ui()

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
        self.bridge.arm_result.connect(self._on_arm_result)

        self.jarvis_bridge = JarvisBridge()
        self.jarvis = JarvisRuntime(self._emit_log)
        self.jarvis.on_status = (
            lambda s: self.jarvis_bridge.status.emit(s)
        )
        self.jarvis.on_transcript = (
            lambda t, s: self.jarvis_bridge.transcript.emit(t, s)
        )
        self.jarvis.on_decision = (
            lambda d: self.jarvis_bridge.decision.emit(d)
        )
        self.jarvis.on_pending_added = (
            lambda i, s: self.jarvis_bridge.pending_added.emit(i, s)
        )
        self.jarvis.on_pending_removed = (
            lambda i: self.jarvis_bridge.pending_removed.emit(i)
        )
        self._jarvis_pending_ids: list[int] = []

        self.cfg = self._load_config_from_settings()

        self._build_ui()
        self._apply_debug_toggle()
        self._apply_send_message_toggle()
        self._apply_sleep_mode_effects()
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
        s.setValue("wake/sleep_mode", self._current_sleep_mode())

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
        if JARVIS_AVAILABLE:
            self.tabs.addTab(self._build_jarvis_tab(), "Jarvis (دستیار صوتی)")
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
            rb.toggled.connect(self._apply_sleep_mode_effects)
            if val == "disk":
                rb.setChecked(True)
        # "Power off S5" cannot be honoured on Windows — it silently becomes
        # hibernate. A label that disagrees with behaviour confuses everyone.
        self._sleep_radio_by_value["off"].setEnabled(not IS_WINDOWS)
        if IS_WINDOWS:
            self._sleep_radio_by_value["off"].setText(
                "خاموش کامل (Power off S5) — روی ویندوز پشتیبانی نمی‌شود")
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
    
    def _build_jarvis_tab(self) -> QWidget:
        w = QWidget()
        self.jarvis_tab = w 
        outer = QVBoxLayout(w)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        # ---- toolbar ----
        bar = QHBoxLayout()
        self.jarvis_status = QLabel("Idle")
        self.jarvis_status.setStyleSheet(
            "color: #888; font-family: monospace; font-size: 12px;"
        )
        bar.addWidget(QLabel("وضعیت:"))
        bar.addWidget(self.jarvis_status, 1)

        settings_btn = QPushButton("تنظیمات")
        apply_button_style(settings_btn, "start")
        settings_btn.clicked.connect(self._open_jarvis_settings)
        bar.addWidget(settings_btn)
        outer.addLayout(bar)

        if not JARVIS_AVAILABLE:
            msg = QLabel(
                "Jarvis نصب نیست. برای فعال‌سازی حالت زنده، پکیج را نصب کنید."
            )
            msg.setWordWrap(True)
            msg.setStyleSheet("color: #c98a00; padding: 20px;")
            outer.addWidget(msg)
            outer.addStretch(1)
            return w

        # ---- main splitter: transcript | right column ----
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.jarvis_transcript = QTextEdit()
        self.jarvis_transcript.setReadOnly(True)
        self.jarvis_transcript.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.jarvis_transcript.setLayoutDirection(
            Qt.LayoutDirection.RightToLeft
        )
        mono = _pick_mono_font()
        if mono:
            self.jarvis_transcript.setFont(QFont(mono, 10))
        self.jarvis_transcript.setStyleSheet(
            "QTextEdit { background: #111; color: #e0e0e0; }"
        )
        splitter.addWidget(self.jarvis_transcript)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(4)

        self.jarvis_decisions = QTextEdit()
        self.jarvis_decisions.setReadOnly(True)
        self.jarvis_decisions.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        if mono:
            self.jarvis_decisions.setFont(QFont(mono, 10))
        self.jarvis_decisions.setStyleSheet(
            "QTextEdit { background: #0a0a0a; color: #cfcfcf; }"
        )
        rv.addWidget(self.jarvis_decisions, 3)

        self.jarvis_pending_header = QLabel("تاییدهای در انتظار  (هیچ)")
        self.jarvis_pending_header.setStyleSheet(
            "color: #888; font-family: monospace; font-size: 11px; "
            "padding: 4px 8px;"
        )
        rv.addWidget(self.jarvis_pending_header)

        self.jarvis_pending_view = QTextEdit()
        self.jarvis_pending_view.setReadOnly(True)
        self.jarvis_pending_view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        if mono:
            self.jarvis_pending_view.setFont(QFont(mono, 10))
        self.jarvis_pending_view.setStyleSheet(
            "QTextEdit { background: #0a0a0a; color: #ffd166; }"
        )
        rv.addWidget(self.jarvis_pending_view, 1)

        hint = QLabel("Enter برای تایید اولی · Esc برای رد کردن")
        hint.setStyleSheet(
            "color: #555; font-family: monospace; font-size: 10px; "
            "padding: 2px 8px;"
        )
        rv.addWidget(hint)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        outer.addWidget(splitter, 1)

        # ---- bridge wiring ----
        self.jarvis_bridge.status.connect(self.jarvis_status.setText)
        self.jarvis_bridge.transcript.connect(self._on_jarvis_transcript)
        self.jarvis_bridge.decision.connect(self._on_jarvis_decision)
        self.jarvis_bridge.pending_added.connect(self._on_jarvis_pending_added)
        self.jarvis_bridge.pending_removed.connect(
            self._on_jarvis_pending_removed
        )

        return w
    
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

        self.bot = SkyroomBot(cfg, self.users, self._emit_log,
                              jarvis=self.jarvis)

        # Environment problems the user can actually fix, in plain language,
        # before a single browser window opens.
        problems = self.bot.preflight()
        if problems:
            for p in problems:
                self._append_log("WARNING", p)
            QMessageBox.warning(
                self, "بررسی سیستم",
                "\n\n".join("• " + p for p in problems)
                + "\n\nجزئیات کامل در تب «گزارش».")
            # A missing browser or driver is fatal: nothing can be started.
            if any("not installed" in p or "cannot be found" in p
                   for p in problems):
                self.bot = None
                return

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
        bot = self.bot
        if bot is None:
            return
        for t in list(bot.threads):
            t.join()
        self.bridge.finished.emit(bot)

    def _on_bot_finished(self, bot=None):
        # A stale emission from a bot that was already replaced must not
        # clear the live one (it would re-enable Start, make Stop a no-op
        # and let closeEvent skip stopping real Chrome).
        if bot is not None and self.bot is not bot:
            return
        self.bot = None
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.v_status.setText("آماده")
        self._append_log("INFO", "=== تمام وظایف ربات پایان یافت ===")

    def _stop_bot_and_wait(self, timeout_ms: int = 15000) -> bool:
        """
        Stop the bot and let the GUI keep running while it shuts down.

        A plain time.sleep() loop here would deadlock: stop_bot() only
        starts a worker thread, and self.bot is cleared by _on_bot_finished
        on the *main* thread — which is exactly the thread doing the
        sleeping. Pumping a nested event loop lets that queued signal land.
        """
        if self.bot is None:
            return True
        loop = QEventLoop(self)
        self.bridge.finished.connect(loop.quit)
        self.stop_bot()
        QTimer.singleShot(timeout_ms, loop.quit)
        loop.exec()
        try:
            self.bridge.finished.disconnect(loop.quit)
        except (TypeError, RuntimeError):
            pass
        if self.bot is None:
            return True
        QMessageBox.warning(
            self, "خواب و بیدارباش",
            "ربات پس از ۱۵ ثانیه متوقف نشد. لطفاً ابتدا با دکمهٔ «توقف» "
            "آن را متوقف کنید و دوباره تلاش کنید.")
        return False

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
                self.bridge.finished.emit(bot)
                self._emit_log("INFO", "ربات توسط کاربر متوقف شد")

        threading.Thread(target=_bg, daemon=True).start()

    # ---------------------------------------------------------
    # Wake scheduling
    # ---------------------------------------------------------
    def app_pump(self, seconds: float = 0.1) -> None:
        """Keep the GUI responsive while a worker thread finishes.

        Used when we must wait for something on another thread but still want
        the window to repaint and stay interactive. Unlike time.sleep(), this
        dispatches pending Qt events instead of freezing the interface.
        """
        app = QApplication.instance()
        if app is None:
            time.sleep(seconds)
            return
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            app.processEvents(
                QEventLoop.ProcessEventsFlag.AllEvents,
                50,
            )
            time.sleep(0.01)

    def _current_sleep_mode(self) -> str:
        """The checked radio in the sleep-mode group."""
        for val, rb in self._sleep_radio_by_value.items():
            if rb.isChecked():
                return val
        return "disk"

    def _compute_next_wake(self):
        if not self.users:
            return None, None
        now = datetime.now(TEHRAN_TZ)
        soonest = None
        for u in self.users:
            for cls in u.get("classes", []):
                try:
                    start, _end = next_occurrence_range(
                        cls["day"], cls["time"], cls.get("end_time"))
                except Exception:
                    continue
                # next_occurrence() (not the range version) would roll +7 days
                # for a class already under way and arm next week's alarm.
                if start <= now:
                    continue
                if soonest is None or start < soonest:
                    soonest = start
        if soonest is None:
            return None, None
        wake_at = soonest - timedelta(minutes=self.v_wake_lead.value())
        if wake_at <= now:
            # The class starts sooner than the wake lead. Wake almost now
            # rather than showing the user a "time is in the past" error.
            wake_at = now + timedelta(seconds=60)
        return soonest, wake_at

    def _apply_sleep_mode_effects(self):
        # Called from the radio buttons' toggled signal during _build_ui,
        # before the checkbox below them exists — guard instead of relying on
        # construction order.
        if not hasattr(self, "v_auto_after_wake"):
            return
        # `rtcwake -m off` powers the machine down: nothing survives to run
        # "start after wake", so offering the option would be a lie.
        if self._current_sleep_mode() == "off":
            if IS_LINUX:
                self.v_auto_after_wake.setChecked(False)
                self.v_auto_after_wake.setEnabled(False)
                self.v_auto_after_wake.setToolTip(
                    "با «خاموش کامل» برنامه پس از روشن شدن اجرا نمی‌شود، "
                    "پس شروع خودکار معنا ندارد.")
            else:
                self.v_auto_after_wake.setEnabled(True)
                self.v_auto_after_wake.setToolTip("")
        else:
            self.v_auto_after_wake.setEnabled(True)
            self.v_auto_after_wake.setToolTip("")

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
        if not Path(RTCWAKE_PATH).exists():
            self._append_log(
                "WARNING",
                f"rtcwake not found at {RTCWAKE_PATH} — wake from sleep is "
                f"not available on this system")
            return False
        try:
            # `sudo -l <cmd>` echoes back only the command path, with no
            # "NOPASSWD" text, so it cannot prove the rule is there. Plain
            # `sudo -l` lists every rule the user holds, which is what we
            # need to inspect. -n keeps it from hanging on a password.
            r = subprocess.run(
                ["sudo", "-n", "-l"],
                capture_output=True, text=True, timeout=6,
            )
        except Exception:
            return False
        if r.returncode != 0:
            return False
        out = r.stdout or ""
        # Require a NOPASSWD entry for the exact rtcwake path — a bare
        # "(ALL) ALL" line means sudo would still ask for a password.
        for line in out.splitlines():
            if "NOPASSWD" not in line:
                continue
            if RTCWAKE_PATH in line:
                return True
        return False

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
        # Kept as a fallback for machines without polkit/pkexec, and shown
        # so a user who prefers the terminal can still copy it.
        cmd = (f"echo '{user} ALL=(ALL) NOPASSWD: {RTCWAKE_PATH}' | "
               f"sudo tee {LINUX_SUDOERS_FILE} && "
               f"sudo chmod 440 {LINUX_SUDOERS_FILE} && "
               f"sudo chown root:root {LINUX_SUDOERS_FILE} && "
               f"{VISUDO_PATH} -cf {LINUX_SUDOERS_FILE} && "
               f"sudo -k")

        result = {"ok": False}

        dlg = QDialog(self)
        dlg.setWindowTitle("نیاز به دسترسی برای خواب و بیدارباش")
        dlg.resize(780, 340)
        v = QVBoxLayout(dlg)

        info = QLabel(
            "برای اجرای بیدارباش، برنامه یک‌بار به اجازهٔ اجرای "
            "rtcwake بدون رمز عبور نیاز دارد.\n\n"
            "روی «درخواست خودکار دسترسی» بزنید؛ سیستم‌عامل یک پنجره "
            "می‌پرسد، رمز عبور خود را وارد کنید و همه‌چیز خودکار "
            "انجام می‌شود. این کار فقط یک‌بار لازم است."
            + ("" if shutil.which("pkexec") else
               "\n\n(روی این سیستم درخواست خودکار در دسترس نیست؛ "
               "دستور زیر را در ترمینال اجرا کنید.)")
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
        auto_btn = QPushButton("🔐 درخواست خودکار دسترسی")
        apply_button_style(auto_btn, "start")
        copy_btn = QPushButton("کپی دستور")
        apply_button_style(copy_btn, "neutral")
        recheck_btn = QPushButton("بررسی مجدد")
        apply_button_style(recheck_btn, "neutral")
        cancel_btn = QPushButton("لغو")
        apply_button_style(cancel_btn, "stop")
        if not shutil.which("pkexec"):
            # No polkit: the terminal command is the only route.
            auto_btn.setEnabled(False)
            auto_btn.setToolTip("روی این سیستم pkexec در دسترس نیست")
        h.addWidget(auto_btn)
        h.addWidget(copy_btn)
        h.addWidget(recheck_btn)
        h.addWidget(cancel_btn)
        h.addStretch(1)
        v.addLayout(h)

        def _do_copy():
            QApplication.clipboard().setText(cmd)
            status.setText("دستور در کلیپ‌بورد کپی شد.")
            status.setStyleSheet("color: #7a8ba8; font-size: 12px;")

        def _finish_ok():
            result["ok"] = True
            status.setText("✅ دسترسی تایید شد.")
            status.setStyleSheet(
                "color: #2a7f42; font-size: 12px; font-weight: bold;")
            self._append_log("INFO", "دسترسی rtcwake تایید شد")
            QTimer.singleShot(600, dlg.accept)

        def _do_recheck():
            if self._check_wake_permissions():
                _finish_ok()
            else:
                status.setText("❌ هنوز تنظیم نشده.")
                status.setStyleSheet(
                    "color: #c0392b; font-size: 12px; font-weight: bold;")

        def _on_setup_done(rc: int, err: str):
            auto_btn.setEnabled(True)
            if rc == 0:
                # Trust the rule, not just the exit code.
                if self._check_wake_permissions():
                    self._append_log("INFO",
                                     "قاعدهٔ sudoers با موفقیت نصب شد")
                    _finish_ok()
                else:
                    status.setText(
                        "❌ قاعده نصب شد ولی هنوز کار نمی‌کند.")
                    status.setStyleSheet(
                        "color: #c0392b; font-size: 12px; font-weight: bold;")
            elif rc == 2:
                status.setText(
                    "❌ درخواست خودکار روی این سیستم در دسترس نیست — "
                    "دستور زیر را در ترمینال اجرا کنید.")
                status.setStyleSheet(
                    "color: #c98a00; font-size: 12px; font-weight: bold;")
            elif rc == 3:
                status.setText("⚠️ درخواست لغو شد.")
                status.setStyleSheet(
                    "color: #c98a00; font-size: 12px; font-weight: bold;")
                self._append_log("INFO", "کاربر درخواست دسترسی را لغو کرد")
            else:
                status.setText(f"❌ انجام نشد: {err or '(بدون جزئیات)'}")
                status.setStyleSheet(
                    "color: #c0392b; font-size: 12px; font-weight: bold;")
                self._append_log("ERROR",
                                 f"راه‌اندازی دسترسی ناموفق: {err}")

        def _bg_request():
            try:
                rc, err = _linux_elevated_setup()
            except Exception as e:  # noqa: BLE001 — reported to the user
                rc, err = 1, str(e)
            # Signal, not QTimer.singleShot: no event loop in this thread.
            self.bridge.setup_done.emit(rc, err, "")

        def _do_auto():
            auto_btn.setEnabled(False)
            copy_btn.setEnabled(False)
            recheck_btn.setEnabled(False)
            cancel_btn.setEnabled(False)
            status.setText("⏳ منتظر تایید رمز عبور در پنجرهٔ سیستم…")
            status.setStyleSheet("color: #7a8ba8; font-size: 12px;")
            threading.Thread(target=_bg_request, daemon=True).start()

        copy_btn.clicked.connect(_do_copy)
        auto_btn.clicked.connect(_do_auto)
        recheck_btn.clicked.connect(_do_recheck)
        cancel_btn.clicked.connect(dlg.reject)

        self.bridge.setup_done.connect(_on_setup_done)
        dlg.exec()
        try:
            self.bridge.setup_done.disconnect(_on_setup_done)
        except (TypeError, RuntimeError):
            pass
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
                # Signal, not QTimer.singleShot: this is a plain thread with
                # no event loop, where a singleShot would never fire and the
                # dialog would sit on "in progress" forever.
                self.bridge.setup_done.emit(rc, out, err)
            except Exception as e:
                self.bridge.setup_done.emit(1, "", str(e))

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

        self.bridge.setup_done.connect(_on_done)
        dlg.exec()
        try:
            self.bridge.setup_done.disconnect(_on_done)
        except (TypeError, RuntimeError):
            pass
        return result["ok"]

    def arm_wake_and_sleep(self):
        if not self.users:
            QMessageBox.information(self, "توجه",
                                    "ابتدا فایل کاربران را بارگذاری کنید")
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
            if not self._stop_bot_and_wait():
                return

        # ---- permission gate ----
        # Ask the OS for the permission ourselves on Linux (polkit shows the
        # same password dialog a manual `sudo` would). The user only types a
        # password — never a command. Windows already self-elevates via UAC in
        # the setup script, so it goes straight to its own dialog.
        if not self._check_wake_permissions():
            self._append_log("INFO",
                             "دسترسی خواب/بیداری تنظیم نشده — درخواست خودکار")
            if not IS_WINDOWS and shutil.which("pkexec"):
                self.btn_arm_sleep.setEnabled(False)
                self._append_log("INFO",
                                 "در انتظار تایید رمز عبور سیستم‌عامل…")
                # Worker thread: pkexec blocks on the user's password for as
                # long as it takes, and the GUI must stay responsive.
                outcome: dict = {}

                def _bg_elevate():
                    try:
                        outcome["rc"], outcome["err"] = \
                            _linux_elevated_setup()
                    except Exception as e:  # noqa: BLE001
                        outcome["rc"], outcome["err"] = 1, str(e)

                th = threading.Thread(target=_bg_elevate, daemon=True)
                th.start()
                while th.is_alive():
                    self.app_pump(0.2)
                rc, err = outcome.get("rc", 1), outcome.get("err", "")
                self.btn_arm_sleep.setEnabled(True)
                if rc == 3:
                    self._append_log("INFO", "کاربر درخواست را لغو کرد")
                    return
                if rc == 2:
                    self._append_log(
                        "WARNING",
                        "pkexec در دسترس نیست — نمایش راهنمای دستی")
                elif rc != 0:
                    self._append_log(
                        "ERROR",
                        f"درخواست خودکار دسترسی ناموفق: {err}")
            if not self._check_wake_permissions():
                if not self._show_setup_prompt():
                    self._append_log("INFO", "کاربر راه‌اندازی را لغو کرد")
                    return
                if not self._check_wake_permissions():
                    QMessageBox.critical(
                        self, "راه‌اندازی کامل نشد",
                        "به نظر می‌رسد راه‌اندازی اولیه به درستی انجام "
                        "نشده است.\nدوباره تلاش کنید یا دستور را در ترمینال "
                        "اجرا کنید."
                    )
                    return

        # ---- mode selection ----
        mode = self._current_sleep_mode()

        # Recomputed here, not before the dialogs: stopping the bot, the UAC
        # prompt and this confirmation box can all take minutes, and the value
        # shown in the dialog must be the one that actually gets armed.
        class_start, wake_at = self._compute_next_wake()
        if wake_at is None:
            QMessageBox.critical(self, "خطا", "کلاس معتبری یافت نشد")
            return

        mode_name = {
            "mem":  "خواب (Suspend)",
            "disk": "هایبرنیت (Hibernate)",
            "off":  ("خاموش کامل (Power off)" if IS_LINUX
                     else "هایبرنیت (Hibernate) — «خاموش کامل» روی ویندوز "
                          "پشتیبانی نمی‌شود"),
        }[mode]

        warn_off = ""
        if mode == "off" and IS_LINUX:
            warn_off = ("\n\nتوجه: با «خاموش کامل»، برنامه پس از روشن شدن "
                        "دوباره اجرا نمی‌شود.")

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

        # One last check: the user may have sat on the dialog for minutes.
        class_start, wake_at = self._compute_next_wake()
        if wake_at is None:
            QMessageBox.critical(self, "خطا", "کلاس معتبری یافت نشد")
            return
        self._append_log(
            "INFO",
            f"زمان بیداری نهایی: {wake_at:%Y-%m-%d %H:%M:%S} — "
            f"شروع کلاس {class_start:%Y-%m-%d %H:%M:%S}"
        )

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
                cmd = ["sudo", "-n", RTCWAKE_PATH, "-m", mode,
                       "-t", str(epoch)]
                self._emit_log("INFO",
                               "اجرای دستور: " + " ".join(cmd))
                proc = subprocess.run(cmd, capture_output=True, text=True)
                rc = proc.returncode
                err = (proc.stderr or proc.stdout or "").strip()
        except Exception as e:
            rc, err = 1, str(e)

        if rc != 0:
            self._emit_log("ERROR", f"خواب/بیداری ناموفق: {err}")
        # Emitted, not QTimer.singleShot: this runs on a worker thread with
        # no event loop, where a singleShot would never fire and the button
        # would stay dead forever.
        self.bridge.arm_result.emit(rc, err)

    def _on_arm_result(self, rc: int, err: str):
        # Owns the button re-enable: _on_wake_error is also called from
        # elsewhere, where re-enabling would be wrong.
        self.btn_arm_sleep.setEnabled(True)
        if rc != 0:
            self._on_wake_error(err)
        else:
            self._on_woke_up()

    def _on_wake_error(self, msg: str):
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

    def _on_jarvis_transcript(self, text: str, seconds: float) -> None:
        ts = time.strftime("%H:%M:%S")
        cursor = self.jarvis_transcript.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor("#666"))
        cursor.insertText(f"[{ts} {seconds:.1f}s] ", fmt)
        fmt.setForeground(QColor("#d0d0d0"))
        cursor.insertText(text + "\n", fmt)
        self.jarvis_transcript.verticalScrollBar().setValue(
            self.jarvis_transcript.verticalScrollBar().maximum()
        )

    def _on_jarvis_decision(self, decision) -> None:
        action = getattr(decision, "action", "?")
        args = getattr(decision, "args", {}) or {}
        reasoning = getattr(decision, "reasoning", "")
        ts = time.strftime("%H:%M:%S")

        cursor = self.jarvis_decisions.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        fmt_ts = QTextCharFormat()
        fmt_ts.setForeground(QColor("#555"))
        cursor.insertText(f"\n[{ts}]\n", fmt_ts)

        fmt_act = QTextCharFormat()
        fmt_act.setForeground(QColor("#7fd67f"))
        cursor.insertText(f"ACTION: {action}\n", fmt_act)

        fmt_args = QTextCharFormat()
        fmt_args.setForeground(QColor("#e0e0e0"))
        cursor.insertText(f"ARGS:   {args}\n", fmt_args)

        if reasoning:
            fmt_why = QTextCharFormat()
            fmt_why.setForeground(QColor("#888"))
            cursor.insertText(f"WHY:    {reasoning}\n", fmt_why)

        self.jarvis_decisions.verticalScrollBar().setValue(
            self.jarvis_decisions.verticalScrollBar().maximum()
        )

    def _on_jarvis_pending_added(self, action_id: int, desc: str) -> None:
        self._jarvis_pending_ids.append(action_id)
        self._render_jarvis_pending()

    def _on_jarvis_pending_removed(self, action_id: int) -> None:
        if action_id in self._jarvis_pending_ids:
            self._jarvis_pending_ids.remove(action_id)
        self._render_jarvis_pending()

    def _render_jarvis_pending(self) -> None:
        self.jarvis_pending_view.clear()
        if not self._jarvis_pending_ids:
            self.jarvis_pending_header.setText("تاییدهای در انتظار  (هیچ)")
            return
        self.jarvis_pending_header.setText(
            f"تاییدهای در انتظار  ({len(self._jarvis_pending_ids)})"
        )
        cursor = self.jarvis_pending_view.textCursor()
        for i, pid in enumerate(self._jarvis_pending_ids):
            prefix = "▶ " if i == 0 else "   "
            fmt = QTextCharFormat()
            fmt.setForeground(QColor("#ffd166"))
            cursor.insertText(f"{prefix}id={pid}\n", fmt)
    def _open_jarvis_settings(self) -> None:
        if not JARVIS_AVAILABLE:
            QMessageBox.warning(
                self, "جارویس",
                "پکیج Jarvis نصب نیست؛ فایل تنظیمات در دسترس نیست.",
            )
            return
        dlg = JarvisConfigDialog(self)
        if dlg.exec():
            self._append_log("INFO", "تنظیمات جارویس ذخیره شد")
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

    def keyPressEvent(self, event):
        try:
            on_jarvis_tab = (
            hasattr(self, "tabs")
            and hasattr(self, "jarvis_tab")
            and self.tabs.currentWidget() is self.jarvis_tab
        )
        except Exception:
            on_jarvis_tab = False

        if (hasattr(self, "_jarvis_pending_ids")
                and self._jarvis_pending_ids
                and on_jarvis_tab):
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._jarvis_approve_top()
                return
            if event.key() == Qt.Key.Key_Escape:
                self._jarvis_dismiss_top()
                return

        super().keyPressEvent(event)

    def _jarvis_approve_top(self) -> None:
        if not self._jarvis_pending_ids:
            return
        pid = self._jarvis_pending_ids[0]
        kind = self.jarvis.approve(pid)
        self._append_log("INFO", f"تایید Jarvis id={pid} ({kind})")

    def _jarvis_dismiss_top(self) -> None:
        if not self._jarvis_pending_ids:
            return
        pid = self._jarvis_pending_ids[0]
        self.jarvis.dismiss(pid)
        self._append_log("INFO", f"رد کردن Jarvis id={pid}")

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