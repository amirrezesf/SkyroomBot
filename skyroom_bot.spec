# skyroom_bot.spec
import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_submodules,
    collect_dynamic_libs,
)

CHROMEDRIVER = ("drivers/chromedriver.exe" if sys.platform == "win32"
                else "drivers/chromedriver")

WITH_JARVIS = os.environ.get("BUILD_VARIANT") == "full"

# =========================================================
# Data files and hidden imports
# =========================================================
# Note: no collect_data_files("PyQt6"). PyInstaller's built-in PyQt6
# hook collects only the Qt libraries the imported modules actually
# need. Wholesale collection would pull in Bluetooth, WebEngine,
# Sensors, and other unused libraries — inflating the binary and
# breaking onefile extraction.
datas = [
    (str(CHROMEDRIVER), "drivers"),
    ("assets/icon-512.png", "assets"),
]

hiddenimports = [
    "selenium.webdriver.chrome.service",
    "selenium.webdriver.chrome.options",
    "selenium.webdriver.common.by",
    "selenium.webdriver.common.keys",
    "selenium.webdriver.support.expected_conditions",
    "selenium.webdriver.support.ui",
    "tzdata",
] + collect_submodules("selenium") + collect_submodules("tzdata")

binaries = []

# =========================================================
# Jarvis bundle (full variant only)
# =========================================================
if WITH_JARVIS:
    import jarvis  # noqa: F401

    hiddenimports += collect_submodules("jarvis")
    datas += collect_data_files("jarvis")

    hiddenimports += [
        "faster_whisper",
        "ctranslate2",
        "sounddevice",
        "soundfile",
        "onnxruntime",
        "rapidfuzz",
        "rapidfuzz.fuzz",
        "rapidfuzz.process_cpp",
        "numpy",
        "requests",
        "dotenv",
    ]
    hiddenimports += collect_submodules("faster_whisper")
    hiddenimports += collect_submodules("ctranslate2")

    binaries += collect_dynamic_libs("ctranslate2")
    binaries += collect_dynamic_libs("soundfile")
    binaries += collect_dynamic_libs("sounddevice")
    binaries += collect_dynamic_libs("onnxruntime")

    datas += collect_data_files("faster_whisper")

# =========================================================
# Analysis
# =========================================================
a = Analysis(
    ["pyqt.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "PyQt6.QtWebEngineCore",
        "PyQt6.QtWebEngineWidgets",
        "PyQt6.QtWebEngineQuick",
        "PyQt6.QtWebChannel",
        "PyQt6.QtWebSockets",
        "PyQt6.QtQml",
        "PyQt6.QtQuick",
        "PyQt6.QtQuick3D",
        "PyQt6.QtQuickWidgets",
        "PyQt6.QtBluetooth",
        "PyQt6.QtNetworkAuth",
        "PyQt6.QtNfc",
        "PyQt6.QtPositioning",
        "PyQt6.QtPositioningQuick",
        "PyQt6.QtRemoteObjects",
        "PyQt6.QtSensors",
        "PyQt6.QtSensorQuick",
        "PyQt6.QtSerialPort",
        "PyQt6.QtSerialBus",
        "PyQt6.QtSql",
        "PyQt6.QtTest",
        "PyQt6.QtTextToSpeech",
        "PyQt6.QtCharts",
        "PyQt6.QtDataVisualization",
        "PyQt6.QtGraphs",
        "PyQt6.QtMultimedia",
        "PyQt6.QtMultimediaWidgets",
        "PyQt6.QtOpenGL",
        "PyQt6.QtOpenGLWidgets",
        "PyQt6.QtPdf",
        "PyQt6.QtPdfWidgets",
        "PyQt6.QtDesigner",
        "PyQt6.QtHelp",
        "PyQt6.QtUiTools",
        "PyQt6.QtSvg",
        "PyQt6.QtSvgWidgets",

        "nvidia",
        "nvidia.cublas",
        "nvidia.cudnn",

        "torch",
        "torchaudio",
        "torchvision",
        "transformers",
        "tensorflow",
        "matplotlib",
        "scipy",
        "pandas",
        "IPython",
        "jupyter",
        "pytest",
        "tkinter",
        "PySide6",
        "PyQt5",

        "setuptools",
        "pip",
        "wheel",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

# =========================================================
# Output: full = onedir, base = onefile
# =========================================================
if WITH_JARVIS:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="SkyroomBot",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        windowed=True,
        icon="assets/icon.ico",
    )

    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="SkyroomBot",
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="SkyroomBot",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        windowed=True,
        icon="assets/icon.ico",
    )