"""Real spawned-process tests for shared models and private stream handles."""
import asyncio
import os
import time
import unittest
from dataclasses import dataclass, replace
from pathlib import Path

from smartvoice.config.settings import Settings
from smartvoice.domain.streaming import StageError
from smartvoice.domain.stream_context import SessionContext
from smartvoice.adapters.inference.runtime.compute_budget import ComputeBudget
from smartvoice.adapters.inference.runtime.stream_profile import Profiler
from smartvoice.adapters.inference.runtime.stream_pool import SharedPool, PooledWorker


class Counter:
    def __init__(self, model): self.model=model; self.value=0
    def increment(self):
        self.value+=1
        return os.getpid(), self.model, self.value
    def delay(self, seconds): time.sleep(seconds); return self.increment()
    def crash(self): os._exit(42)
    def release_stream(self): self.model=None


@dataclass(frozen=True)
class Binding:
    key: tuple=('model',)
    resident: int=100
    model_ids: tuple=()
    def factory(self): return os.urandom(8)
    def spawn(self, template): return Counter(template)


@dataclass(frozen=True)
class FailingBinding(Binding):
    def factory(self): raise StageError('model_unavailable','Injected initialization error')


class PoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        settings=replace(Settings(data_dir=Path("/tmp/smartvoice-pool-test")), streaming_memory_mib=200,
            streaming_max_model_workers=2, streaming_worker_idle_seconds=.02,
            streaming_cancel_grace_seconds=.05, streaming_native_seconds=2)
        self.pool=SharedPool(settings,ComputeBudget(2))
        self.addAsyncCleanup(self.pool.close)

    async def open(self, binding=Binding()):
        worker=PooledWorker(self.pool,'asr',Profiler())
        handle=await worker.construct(binding)
        self.addAsyncCleanup(worker.aclose)
        return worker,handle

    async def test_reuse_isolation_cancel_and_idle_reclaim(self):
        a,x=await self.open(); b,y=await self.open()
        first=await a.call(x.increment); second=await b.call(y.increment)
        self.assertEqual(first[:2],second[:2])  # One process and one loaded model.
        self.assertEqual(first[2],second[2])  # Independent state.
        self.assertEqual((await a.call(x.increment))[2],2)
        self.assertEqual(self.pool.resident_mib,100)
        await a.aclose()
        self.assertEqual((await b.call(y.increment))[2],2)
        await b.aclose(); await asyncio.sleep(.03); await self.pool.reap()
        self.assertEqual(self.pool.metrics()['model_workers'],0)
        self.assertFalse(self.pool.owned_pids())

    async def test_memory_budget_counts_models_and_evicts_idle(self):
        a,_=await self.open(); b,_=await self.open(Binding(('second',)))
        with self.assertRaises(StageError) as error: await self.open(Binding(('third',)))
        self.assertEqual(error.exception.code,'scheduler_overload')
        await a.aclose()
        c,_=await self.open(Binding(('third',)))
        self.assertEqual(self.pool.resident_mib,200)
        self.assertNotIn(('model',),self.pool.entries)

    async def test_cancel_waiting_job_keeps_sibling_healthy(self):
        a,x=await self.open(); b,y=await self.open()
        task=asyncio.create_task(a.call(x.delay,.12))
        while not a.entry.worker.inflight: await asyncio.sleep(.001)
        waiting=asyncio.create_task(b.call(y.increment))
        await asyncio.sleep(.01); waiting.cancel()
        with self.assertRaises(asyncio.CancelledError): await waiting
        await task
        self.assertEqual((await b.call(y.increment))[2],1)
        self.assertFalse(a.entry.poisoned)

    async def test_cancel_running_short_job_preserves_sibling(self):
        a,x=await self.open();b,y=await self.open()
        a.entry.worker.grace_seconds=.5
        task=asyncio.create_task(a.call(x.delay,.03))
        while not a.entry.worker.inflight:await asyncio.sleep(.001)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        await a.aclose()
        self.assertEqual((await b.call(y.increment))[2],1)
        self.assertFalse(b.entry.poisoned)

    async def test_queued_sibling_observes_worker_loss_other_model_survives(self):
        a,x=await self.open();b,y=await self.open();c,z=await self.open(Binding(('other',)))
        task=asyncio.create_task(a.call(x.delay,2,deadline=.02))
        while not a.entry.worker.inflight:await asyncio.sleep(.001)
        waiting=asyncio.create_task(b.call(y.increment))
        results=await asyncio.gather(task,waiting,return_exceptions=True)
        self.assertIsInstance(results[0],StageError)
        self.assertEqual(results[1].code,'worker_lost')
        self.assertEqual((await c.call(z.increment))[2],1)

    async def test_simultaneous_initialization_shares_one_template(self):
        handles=await asyncio.gather(self.open(),self.open(),self.open())
        values=await asyncio.gather(*(worker.call(handle.increment) for worker,handle in handles))
        self.assertEqual(len({value[:2] for value in values}),1)
        self.assertTrue(all(value[2]==1 for value in values))

    async def test_concurrent_close_releases_one_handle_once(self):
        a,x=await self.open();b,y=await self.open()
        await asyncio.gather(a.aclose(),a.aclose())
        self.assertEqual(self.pool.metrics()['stage_handles'],1)
        self.assertEqual((await b.call(y.increment))[2],1)

    async def test_cancel_close_waiter_does_not_cancel_shared_cleanup(self):
        a,x=await self.open();b,y=await self.open()
        active=asyncio.create_task(b.call(y.delay,.1))
        while not b.entry.worker.inflight:await asyncio.sleep(.001)
        closing=asyncio.create_task(a.aclose())
        while a.close_task is None:await asyncio.sleep(.001)
        closing.cancel()
        with self.assertRaises(asyncio.CancelledError):await closing
        await active;await a.aclose()
        self.assertEqual(self.pool.metrics()['stage_handles'],1)
        self.assertEqual((await b.call(y.increment))[2],2)

    async def test_native_deadline_poisoning_is_visible_to_all_handles(self):
        a,x=await self.open(); b,y=await self.open()
        with self.assertRaises(StageError): await a.call(x.delay,2,deadline=.02)
        with self.assertRaises(StageError) as error: await b.call(y.increment)
        self.assertEqual(error.exception.code,'worker_lost')
        await a.aclose(); await b.aclose()
        c,z=await self.open()
        self.assertEqual((await c.call(z.increment))[2],1)

    async def test_process_crash_closes_worker_and_surfaces_worker_lost(self):
        a,x=await self.open(); b,y=await self.open()
        with self.assertRaises(StageError) as error: await a.call(x.crash)
        self.assertEqual(error.exception.code,'worker_lost')
        self.assertTrue(a.entry.worker.closed)
        with self.assertRaises(StageError): await b.call(y.increment)

    async def test_failed_initialization_releases_handle_and_can_retry(self):
        with self.assertRaises(StageError): await self.open(FailingBinding())
        self.assertEqual(self.pool.metrics()['stage_handles'],0)
        worker,handle=await self.open()
        self.assertEqual((await worker.call(handle.increment))[2],1)

    async def test_bounded_queue_refuses_extra_job_without_leaking_permit(self):
        self.pool.settings=replace(self.pool.settings,streaming_max_pending_jobs=1)
        a,x=await self.open(); b,y=await self.open(); c,z=await self.open()
        active=asyncio.create_task(a.call(x.delay,.15))
        while not a.entry.worker.inflight: await asyncio.sleep(.001)
        waiting=asyncio.create_task(b.call(y.increment))
        while not a.entry.gate.waiters: await asyncio.sleep(.001)
        with self.assertRaises(StageError) as error: await c.call(z.increment)
        self.assertEqual(error.exception.code,'scheduler_overload')
        await active;await waiting
        self.assertEqual((await c.call(z.increment))[2],1)
        self.assertEqual(self.pool.metrics()['pending_jobs'],0)


class ContextTests(unittest.TestCase):
    def test_private_bounded_confirmed_context(self):
        a=SessionContext('en',('SmartVoice',)); b=SessionContext('en')
        for index in range(2000): a.commit(str(index),'GPU is active. '+('x'*300))
        snapshot=a.snapshot()
        self.assertLessEqual(sum(map(len,snapshot.recent_text)),512)
        self.assertLessEqual(len(a.seen),128)
        self.assertEqual(snapshot.version,2000)
        self.assertIn('GPU',snapshot.terms)
        self.assertIn('SmartVoice',snapshot.terms)
        self.assertFalse(b.snapshot().terms)
        a.commit('1999','Changed partial must not overwrite a commit.')
        self.assertEqual(a.snapshot(),snapshot)

    def test_hours_are_not_a_default_duration_limit(self):
        settings=Settings(data_dir=Path("/tmp/smartvoice-pool-test"))
        self.assertEqual(settings.streaming_lifetime_seconds,0)
        self.assertEqual(settings.streaming_max_audio_seconds,0)
