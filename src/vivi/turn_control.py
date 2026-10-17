"""Cancellation remains observable by executor threads after coroutine cancellation."""

import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class TurnControl:
    deadline: float
    cancelled: threading.Event = field(default_factory=threading.Event)
    dispatched: bool = False
    stage: str = "queue"
    timings: dict = field(default_factory=dict)
    io_timings: dict = field(default_factory=dict)
    model_attempts: list[dict] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def cancel(self):
        with self.lock:
            self.cancelled.set()
            return self.dispatched

    def allow_dispatch(self):
        with self.lock:
            if self.cancelled.is_set() or time.monotonic() >= self.deadline:
                raise RuntimeError("Turn cancelled or deadline exceeded before dispatch")
            self.dispatched = True


current_control: ContextVar[TurnControl | None] = ContextVar("vivi_turn_control", default=None)


def record_io(name, started):
    """Cumulative await time, diagnostic only; never add it to wall-time total."""
    control = current_control.get()
    if control is not None:
        metric = control.io_timings.setdefault(name, {"calls": 0, "latency_ms": 0.0})
        metric["calls"] += 1
        metric["latency_ms"] = round(metric["latency_ms"] + (time.perf_counter() - started) * 1000, 2)
