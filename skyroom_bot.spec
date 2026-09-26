# skyroom_bot.spec
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

CHROMEDRIVER = ("drivers/chromedriver.exe" if sys.platform == "win32"
                else "drivers/chromedriver")

a = Analysis(
    ['pyqt.py'],
    pathex=[],
    binaries=[],
    datas=[
        (str(CHROMEDRIVER), 'drivers'),
    ] + collect_data_files('PyQt6'),
    hiddenimports=[
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',
        'selenium.webdriver.chrome.service',
        'selenium.webdriver.chrome.options',
        'selenium.webdriver.common.by',
        'selenium.webdriver.common.keys',
        'selenium.webdriver.support.expected_conditions',
        'selenium.webdriver.support.ui',
    ] + collect_submodules('selenium'),
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        'PyQt6.QtWebEngineCore',
        'PyQt6.QtWebEngineWidgets',
        'PyQt6.QtQml',
        'PyQt6.QtQuick',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='SkyroomBot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    windowed=True,
)