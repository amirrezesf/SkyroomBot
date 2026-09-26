# skyroom_bot.spec
# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# --- Configuration ---
# The name of your main script
MAIN_SCRIPT = 'pyqt.py'
# The name of the output executable
EXE_NAME = 'SkyroomBot'
# Path to chromedriver (Windows: 'chromedriver.exe', Linux: 'chromedriver')
# Place the driver in a 'drivers' folder next to this spec file.
CHROMEDRIVER_PATH = Path('drivers') / ('chromedriver.exe' if sys.platform == 'win32' else 'chromedriver')

# --- Analysis ---
a = Analysis(
    [MAIN_SCRIPT],
    pathex=[],
    binaries=[
        # Bundle the chromedriver binary
        (str(CHROMEDRIVER_PATH), 'drivers'),
    ],
    datas=[
        # Include any data files (e.g., your JSON schedule, icons)
        # ('users.json', '.'),
        # ('assets/icon.png', 'assets'),
    ] + collect_data_files('PyQt6'),  # Include PyQt6 data files
    hiddenimports=[
        # Ensure Selenium's dynamic imports are included
        'selenium.webdriver.chrome.service',
        'selenium.webdriver.chrome.options',
        'selenium.webdriver.common.by',
        'selenium.webdriver.common.keys',
        'selenium.webdriver.support.expected_conditions',
        'selenium.webdriver.support.ui',
    ] + collect_submodules('selenium'),  # Collect all selenium submodules
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude unnecessary Qt modules to reduce size
        'PyQt6.QtWebEngineCore',
        'PyQt6.QtWebEngineWidgets',
        'PyQt6.QtQml',
        'PyQt6.QtQuick',
        'PyQt6.Qt3DCore',
    ],
    noarchive=False,
)

# --- PYZ ---
pyz = PYZ(a.pure)

# --- EXE ---
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,  # Use UPX for compression if available
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # Set to True if you want a console window for debugging
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/icon.ico',  # Optional: path to your icon
)