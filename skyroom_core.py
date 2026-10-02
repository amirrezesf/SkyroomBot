"""
Skyroom Bot - core engine (no UI dependencies).

Schedule is interpreted in Asia/Tehran timezone using zoneinfo.
Each class has a start time and an end time on the same Persian weekday.
"""

import os
from pathlib import Path
import queue
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time as dtime
from typing import Callable, List, Optional
from zoneinfo import ZoneInfo
from jarvis_integration import JarvisRuntime, JARVIS_AVAILABLE
from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


# ============================================================
# Timezone & Persian day mapping
# ============================================================
try:
    from zoneinfo import ZoneInfo
    TEHRAN_TZ = ZoneInfo("Asia/Tehran")
except Exception:
    # Fallback: Tehran is UTC+3:30 year-round since 2022
    # (Iran abolished DST in September 2022)
    from datetime import timezone, timedelta
    TEHRAN_TZ = timezone(timedelta(hours=3, minutes=30))

# Python weekday: Monday=0 ... Sunday=6
PERSIAN_DAYS = {
    "شنبه": 5,
    "شنیه": 5,
    "یکشنبه": 6,
    "یک شنبه": 6,
    "یک‌شنبه": 6,
    "دوشنبه": 0,
    "دو شنبه": 0,
    "دو‌شنبه": 0,
    "سه‌شنبه": 1,
    "سه شنبه": 1,
    "سهشنبه": 1,
    "چهارشنبه": 2,
    "چهار شنبه": 2,
    "چهار‌شنبه": 2,
    "پنجشنبه": 3,
    "پنج‌شنبه": 3,
    "پنج شنبه": 3,
    "جمعه": 4,
}

DEFAULT_CLASS_DURATION = timedelta(hours=2)


def parse_time_string(s: str) -> dtime:
    """Parse '10:00am', '10:00 AM', '22:30', etc. into a time object."""
    s = s.strip().lower().replace(" ", "")
    s = s.replace("a.m.", "am").replace("p.m.", "pm")
    for fmt in ("%I:%M%p", "%I:%M:%S%p", "%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Unrecognized time format: {s!r}")


def next_occurrence(day_name: str, time_str: str) -> datetime:
    """
    Return the next *future* datetime (Tehran tz) matching the given Persian
    weekday + time. If today matches but the time already passed, returns the
    same weekday next week.

    Used by the Wake tab, which always cares about the next upcoming class.
    """
    day = day_name.strip()
    if day not in PERSIAN_DAYS:
        raise ValueError(f"Unknown Persian day: {day!r} "
                         f"(known: {sorted(PERSIAN_DAYS)})")
    target_wd = PERSIAN_DAYS[day]
    t = parse_time_string(time_str)

    now = datetime.now(TEHRAN_TZ)
    days_ahead = (target_wd - now.weekday()) % 7
    candidate = (now + timedelta(days=days_ahead)).replace(
        hour=t.hour, minute=t.minute, second=0, microsecond=0
    )
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def next_occurrence_range(day_name: str,
                          start_str: str,
                          end_str: Optional[str]):
    """
    Return (start_dt, end_dt) aware datetimes in Tehran tz.

    Unlike next_occurrence(), this function is *class-aware*:
      - If we are currently INSIDE today's class
        (today is the class weekday AND start <= now < end),
        it returns today's range. This is what makes pressing Start
        mid-class work.
      - If the class starts later today, it returns today's range.
      - Otherwise it returns the next upcoming occurrence.

    `end_dt` is on the same weekday as `start_dt`; if the end time is <=
    start time it rolls to the next day (unusual for classes, but handled).
    """
    day = day_name.strip()
    if day not in PERSIAN_DAYS:
        raise ValueError(f"Unknown Persian day: {day!r} "
                         f"(known: {sorted(PERSIAN_DAYS)})")
    target_wd = PERSIAN_DAYS[day]
    start_t = parse_time_string(start_str)
    end_t = parse_time_string(end_str) if end_str else None

    now = datetime.now(TEHRAN_TZ)

    # ---- Candidate: today's (or the next matching weekday's) start ----
    days_ahead = (target_wd - now.weekday()) % 7
    candidate_start = (now + timedelta(days=days_ahead)).replace(
        hour=start_t.hour, minute=start_t.minute, second=0, microsecond=0
    )

    def _end_for(start_dt: datetime) -> datetime:
        if end_t is None:
            return start_dt + DEFAULT_CLASS_DURATION
        e = start_dt.replace(hour=end_t.hour, minute=end_t.minute,
                             second=0, microsecond=0)
        if e <= start_dt:
            e += timedelta(days=1)
        return e

    candidate_end = _end_for(candidate_start)

    # ---- Case 0: a class that started YESTERDAY evening and is still
    # running now. It belongs to the previous weekday's occurrence, which
    # `candidate_start` (built from today's weekday) never looks at, so
    # without this a 23:00-01:00 class is invisible from 00:00 to 01:00.
    prev_start = candidate_start - timedelta(days=7)
    prev_end = _end_for(prev_start)
    if prev_start <= now < prev_end:
        return prev_start, prev_end

    # ---- Case 1: we are inside today's class right now ----
    # No weekday test here: a class that ends after midnight lives on the
    # *next* weekday for its second half, so "now.weekday() == target_wd"
    # would fail at 00:30 for a 23:00-01:00 class and send us a week ahead.
    if candidate_start <= now < candidate_end:
        return candidate_start, candidate_end

    # ---- Case 2: class starts in the future (today or later) ----
    if candidate_start > now:
        return candidate_start, candidate_end

    # ---- Case 3: today's class already ended -> next week ----
    candidate_start += timedelta(days=7)
    return candidate_start, _end_for(candidate_start)


