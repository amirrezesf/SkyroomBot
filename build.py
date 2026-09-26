#!/usr/bin/env python3
"""
Build script for SkyroomBot.
Run: python build.py
"""
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

# --- Configuration ---
PROJECT_ROOT = Path(__file__).parent
DRIVERS_DIR = PROJECT_ROOT / 'drivers'
SPEC_FILE = PROJECT_ROOT / 'skyroom_bot.spec'
VENV_DIR = PROJECT_ROOT / 'Lvenv'

def setup_venv():
    """Create and activate a virtual environment."""
    if not VENV_DIR.exists():
        print("📦 Creating virtual environment...")
        subprocess.run([sys.executable, '-m', 'venv', str(VENV_DIR)], check=True)
    
    # Determine the Python executable path based on OS
    if platform.system() == 'Windows':
        python_path = VENV_DIR / 'Scripts' / 'python.exe'
    else:
        python_path = VENV_DIR / 'bin' / 'python'
    
    # Install dependencies
    print("📦 Installing dependencies...")
    subprocess.run([str(python_path), '-m', 'pip', 'install', '--upgrade', 'pip'], check=True)
    subprocess.run([str(python_path), '-m', 'pip', 'install', '-r', 'requirements.txt'], check=True)
    subprocess.run([str(python_path), '-m', 'pip', 'install', 'pyinstaller'], check=True)
    
    return python_path

def get_chrome_version():
    """Detect the installed Chrome version."""
    system = platform.system()
    try:
        if system == 'Windows':
            # Windows: Check registry or common paths
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Google\Chrome\BLBeacon')
            version, _ = winreg.QueryValueEx(key, 'version')
            return version
        elif system == 'Linux':
            # Linux: Use google-chrome command
            result = subprocess.run(['google-chrome', '--version'], capture_output=True, text=True)
            if result.returncode == 0:
                return result.stdout.strip().split()[-1]
        elif system == 'Darwin':
            # macOS
            result = subprocess.run(['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', '--version'], capture_output=True, text=True)
            if result.returncode == 0:
                return result.stdout.strip().split()[-1]
    except Exception as e:
        print(f"⚠️  Could not detect Chrome version: {e}")
    return None

def download_chromedriver():
    """Download the matching chromedriver for the detected Chrome version."""
    chrome_version = get_chrome_version()
    if not chrome_version:
        print("❌ Could not detect Chrome. Please install Google Chrome.")
        sys.exit(1)
    
    major_version = chrome_version.split('.')[0]
    system = platform.system()
    
    # Determine the correct driver URL
    if system == 'Windows':
        driver_zip = f'chromedriver_win32.zip'
        driver_exe = 'chromedriver.exe'
    elif system == 'Linux':
        driver_zip = f'chromedriver_linux64.zip'
        driver_exe = 'chromedriver'
    elif system == 'Darwin':
        driver_zip = f'chromedriver_mac64.zip'
        driver_exe = 'chromedriver'
    else:
        print(f"❌ Unsupported OS: {system}")
        sys.exit(1)
    
    # Try to find the correct version from the Chrome for Testing API
    # For simplicity, we'll use the known good versions endpoint
    # In production, you'd query: https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json
    # For this example, we'll use a recent stable version that matches the major version.
    # A more robust solution would parse the JSON API.
    
    # Placeholder: Use a hardcoded recent version for demonstration.
    # In a real scenario, parse the JSON to find the exact build.
    version_map = {
        '153': '153.0.8010.52',
        # Add more mappings as needed
    }
    driver_version = version_map.get(major_version, '153.0.8010.52')
    
    base_url = f'https://storage.googleapis.com/chrome-for-testing-public/{driver_version}'
    if system == 'Windows':
        url = f'{base_url}/win64/{driver_zip}'
    elif system == 'Linux':
        url = f'{base_url}/linux64/{driver_zip}'
    elif system == 'Darwin':
        url = f'{base_url}/mac-x64/{driver_zip}'
    
    DRIVERS_DIR.mkdir(exist_ok=True)
    zip_path = DRIVERS_DIR / driver_zip
    
    print(f"📥 Downloading chromedriver {driver_version} for {system}...")
    urllib.request.urlretrieve(url, zip_path)
    
    # Extract
    with zipfile.ZipFile(zip_path, 'r') as z:
        # The zip contains a folder like 'chromedriver-linux64/chromedriver'
        # We need to extract the binary to the drivers folder
        for name in z.namelist():
            if name.endswith(driver_exe):
                with z.open(name) as src, open(DRIVERS_DIR / driver_exe, 'wb') as dst:
                    shutil.copyfileobj(src, dst)
                break
    
    # Clean up zip
    zip_path.unlink()
    
    # Make executable on Linux/macOS
    if system in ('Linux', 'Darwin'):
        os.chmod(DRIVERS_DIR / driver_exe, 0o755)
    
    print(f"✅ Chromedriver saved to {DRIVERS_DIR / driver_exe}")

def build():
    """Run PyInstaller with the spec file."""
    python_path = VENV_DIR / ('Scripts' if platform.system() == 'Windows' else 'bin') / 'python'
    
    print("🔨 Building executable...")
    subprocess.run([
        str(python_path), '-m', 'PyInstaller',
        '--clean',
        str(SPEC_FILE),
    ], check=True, cwd=PROJECT_ROOT)
    
    # Determine output path
    dist_dir = PROJECT_ROOT / 'dist'
    exe_name = 'SkyroomBot.exe' if platform.system() == 'Windows' else 'SkyroomBot'
    exe_path = dist_dir / exe_name
    
    print(f"\n🎉 Build complete!")
    print(f"📁 Executable: {exe_path}")
    print(f"📁 Dist folder: {dist_dir}")

if __name__ == '__main__':
    print("🚀 SkyroomBot Build Script")
    print("=" * 40)
    
    # Ensure requirements.txt exists
    if not (PROJECT_ROOT / 'requirements.txt').exists():
        print("❌ requirements.txt not found. Creating from imports...")
        # In a real project, you'd have this file. For now, we'll create a basic one.
        with open(PROJECT_ROOT / 'requirements.txt', 'w') as f:
            f.write("PyQt6\nselenium\n")
    
    setup_venv()
    download_chromedriver()
    build()