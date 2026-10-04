"""A bounded, lazy pool of independently owned runtimes (no native changes)."""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Hashable, TypeVar

from smartvoice.domain.errors import InferenceOverloadedError
from smartvoice.ports.inference_context import request_cancelled, request_continuation, request_deadline, check_execution

T = TypeVar("T")


@dataclass(eq=False)
class _Instance:
    runtime: object | None = None
    busy: bool = True
    ready: bool = False
    retiring: bool = False
    last_used: float = field(default_factory=time.monotonic)


@dataclass
class _Group:
    instances: list[_Instance] = field(default_factory=list)
    waiting: deque = field(default_factory=deque)
    warming: bool = False
    completed: int = 0
    peak_active: int = 0
    warmups: deque = field(default_factory=lambda: deque(maxlen=32))


class ElasticRuntimePool:
    """FIFO admission per key; initialization, execution and disposal outside locks.

    The first operation both warms the runtime and serves its request. Expansion
    waits for a usable seed, and at most one runtime per key warms at a time.
    A lease lasts until native execution ends, even if its HTTP caller times out.
    """

    def __init__(self, factory: Callable[[object | None], object], *, min_instances=1,
                 max_instances=2, max_waiting=4, idle_seconds=300.0, wait_seconds=60.0):
        if not 1 <= min_instances <= max_instances or max_waiting < 0:
            raise ValueError("Invalid runtime pool capacity")
        if not all(math.isfinite(v) and v > 0 for v in (idle_seconds, wait_seconds)):
            raise ValueError("Pool time limits must be finite and positive")
        self.factory = factory
        self.minimum, self.maximum = min_instances, max_instances
        self.max_waiting, self.idle_seconds, self.wait_seconds = max_waiting, idle_seconds, wait_seconds
        self._condition = threading.Condition()
        self._groups: dict[Hashable, _Group] = {}
        self._closed = False
        self._stop = threading.Event()
        self._reaper = threading.Thread(target=self._reap, name="smartvoice-runtime-reaper", daemon=True)
        self._reaper.start()

    def run(self, key: Hashable, operation: Callable[[object], T]) -> tuple[T, float]:
        started = time.monotonic()
        check_execution()
        deadline = min(started + self.wait_seconds, request_deadline.get() or float("inf"))
        token = object()
        cancelled = request_cancelled.get()
        with self._condition:
            if self._closed:
                raise InferenceOverloadedError("Inference runtime is shutting down.")
            group = self._groups.setdefault(key, _Group())
            # An immediately available lease does not count as queued work.
            idle = any(i.ready and not i.busy for i in group.instances)
            grow = len(group.instances) < self.maximum and not group.warming
            # Reserved continuation capacity is bounded by active instances. It
            # lets admitted operations rejoin behind waiting short requests.
            waiting_limit = self.max_waiting + (self.maximum if request_continuation.get() else 0)
            if (group.waiting or not (idle or grow)) and len(group.waiting) >= waiting_limit:
                raise InferenceOverloadedError("The model inference queue is full.")
            group.waiting.append(token)
            try:
                while True:
                    if self._closed or (cancelled is not None and cancelled.is_set()):
                        raise InferenceOverloadedError("Queued inference was cancelled.")
                    if time.monotonic() >= deadline:
                        execution_deadline = request_deadline.get()
                        if execution_deadline is not None and time.monotonic() >= execution_deadline:
                            check_execution()
                        raise InferenceOverloadedError("Timed out waiting for a model instance.")
                    if group.waiting[0] is token:
                        instance = next((i for i in group.instances if i.ready and not i.busy), None)
                        if instance is not None:
                            instance.busy = True
                            new = False
                            seed = None
                            break
                        if len(group.instances) < self.maximum and not group.warming:
                            seed = next((i.runtime for i in group.instances if i.ready), None)
                            instance = _Instance()
                            group.instances.append(instance)
                            group.warming = True
                            new = True
                            break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise InferenceOverloadedError("Timed out waiting for a model instance.")
                    self._condition.wait(min(remaining, 0.1))
            finally:
                group.waiting.remove(token)
                self._condition.notify_all()
            group.peak_active = max(group.peak_active, sum(i.busy for i in group.instances))
            ordinal = len(group.instances)
        wait = time.monotonic() - started
        warm_started = time.monotonic()
        succeeded = False
        try:
            if new:
                instance.runtime = self.factory(seed)
            check_execution()
            result = operation(instance.runtime)
            succeeded = True
            return result, wait
        finally:
            dispose = None
            with self._condition:
                if new:
                    group.warming = False
                    group.warmups.append({"instance_count": ordinal, "seconds": time.monotonic() - warm_started,
                                          "success": succeeded})
                    if not succeeded:
                        group.instances.remove(instance)
                        dispose = instance.runtime
                instance.ready = succeeded or instance.ready
                instance.busy = False
                instance.last_used = time.monotonic()
                group.completed += int(succeeded)
                self._condition.notify_all()
            if dispose is not None:
                dispose.close()

    def snapshot(self) -> dict[str, object]:
        with self._condition:
            return {str(key): {"instances": len(g.instances), "ready": sum(i.ready for i in g.instances),
                              "active": sum(i.busy for i in g.instances), "waiting": len(g.waiting),
                              "warming": g.warming, "retiring": sum(i.retiring for i in g.instances), "completed": g.completed, "peak_active": g.peak_active,
                              "warmups": list(g.warmups)} for key, g in self._groups.items()}

    def contains(self, key: Hashable) -> bool:
        with self._condition:
            group = self._groups.get(key)
            return bool(group and group.instances)

    def _reap(self) -> None:
        while not self._stop.wait(min(1.0, self.idle_seconds / 2)):
            dispose = []
            with self._condition:
                now = time.monotonic()
                for group in self._groups.values():
                    if group.waiting or group.warming:
                        continue
                    # Keep the original warm floor, including language/shape
                    # caches accumulated there, rather than retire it first.
                    for instance in reversed(group.instances[self.minimum:]):
                        if instance.ready and not instance.busy and now - instance.last_used >= self.idle_seconds:
                            instance.ready = False
                            instance.retiring = True
                            dispose.append((group, instance))
            for group, instance in dispose:
                try:
                    instance.runtime.close()
                finally:
                    with self._condition:
                        group.instances.remove(instance)
                        self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        self._stop.set()
        self._reaper.join()
        with self._condition:
            self._condition.wait_for(lambda: not any(i.busy for g in self._groups.values() for i in g.instances))
            runtimes = [i.runtime for g in self._groups.values() for i in g.instances if i.runtime is not None]
            self._groups.clear()
        for runtime in runtimes:
            runtime.close()
