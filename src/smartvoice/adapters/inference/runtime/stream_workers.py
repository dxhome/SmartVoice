"""Managed affinity workers and bounded fair finite-job admission."""
import asyncio
from contextlib import asynccontextmanager
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
import multiprocessing
import time
from smartvoice.domain.streaming import StageError
from smartvoice.adapters.inference.runtime.stream_profile import RECORDER,CONTEXT,Profiler
import os

class FairGate:
    """One native job. Final jobs get priority, with a bounded final burst."""
    def __init__(self,max_waiters=32):
        self.busy=False;self.waiters=[];self.serial=0;self.final_burst=0;self.max_waiters=max_waiters
    def locked(self):return self.busy
    async def acquire(self,priority=1):
        if len(self.waiters)>=self.max_waiters:raise StageError('scheduler_overload','Admission queue full')
        loop=asyncio.get_running_loop();future=loop.create_future()
        self.serial+=1;entry=(priority,self.serial,future);self.waiters.append(entry);self._dispatch()
        try:await future
        except BaseException:
            if entry in self.waiters:self.waiters.remove(entry)
            elif future.done() and not future.cancelled():self.release()
            raise
    def _dispatch(self):
        if self.busy or not self.waiters:return
        drafts=[r for r in self.waiters if r[0]>0]
        chosen=min(drafts,key=lambda r:r[1]) if drafts and self.final_burst>=3 else min(self.waiters,key=lambda r:(r[0],r[1]))
        self.waiters.remove(chosen)
        if chosen[2].cancelled():self._dispatch();return
        self.busy=True;self.final_burst=self.final_burst+1 if chosen[0]==0 else 0
        chosen[2].set_result(None)
    def release(self):
        if not self.busy:raise RuntimeError('Native permit released twice')
        self.busy=False;self._dispatch()
    async def __aenter__(self):await self.acquire();return self
    async def __aexit__(self,*_):self.release()
    @asynccontextmanager
    async def permit(self,priority=1):
        await self.acquire(priority)
        try:yield
        finally:self.release()

_stage=None
def _descriptor():
    return {name:getattr(_stage,name) for name in ('plan','partial_support','projection_repairs') if hasattr(_stage,name)}
def _construct_native(factory,args):
    global _stage
    _stage=factory(*args)
    return _descriptor()
class RemoteMethod:
    def __init__(self,name):self.__name__=name
    def __call__(self,*args):return getattr(_stage,self.__name__)(*args)
class RemoteStage:
    def __init__(self,metadata):self.__dict__.update(metadata)
    def __getattr__(self,name):
        if name.startswith('_'):raise AttributeError(name)
        return RemoteMethod(name)
def _process_invoke(function,args,submitted,context):
    began=time.monotonic()
    recorder=Profiler();token=RECORDER.set(recorder);ctx_token=CONTEXT.set({**(context or {}),'execution_pid':os.getpid(),'execution_scope':'owned native process'})
    try:
        value=function(*args);error=None
    except Exception as exc:
        value=None;error=(exc.code,exc.message) if isinstance(exc,StageError) else ('native_failure',type(exc).__name__)
    finally:RECORDER.reset(token);CONTEXT.reset(ctx_token)
    return value,error,began-submitted,time.monotonic()-began,recorder.export(),_descriptor() if _stage is not None else None

def _silence_native_output():
    # Native engines may print recognized tokens on OOV errors. Return bounded
    # structured errors/profiling to the parent instead of retaining raw text.
    with open(os.devnull,'w') as sink:
        os.dup2(sink.fileno(),1);os.dup2(sink.fileno(),2)

