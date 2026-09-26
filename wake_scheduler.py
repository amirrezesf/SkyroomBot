"""
Arm the RTC alarm for the next Skyroom class, then hibernate.

Usage:
    sudo python wake_scheduler.py users.json
"""

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Reuse the core's schedule helpers
sys.path.insert(0, str(Path(__file__).parent))
from skyroom_core import next_occurrence, TEHRAN_TZ

# How many minutes before class start should the machine wake up?
# 3 minutes is enough on an NVMe SSD to boot + login + launch Chrome.
WAKE_LEAD_MINUTES = 3

# Sleep mode: 'mem' = suspend, 'disk' = hibernate, 'off' = poweroff
SLEEP_MODE = "disk"


def next_wake_datetime(users: list) -> datetime:
    """Return the earliest upcoming class start (Tehran tz), minus lead time."""
    soonest = None
    for user in users:
        for cls in user.get("classes", []):
            try:
                dt = next_occurrence(cls["day"], cls["time"])
            except Exception:
                continue
            wake_at = dt - timedelta(minutes=WAKE_LEAD_MINUTES)
            if soonest is None or wake_at < soonest:
                soonest = wake_at
    if soonest is None:
        raise RuntimeError("no valid classes in schedule")
    return soonest


def arm_wake(users: list, mode: str = SLEEP_MODE) -> None:
    wake_at = next_wake_datetime(users)
    now = datetime.now(TEHRAN_TZ)
    delta_s = int((wake_at - now).total_seconds())

    if delta_s <= 0:
        print(f"[!] Next wake time already passed: {wake_at}. Nothing to do.")
        return

    epoch = int(wake_at.timestamp())
    print(f"Now (Tehran):       {now:%Y-%m-%d %H:%M:%S}")
    print(f"Next class wake:    {wake_at:%Y-%m-%d %H:%M:%S}")
    print(f"Sleeping for:       {delta_s//3600}h {(delta_s%3600)//60}m {delta_s%60}s")
    print(f"Mode:               {mode}")
    print(f"Arming RTC and entering sleep…")

    # rtcwake writes the alarm THEN performs the sleep, in one call.
    subprocess.run(
        ["sudo", "rtcwake", "-m", mode, "-t", str(epoch)],
        check=True,
    )


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: sudo python wake_scheduler.py users.json [mem|disk|off]")
        sys.exit(2)

    users = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    mode = sys.argv[2] if len(sys.argv) > 2 else SLEEP_MODE
    arm_wake(users, mode)


if __name__ == "__main__":
    main()