"""Bound in-flight inference requests and reject excess work predictably."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from typing import TypeVar

import anyio

from smartvoice.domain.errors import InferenceOverloadedError, InferenceTimeoutError
from smartvoice.ports.diagnostics import stage
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
        # asyncio keeps only weak references to tasks. A request can be cancelled
        # while native inference is still running, so retain detached workers
        # until their completion callback releases the reservation.
        self._running_tasks: set[asyncio.Task[T]] = set()

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
                with stage("transport_queue"):
                    await asyncio.wait_for(self._semaphore.acquire(), timeout=timeout_seconds)
                acquired = True
            except TimeoutError as exc:
                raise InferenceOverloadedError("Timed out while waiting for an inference slot. Retry the request.") from exc
            queue_wait = time.perf_counter() - wait_started
            acquired = False  # The execution task owns permit and reservation cleanup.
            reserved = False

            cancelled = threading.Event()
            loop = asyncio.get_running_loop()
            ownership = {"started": False, "released": False}

            def release_owned_slot(*, unstarted=False):
                with self._lock:
                    if ownership["released"] or (unstarted and ownership["started"]):
                        return
                    ownership["released"] = True
                    self._reserved -= 1
                # A worker can outlive cancellation of its asyncio wrapper,
                # including loop shutdown. Capacity belongs to the native work.
                try:
                    loop.call_soon_threadsafe(self._semaphore.release)
                except RuntimeError:
                    pass  # The owning event loop has already closed.

            def run_owned():
                with self._lock:
                    if ownership["released"]:
                        raise InferenceTimeoutError("Inference was cancelled before execution.")
                    ownership["started"] = True
                try:
                    return operation()
                finally:
                    release_owned_slot()

            async def execute() -> T:
                context_token = request_cancelled.set(cancelled)
                deadline_token = request_deadline.set(time.monotonic() + execution_timeout_seconds)
                try:
                    if self._thread_limiter is None:
                        self._thread_limiter = anyio.CapacityLimiter(self._max_concurrent)
                    # Native inference calls cannot be interrupted once running.
                    # Keep this coroutine (and its queue reservation) alive until
                    # the worker thread returns, even if the HTTP request is
                    # cancelled after a client disconnects.
                    return await anyio.to_thread.run_sync(
                        run_owned,
                        limiter=self._thread_limiter,
                        abandon_on_cancel=False,
                    )
                finally:
                    request_cancelled.reset(context_token)
                    request_deadline.reset(deadline_token)
                    release_owned_slot(unstarted=True)

            task = asyncio.create_task(execute())
            self._running_tasks.add(task)
            task.add_done_callback(self._on_task_done)
            done, _ = await asyncio.wait({task}, timeout=execution_timeout_seconds)
            if not done:
                cancelled.set()
                raise InferenceTimeoutError("Inference exceeded the configured execution time limit.")
            return task.result(), queue_wait
        except asyncio.CancelledError:
            if "cancelled" in locals():
                cancelled.set()
            raise
        finally:
            if acquired or reserved:
                if acquired:
                    self._semaphore.release()
                if reserved:
                    with self._lock:
                        self._reserved -= 1

    def _on_task_done(self, task: asyncio.Task) -> None:
        self._running_tasks.discard(task)
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass
