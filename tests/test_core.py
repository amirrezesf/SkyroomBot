"""Unit tests for SkyroomBot core logic.

Pure logic only: no browser, no GUI, no network.
Run from the repo root:

    python3 -m unittest discover -s tests -v
"""
import queue
import sys
import unittest
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from selenium.common.exceptions import WebDriverException

from jarvis_integration import (
    JARVIS_AVAILABLE,
    JarvisRuntime,
    SkyroomBackend,
    _format_pending,
)
from skyroom_core import (
    PERSIAN_DAYS,
    TEHRAN_TZ,
    RuntimeConfig,
    SkyroomBot,
    Task,
    next_occurrence,
    next_occurrence_range,
    parse_time_string,
)


def _persian_name_for_weekday(wd: int) -> str:
    """First Persian day name that maps to the given Python weekday."""
    for name, value in PERSIAN_DAYS.items():
        if value == wd:
            return name
    raise AssertionError(f"no Persian name for weekday {wd}")


def _make_task(**overrides) -> Task:
    now = datetime.now(TEHRAN_TZ)
    kwargs = {
        "user_name": "test-user",
        "class_name": "test-class",
        "url": "https://example.com/room",
        "scheduled_time": now + timedelta(hours=1),
        "end_time": now + timedelta(hours=3),
    }
    kwargs.update(overrides)
    return Task(**kwargs)


# ============================================================
# Schedule helpers
# ============================================================
class ParseTimeStringTest(unittest.TestCase):
    def test_am(self):
        self.assertEqual(parse_time_string("10:00am"), dtime(10, 0))

    def test_pm_uppercase_with_space(self):
        self.assertEqual(parse_time_string("10:00 PM"), dtime(22, 0))

    def test_24h(self):
        self.assertEqual(parse_time_string("22:30"), dtime(22, 30))

    def test_dotted_meridiem(self):
        self.assertEqual(parse_time_string("9:05 a.m."), dtime(9, 5))

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            parse_time_string("banana")


class PersianDaysTest(unittest.TestCase):
    def test_all_values_are_valid_weekdays(self):
        for name, wd in PERSIAN_DAYS.items():
            self.assertIn(wd, range(7), f"{name} -> {wd}")

    def test_spot_checks(self):
        self.assertEqual(PERSIAN_DAYS["شنبه"], 5)
        self.assertEqual(PERSIAN_DAYS["دوشنبه"], 0)
        self.assertEqual(PERSIAN_DAYS["جمعه"], 4)


class NextOccurrenceTest(unittest.TestCase):
    def test_result_is_future_and_matches_weekday(self):
        before = datetime.now(TEHRAN_TZ)
        dt = next_occurrence("جمعه", "10:00")
        self.assertIsNotNone(dt.tzinfo)
        self.assertGreater(dt, before)
        self.assertEqual(dt.weekday(), PERSIAN_DAYS["جمعه"])
        self.assertEqual((dt.hour, dt.minute), (10, 0))

    def test_unknown_day_raises(self):
        with self.assertRaises(ValueError):
            next_occurrence("nonsense", "10:00")


class NextOccurrenceRangeTest(unittest.TestCase):
    def test_missing_end_time_defaults_to_two_hours(self):
        tomorrow = datetime.now(TEHRAN_TZ) + timedelta(days=1)
        start, end = next_occurrence_range(
            _persian_name_for_weekday(tomorrow.weekday()), "10:00", None
        )
        self.assertEqual(end - start, timedelta(hours=2))

    def test_mid_class_returns_todays_range(self):
        now = datetime.now(TEHRAN_TZ)
        day = _persian_name_for_weekday(now.weekday())
        lo = now - timedelta(hours=1)
        hi = now + timedelta(hours=1)
        if lo.date() == now.date() == hi.date():
            start_s = lo.strftime("%I:%M%p").lower()
            end_s = hi.strftime("%I:%M%p").lower()
        else:
            # Near midnight a +/-1h window crosses the date boundary and
            # no longer means "mid-class today" — use a window that
            # definitely contains any time-of-day instead.
            start_s, end_s = "00:01", "23:59"
        start, end = next_occurrence_range(day, start_s, end_s)
        check = datetime.now(TEHRAN_TZ)
        self.assertLessEqual(start, check)
        self.assertGreater(end, check)

    def test_unknown_day_raises(self):
        with self.assertRaises(ValueError):
            next_occurrence_range("nonsense", "10:00", "12:00")


# ============================================================
# RuntimeConfig / Task timing
# ============================================================
class RuntimeConfigEffectiveTest(unittest.TestCase):
    def test_debug_subflags_cleared_when_debug_off(self):
        cfg = RuntimeConfig(
            debug_mode=False,
            debug_run_now=True,
            debug_disable_random_delay=True,
            debug_disable_ws_check=True,
        ).effective()
        self.assertFalse(cfg.debug_run_now)
        self.assertFalse(cfg.debug_disable_random_delay)
        self.assertFalse(cfg.debug_disable_ws_check)

    def test_debug_subflags_kept_when_debug_on(self):
        cfg = RuntimeConfig(debug_mode=True, debug_run_now=True).effective()
        self.assertTrue(cfg.debug_run_now)


