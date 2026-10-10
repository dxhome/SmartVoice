"""Bounded shared native processes, with independent session handles.

A process contains one model template and multiple session adapters. Calls are
serialized fairly; no native object or mutable context crosses the boundary.
"""
import asyncio
import time
import uuid
from dataclasses import dataclass
from smartvoice.domain.streaming import StageError
from .stream_workers import ProcessAffinityWorker, RemoteMethod, FairGate


class Registry:
    def __init__(self):
        self.template = None
        self.handles = {}

    def open(self, token, binding):
        from .stream_profile import span
        if self.template is None:
            with span(getattr(binding,'stage','model')+'.model_initialize'):
                self.template = binding.factory()
        with span(getattr(binding,'stage','model')+'.session_initialize'):
            adapter = binding.spawn(self.template)
        self.handles[token] = adapter
        return {name: getattr(adapter, name) for name in
                ('plan', 'partial_support', 'projection_repairs', 'context_support') if hasattr(adapter, name)}

    def invoke(self, token, name, args):
        if token not in self.handles:
            raise StageError('stage_closed', 'Session stage is no longer available')
        adapter = self.handles[token]
        if name == 'close':
            self.release(token)
            return None
        value = getattr(adapter, name)(*args)
        return Invocation(value, {key:getattr(adapter,key) for key in
                                 ('projection_repairs',) if hasattr(adapter,key)})

    def release(self, token):
        adapter = self.handles.pop(token, None)
        if adapter is not None and hasattr(adapter, 'release_stream'):
            adapter.release_stream()


@dataclass(frozen=True)
class Invocation:
    value: object
    metadata: dict


@dataclass(frozen=True)
class Operation:
    token: str
    name: str
    @property
    def __name__(self): return self.name


class Handle:
    def __init__(self, token, metadata):
        self.token = token
        self.__dict__.update(metadata)
    def __getattr__(self, name):
        if name.startswith('_'): raise AttributeError(name)
        return Operation(self.token, name)


class Entry:
    def __init__(self, name, grace, waiters, lease, resident):
        from .stream_profile import Profiler
        self.worker = ProcessAffinityWorker(name, Profiler(), grace)
        self.gate = FairGate(waiters)
        self.initialized = False
        self.users = 0
        self.used = time.monotonic()
        self.lease, self.resident = lease, resident
        self.poisoned = False


class SharedPool:
    def __init__(self, settings, compute, repository=None):
        self.settings, self.compute, self.repository = settings, compute, repository
        self.entries = {}
        self.closed = False
        self.lock = asyncio.Lock()

    @property
    def resident_mib(self): return sum(e.resident for e in self.entries.values())

    async def acquire(self, binding, name):
        async with self.lock:
            if self.closed: raise StageError('stage_closed', 'Worker pool is closing')
            key = binding.key
            entry = self.entries.get(key)
            if entry and (entry.poisoned or entry.worker.closed):
                if entry.users: raise StageError('worker_lost', 'Model worker failed; retry after affected sessions close')
                await self._evict(key)
                entry = None
            if entry is None:
                # Warm idle entries are evicted before refusing new model residency.
                for old, candidate in sorted(list(self.entries.items()), key=lambda item: item[1].used):
                    if (len(self.entries) < self.settings.streaming_max_model_workers and
                            self.resident_mib + binding.resident <= self.settings.streaming_memory_mib): break
                    if not candidate.users: await self._evict(old)
                if len(self.entries) >= self.settings.streaming_max_model_workers:
                    raise StageError('scheduler_overload', 'Model worker pool is full')
                if self.resident_mib + binding.resident > self.settings.streaming_memory_mib:
                    raise StageError('memory_admission', 'Shared model residency budget exceeded')
                lease = self.repository.hold_models(binding.model_ids) if self.repository else None
                if lease: lease.__enter__()
                try:
                    entry = Entry(name, self.settings.streaming_cancel_grace_seconds,
                                  self.settings.streaming_max_pending_jobs, lease, binding.resident)
                except BaseException:
                    if lease: lease.__exit__(None, None, None)
                    raise
                self.entries[key] = entry
            entry.users += 1
            return entry

    async def _evict(self, key):
        entry = self.entries[key]
        await entry.worker.aclose()
        try:
            if entry.lease: entry.lease.__exit__(None, None, None)
        finally: self.entries.pop(key)

    async def reap(self):
        async with self.lock:
            now = time.monotonic()
            for key, entry in list(self.entries.items()):
                if not entry.users and now-entry.used >= self.settings.streaming_worker_idle_seconds:
                    await self._evict(key)

    async def close(self):
        async with self.lock:
            self.closed = True
            for key in list(self.entries): await self._evict(key)

    def metrics(self):
        return dict(model_workers=len(self.entries), resident_mib=self.resident_mib,
                    stage_handles=sum(e.users for e in self.entries.values()),
                    active_jobs=sum(e.gate.busy for e in self.entries.values()),
                    pending_jobs=sum(len(e.gate.waiters) for e in self.entries.values()))

    def owned_pids(self):
        return tuple(pid for e in self.entries.values() for pid in e.worker.owned_pids())