# ============================================================
# Runtime config
# ============================================================
@dataclass
class RuntimeConfig:
    # ---- Chat message ----
    send_message: bool = True
    chat_message: str = "سلام"

    page_load_timeout: int = 60
    element_timeout: int = 30
    ws_check_interval: int = 5
    ws_dead_grace: int = 15
    max_restarts: int = 30

    min_delay_min: int = 10
    max_delay_min: int = 15

    # ---- Debug master + sub-flags ----
    debug_mode: bool = False
    debug_run_now: bool = False
    debug_disable_random_delay: bool = False
    debug_disable_ws_check: bool = False

    def effective(self) -> "RuntimeConfig":
        cfg = RuntimeConfig(**self.__dict__)
        if not self.debug_mode:
            cfg.debug_run_now = False
            cfg.debug_disable_random_delay = False
            cfg.debug_disable_ws_check = False
        return cfg


# ============================================================
# WS monitor JS
# ============================================================
WS_MONITOR_JS = r"""
(function () {
    if (window.__wsMonitorInstalled) return;
    window.__wsMonitorInstalled = true;

    window.__wsActive    = 0;
    window.__wsOpened    = 0;
    window.__wsLastClose = 0;

    const OriginalWS = window.WebSocket;
    function WrappedWS(url, protocols) {
        const ws = (protocols === undefined)
            ? new OriginalWS(url)
            : new OriginalWS(url, protocols);
        // Only count sockets that actually reached 'open'.  A socket that
        // fails before opening fires 'error'/'close' without ever firing
        // 'open', and counting those pushed __wsActive negative, which made
        // the watchdog report a dead connection while the page was still
        // retrying — triggering a needless full re-login.
        let counted = false;
        ws.addEventListener('open',  function () {
            counted = true;
            window.__wsActive++; window.__wsOpened++;
        });
        ws.addEventListener('close', function () {
            if (!counted) { return; }
            counted = false;
            window.__wsActive--;
            window.__wsLastClose = Date.now();
        });
        ws.addEventListener('error', function () {
            if (counted) { window.__wsLastClose = Date.now(); }
        });
        return ws;
    }
    WrappedWS.prototype = OriginalWS.prototype;
    for (const k of ['CONNECTING','OPEN','CLOSING','CLOSED']) {
        try { WrappedWS[k] = OriginalWS[k]; } catch (e) {}
    }
    window.WebSocket = WrappedWS;
})();
"""