class TaskComputeActualTest(unittest.TestCase):
    def test_debug_run_now_joins_immediately(self):
        now = datetime.now(TEHRAN_TZ)
        task = _make_task()
        task._compute_actual_at(
            now, RuntimeConfig(debug_mode=True, debug_run_now=True)
        )
        self.assertEqual(task.actual_time, now)

    def test_debug_fixed_delay_uses_class_start(self):
        now = datetime.now(TEHRAN_TZ)
        task = _make_task(scheduled_time=now - timedelta(minutes=5))
        task._compute_actual_at(
            now, RuntimeConfig(debug_mode=True, debug_disable_random_delay=True)
        )
        self.assertEqual(task.actual_time, now)

    def test_past_window_joins_now(self):
        now = datetime.now(TEHRAN_TZ)
        task = _make_task(
            scheduled_time=now - timedelta(hours=5),
            end_time=now - timedelta(hours=4),
        )
        task._compute_actual_at(now, RuntimeConfig())
        self.assertEqual(task.actual_time, now)

    def test_random_join_stays_inside_window(self):
        now = datetime.now(TEHRAN_TZ)
        scheduled = now + timedelta(minutes=60)
        task = _make_task(
            scheduled_time=scheduled,
            end_time=now + timedelta(hours=5),
        )
        cfg = RuntimeConfig(min_delay_min=10, max_delay_min=15)
        task._compute_actual_at(now, cfg)
        self.assertGreaterEqual(task.actual_time, scheduled + timedelta(minutes=10))
        self.assertLessEqual(task.actual_time, scheduled + timedelta(minutes=15))


# ============================================================
# Jarvis bridge (no Jarvis package installed -> degraded mode)
# ============================================================
class FormatPendingTest(unittest.TestCase):
    def test_type_number(self):
        d = SimpleNamespace(action="type_number", args={"n": 7})
        self.assertIn("7", _format_pending(d))

    def test_send_chat(self):
        d = SimpleNamespace(action="send_chat", args={"text": "hello"})
        self.assertIn("hello", _format_pending(d))

    def test_unknown_action_falls_back(self):
        d = SimpleNamespace(action="custom", args={"x": 1})
        self.assertIn("custom", _format_pending(d))


class SkyroomBackendTest(unittest.TestCase):
    def test_type_number_queues_action(self):
        q: queue.Queue = queue.Queue()
        SkyroomBackend(q).type_number(42)
        self.assertEqual(q.get_nowait(), ("type_number", {"n": 42}))

    def test_send_chat_queues_action(self):
        q: queue.Queue = queue.Queue()
        SkyroomBackend(q).send_chat("hi")
        self.assertEqual(q.get_nowait(), ("send_chat", {"text": "hi"}))


@unittest.skipIf(JARVIS_AVAILABLE, "requires Jarvis NOT installed")
class JarvisDegradedTest(unittest.TestCase):
    def test_acquire_returns_false_without_jarvis(self):
        runtime = JarvisRuntime(lambda level, msg: None)
        self.assertFalse(runtime.is_available)
        ok = runtime.acquire("tag", {}, queue.Queue())
        self.assertFalse(ok)

    def test_confirm_queue_unavailable_without_jarvis(self):
        runtime = JarvisRuntime(lambda level, msg: None)
        self.assertEqual(runtime.approve(1), "unavailable")
        self.assertEqual(runtime.dismiss(1), "unavailable")
        self.assertEqual(runtime.pending(), [])


# ============================================================
# WebSocket watchdog predicate
# ============================================================
class FakeDriver:
    def __init__(self, state=None, exc=None):
        self._state = state
        self._exc = exc

    def execute_script(self, _js):
        if self._exc is not None:
            raise self._exc
        return self._state


class WsIsDeadTest(unittest.TestCase):
    def setUp(self):
        self.bot = SkyroomBot(
            RuntimeConfig(), [], lambda level, msg: None
        )

    def test_no_socket_yet_is_not_dead(self):
        driver = FakeDriver({"active": 0, "opened": 0, "lastClose": 0, "now": 1})
        self.assertFalse(self.bot._ws_is_dead(driver))

    def test_active_socket_is_not_dead(self):
        driver = FakeDriver({"active": 2, "opened": 2, "lastClose": 0, "now": 1})
        self.assertFalse(self.bot._ws_is_dead(driver))

    def test_recently_closed_is_not_dead(self):
        now_ms = int(datetime.now().timestamp() * 1000)
        driver = FakeDriver(
            {"active": 0, "opened": 3, "lastClose": now_ms - 5000, "now": now_ms}
        )
        self.assertFalse(self.bot._ws_is_dead(driver))

    def test_long_dead_is_dead(self):
        now_ms = int(datetime.now().timestamp() * 1000)
        driver = FakeDriver(
            {"active": 0, "opened": 3, "lastClose": now_ms - 60000, "now": now_ms}
        )
        self.assertTrue(self.bot._ws_is_dead(driver))

    def test_missing_state_is_dead(self):
        self.assertTrue(self.bot._ws_is_dead(FakeDriver(None)))

    def test_driver_error_is_dead(self):
        self.assertTrue(
            self.bot._ws_is_dead(FakeDriver(exc=WebDriverException("gone")))
        )


if __name__ == "__main__":
    unittest.main()
