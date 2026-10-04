"""Opt-in internal stage timing, independent of HTTP and inference engines.

Scopes report exclusive wall time: a nested stage is subtracted from its parent.
No audio, text, identifiers or request parameters are accepted by this port.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import threading
import time


@dataclass
class _Frame:
    children: float = 0.0


class StageTimings:
    def __init__(self):
        self._lock = threading.Lock()
        self._rows = {}

    def add(self, name: str, seconds: float, failed: bool = False):
        with self._lock:
            row = self._rows.setdefault(name, {"seconds": 0.0, "calls": 0, "failures": 0})
            row["seconds"] += max(0.0, seconds)
            row["calls"] += 1
            row["failures"] += int(failed)

    def snapshot(self):
        with self._lock:
            return {name: {**row, "seconds": round(row["seconds"], 6)}
                    for name, row in self._rows.items()}


stage_timings: ContextVar[StageTimings | None] = ContextVar("stage_timings", default=None)
_stack: ContextVar[tuple[_Frame, ...]] = ContextVar("stage_stack", default=())


@contextmanager
def stage(name: str):
    collector = stage_timings.get()
    if collector is None:
        yield
        return
    parent = _stack.get()
    frame = _Frame()
    token = _stack.set((*parent, frame))
    started = time.perf_counter()
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        elapsed = time.perf_counter() - started
        _stack.reset(token)
        if parent:
            parent[-1].children += elapsed
        collector.add(name, elapsed - frame.children, failed)
