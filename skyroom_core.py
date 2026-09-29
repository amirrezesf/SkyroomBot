"""
Skyroom Bot - core engine (no UI dependencies).

Schedule is interpreted in Asia/Tehran timezone using zoneinfo.
Each class has a start time and an end time on the same Persian weekday.
"""

import os
from pathlib import Path
import random
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time as dtime
from typing import Callable, List, Optional
from zoneinfo import ZoneInfo

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

    # ---- Case 1: we are inside today's class right now ----
    if (now.weekday() == target_wd
            and candidate_start <= now < candidate_end):
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
    chromedriver_path: str = "/usr/bin/chromedriver"

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
        ws.addEventListener('open',  function () {
            window.__wsActive++; window.__wsOpened++;
        });
        ws.addEventListener('close', function () {
            window.__wsActive--; window.__wsLastClose = Date.now();
        });
        ws.addEventListener('error', function () {
            window.__wsLastClose = Date.now();
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
                 log_cb: Callable[[str, str], None]):
        self.cfg = config.effective()
        self.users = users
        self.log_cb = log_cb
        self.stop_event = threading.Event()
        self.threads: List[threading.Thread] = []
        self.active_drivers: set = set()
        self._drivers_lock = threading.Lock()

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
            except Exception as e:
                self._log("ERROR", f"{tag} attempt {attempt} unexpected: {e}")

            if status in ("ENDED", "STOPPED"):
                return
            if self.stop_event.wait(3.0):
                return

        self._log("ERROR", f"{tag} giving up after {self.cfg.max_restarts} attempts")

    # ----- driver + clicks -----
    def get_chromedriver_path(self):
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

    def _build_driver(self) -> webdriver.Chrome:
        # Resolution order: explicit user path -> bundled driver -> PATH ->
        # Selenium Manager (auto-download). The user never needs to set up
        # chromedriver manually: the release binaries ship one under drivers/.
        candidates: list[str] = []
        manual = (self.cfg.chromedriver_path or "").strip()
        if manual:
            candidates.append(manual)
        bundled = self.get_chromedriver_path()
        if bundled and bundled not in candidates:
            candidates.append(bundled)

        opts = Options()
        opts.add_argument("--incognito")
        opts.add_argument("--start-maximized")
        opts.add_argument("--disable-notifications")
        opts.add_argument("--disable-infobars")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)

        last_err: Optional[Exception] = None
        for path in candidates:
            if not os.path.isfile(path):
                continue
            try:
                service = Service(executable_path=path)
                driver = webdriver.Chrome(service=service, options=opts)
                break
            except WebDriverException as e:
                last_err = e
                continue
        else:
            # No usable bundled/manual driver (or none present): fall back to
            # Selenium Manager, which resolves/downloads a matching driver.
            try:
                driver = webdriver.Chrome(options=opts)
            except WebDriverException as e:
                hint = f" (tried bundled/manual drivers too: {last_err})" if last_err else ""
                raise WebDriverException(
                    "Could not start Chrome: no bundled chromedriver found and "
                    f"Selenium Manager failed: {e}{hint}. "
                    "Make sure Google Chrome is installed."
                )
        driver.set_page_load_timeout(self.cfg.page_load_timeout)

        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": WS_MONITOR_JS},
        )
        return driver

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
            while True:
                if datetime.now(TEHRAN_TZ) >= task.end_time:
                    self._log("INFO", f"{tag} end time reached — closing session")
                    return "ENDED"

                if self.stop_event.wait(self.cfg.ws_check_interval):
                    return "STOPPED"

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
            self._unregister_driver(driver)
            try:
                driver.quit()
            except Exception:
                pass
            self._log("INFO", f"{tag} browser closed")

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