# ============================================================
# Task model
# ============================================================
@dataclass
class Task:
    user_name: str
    class_name: str
    url: str
    scheduled_time: datetime            # class start (Tehran tz)
    end_time: datetime                  # class end   (Tehran tz)
    actual_time: datetime = field(init=False)
    message_sent: bool = field(default=False, init=False)
    user_dict: dict = field(default_factory=dict)   
    live: bool = False                              
    work_queue: "queue.Queue[tuple[str, dict]] | None" = field(
        default=None, init=False,
    )

    def compute_actual(self, cfg: RuntimeConfig) -> None:
        """
        Decide when to actually join the class, based on the current moment.

        Rules (in order):
          * Debug 'run now'                 -> join immediately.
          * Debug 'no random delay'         -> join at exactly start_time
                                               (or now, if start has passed).
          * now >= start + max_delay        -> join now (past the random window).
          * now is inside the random window -> random point in [now, window_end].
          * otherwise                       -> random point in
                                               [start + min_delay, start + max_delay].

        The chosen join time is always clamped to be < end_time.
        """
        now = datetime.now(TEHRAN_TZ)
        self._compute_actual_at(now, cfg)

    def _compute_actual_at(self, now: datetime, cfg: RuntimeConfig) -> None:
        # Debug: join now
        if cfg.debug_run_now:
            self.actual_time = now
            return

        # Debug: fixed time (= class start, or now if start already passed)
        if cfg.debug_disable_random_delay:
            self.actual_time = max(self.scheduled_time, now)
            if self.actual_time >= self.end_time and now < self.end_time:
                self.actual_time = now
            return

        lo = min(cfg.min_delay_min, cfg.max_delay_min)
        hi = max(cfg.min_delay_min, cfg.max_delay_min)

        window_start = self.scheduled_time + timedelta(minutes=lo)
        window_end = self.scheduled_time + timedelta(minutes=hi)

        # Never aim past the class end
        latest = min(window_end, self.end_time)

        # Past the random window -> join immediately
        if now >= latest:
            self.actual_time = now
            return

        # Earliest allowed join is either 'now' or the window start, whichever
        # is later.
        earliest = max(now, window_start)
        if earliest >= latest:
            self.actual_time = now
            return

        # Random point inside [earliest, latest]
        span = (latest - earliest).total_seconds()
        self.actual_time = earliest + timedelta(seconds=random.uniform(0, span))