class PooledWorker:
    def __init__(self, pool, name, profiler):
        self.pool, self.name, self.profiler = pool, name, profiler
        self.entry = None; self.handle = None; self.closed = False; self.inflight = False
        self.close_task = None

    async def _execute(self, function, *args, deadline=None, context=None):
        entry = self.entry
        if self.closed or entry.poisoned or entry.worker.closed:
            raise StageError('worker_lost', 'Shared worker is unavailable', self.name)
        began = time.monotonic(); budget = deadline or self.pool.settings.streaming_native_seconds
        priority = 0 if self.name == 'asr' else 1
        try:
            await asyncio.wait_for(entry.gate.acquire(priority), budget)
        except TimeoutError as exc:
            raise StageError('scheduler_overload', 'Stage admission deadline exceeded', self.name) from exc
        try:
            # A sibling job can invalidate the process while this job is queued.
            if entry.poisoned or entry.worker.closed:
                raise StageError('worker_lost','Shared worker failed while this job was queued',self.name)
            self.profiler.add(self.name+'.pool_queue_wait', time.monotonic()-began)
            remaining = max(.001, budget-(time.monotonic()-began))
            from smartvoice.domain.errors import InferenceOverloadedError
            compute_began=time.monotonic()
            try:
                async with self.pool.compute.async_permit(priority=priority, timeout=remaining):
                    self.profiler.add(self.name+'.compute_admission_wait', time.monotonic()-compute_began)
                    if not entry.initialized:
                        await entry.worker.construct(Registry, deadline=remaining)
                        self.profiler.merge(entry.worker.profiler.export())
                        entry.initialized = True
                    # Export only this finite job's samples to the owning session.
                    from .stream_profile import Profiler
                    entry.worker.profiler = Profiler()
                    value = await entry.worker.call(function, *args,
                        deadline=max(.001,budget-(time.monotonic()-began)), context=context)
                    self.profiler.merge(entry.worker.profiler.export())
                    if isinstance(value,Invocation):
                        self.handle.__dict__.update(value.metadata)
                        value=value.value
                    return value
            except InferenceOverloadedError as exc:
                raise StageError('scheduler_overload', 'Compute admission unavailable', self.name) from exc
        finally:
            entry.poisoned = entry.poisoned or entry.worker.closed
            entry.gate.release()
            entry.used = time.monotonic()

    async def construct(self, binding, deadline=None):
        self.entry = await self.pool.acquire(binding, self.name)
        token = uuid.uuid4().hex
        # Keep the token even if initialization is canceled; release removes any
        # child handle created before the response was interrupted.
        self.handle = Handle(token, {})
        try:
            metadata = await self._execute(RemoteMethod('open'), token, binding, deadline=deadline)
            self.handle.__dict__.update(metadata)
            return self.handle
        except BaseException:
            await self.aclose()
            raise

    async def call(self, function, *args, deadline=None, context=None, **kwargs):
        if self.closed or self.close_task is not None:
            raise StageError('stage_closed','Session stage is closing',self.name)
        if self.inflight: raise StageError('stage_busy', 'Concurrent session stage use')
        if not isinstance(function, Operation) or function.token != self.handle.token:
            raise StageError('invalid_stage_operation', 'Operation does not belong to this stage handle')
        self.inflight = True
        began=time.monotonic()
        if context is None and args and hasattr(args[0],'sequence'):
            context={'audio_sequence':args[0].sequence,'start_sample':args[0].start_sample}
        try:
            return await self._execute(RemoteMethod('invoke'), function.token, function.name, args,
                                       deadline=deadline, context=context)
        finally:
            self.profiler.add(self.name+'.'+function.name+'.wall',time.monotonic()-began)
            self.inflight = False

    async def aclose(self):
        if self.closed: return
        if self.close_task is None:
            self.close_task=asyncio.create_task(self._close())
        # Duplicate close/cancel callers share one cleanup; canceling a caller
        # must not cancel native release or decrement the model handle twice.
        await asyncio.shield(self.close_task)

    async def _close(self):
        try:
            if self.entry and self.handle and not self.entry.worker.closed:
                await self._execute(RemoteMethod('release'), self.handle.token,
                                    deadline=self.pool.settings.streaming_native_seconds)
        finally:
            if self.entry:
                self.entry.users -= 1
                self.entry.used = time.monotonic()
            self.closed = True
