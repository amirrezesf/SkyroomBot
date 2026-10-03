"""
Download a chromedriver that matches the current Chrome for Testing
"Stable" channel, for the current OS, and place it in ./drivers/.

Used by the GitHub Actions build. Can also be run locally.
"""

import json
import os
import platform
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DRIVERS = ROOT / "drivers"

ENDPOINT = ("https://googlechromelabs.github.io/chrome-for-testing/"
            "last-known-good-versions-with-downloads.json")

PLATFORMS = {
    "Windows": ("win64", "chromedriver.exe"),
    "Linux":   ("linux64", "chromedriver"),
    "Darwin":  ("mac-x64", "chromedriver"),   # in case you build on macOS
}


import os
import socks
import socket

_proxy = os.environ.get("ALL_PROXY") or os.environ.get("HTTPS_PROXY")
if _proxy and _proxy.startswith("socks"):
    # Parse socks5h://host:port
    _host_port = _proxy.split("://", 1)[1]
    _host, _port = _host_port.rsplit(":", 1)
    socks.set_default_proxy(socks.SOCKS5, _host, int(_port),
                            rdns=True)
    socket.socket = socks.socksocket


def main() -> int:
    system = platform.system()
    if system not in PLATFORMS:
        print(f"unsupported OS: {system}", file=sys.stderr)
        return 1
    plat, exe_name = PLATFORMS[system]

    # ---- 1) find the current stable version ----
    with urllib.request.urlopen(ENDPOINT, timeout=30) as r:
        data = json.load(r)
    version = data["channels"]["Stable"]["version"]
    print(f"Chrome for Testing Stable = {version}")

    # ---- 2) build the URL ----
    url = (f"https://storage.googleapis.com/"
           f"chrome-for-testing-public/{version}/{plat}/"
           f"chromedriver-{plat}.zip")
    print(f"downloading {url}")

    DRIVERS.mkdir(exist_ok=True)
    zip_path = DRIVERS / "chromedriver.zip"
    urllib.request.urlretrieve(url, zip_path)

    # ---- 3) extract just the binary ----
    with zipfile.ZipFile(zip_path, "r") as z:
        for name in z.namelist():
            if name.endswith("/" + exe_name) or name == exe_name:
                with z.open(name) as src, open(DRIVERS / exe_name, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                break
        else:
            print("chromedriver binary not found inside zip", file=sys.stderr)
            return 1

    zip_path.unlink()

    if system != "Windows":
        os.chmod(DRIVERS / exe_name, 0o755)

    print(f"OK -> {DRIVERS / exe_name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
