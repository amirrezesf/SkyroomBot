"""
Skyroom Bot - PyQt6 GUI

Run:
    python pyqt.py
    python pyqt.py --json users.json --auto-start

Persian text is rendered natively by Qt (no arabic_reshaper / bidi needed).
All settings, the last opened JSON file, and window layout are persisted
via QSettings (~/.config/SkyroomBot/SkyroomBotGUI.conf on Linux).
"""

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    from PyQt6.QtCore import (
        Qt, QTimer, pyqtSignal, QObject, QSize, QSettings
    )
    from PyQt6.QtGui import (
        QAction, QFont, QColor, QTextCharFormat, QTextCursor, QFontDatabase
    )
    from PyQt6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QGridLayout, QSplitter, QTreeWidget, QTreeWidgetItem, QPushButton,
        QLabel, QTabWidget, QLineEdit, QSpinBox, QCheckBox, QComboBox,
        QRadioButton, QButtonGroup, QPlainTextEdit, QMessageBox,
        QFileDialog, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
        QMenu, QStatusBar, QFrame, QSizePolicy, QScrollArea, QTextEdit,
        QListWidget, QListWidgetItem, QAbstractItemView,
        QAction, QFont, QColor, QTextCharFormat, QTextCursor, QFontDatabase, QIcon
        
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


def settings() -> QSettings:
    return QSettings(SETTINGS_ORG, SETTINGS_APP)

import sys
from pathlib import Path