class ProcessAffinityWorker:
    """One scheduler-owned spawned process; native completion precedes exit.

    The adapter and its native objects stay in the child. Only registered
    top-level functions and typed serializable records cross this boundary.
    """
    def __init__(self,name,profiler,grace_seconds=1.0):
        self.grace_seconds=grace_seconds
        self.executor=ProcessPoolExecutor(max_workers=1,mp_context=multiprocessing.get_context('spawn'),initializer=_silence_native_output)
        self.name,self.profiler=name,profiler;self.inflight=False;self.closed=False;self.handle=None
    async def construct(self,factory,*args,deadline=None):
        metadata=await self.call(_construct_native,factory,args,deadline=deadline)
        self.handle=RemoteStage(metadata);return self.handle
    async def call(self,function,*args,deadline=None,on_timeout=None,context=None):
        if self.closed:raise StageError('stage_closed','Worker closed')
        if self.inflight:raise StageError('stage_busy','Concurrent use of process worker')
        submitted=time.monotonic();self.inflight=True;future=None
        try:
            if context is None and args and hasattr(args[0],'sequence'):context={'audio_sequence':args[0].sequence,'start_sample':args[0].start_sample}
            future=asyncio.get_running_loop().run_in_executor(self.executor,_process_invoke,function,args,submitted,context)
            try:
                result=await asyncio.shield(future) if deadline is None else await asyncio.wait_for(asyncio.shield(future),deadline)
            except TimeoutError:
                if on_timeout:on_timeout()
                await self._settle(future)
                raise StageError(self.name+'_timeout','Native process job exceeded logical deadline')
            except asyncio.CancelledError:
                await self._settle(future);raise
            value,error,*_=result
            if error:raise StageError(error[0] if error[0]!='native_failure' else self.name+'_failure',error[1])
            return value
        except StageError as exc:
            exc.stage=exc.stage or self.name
            raise
        except asyncio.CancelledError:raise
        except BrokenProcessPool as exc:
            await self.aclose()
            raise StageError('worker_lost','Owned native worker exited unexpectedly',self.name) from exc
        except Exception as exc:raise StageError(self.name+'_failure',type(exc).__name__) from exc
        finally:
            if future and future.done() and not future.cancelled() and future.exception() is None:
                _,_,wait,native,recording,metadata=future.result();token=CONTEXT.set(context)
                try:
                    self.profiler.merge(recording)
                    if self.handle and metadata:self.handle.__dict__.update(metadata)
                    self.profiler.add(self.name+'.executor_wait',wait)
                    self.profiler.add(self.name+'.'+function.__name__,native)
                    self.profiler.add(self.name+'.ipc_roundtrip',max(0.,time.monotonic()-submitted-wait-native))
                finally:CONTEXT.reset(token)
            self.inflight=False
    def close(self):
        if self.closed:return
        began=time.monotonic()
        processes=tuple((self.executor._processes or {}).values())
        manager=self.executor._executor_manager_thread
        result_queue=self.executor._result_queue
        self.executor.shutdown(wait=False,cancel_futures=True)
        # The executor manager owns waitpid/join. Concurrently joining the same
        # multiprocessing.Process can race its returncode and falsely leave it
        # alive. Bound the manager join and signal only this worker's children.
        if manager:
            manager.join(self.grace_seconds)
            if manager.is_alive():
                for process in processes:process.terminate()
                manager.join(1)
            if manager.is_alive():
                for process in processes:process.kill()
                manager.join(1)
            if manager.is_alive():raise StageError('cleanup_failure','Owned worker manager did not exit',self.name)
        if any(process.is_alive() for process in processes):
            raise StageError('cleanup_failure','Owned worker did not exit',self.name)
        if result_queue:result_queue.close()
        self.closed=True
        self.profiler.add(self.name+'.process_shutdown',time.monotonic()-began)
    async def _settle(self, future):
        try:
            await asyncio.wait_for(asyncio.shield(future), self.grace_seconds)
        except TimeoutError:
            await self.aclose()
            await asyncio.gather(future,return_exceptions=True)
        except Exception:
            pass  # Broken/crashed worker has completed its native ownership.
    async def aclose(self):
        await asyncio.to_thread(self.close)
    def owned_pids(self):
        # Python 3.11 executor handles: inspect only processes we created, never
        # enumerate unrelated host processes. Kept at the scheduler boundary.
        return tuple(p.pid for p in (self.executor._processes or {}).values() if p.pid)