# ============================================================
# Engine
# ============================================================
class SkyroomBot:
    def __init__(self,
                 config: RuntimeConfig,
                 users: List[dict],
                 log_cb: Callable[[str, str], None],
                 jarvis: Optional["JarvisRuntime"] = None):
        self.cfg = config.effective()
        self.users = users
        self.log_cb = log_cb
        self.jarvis = jarvis if jarvis is not None else JarvisRuntime(log_cb)
        self.stop_event = threading.Event()
        self.threads: List[threading.Thread] = []
        self.active_drivers: set = set()
        self._drivers_lock = threading.Lock()
        self._driver_lock = threading.Lock()
        self._driver_ready = threading.Event()
        self._driver_path: Optional[str] = None
        self._sm_lock = threading.Lock()

    # ----- public API -----
    def start(self) -> List[Task]:
        tasks = self._build_tasks()
        for task in tasks:
            th = threading.Thread(
                target=self._worker,
                args=(task,),
                name=f"{task.user_name}@{task.class_name}",
                daemon=True,
            )
            self.threads.append(th)
            th.start()
        return tasks

    def stop(self, join_timeout: float = 10.0) -> None:
        self.stop_event.set()
        with self._drivers_lock:
            drivers = list(self.active_drivers)
        for d in drivers:
            try:
                d.quit()
            except Exception:
                pass
        for t in self.threads:
            t.join(timeout=join_timeout)

    # ----- internals -----
    def _build_tasks(self) -> List[Task]:
        tasks: List[Task] = []
        now = datetime.now(TEHRAN_TZ)

        for user in self.users:
            user_name = user.get("user_name", "").strip()
            for cls in user.get("classes", []):
                cls_name = cls.get("name", "class")
                end_raw = cls.get("end_time")
                try:
                    start_dt, end_dt = next_occurrence_range(
                        cls["day"], cls["time"], end_raw
                    )
                except Exception as e:
                    self.log_cb("ERROR",
                                f"[{user_name}] bad schedule in class "
                                f"'{cls_name}': {e}")
                    continue

                if not end_raw:
                    self.log_cb("WARNING",
                                f"[{user_name}@{cls_name}] no 'end_time' in "
                                f"JSON — assuming +2h "
                                f"({end_dt:%H:%M} Tehran)")

                # Classify so the user can see exactly what was decided.
                # "already ended" is unreachable: next_occurrence_range()
                # never returns a past range — it rolls forward a week
                # instead. Kept only as a guard against a clock change.
                if now >= end_dt:
                    status = "already ended — will skip"
                elif start_dt <= now < end_dt:
                    status = "in progress — will join immediately"
                else:
                    status = "upcoming"

                self.log_cb("INFO",
                            f"[{user_name}@{cls_name}] "
                            f"{start_dt:%a %Y-%m-%d %H:%M}"
                            f"-{end_dt:%H:%M} Tehran ({status})")

                task = Task(
                    user_name=user_name,
                    class_name=cls_name,
                    url=cls.get("url", "").strip(),
                    scheduled_time=start_dt,
                    end_time=end_dt, 
                    user_dict=user,                        
                    live=bool(cls.get("live", False)), 
                )
                tasks.append(task)
        return tasks

    def _log(self, level: str, msg: str):
        try:
            self.log_cb(level, msg)
        except Exception:
            pass

    def _worker(self, task: Task) -> None:
        tag = f"[{task.user_name}@{task.class_name}]"

        # ---- 1) end-of-class guard ----
        # next_occurrence_range() never hands us an ended class, so this only
        # triggers if the wall clock jumped forward after the task was built.
        now = datetime.now(TEHRAN_TZ)
        if now >= task.end_time:
            self._log("INFO",
                      f"{tag} class already ended "
                      f"({task.end_time:%H:%M} Tehran) — skipping")
            return

        # ---- 2) recompute join time with a fresh 'now' ----
        task.compute_actual(self.cfg)

        # ---- 3) describe what we're about to do ----
        now = datetime.now(TEHRAN_TZ)
        wait_s = (task.actual_time - now).total_seconds()

        if task.actual_time >= task.end_time:
            self._log("INFO",
                      f"{tag} computed join time "
                      f"({task.actual_time:%H:%M:%S}) is at/after end time "
                      f"({task.end_time:%H:%M}) — skipping")
            return

        if wait_s > 0:
            mode = "DEBUG-NOW" if self.cfg.debug_run_now else (
                "FIXED-TIME" if self.cfg.debug_disable_random_delay
                else "RANDOM-WINDOW"
            )
            self._log("INFO",
                      f"{tag} [{mode}] class {task.scheduled_time:%H:%M}"
                      f"-{task.end_time:%H:%M} — joining at "
                      f"{task.actual_time:%H:%M:%S} "
                      f"(in {wait_s/60:.2f} min)")
            deadline = time.time() + wait_s
            while time.time() < deadline:
                if self.stop_event.wait(min(1.0, deadline - time.time())):
                    self._log("INFO", f"{tag} stop requested during wait")
                    return
        else:
            self._log("INFO",
                      f"{tag} class {task.scheduled_time:%H:%M}"
                      f"-{task.end_time:%H:%M} is in progress — joining now")

        # ---- 4) restart loop ----
        for attempt in range(1, self.cfg.max_restarts + 1):
            if self.stop_event.is_set():
                self._log("INFO", f"{tag} stop requested before attempt {attempt}")
                return

            if datetime.now(TEHRAN_TZ) >= task.end_time:
                self._log("INFO", f"{tag} end time reached — stopping")
                return

            status = "RETRY"
            try:
                status = self._join_room_once(task, attempt, tag)
            except TimeoutException as e:
                self._log("ERROR", f"{tag} attempt {attempt} timeout: {e}")
            except NoSuchElementException as e:
                self._log("ERROR", f"{tag} attempt {attempt} element missing: {e}")
            except WebDriverException as e:
                self._log("ERROR", f"{tag} attempt {attempt} driver error: {e}")
                if "Could not start Chrome" in str(e):
                    # Fatal environment problem (Chrome missing/broken,
                    # no usable driver) — retrying every 3s won't fix it.
                    # Stop this task instead of spamming 30 identical errors.
                    self._log("ERROR",
                              f"{tag} Chrome cannot start on this machine — "
                              f"task stopped (fix the issue above and press "
                              f"Start again)")
                    return
            except Exception as e:
                self._log("ERROR", f"{tag} attempt {attempt} unexpected: {e}")

            if status in ("ENDED", "STOPPED"):
                return
            if self.stop_event.wait(3.0):
                return

        self._log("ERROR", f"{tag} giving up after {self.cfg.max_restarts} attempts")

    # ----- driver + clicks -----
    @staticmethod
    def _chrome_launch_env() -> dict:
        # PyInstaller onefile exports its bundled libs via LD_LIBRARY_PATH
        # pointing at the _MEI temp dir. Chrome honors that and picks up
        # the bundle's older libnss3.so instead of the system one, then
        # dies with "libnss3.so: version `NSS_x' not found". Strip any
        # bundle dir from the library search path so Chrome uses system NSS.
        env = os.environ.copy()
        bundle_dir = getattr(sys, "_MEIPASS", None)

        def _clean(var: str) -> None:
            val = env.get(var)
            if not val:
                return
            kept = []
            for part in val.split(os.pathsep):
                if not part:
                    continue
                if bundle_dir and part == bundle_dir:
                    continue
                if "_MEI" in part:
                    continue
                kept.append(part)
            if kept:
                env[var] = os.pathsep.join(kept)
            else:
                env.pop(var, None)

        _clean("LD_LIBRARY_PATH")
        _clean("LD_PRELOAD")
        return env

    def get_chromedriver_path(self):
        if getattr(sys, "frozen", False):
            base_dir = getattr(sys, "_MEIPASS", None)
            if base_dir:
                name = ("chromedriver.exe" if sys.platform == "win32"
                        else "chromedriver")
                p = Path(base_dir) / "drivers" / name
                if p.exists():
                    return str(p)
        # dev mode
        local = Path(__file__).parent / 'drivers' / (
            'chromedriver.exe' if sys.platform == 'win32' else 'chromedriver')
        if local.exists():
            return str(local)
        return None    # fall back to Selenium Manager
    # ----- driver discovery (once per run, shared by every task) -----
    @staticmethod
    def _chrome_binary() -> Optional[str]:
        """Locate the installed Chrome/Chromium, or None if it is missing."""
        for name in ("google-chrome", "google-chrome-stable", "chromium",
                     "chromium-browser", "chrome"):
            hit = shutil.which(name)
            if hit:
                return hit
        if sys.platform == "win32":
            for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
                base = os.environ.get(env)
                if not base:
                    continue
                cand = Path(base) / "Google/Chrome/Application/chrome.exe"
                if cand.exists():
                    return str(cand)
        elif sys.platform == "darwin":
            cand = (Path("/Applications/Google Chrome.app/Contents/MacOS")
                    / "Google Chrome")
            if cand.exists():
                return str(cand)
        return None

    def _probe_driver_candidates(self) -> Optional[str]:
        """Return the first chromedriver binary that actually runs here."""
        candidates: List[str] = []
        bundled = self.get_chromedriver_path()
        if bundled:
            candidates.append(bundled)
        path_hit = shutil.which("chromedriver")
        if path_hit and path_hit not in candidates:
            candidates.append(path_hit)

        for path in candidates:
            if not os.path.isfile(path):
                continue
            try:
                proc = subprocess.run(
                    [path, "--version"], capture_output=True, text=True,
                    timeout=15,
                )
            except Exception as e:
                self._log("WARNING",
                          f"chromedriver not runnable ({path}): {e} — skipping")
                continue
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip()
                self._log("WARNING",
                          f"chromedriver --version failed ({path}): {err} — "
                          "skipping")
                continue
            self._log("INFO", f"{path}: {(proc.stdout or '').strip()}")
            return path
        return None

    def _resolve_driver(self) -> Optional[str]:
        """
        Decide once per bot run which chromedriver to use (None = let
        Selenium Manager handle it).

        Every task thread used to repeat this on every retry, so N classes
        over R restarts meant N×R `chromedriver --version` subprocesses, and
        several threads could enter Selenium Manager simultaneously and race
        each other's download into the shared cache.
        """
        if self._driver_ready.is_set():
            return self._driver_path
        with self._driver_lock:
            if self._driver_ready.is_set():
                return self._driver_path
            try:
                self._driver_path = self._probe_driver_candidates()
            finally:
                self._driver_ready.set()
        return self._driver_path

    def preflight(self) -> List[str]:
        """
        Cheap environment checks the user can act on, in plain language.
        An empty list means the machine looks ready.  Runs before the first
        browser is opened so problems surface as one readable message
        instead of a wall of Selenium errors.
        """
        problems: List[str] = []

        if not self._chrome_binary():
            problems.append(
                "Google Chrome is not installed or cannot be found on this "
                "system. Install it, then press Start again."
            )

        if self._resolve_driver() is None:
            problems.append(
                "No working chromedriver was found next to the app. The bot "
                "will try to download a matching one automatically — this "
                "needs an internet connection and only happens on the first "
                "run."
            )

        try:
            if hasattr(os, "geteuid") and os.geteuid() == 0 \
                    and sys.platform != "win32":
                problems.append(
                    "You are running as root. Chrome will be started without "
                    "its sandbox; a normal user account is recommended."
                )
        except Exception:
            pass

        return problems

    def _chrome_options(self) -> Options:
        opts = Options()
        opts.add_argument("--incognito")
        opts.add_argument("--start-maximized")
        opts.add_argument("--disable-notifications")
        opts.add_argument("--disable-infobars")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-first-run")
        opts.add_argument("--no-default-browser-check")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)

        # On Linux, running Chrome as root without --no-sandbox makes it exit
        # immediately ("Running as root without --no-sandbox is not
        # supported"), which surfaces as "session not created: Chrome
        # instance exited". Also detect Wayland and force X11/Xvfb-compatible
        # flags where needed. Do not blindly pass --no-sandbox on Windows.
        try:
            is_root = (os.geteuid() == 0) if hasattr(os, "geteuid") else False
        except Exception:
            is_root = False
        if is_root and sys.platform != "win32":
            opts.add_argument("--no-sandbox")
            self._log("WARNING",
                      "running as root — added --no-sandbox so Chrome can start")
        if sys.platform.startswith("linux") and os.environ.get("WAYLAND_DISPLAY"):
            opts.add_argument("--ozone-platform-hint=x11")
        return opts

    def _build_driver(self) -> webdriver.Chrome:
        """
        Open one fresh incognito session.

        The driver *binary* was already resolved once in _resolve_driver;
        only the launch itself is repeated per attempt.  Selenium Manager is
        serialized so two classes starting at the same time cannot race each
        other's download.
        """
        opts = self._chrome_options()
        chrome_env = self._chrome_launch_env()

        path = self._resolve_driver()
        last_err: Optional[Exception] = None
        version_mismatch = False

        if path:
            try:
                driver = webdriver.Chrome(
                    service=Service(executable_path=path, env=chrome_env),
                    options=opts)
                self._log("INFO", f"using chromedriver: {path}")
                return self._finish_driver(driver)
            except WebDriverException as e:
                last_err = e
                msg = str(e)
                if "session not created" in msg and (
                        "This version of ChromeDriver only supports" in msg
                        or "Current browser version is" in msg):
                    version_mismatch = True
                    self._log("WARNING",
                              f"version mismatch, driver ignored ({path}): "
                              f"{msg.splitlines()[0] if msg else e}")
                else:
                    self._log("WARNING",
                              f"bundled/manual driver failed ({path}): {e} — "
                              "falling back to Selenium Manager")
                # Don't let the rest of the run keep retrying a driver we
                # already know is wrong.
                with self._driver_lock:
                    if self._driver_path == path:
                        self._driver_path = None

        # ---- Selenium Manager (auto-resolve / auto-download) ----
        # Skip it when the pinned driver failed with a plain "Chrome instance
        # exited": that means Chrome itself cannot start, and SM would hit
        # the same wall while hiding the real cause.
        if last_err and not version_mismatch and (
                "session not created" in str(last_err)
                and "Chrome instance exited" in str(last_err)):
            service_log = self._capture_service_log(
                opts, [path] if path else [])
            # The verbose log is diagnostic noise for a dialog: it belongs in
            # the log tab, not in a message a user has to read.
            self._log("ERROR", f"chromedriver diagnostic: {service_log}")
            raise WebDriverException(
                "Could not start Chrome: Chrome itself exited "
                "immediately (not a driver-version problem). "
                f"Driver error was: {last_err}. "
                "Check: google-chrome is installed and runnable "
                "(try `google-chrome --headless --dump-dom about:blank`), "
                "no stale /dev/shm or */.config/google-chrome lock, "
                "and enough disk/RAM. "
                "The chromedriver log is in the report tab."
            )

        self._log("INFO",
                  "no usable bundled chromedriver — Selenium Manager "
                  "will download the matching version "
                  "(needs internet, first run only)…")
        with self._sm_lock:
            try:
                driver = webdriver.Chrome(options=opts,
                                          service=Service(env=chrome_env))
                self._log("INFO", "Selenium Manager provided a driver")
            except WebDriverException as e:
                hint = (f" (tried bundled/manual drivers too: {last_err})"
                        if last_err else "")
                raise WebDriverException(
                    "Could not start Chrome: no bundled chromedriver found and "
                    f"Selenium Manager failed: {e}{hint}. "
                    "Make sure Google Chrome is installed."
                )
        return self._finish_driver(driver)

    def _finish_driver(self, driver) -> webdriver.Chrome:
        driver.set_page_load_timeout(self.cfg.page_load_timeout)
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": WS_MONITOR_JS},
        )
        return driver

    def _capture_service_log(self, opts: Options,
                             tried: list) -> str:
        """Re-run one launch with a verbose driver log to say WHY Chrome died.

        Returns a short human-readable hint (paths quoted for the GUI log).
        """
        exe = (tried[0] if tried else None) or shutil.which("chromedriver")
        if not exe:
            return ""
        log_path = Path(tempfile.gettempdir()) / "skyroombot-chromedriver.log"
        try:
            service = Service(executable_path=exe,
                              service_args=["--verbose"],
                              log_output=str(log_path))
            probe = webdriver.Chrome(service=service, options=opts)
            probe.quit()
            return ""
        except WebDriverException as e:
            detail = str(e).splitlines()[0] if str(e) else ""
        except Exception as e:  # noqa: BLE001 — diagnostic only
            detail = str(e).splitlines()[0] if str(e) else ""
        try:
            tail = log_path.read_text(
                encoding="utf-8", errors="replace").strip().splitlines()
            tail = [ln for ln in tail if ln.strip()][-15:]
        except Exception:
            tail = []
        hint = f"Driver said: {detail}. " if detail else ""
        if tail:
            hint += ("Verbose chromedriver log tail "
                     f"({log_path}): " + " | ".join(tail) + ". ")
        else:
            hint += (f"(no verbose log captured at {log_path}). ")
        return hint

    @staticmethod
    def _js_click(driver, element) -> None:
        driver.execute_script(
            """
            const el = arguments[0];
            const evt = new MouseEvent('click', {
                bubbles: true, cancelable: true, view: window
            });
            el.dispatchEvent(evt);
            """,
            element,
        )

    def _safe_click(self, driver, element) -> bool:
        try:
            element.click()
            return True
        except WebDriverException:
            try:
                self._js_click(driver, element)
                return True
            except WebDriverException:
                return False

    def _register_driver(self, driver):
        with self._drivers_lock:
            self.active_drivers.add(driver)

    def _unregister_driver(self, driver):
        with self._drivers_lock:
            self.active_drivers.discard(driver)

    # ----- one full attempt -----
    def _join_room_once(self, task: Task, attempt: int, tag: str) -> str:
        self._log("INFO", f"{tag} attempt {attempt}: fresh incognito session")
        driver = self._build_driver()
        self._register_driver(driver)
        try:
            self._log("INFO", f"{tag} navigating to {task.url}")
            driver.get(task.url)
            wait = WebDriverWait(driver, self.cfg.element_timeout)

            # STEP 1 - guest login
            guest_btn = wait.until(EC.element_to_be_clickable(
                (By.CSS_SELECTOR, '[data-name="guess-login-button"]')
            ))
            if not self._safe_click(driver, guest_btn):
                raise WebDriverException("could not click guest-login")
            self._log("INFO", f"{tag} clicked 'ورود میهمان'")

            # STEP 2 - nickname
            nick_input = wait.until(EC.visibility_of_element_located(
                (By.CSS_SELECTOR, '[data-name="nickname-dialog-input"]')
            ))
            nick_input.clear()
            nick_input.send_keys(task.user_name)
            self._log("INFO", f"{tag} filled nickname = '{task.user_name}'")

            confirm_btn = wait.until(EC.element_to_be_clickable(
                (By.CSS_SELECTOR, '[data-name="nickname-dialog-button"]')
            ))
            if not self._safe_click(driver, confirm_btn):
                raise WebDriverException("could not click nickname-confirm")
            self._log("INFO", f"{tag} nickname confirmed")
            jarvis_active = False
            if task.live:
                task.work_queue = queue.Queue()
                jarvis_active = self.jarvis.acquire(
                    tag, task.user_dict, task.work_queue,
                )
            # STEP 3 - chat message (optional)
            if not self.cfg.send_message:
                if not getattr(task, "_skip_msg_logged", False):
                    self._log("INFO",
                              f"{tag} chat message disabled in config — skipping")
                    setattr(task, "_skip_msg_logged", True)
            elif task.message_sent:
                self._log("INFO", f"{tag} message already sent - skipping")
            else:
                try:
                    chat_input = wait.until(EC.element_to_be_clickable(
                        (By.CSS_SELECTOR, '#txt_input')
                    ))
                    time.sleep(2)
                    chat_input.click()
                    chat_input.send_keys(self.cfg.chat_message)
                    try:
                        chat_input.send_keys(Keys.ENTER)
                    except WebDriverException:
                        pass
                    try:
                        send_icon = driver.find_element(
                            By.CSS_SELECTOR, 'svg.chatBox__input__sendIcon'
                        )
                        self._safe_click(driver, send_icon)
                    except (NoSuchElementException, WebDriverException):
                        pass
                    task.message_sent = True
                    self._log("INFO",
                              f"{tag} sent '{self.cfg.chat_message}'")
                except (TimeoutException, WebDriverException) as e:
                    self._log("WARNING", f"{tag} could not send message: {e}")

            # STEP 4 - stay in room until end_time or interruption
            self._log("INFO",
                      f"{tag} staying in room until {task.end_time:%H:%M} Tehran")

            stream_started = False
            checks_without_stream = 0
            try:
                while True:
                    if datetime.now(TEHRAN_TZ) >= task.end_time:
                        self._log("INFO",
                                  f"{tag} end time reached — closing session")
                        return "ENDED"

                    # Shorter poll when Jarvis is active so decisions are
                    # logged promptly. The loop body itself is cheap.
                    interval = 1.0 if jarvis_active else self.cfg.ws_check_interval
                    if self.stop_event.wait(interval):
                        return "STOPPED"

                    if jarvis_active and task.work_queue is not None:
                        self._drain_action_queue(driver, task.work_queue, tag)

                    if self.cfg.debug_disable_ws_check:
                        continue
                    if not stream_started:
                        try:
                            opened = driver.execute_script(
                                "return window.__wsOpened || 0;"
                            )
                        except WebDriverException:
                            return "RETRY"
                        if opened and opened > 0:
                            stream_started = True
                            self._log("INFO",
                                      f"{tag} stream detected "
                                      f"({opened} socket(s)) — monitoring")
                        else:
                            checks_without_stream += 1
                            waited = (checks_without_stream
                                      * interval)
                            if checks_without_stream in (6, 24):
                                self._log(
                                    "WARNING",
                                    f"{tag} no class media socket after "
                                    f"{waited // 60} min — login may have "
                                    f"failed; check the browser window")
                        continue

                    if self._ws_is_dead(driver):
                        self._log("WARNING",
                                  f"{tag} WebSocket interrupted — restarting")
                        return "RETRY"

                    try:
                        alive = driver.execute_script(
                            "return !!document.querySelector('.roomTimer') "
                            "|| !!document.querySelector('#txt_input');"
                        )
                        if not alive:
                            self._log("WARNING",
                                      f"{tag} room DOM vanished — restarting")
                            return "RETRY"
                    except WebDriverException:
                        return "RETRY"
            finally:
                if jarvis_active:
                    self.jarvis.release(tag)
        finally:
            self._unregister_driver(driver)
            try:
                driver.quit()
            except Exception:
                pass
            self._log("INFO", f"{tag} browser closed")
    def _drain_action_queue(
        self, driver, work_queue: "queue.Queue[tuple[str, dict]]", tag: str,
    ) -> None:
        """Execute any driver actions queued by the Jarvis backend."""
        while True:
            try:
                action, args = work_queue.get_nowait()
            except queue.Empty:
                return
            try:
                if action == "type_number":
                    self._send_chat(driver, str(args.get("n", "")))
                    self._log("INFO",
                              f"{tag} [jarvis] typed: {args.get('n')}")
                elif action == "send_chat":
                    self._send_chat(driver, str(args.get("text", "")))
                    self._log("INFO",
                              f"{tag} [jarvis] sent: {args.get('text')}")
                else:
                    self._log("WARNING",
                              f"{tag} [jarvis] unknown action {action!r}")
            except Exception as e:
                self._log("ERROR",
                          f"{tag} [jarvis] {action} failed: {e}")

    def _send_chat(self, driver, text: str) -> None:
        """Type text into the Skyroom chat box and press Enter."""
        wait = WebDriverWait(driver, self.cfg.element_timeout)
        chat_input = wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "#txt_input"))
        )
        # Clear any leftover text — handles both input.value and contenteditable.
        try:
            driver.execute_script(
                "var el = document.querySelector('#txt_input'); "
                "if (el) { if ('value' in el) el.value = ''; "
                "el.textContent = ''; }"
            )
        except WebDriverException:
            pass
        chat_input.click()
        chat_input.send_keys(text)
        chat_input.send_keys(Keys.ENTER)
    def _ws_is_dead(self, driver) -> bool:
        if self.cfg.debug_disable_ws_check:
            return False
        try:
            state = driver.execute_script(
                "return { active:    window.__wsActive    || 0, "
                "         opened:    window.__wsOpened    || 0, "
                "         lastClose: window.__wsLastClose || 0, "
                "         now:       Date.now() };"
            )
        except WebDriverException:
            return True
        if state is None:
            return True
        active = state.get("active", 0)
        opened = state.get("opened", 0)
        last_close = state.get("lastClose", 0)
        now = state.get("now", 0)
        if opened == 0:
            return False
        if active > 0:
            return False
        if last_close == 0:
            return False
        return (now - last_close) > (self.cfg.ws_dead_grace * 1000)
