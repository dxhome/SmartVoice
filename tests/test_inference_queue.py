from __future__ import annotations

import asyncio
import threading
import unittest

from smartvoice.domain.errors import InferenceOverloadedError, InferenceTimeoutError
from smartvoice.services.inference_queue import InferenceQueue


class InferenceQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_is_bounded_and_releases_capacity(self):
        queue = InferenceQueue(max_concurrent=1, max_queued=1)
        started = threading.Event()
        release = threading.Event()

        def blocked_operation():
            started.set()
            release.wait(timeout=3)
            return "first"

        first = asyncio.create_task(queue.run(blocked_operation, timeout_seconds=2))
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        second = asyncio.create_task(queue.run(lambda: "second", timeout_seconds=2))
        await asyncio.sleep(0.02)
        with self.assertRaises(InferenceOverloadedError):
            await queue.run(lambda: "overflow", timeout_seconds=1)

        release.set()
        self.assertEqual((await first)[0], "first")
        self.assertEqual((await second)[0], "second")
        self.assertEqual((await queue.run(lambda: "after", timeout_seconds=1))[0], "after")

    async def test_waiting_request_times_out(self):
        queue = InferenceQueue(max_concurrent=1, max_queued=1)
        started = threading.Event()
        release = threading.Event()

        def blocked_operation():
            started.set()
            release.wait(timeout=3)
            return "done"

        first = asyncio.create_task(queue.run(blocked_operation, timeout_seconds=2))
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        try:
            with self.assertRaises(InferenceOverloadedError):
                await queue.run(lambda: "never", timeout_seconds=0.01)
        finally:
            release.set()
        self.assertEqual((await first)[0], "done")

    async def test_execution_timeout_keeps_the_running_worker_inside_capacity(self):
        queue = InferenceQueue(max_concurrent=1, max_queued=0)
        started = threading.Event()
        release = threading.Event()

        def blocked_operation():
            started.set()
            release.wait(timeout=3)
            return "done"

        try:
            with self.assertRaises(InferenceTimeoutError):
                await queue.run(blocked_operation, timeout_seconds=1, execution_timeout_seconds=0.02)
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            with self.assertRaises(InferenceOverloadedError):
                await queue.run(lambda: "must not start", timeout_seconds=0.01)
        finally:
            release.set()
        await asyncio.sleep(0.05)
        self.assertEqual((await queue.run(lambda: "after", timeout_seconds=1))[0], "after")

    async def test_canceled_request_does_not_release_slot_before_worker_finishes(self):
        queue = InferenceQueue(max_concurrent=1, max_queued=0)
        started = threading.Event()
        release = threading.Event()

        def blocked_operation():
            started.set()
            release.wait(timeout=3)

        task = asyncio.create_task(queue.run(blocked_operation, timeout_seconds=1))
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        try:
            with self.assertRaises(InferenceOverloadedError):
                await queue.run(lambda: "must not start", timeout_seconds=0.01)
        finally:
            release.set()
        await asyncio.sleep(0.05)
        self.assertEqual((await queue.run(lambda: "after", timeout_seconds=1))[0], "after")


if __name__ == "__main__":
    unittest.main()
