"""
SkyroomBot <-> Jarvis bridge.

Owns the single Listener instance that can run at a time, routes its
decisions through an Executor, and queues driver actions for the task
thread to execute.

Phase 2: decisions now reach the driver. type_number and send_chat
queue a work item; the SkyroomBot task thread drains the queue in its
stay-in-room loop and drives the chat box.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Callable, Optional

try:
    from jarvis.core.listener import Listener as _Listener
    from jarvis.core.context import StudentContext as _StudentContext
    from jarvis.core.actions import (
        ActionBackend as _ActionBackend,
        Executor as _Executor,
    )
    JARVIS_AVAILABLE = True
except ImportError:
    JARVIS_AVAILABLE = False
    _Listener = None  # type: ignore[assignment]
    _StudentContext = None  # type: ignore[assignment]
    _ActionBackend = object  # type: ignore[assignment,misc]
    _Executor = None  # type: ignore[assignment]


Listener: Any = _Listener
StudentContext: Any = _StudentContext
ActionBackend: Any = _ActionBackend
Executor: Any = _Executor

LogCallback = Callable[[str, str], None]


# ---------------------------------------------------------------------------
# Skyroom backend
# ---------------------------------------------------------------------------
class SkyroomBackend:
    """
    ActionBackend that queues work for the SkyroomBot task thread.

    The queue is drained by the task thread in the stay-in-room loop,
    so every driver call happens on the thread that created the driver.
    """

    def __init__(self, work_queue: "queue.Queue[tuple[str, dict]]") -> None:
        self.work_queue = work_queue

    def type_number(self, n: int) -> None:
        self.work_queue.put(("type_number", {"n": int(n)}))

    def send_chat(self, text: str) -> None:
        self.work_queue.put(("send_chat", {"text": str(text)}))


# ---------------------------------------------------------------------------
# JarvisRuntime
# ---------------------------------------------------------------------------
class JarvisRuntime:
    """
    Coordinates the one Listener that can run at a time and routes its
    decisions through an Executor.

    Loopback capture is a single global input, so two classes cannot
    listen simultaneously. The first task to acquire a lease gets it;
    any other task is told no and continues without Jarvis.
    """

    def __init__(self, log_cb: LogCallback) -> None:
        self.log_cb = log_cb
        self._lock = threading.Lock()
        self._listener = None
        self._executor = None
        self._active_tag: Optional[str] = None

    # ------------------------------------------------------------------
    @property
    def is_available(self) -> bool:
        return JARVIS_AVAILABLE

    @property
    def active(self) -> bool:
        with self._lock:
            return self._listener is not None

    @property
    def active_tag(self) -> Optional[str]:
        with self._lock:
            return self._active_tag

    # ------------------------------------------------------------------
    def acquire(
        self,
        tag: str,
        user_dict: dict,
        work_queue: "queue.Queue[tuple[str, dict]]",
    ) -> bool:
        """Start a Listener for this task. True if granted."""
        if not JARVIS_AVAILABLE:
            self.log_cb(
                "WARNING",
                f"{tag} Jarvis is not installed — live mode unavailable",
            )
            return False

        with self._lock:
            if self._listener is not None:
                self.log_cb(
                    "WARNING",
                    f"{tag} Jarvis is already listening for "
                    f"{self._active_tag} — live mode skipped for this class",
                )
                return False

            try:
                ctx = StudentContext.from_user(user_dict)
            except Exception as e:
                self.log_cb("ERROR", f"{tag} bad student context: {e}")
                return False

            try:
                backend = SkyroomBackend(work_queue)
                executor = Executor(backend=backend)
                listener = Listener(
                    context=ctx,
                    on_decision=lambda d: self._on_decision(tag, d),
                    on_error=lambda e: self.log_cb(
                        "ERROR", f"{tag} jarvis: {e}"
                    ),
                    
                    alarm=True,
                )
                listener.start(wait=0.0)
            except Exception as e:
                self.log_cb("ERROR", f"{tag} could not start Jarvis: {e}")
                return False

            self._listener = listener
            self._executor = executor
            self._active_tag = tag
            self.log_cb(
                "INFO",
                f"{tag} Jarvis listening as {ctx.user_name}",
            )
            return True

    # ------------------------------------------------------------------
    def release(self, tag: str) -> None:
        with self._lock:
            if self._listener is None or self._active_tag != tag:
                return
            try:
                self._listener.stop(timeout=3.0)
            except Exception:
                pass
            self._listener = None
            self._executor = None
            self._active_tag = None
            self.log_cb("INFO", f"{tag} Jarvis listener stopped")

    # ------------------------------------------------------------------
    # Confirm queue
    # ------------------------------------------------------------------
    def approve(self, action_id: int) -> str:
        with self._lock:
            if self._executor is None:
                return "unavailable"
            outcome = self._executor.approve(action_id)
            return getattr(outcome, "value", str(outcome))

    def dismiss(self, action_id: int) -> str:
        with self._lock:
            if self._executor is None:
                return "unavailable"
            self._executor.dismiss(action_id)
            return "dismissed"

    def pending(self) -> list:
        with self._lock:
            if self._executor is None:
                return []
            return self._executor.pending()

    # ------------------------------------------------------------------
    def _on_decision(self, tag: str, decision) -> None:
        """Called on the Listener's thread for every decision."""
        try:
            action = getattr(decision, "action", "?")
            args = getattr(decision, "args", {})

            if action == "notify_me":
                self.log_cb(
                    "INFO",
                    f"{tag} [jarvis] notify_me  {args}",
                )
                return

            with self._lock:
                executor = self._executor
            if executor is None:
                return

            outcome = executor.submit(decision)
            kind = getattr(outcome, "value", str(outcome))

            if kind == "pending":
                with self._lock:
                    pending = executor.pending()
                pid = pending[-1].id if pending else -1
                self.log_cb(
                    "INFO",
                    f"{tag} [jarvis] {action} {args}  [pending id={pid}]",
                )
            elif kind == "executed":
                self.log_cb(
                    "INFO",
                    f"{tag} [jarvis] {action} {args}  [executed]",
                )
            elif kind == "skipped":
                self.log_cb(
                    "INFO",
                    f"{tag} [jarvis] {action} {args}  [skipped]",
                )
            else:
                self.log_cb(
                    "WARNING",
                    f"{tag} [jarvis] {action} {args}  [{kind}]",
                )
        except Exception as e:
            self.log_cb("ERROR", f"{tag} jarvis decision failed: {e}")