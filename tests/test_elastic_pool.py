"""Concurrency, bounded admission and lifecycle invariants for native runtimes."""
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from smartvoice.adapters.inference.runtime.elastic_pool import ElasticRuntimePool
from smartvoice.domain.errors import InferenceOverloadedError
from smartvoice.ports.inference_context import request_cancelled


class Runtime:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class ElasticPoolTests(unittest.TestCase):
    def make_pool(self, **kwargs):
        pool = ElasticRuntimePool(lambda seed: Runtime(), **kwargs)
        self.addCleanup(pool.close)
        return pool

    def test_expansion_is_real_parallel_and_bounded(self):
        pool = self.make_pool(max_waiting=0)
        pool.run("a", lambda runtime: 1)
        started, release = threading.Barrier(3), threading.Event()
        def operation(runtime):
            started.wait(timeout=2)
            release.wait(timeout=2)
            return id(runtime)
        with ThreadPoolExecutor(2) as executor:
            futures = [executor.submit(pool.run, "a", operation) for _ in range(2)]
            started.wait(timeout=2)
            try:
                self.assertEqual(pool.snapshot()["a"]["active"], 2)
                with self.assertRaises(InferenceOverloadedError):
                    pool.run("a", operation)
            finally:
                release.set()
            self.assertNotEqual(futures[0].result()[0], futures[1].result()[0])

    def test_new_model_is_not_blocked_by_another_models_load(self):
        pool = self.make_pool()
        entered, release = threading.Event(), threading.Event()
        def slow(runtime):
            entered.set()
            release.wait(2)
        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(pool.run, "a", slow)
            entered.wait(2)
            try:
                self.assertEqual(pool.run("b", lambda runtime: 42)[0], 42)
            finally:
                release.set()
            future.result()

    def test_reclaims_only_extra_idle_instance(self):
        pool = self.make_pool(idle_seconds=0.05)
        resident = pool.run("a", lambda runtime: runtime)[0]
        barrier = threading.Barrier(2)
        with ThreadPoolExecutor(2) as executor:
            futures = [executor.submit(pool.run, "a", lambda runtime: (barrier.wait(2), runtime)[1]) for _ in range(2)]
            runtimes = [f.result()[0] for f in futures]
        deadline = time.monotonic() + 2
        while pool.snapshot()["a"]["instances"] != 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(sum(r.closed for r in runtimes), 1)
        self.assertFalse(resident.closed)
        self.assertEqual(pool.snapshot()["a"]["ready"], 1)

    def test_cancelled_waiter_does_not_execute_or_release_running_lease(self):
        pool = self.make_pool(max_instances=1)
        entered, release, cancel = threading.Event(), threading.Event(), threading.Event()
        executed = []
        def running(runtime):
            entered.set()
            release.wait(2)
        def queued():
            token = request_cancelled.set(cancel)
            try:
                return pool.run("a", lambda runtime: executed.append(1))
            finally:
                request_cancelled.reset(token)
        with ThreadPoolExecutor(2) as executor:
            active = executor.submit(pool.run, "a", running)
            entered.wait(2)
            waiting = executor.submit(queued)
            cancel.set()
            try:
                with self.assertRaises(InferenceOverloadedError):
                    waiting.result(timeout=2)
                self.assertEqual(pool.snapshot()["a"]["active"], 1)
                self.assertEqual(executed, [])
            finally:
                release.set()
            active.result()

    def test_failed_initialization_is_retryable(self):
        pool = self.make_pool()
        def fail(runtime):
            raise ValueError("failed")
        with self.assertRaises(ValueError):
            pool.run("a", fail)
        self.assertFalse(pool.contains("a"))
        self.assertEqual(pool.run("a", lambda runtime: 3)[0], 3)

    def test_close_waits_for_active_work_and_rejects_new_work(self):
        pool = self.make_pool()
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        def operation(runtime):
            entered.set()
            release.wait(2)
            self.assertFalse(runtime.closed)
            return runtime
        with ThreadPoolExecutor(2) as executor:
            future = executor.submit(pool.run, "a", operation)
            entered.wait(2)
            close_future = executor.submit(lambda: (pool.close(), closed.set()))
            try:
                self.assertFalse(closed.wait(0.05))
                with self.assertRaises(InferenceOverloadedError):
                    pool.run("b", operation)
            finally:
                release.set()
            runtime = future.result()[0]
            close_future.result()
            self.assertTrue(runtime.closed)

    def test_only_one_initialization_runs_per_model(self):
        warming, release = threading.Event(), threading.Event()
        created = []
        def factory(seed):
            created.append(seed)
            warming.set()
            release.wait(2)
            return Runtime()
        pool = ElasticRuntimePool(factory, max_waiting=4)
        self.addCleanup(pool.close)
        with ThreadPoolExecutor(2) as executor:
            first = executor.submit(pool.run, "a", lambda runtime: 1)
            warming.wait(2)
            second = executor.submit(pool.run, "a", lambda runtime: 2)
            try:
                time.sleep(0.03)
                self.assertEqual(len(created), 1)
                self.assertEqual(pool.snapshot()["a"]["waiting"], 1)
            finally:
                release.set()
            first.result()
            second.result()

    def test_wait_timeout_does_not_start_extra_work(self):
        pool = self.make_pool(max_instances=1, wait_seconds=0.03)
        entered, release = threading.Event(), threading.Event()
        def operation(runtime):
            entered.set()
            release.wait(2)
        with ThreadPoolExecutor(1) as executor:
            running = executor.submit(pool.run, "a", operation)
            entered.wait(2)
            try:
                with self.assertRaises(InferenceOverloadedError):
                    pool.run("a", lambda runtime: self.fail("expired request executed"))
                self.assertEqual(pool.snapshot()["a"]["waiting"], 0)
            finally:
                release.set()
            running.result()

    def test_retiring_instance_counts_against_capacity_until_close_finishes(self):
        closing, close_gate = threading.Event(), threading.Event()
        active, active_gate = threading.Event(), threading.Event()
        created = []
        class SlowClose(Runtime):
            def close(self):
                closing.set()
                close_gate.wait(2)
                super().close()
        def factory(seed):
            runtime = SlowClose()
            created.append(runtime)
            return runtime
        pool = ElasticRuntimePool(factory, idle_seconds=0.05, max_waiting=0)
        self.addCleanup(pool.close)
        pool.run("a", lambda runtime: 1)
        barrier = threading.Barrier(2)
        with ThreadPoolExecutor(2) as executor:
            futures = [executor.submit(pool.run, "a", lambda runtime: barrier.wait(2)) for _ in range(2)]
            [future.result() for future in futures]
            self.assertTrue(closing.wait(2))
            def operation(runtime):
                active.set()
                active_gate.wait(2)
            running = executor.submit(pool.run, "a", operation)
            active.wait(2)
            try:
                self.assertEqual(pool.snapshot()["a"]["retiring"], 1)
                self.assertEqual(pool.snapshot()["a"]["instances"], 2)
                with self.assertRaises(InferenceOverloadedError):
                    pool.run("a", lambda runtime: 2)
                self.assertEqual(len(created), 2)
            finally:
                close_gate.set()
                active_gate.set()
            running.result()


if __name__ == "__main__":
    unittest.main()
