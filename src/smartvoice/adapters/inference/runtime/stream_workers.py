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


def _worker_resource_sample(*,sample_rss):
    """Return resource data measured by the owned native process itself.

    Parent-side process enumeration is unavailable in some sandboxed hosts.
    Keep this deliberately small and portable: cumulative CPU is from
    getrusage and RSS is the worker's own current RSS when psutil is present.
    """
    try:
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF)
    except ImportError:  # Windows has no resource module.
        usage = None
    cpu_user=usage.ru_utime if usage else None
    cpu_system=usage.ru_stime if usage else None
    rss_bytes = None;rss_kind='unavailable';process_tree_complete=False;process_count=1
    self_rss_bytes=None;self_cpu_user=None;self_cpu_system=None;self_sample_complete=False
    descendant_discovery_complete=False
    if sample_rss:
        try:
            import psutil
            root=psutil.Process(os.getpid())
            own_times=root.cpu_times();own_memory=root.memory_info()
            self_cpu_user=own_times.user;self_cpu_system=own_times.system
            self_rss_bytes=own_memory.rss;self_sample_complete=True
            rss_bytes=self_rss_bytes;cpu_user=self_cpu_user;cpu_system=self_cpu_system
            rss_kind='worker_process_self'
            try:
                children=root.children(recursive=True)
                processes=[root,*children]
                process_tree_complete=True
                descendant_discovery_complete=True
            except Exception:
                processes=[root]
            if process_tree_complete:
                cpu_user=sum(p.cpu_times().user for p in processes)
                cpu_system=sum(p.cpu_times().system for p in processes)
                rss_bytes=sum(p.memory_info().rss for p in processes)
                process_count=len(processes);rss_kind='worker_process_and_visible_descendants'
        except Exception:
            pass
    if sample_rss and rss_bytes is None:
        # ru_maxrss is a high-water mark (not current RSS); normalize its
        # platform-dependent units so the caller can still report it honestly.
        if usage is not None:
            import sys
            value = int(usage.ru_maxrss)
            rss_bytes = value if sys.platform == 'darwin' else value * 1024
            rss_kind='high_water'
    return dict(cpu_user_seconds=cpu_user if sample_rss and rss_bytes is not None else usage.ru_utime if usage else None,
                cpu_system_seconds=cpu_system if sample_rss and rss_bytes is not None else usage.ru_stime if usage else None,
                pid=os.getpid(),
                resource_sampled=bool(sample_rss),
                rss_bytes=int(rss_bytes) if rss_bytes is not None else None, rss_kind=rss_kind,
                self_cpu_user_seconds=self_cpu_user,self_cpu_system_seconds=self_cpu_system,
                self_rss_bytes=int(self_rss_bytes) if self_rss_bytes is not None else None,
                process_self_complete=self_sample_complete,
                descendant_discovery_complete=descendant_discovery_complete,
                process_tree_complete=process_tree_complete,process_count=process_count)

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
_resource_sample_counter=0
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
    global _resource_sample_counter
    began=time.monotonic()
    recorder=Profiler();token=RECORDER.set(recorder);ctx_token=CONTEXT.set({**(context or {}),'execution_pid':os.getpid(),'execution_scope':'owned native process'})
    try:
        value=function(*args);error=None
    except Exception as exc:
        value=None;error=(exc.code,exc.message) if isinstance(exc,StageError) else ('native_failure',type(exc).__name__)
    finally:RECORDER.reset(token);CONTEXT.reset(ctx_token)
    _resource_sample_counter+=1
    resources=_worker_resource_sample(sample_rss=(_resource_sample_counter==1 or _resource_sample_counter%32==0))
    return (value,error,began-submitted,time.monotonic()-began,recorder.export(),
            _descriptor() if _stage is not None else None,resources)

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
        self.resource_samples=0;self.cpu_user_seconds=0.;self.cpu_system_seconds=0.
        self.peak_rss_bytes=0;self.last_rss_bytes=None;self.rss_kind=None
        self.process_tree_complete=False;self.process_count=0
        self.self_cpu_user_seconds=0.;self.self_cpu_system_seconds=0.;self.self_rss_bytes=None
        self.process_self_complete=False;self.descendant_discovery_complete=False
        self.worker_pid=None
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
                _,_,wait,native,recording,metadata,resources=future.result();token=CONTEXT.set(context)
                try:
                    self.worker_pid=resources.get('pid') or self.worker_pid
                    self.profiler.merge(recording)
                    if self.handle and metadata:self.handle.__dict__.update(metadata)
                    self.profiler.add(self.name+'.executor_wait',wait)
                    self.profiler.add(self.name+'.'+function.__name__,native)
                    self.profiler.add(self.name+'.ipc_roundtrip',max(0.,time.monotonic()-submitted-wait-native))
                    if resources['rss_bytes'] is not None:self.resource_samples += 1
                    if resources['cpu_user_seconds'] is not None:
                        self.cpu_user_seconds = max(self.cpu_user_seconds, resources['cpu_user_seconds'])
                    if resources['cpu_system_seconds'] is not None:
                        self.cpu_system_seconds = max(self.cpu_system_seconds, resources['cpu_system_seconds'])
                    if resources.get('self_cpu_user_seconds') is not None:
                        self.self_cpu_user_seconds=max(self.self_cpu_user_seconds,resources['self_cpu_user_seconds'])
                    if resources.get('self_cpu_system_seconds') is not None:
                        self.self_cpu_system_seconds=max(self.self_cpu_system_seconds,resources['self_cpu_system_seconds'])
                    if resources.get('self_rss_bytes') is not None:self.self_rss_bytes=resources['self_rss_bytes']
                    if resources.get('resource_sampled'):
                        self.process_self_complete=resources.get('process_self_complete',False)
                        self.descendant_discovery_complete=resources.get('descendant_discovery_complete',False)
                    if resources['rss_bytes'] is not None:
                        self.peak_rss_bytes = max(self.peak_rss_bytes, resources['rss_bytes'])
                    if resources['rss_bytes'] is not None:
                        self.last_rss_bytes = resources['rss_bytes']
                        self.rss_kind = resources['rss_kind']
                        self.process_tree_complete=resources['process_tree_complete']
                        self.process_count=resources['process_count']
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
        # Learned from calls running inside our spawned process. This survives
        # ProcessPoolExecutor clearing its private registry and avoids scanning
        # unrelated host processes.
        if self.closed:return ()
        # A first native call may never return (timeout/cancel/crash). Its PID
        # must still be visible for cancellation and resource sampling before
        # the first self-report arrives.
        if self.worker_pid:return (self.worker_pid,)
        return tuple(process.pid for process in (self.executor._processes or {}).values()
                     if process.pid is not None)

    def resource_metrics(self):
        return dict(samples=self.resource_samples,cumulative_cpu_user_seconds=self.cpu_user_seconds,
                    cumulative_cpu_system_seconds=self.cpu_system_seconds,peak_rss_bytes=self.peak_rss_bytes,
                    last_rss_bytes=self.last_rss_bytes,rss_kind=self.rss_kind,
                    self_cpu_user_seconds=self.self_cpu_user_seconds,
                    self_cpu_system_seconds=self.self_cpu_system_seconds,self_rss_bytes=self.self_rss_bytes,
                    process_self_complete=self.process_self_complete,
                    descendant_discovery_complete=self.descendant_discovery_complete,
                    process_count=self.process_count,process_tree_complete=self.process_tree_complete)
