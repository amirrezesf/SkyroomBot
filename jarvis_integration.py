"""
SkyroomBot <-> Jarvis bridge.

Owns the single Listener instance that can be active at a time and
routes its decisions back to the bot for logging.

Phase 1: no execution, no GUI confirmation. The runtime starts one
Listener when a task with live=True enters a room, logs every decision,
and stops the Listener when the room closes.

Jarvis is optional. If it isn't installed, JARVIS_AVAILABLE is False
and every acquire() call returns False.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

try:
    from jarvis.core.listener import Listener
    from jarvis.core.context import StudentContext
    JARVIS_AVAILABLE = True
except ImportError:
    JARVIS_AVAILABLE = False
    Listener = None  # type: ignore
    StudentContext = None  # type: ignore


LogCallback = Callable[[str, str], None]

import threading
from typing import Any, Callable, Optional

try:
    from jarvis.core.listener import Listener as _Listener
    from jarvis.core.context import StudentContext as _StudentContext
    JARVIS_AVAILABLE = True
except ImportError:
    JARVIS_AVAILABLE = False
    _Listener = None  # type: ignore[assignment]
    _StudentContext = None  # type: ignore[assignment]


# The type checker can't see through the try/except, so expose them as Any.
# At runtime these are the real classes whenever JARVIS_AVAILABLE is True,
# and the code below always checks JARVIS_AVAILABLE before calling them.
Listener: Any = _Listener
StudentContext: Any = _StudentContext

class JarvisRuntime:
    """
    Coordinates the one Listener that can run at a time.

    Loopback capture is a single global input, so two classes cannot
    listen simultaneously. The first task to acquire a lease gets it;
    any other task is told no and continues without Jarvis.
    """

    def __init__(self, log_cb: LogCallback) -> None:
        self.log_cb = log_cb
        self._lock = threading.Lock()
        self._listener = None
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
    def acquire(self, tag: str, user_dict: dict) -> bool:
        """Try to start a Listener for this task. True if granted."""
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
                listener = Listener(
                    context=ctx,
                    on_decision=lambda d: self._on_decision(tag, d),
                    on_error=lambda e: self.log_cb(
                        "ERROR", f"{tag} jarvis: {e}"
                    ),
                    alarm=True,
                    on_transcript=lambda t, s: self.log_cb(
                        "INFO", f"{tag} [jarvis stt] {t}"
                    ),
                    
                )
                listener.start(wait=0.0)
            except Exception as e:
                self.log_cb("ERROR", f"{tag} could not start Jarvis: {e}")
                return False

            self._listener = listener
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
            self._active_tag = None
            self.log_cb("INFO", f"{tag} Jarvis listener stopped")

    # ------------------------------------------------------------------
    def _on_decision(self, tag: str, decision) -> None:
        """Called on the Listener's thread for every decision."""
        try:
            action = getattr(decision, "action", "?")
            args = getattr(decision, "args", {})
            scope = getattr(decision, "instruction_scope", "")
            conf = getattr(decision, "instruction_confidence", 0.0)
            self.log_cb(
                "INFO",
                f"{tag} [jarvis] {action}  args={args}  "
                f"scope={scope}  conf={conf:.2f}",
            )
        except Exception as e:
            self.log_cb("ERROR", f"{tag} jarvis decision log failed: {e}")