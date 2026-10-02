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

# ---- base data + hidden imports ----
datas = [
    (str(CHROMEDRIVER), "drivers"),
    ("assets/icon-512.png", "assets"),
] + collect_data_files("PyQt6")

hiddenimports = [
    "PyQt6.QtCore",
    "PyQt6.QtGui",
    "PyQt6.QtWidgets",
    "selenium.webdriver.chrome.service",
    "selenium.webdriver.chrome.options",
    "selenium.webdriver.common.by",
    "selenium.webdriver.common.keys",
    "selenium.webdriver.support.expected_conditions",
    "selenium.webdriver.support.ui",
    "tzdata",
] + collect_submodules("selenium") + collect_submodules("tzdata")

binaries = []

# ---- Jarvis bundle (full variant only) ----
if WITH_JARVIS:
    import jarvis  # noqa: F401  (fail loudly if not installed)

    jarvis_root = Path(jarvis.__file__).parent

    # Package code, including sub-packages the try/except hides from
    # PyInstaller's static analysis.
    hiddenimports += collect_submodules("jarvis")

    # The packaged alarm sound lives under jarvis/assets, not as a module.
    datas += collect_data_files("jarvis")

    # Runtime dependencies that load native code dynamically.
    hiddenimports += [
        "faster_whisper",
        "ctranslate2",
        "silero_vad",
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
    hiddenimports += collect_submodules("silero_vad")
    hiddenimports += collect_submodules("ctranslate2")

    # Native shared libraries.
    binaries += collect_dynamic_libs("ctranslate2")
    binaries += collect_dynamic_libs("soundfile")
    binaries += collect_dynamic_libs("sounddevice")

    # silero-vad and faster-whisper ship small data files (ONNX model,
    # tokenizer config, etc.) that must be copied next to the module.
    datas += collect_data_files("silero_vad")
    datas += collect_data_files("faster_whisper")

# ---- Analysis ----
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
        "PyQt6.QtQml",
        "PyQt6.QtQuick",
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
    name="SkyroomBot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    windowed=True,
    icon="assets/icon.ico",
)