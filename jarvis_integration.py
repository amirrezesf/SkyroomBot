"""
SkyroomBot <-> Jarvis bridge.

Owns the single Listener instance that can run at a time, routes its
decisions through an Executor, and queues driver actions for the task
thread to execute.

Optional GUI callbacks:
    on_status(str)
    on_transcript(text, seconds)
    on_decision(decision_obj)
    on_pending_added(id, description)
    on_pending_removed(id)
Set them after construction; they fire on the listener thread.
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


def _format_pending(decision) -> str:
    action = getattr(decision, "action", "?")
    args = getattr(decision, "args", {}) or {}
    if action == "type_number":
        return f"type_number  n = {args.get('n')}"
    if action == "send_chat":
        return f"send_chat  \"{args.get('text')}\""
    return f"{action}  {args}"


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


class JarvisRuntime:
    def __init__(self, log_cb: LogCallback) -> None:
        self.log_cb = log_cb
        self._lock = threading.RLock()
        self._listener = None
        self._executor = None
        self._active_tag: Optional[str] = None

        # Optional GUI callbacks — set by the window after construction.
        self.on_status: Optional[Callable[[str], None]] = None
        self.on_transcript: Optional[Callable[[str, float], None]] = None
        self.on_decision: Optional[Callable[[Any], None]] = None
        self.on_pending_added: Optional[Callable[[int, str], None]] = None
        self.on_pending_removed: Optional[Callable[[int], None]] = None

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

    def _safe_call(self, cb, *args) -> None:
        if cb is None:
            return
        try:
            cb(*args)
        except Exception as e:
            self.log_cb("ERROR", f"jarvis callback failed: {e}")

    def acquire(
        self,
        tag: str,
        user_dict: dict,
        work_queue: "queue.Queue[tuple[str, dict]]",
    ) -> bool:
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
                    on_transcript=lambda t, s: self._on_transcript(t, s),
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

        self.log_cb("INFO", f"{tag} Jarvis listening as {ctx.user_name}")
        self._safe_call(self.on_status, f"Listening — {ctx.user_name}")
        return True

    def release(self, tag: str) -> None:
        with self._lock:
            if self._listener is None or self._active_tag != tag:
                return
            listener = self._listener
            self._listener = None
            self._executor = None
            self._active_tag = None

        try:
            listener.stop(timeout=3.0)
        except Exception:
            pass
        self.log_cb("INFO", f"{tag} Jarvis listener stopped")
        self._safe_call(self.on_status, "Idle")

    def approve(self, action_id: int) -> str:
        with self._lock:
            if self._executor is None:
                return "unavailable"
            outcome = self._executor.approve(action_id)
            kind = getattr(outcome, "value", str(outcome))
        self._safe_call(self.on_pending_removed, action_id)
        return kind

    def dismiss(self, action_id: int) -> str:
        with self._lock:
            if self._executor is None:
                return "unavailable"
            self._executor.dismiss(action_id)
        self._safe_call(self.on_pending_removed, action_id)
        return "dismissed"

    def pending(self) -> list:
        with self._lock:
            if self._executor is None:
                return []
            return self._executor.pending()

    def _on_transcript(self, text: str, seconds: float) -> None:
        self._safe_call(self.on_transcript, text, seconds)

    def _on_decision(self, tag: str, decision) -> None:
        action = getattr(decision, "action", "?")
        args = getattr(decision, "args", {}) or {}

        # notify_me: observation only, no execution.
        if action == "notify_me":
            self.log_cb("INFO", f"{tag} [jarvis] notify_me {args}")
            self._safe_call(self.on_decision, decision)
            return

        # Executable actions — submit inside the lock so approve()
        # on the main thread can't race us modifying the pending list.
        with self._lock:
            executor = self._executor
            if executor is None:
                return
            outcome = executor.submit(decision)
            kind = getattr(outcome, "value", str(outcome))
            pending_id = None
            if kind == "pending":
                pl = executor.pending()
                if pl:
                    pending_id = pl[-1].id

        if kind == "pending":
            self.log_cb(
                "INFO",
                f"{tag} [jarvis] {action} {args}  [pending id={pending_id}]",
            )
            if pending_id is not None:
                self._safe_call(
                    self.on_pending_added, pending_id, _format_pending(decision)
                )
        elif kind == "executed":
            self.log_cb("INFO", f"{tag} [jarvis] {action} {args}  [executed]")
        elif kind == "skipped":
            self.log_cb("INFO", f"{tag} [jarvis] {action} {args}  [skipped]")
        else:
            self.log_cb(
                "WARNING", f"{tag} [jarvis] {action} {args}  [{kind}]"
            )

        self._safe_call(self.on_decision, decision)