def get_chromedriver_path():
    if getattr(sys, 'frozen', False):
        base = Path(sys._MEIPASS)
        name = 'chromedriver.exe' if sys.platform == 'win32' else 'chromedriver'
        p = base / 'drivers' / name
        if p.exists():
            return str(p)
    # dev mode
    local = Path(__file__).parent / 'drivers' / (
        'chromedriver.exe' if sys.platform == 'win32' else 'chromedriver')
    if local.exists():
        return str(local)
    return None    # fall back to Selenium Manager


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

        # ---- load config from QSettings BEFORE building the UI ----
        self.cfg = self._load_config_from_settings()

        self._build_ui()
        self._apply_debug_toggle()
        self._apply_send_message_toggle()
        self._refresh_tree()
        self.refresh_next_wake()

        # ---- restore window/splitter/tab and last JSON ----
        self._restore_ui_state()

        self._tick = QTimer(self)
        self._tick.timeout.connect(self.refresh_next_wake)
        self._tick.start(30_000)

    # =========================================================
    # Persistence: load / save
    # =========================================================
    def _load_config_from_settings(self) -> RuntimeConfig:
        s = settings()
        return RuntimeConfig(
            chromedriver_path=s.value("chromedriver_path",
                                      "/usr/bin/chromedriver", type=str),
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
        """Called after _build_ui() — restores everything not covered by cfg."""
        s = settings()

        # -- window geometry / state --
        geo = s.value("window/geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        wstate = s.value("window/state")
        if wstate is not None:
            self.restoreState(wstate)

        # -- splitter --
        splitter_state = s.value("window/splitter")
        if splitter_state is not None and hasattr(self, "_splitter"):
            self._splitter.restoreState(splitter_state)

        # -- active tab --
        tab = s.value("window/active_tab", 0, type=int)
        if 0 <= tab < self.tabs.count():
            self.tabs.setCurrentIndex(tab)

        # -- wake tab --
        self.v_wake_lead.setValue(s.value("wake/lead_min", 3, type=int))
        self.v_auto_after_wake.setChecked(
            s.value("wake/auto_start", True, type=bool))
        mode = s.value("wake/sleep_mode", "disk", type=str)
        if mode in self._sleep_radio_by_value:
            self._sleep_radio_by_value[mode].setChecked(True)

        # -- auto-load last JSON (unless already loaded via --json) --
        if self.json_path is None:
            last = s.value("files/last_json", "", type=str)
            if last:
                p = Path(last)
                if p.is_file():
                    try:
                        self._load_json_from_path(p)
                    except Exception:
                        pass   # silently ignore; user can load manually

    def _save_all_state(self):
        s = settings()
        cfg = self._collect_config()

        # -- general --
        s.setValue("chromedriver_path", cfg.chromedriver_path)
        s.setValue("send_message", cfg.send_message)
        s.setValue("chat_message", cfg.chat_message)
        s.setValue("page_load_timeout", cfg.page_load_timeout)
        s.setValue("element_timeout", cfg.element_timeout)
        s.setValue("ws_check_interval", cfg.ws_check_interval)
        s.setValue("ws_dead_grace", cfg.ws_dead_grace)
        s.setValue("max_restarts", cfg.max_restarts)
        s.setValue("min_delay_min", cfg.min_delay_min)
        s.setValue("max_delay_min", cfg.max_delay_min)

        # -- debug --
        s.setValue("debug_mode", cfg.debug_mode)
        s.setValue("debug_run_now", cfg.debug_run_now)
        s.setValue("debug_disable_random_delay",
                   cfg.debug_disable_random_delay)
        s.setValue("debug_disable_ws_check", cfg.debug_disable_ws_check)

        # -- wake --
        s.setValue("wake/lead_min", self.v_wake_lead.value())
        s.setValue("wake/auto_start", self.v_auto_after_wake.isChecked())
        for val, rb in self._sleep_radio_by_value.items():
            if rb.isChecked():
                s.setValue("wake/sleep_mode", val)
                break

        # -- files --
        if self.json_path is not None:
            s.setValue("files/last_json", str(self.json_path))

        # -- window --
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

        self.v_chromedriver = QLineEdit(self.cfg.chromedriver_path)
        self.v_chromedriver.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        form.addRow("مسیر chromedriver:", self.v_chromedriver)

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
            "ربات به صورت خودکار شروع می‌کند."
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

        setup_btn = QPushButton("راه‌اندازی اولیه دسترسی (یک‌بار)")
        apply_button_style(setup_btn, "neutral")
        setup_btn.clicked.connect(self.setup_wake_permission)
        v.addWidget(setup_btn, alignment=Qt.AlignmentFlag.AlignLeft)

        hint = QLabel(
            "اگر خطای sudo گرفتید، این دکمه را بزنید و دستور نمایش داده شده "
            "را یک‌بار در ترمینال اجرا کنید."
        )
        hint.setStyleSheet("color: gray; font-size: 11px;")
        hint.setWordWrap(True)
        v.addWidget(hint)

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
            chromedriver_path=self.v_chromedriver.text().strip(),
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

        if not Path(cfg.chromedriver_path).is_file():
            QMessageBox.critical(
                self, "خطا",
                f"chromedriver در مسیر زیر پیدا نشد:\n{cfg.chromedriver_path}",
            )
            return

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

        mode = "disk"
        for val, rb in self._sleep_radio_by_value.items():
            if rb.isChecked():
                mode = val
                break
        mode_name = {"mem": "خواب (suspend)",
                     "disk": "هایبرنیت (hibernate)",
                     "off": "خاموش کامل (power off)"}[mode]

        warn_off = ""
        if mode == "off":
            warn_off = ("\n\nتوجه: خاموش کامل باعث می‌شود برنامه "
                        "پس از روشن شدن دوباره باز نشود.")

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

        # save state BEFORE we hibernate, so a power-cut won't lose settings
        self._save_all_state()

        epoch = int(wake_at.timestamp())
        cmd = ["sudo", "-n", "rtcwake", "-m", mode, "-t", str(epoch)]
        self._append_log("INFO", "در حال تنظیم بیداری: " + " ".join(cmd))
        self._append_log(
            "INFO",
            f"کلاس {class_start:%H:%M} -> بیداری {wake_at:%H:%M:%S}"
        )

        self.btn_arm_sleep.setEnabled(False)
        threading.Thread(
            target=self._do_rtcwake, args=(cmd,), daemon=True).start()

    def _do_rtcwake(self, cmd):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        except Exception as e:
            self._emit_log("ERROR", f"rtcwake ناموفق: {e}")
            QTimer.singleShot(0, lambda: self.btn_arm_sleep.setEnabled(True))
            return

        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            self._emit_log("ERROR", f"rtcwake ناموفق: {err}")
            QTimer.singleShot(0, lambda: self._on_rtcwake_error(err))
            return

        QTimer.singleShot(0, self._on_woke_up)

    def _on_rtcwake_error(self, msg: str):
        self.btn_arm_sleep.setEnabled(True)
        low = (msg or "").lower()
        if ("sudo" in low or "password" in low or
                "a terminal is required" in low or
                "no tty present" in low):
            QMessageBox.critical(
                self, "نیاز به sudo",
                "rtcwake به sudo بدون رمز نیاز دارد.\n\n"
                "دکمه «راه‌اندازی اولیه دسترسی» را بزنید و دستور نمایش "
                "داده شده را در ترمینال اجرا کنید."
            )
        else:
            QMessageBox.critical(self, "rtcwake ناموفق", msg or "(بدون خروجی)")

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

    def setup_wake_permission(self):
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or "$USER"
        cmd = (f"echo '{user} ALL=(ALL) NOPASSWD: /usr/bin/rtcwake' | "
               f"sudo tee /etc/sudoers.d/skyroom-rtcwake && "
               f"sudo chmod 440 /etc/sudoers.d/skyroom-rtcwake && "
               f"sudo -k")

        dlg = QDialog(self)
        dlg.setWindowTitle("راه‌اندازی اولیه دسترسی")
        dlg.resize(720, 220)
        v = QVBoxLayout(dlg)

        v.addWidget(QLabel("این دستور را یک‌بار در ترمینال اجرا کنید:"))

        text = QTextEdit()
        text.setPlainText(cmd)
        text.setReadOnly(True)
        text.setFont(QFont(_pick_mono_font(), 10))
        text.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        text.setFixedHeight(80)
        v.addWidget(text)

        h = QHBoxLayout()
        copy_btn = QPushButton("کپی در حافظه")
        apply_button_style(copy_btn, "neutral")
        copy_btn.clicked.connect(
            lambda: (QApplication.clipboard().setText(cmd),
                     self._append_log("INFO", "دستور در کلیپ‌بورد کپی شد")))
        close_btn = QPushButton("بستن")
        apply_button_style(close_btn, "neutral")
        close_btn.clicked.connect(dlg.accept)
        h.addWidget(copy_btn)
        h.addWidget(close_btn)
        h.addStretch(1)
        v.addLayout(h)

        dlg.exec()

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
    def _about(self):
        s = settings()
        path = s.fileName()
        QMessageBox.about(
            self, "درباره",
            "مدیریت ربات اسکای‌روم\n\n"
            "ورود خودکار به کلاس‌ها با مرورگر ناشناس (incognito).\n"
            "تمام زمان‌ها بر اساس منطقه زمانی آسیا/تهران.\n"
            "هر جلسه در زمان پایان کلاس به صورت خودکار بسته می‌شود.\n\n"
            f"تنظیمات در این مسیر ذخیره می‌شوند:\n{path}"
        )

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

        # persist everything before we go
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
    
    # ---- application icon ----
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