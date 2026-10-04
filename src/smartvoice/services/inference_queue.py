"""Bound in-flight inference requests and reject excess work predictably."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from typing import TypeVar

import anyio

from smartvoice.domain.errors import InferenceOverloadedError, InferenceTimeoutError
from smartvoice.ports.inference_context import request_cancelled, request_deadline

T = TypeVar("T")


class InferenceQueue:
    def __init__(self, max_concurrent: int, max_queued: int):
        if max_concurrent < 1 or max_queued < 0:
            raise ValueError("Inference queue limits must be positive/non-negative")
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._capacity = max_concurrent + max_queued
        self._reserved = 0
        self._lock = threading.Lock()
        self._thread_limiter = None
        self._max_concurrent = max_concurrent

    async def run(
        self,
        operation: Callable[[], T],
        timeout_seconds: float,
        execution_timeout_seconds: float = 600.0,
    ) -> tuple[T, float]:
        with self._lock:
            if self._reserved >= self._capacity:
                raise InferenceOverloadedError("The inference queue is full. Retry after a current request finishes.")
            self._reserved += 1

        wait_started = time.perf_counter()
        acquired = False
        reserved = True
        try:
            try:
                await asyncio.wait_for(self._semaphore.acquire(), timeout=timeout_seconds)
                acquired = True
            except TimeoutError as exc:
                raise InferenceOverloadedError("Timed out while waiting for an inference slot. Retry the request.") from exc
            queue_wait = time.perf_counter() - wait_started
            acquired = False  # The execution task owns permit and reservation cleanup.
            reserved = False

            cancelled = threading.Event()

            async def execute() -> T:
                context_token = request_cancelled.set(cancelled)
                deadline_token = request_deadline.set(time.monotonic() + execution_timeout_seconds)
                try:
                    if self._thread_limiter is None:
                        self._thread_limiter = anyio.CapacityLimiter(self._max_concurrent)
                    return await anyio.to_thread.run_sync(operation, limiter=self._thread_limiter)
                finally:
                    request_cancelled.reset(context_token)
                    request_deadline.reset(deadline_token)
                    self._semaphore.release()
                    with self._lock:
                        self._reserved -= 1

            task = asyncio.create_task(execute())
            done, _ = await asyncio.wait({task}, timeout=execution_timeout_seconds)
            if not done:
                cancelled.set()
                task.add_done_callback(self._consume_task_result)
                raise InferenceTimeoutError("Inference exceeded the configured execution time limit.")
            return task.result(), queue_wait
        except asyncio.CancelledError:
            if "task" in locals() and not task.done():
                cancelled.set()
                task.add_done_callback(self._consume_task_result)
            raise
        finally:
            if acquired or reserved:
                if acquired:
                    self._semaphore.release()
                if reserved:
                    with self._lock:
                        self._reserved -= 1

    @staticmethod
    def _consume_task_result(task: asyncio.Task) -> None:
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass
