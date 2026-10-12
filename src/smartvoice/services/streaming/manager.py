"""Session admission, complete-plan validation and installed-model use leases."""
import asyncio
import secrets
from smartvoice.domain.streaming import StageError
from .planning import resolve
from .session import StreamSession
from .recovery import RecoveryState

class StreamingManager:
    def __init__(self, settings, repository, workers, profiler_factory, gate_factory, packager):
        self.settings=settings;self.repository=repository;self.workers=workers
        self.profiler_factory=profiler_factory;self.gate_factory=gate_factory;self.packager=packager
        self.active={};self._resident=0;self.closing=False
        self.recoverable={}

    @property
    def resident(self):
        return self.workers.resident_mib if getattr(self.workers,'shared_models',False) else self._resident

    def open(self, config):
        if self.closing or len(self.active)>=self.settings.streaming_max_sessions:
            raise StageError('session_overload','Streaming session capacity reached')
        if isinstance(config,dict) and config.get('resumable') and len(self.recoverable)>=self.settings.streaming_max_sessions:
            raise StageError('session_overload','Recovery session capacity reached')
        plan,specs,resident=resolve(config,self.repository,self.settings.num_threads)
        self.workers.preflight(specs)
        if (resident if getattr(self.workers,'shared_models',False) else self.resident+resident)>self.settings.streaming_memory_mib:
            raise StageError('memory_admission','Estimated streaming model budget exceeded')
        lease=self.repository.hold_models(plan.model_ids)
        try: lease.__enter__()
        except Exception as exc: raise StageError('model_unavailable','Install or import all selected stage assets first') from exc
        self._resident+=resident
        session=None
        def release():
            self._resident-=resident
            self.active.pop(session.id,None)
            lease.__exit__(None,None,None)
        try:
            session=StreamSession(plan,specs,self.settings,self.workers,self.profiler_factory(),
                self.gate_factory(),self.packager,release,config.get('ack_window',16))
            self.active[session.id]=session
            if config.get('resumable'):
                session.recovery=RecoveryState(session,self.settings.streaming_resume_grace_seconds)
                self.recoverable[session.id]=session.recovery
            return session
        except BaseException:
            self._resident-=resident;lease.__exit__(None,None,None);raise

    def resume(self, request):
        fields={'type','protocol','session_id','resume_token','last_event_sequence','last_audio_played'}
        if (not isinstance(request,dict) or request.keys()!=fields or request['type']!='resume'
                or request['protocol']!='smartvoice.stream.v1'
                or not isinstance(request['session_id'],str) or not isinstance(request['resume_token'],str)):
            raise StageError('invalid_resume','Unexpected resume fields')
        state=self.recoverable.get(request['session_id'])
        if self.closing or state is None or not secrets.compare_digest(state.token.encode(),request['resume_token'].encode('utf-8','surrogatepass')):
            raise StageError('resume_unavailable','Session recovery is unavailable')
        response,replay=state.resume(request['last_event_sequence'],request['last_audio_played'])
        if state.expiry_task: state.expiry_task.cancel();state.expiry_task=None
        return state.session,response,replay

    def forget(self, session):
        state=self.recoverable.pop(session.id,None)
        if state and state.expiry_task:
            state.expiry_task.cancel();state.expiry_task=None

    def detach(self, session):
        state=self.recoverable.get(session.id)
        if state is None or not state.attached:return
        state.attached=False
        async def expire():
            try:
                await asyncio.sleep(state.grace_seconds)
                if self.recoverable.get(session.id) is state and not state.attached:
                    self.recoverable.pop(session.id,None)
                    await session.cancel('resume_expired')
            except asyncio.CancelledError:pass
        state.expiry_task=asyncio.create_task(expire())

    async def close(self):
        self.closing=True
        timers=[s.expiry_task for s in self.recoverable.values() if s.expiry_task]
        for task in timers:task.cancel()
        await asyncio.gather(*timers,return_exceptions=True)
        self.recoverable.clear()
        await asyncio.gather(*(s.cancel('server_shutdown') for s in list(self.active.values())),return_exceptions=True)
        close=getattr(self.workers,'aclose',None)
        if close:await close()

    def capabilities(self):
        from .planning import MODES
        installed={row['id'] for row in self.repository.installed_models()}
        chains=[]
        for mode in MODES:
            for language in ('zh','en'):
                config=dict(type='configure',protocol='smartvoice.stream.v1',mode=mode,source_language=language,
                    audio=dict(encoding='pcm_s16le',sample_rate=16000,channels=1))
                if mode!='transcription':config['target_language']='en' if language=='zh' else 'zh'
                try:
                    plan,specs,resident=resolve(config,self.repository,self.settings.num_threads)
                    missing=[mid for mid in plan.model_ids if mid not in installed]
                    self.workers.preflight(specs)
                    reason='missing_models' if missing else 'memory_admission' if resident>self.settings.streaming_memory_mib else None
                    chains.append(dict(mode=mode,source_language=language,target_language=plan.target_language,
                        available=reason is None,reason=reason,missing_models=missing,plan=plan.public()))
                except StageError as exc:
                    chains.append(dict(mode=mode,source_language=language,available=False,reason=exc.code))
        return dict(chains=chains,protocol='smartvoice.stream.v1',enabled=True,release_status='preview',
            modes=['transcription','translated_subtitles','spoken_interpretation'],
            source_languages=['zh','en'],language_pairs=[['zh','en'],['en','zh']],
            input=dict(encoding='pcm_s16le',sample_rate=16000,channels=1,max_frame_bytes=32000),
            output_audio=dict(encoding='wav',transport='json_descriptor_then_binary'),
            limits=dict(max_sessions=self.settings.streaming_max_sessions,lifetime_seconds=self.settings.streaming_lifetime_seconds,
                idle_seconds=self.settings.streaming_idle_seconds,max_audio_seconds=self.settings.streaming_max_audio_seconds,
                max_input_queue_frames=self.settings.streaming_input_queue_frames,
                max_model_workers=self.settings.streaming_max_model_workers,max_pending_jobs=self.settings.streaming_max_pending_jobs,
                estimated_model_memory_mib=self.settings.streaming_memory_mib),
            active_sessions=len(self.active),estimated_resident_mib=self.resident,
            input_queues=dict(current_frames=sum(s.input.qsize() for s in self.active.values()),
                peak_frames=max((s.input_queue_peak_frames for s in self.active.values()),default=0)),
            recovery_sessions=len(self.recoverable),
            detached_sessions=sum(not state.attached for state in self.recoverable.values()),
            recovery_cache_bytes=sum(state.bytes for state in self.recoverable.values()),
            worker_pool=self.workers.metrics() if hasattr(self.workers,'metrics') else {},
            process_resource_sample=self.process_resource_sample(self.workers,len(self.active)),
            heartbeat={"message":{"type":"heartbeat"},"recommended_interval_seconds":self.settings.streaming_idle_seconds/3},
            verified_platforms=['macOS arm64 (sandbox)'],product_quality_accepted=False,
            performance_targets=dict(source_partial_p90_seconds=1,target_partial_p90_seconds=2,
                speech_ttfo_threshold_seconds=None,speech_ttfo_clock='first_pcm_send'),
            acknowledgement=dict(delivery='ack(event_sequence); accepts audio delivery without playback',
                playback='ack plus audio_started/audio_played(audio_sequence)'),
            resumption=dict(opt_in='configure.resumable=true',grace_seconds=self.settings.streaming_resume_grace_seconds,
                same_process_only=True,input_history_frames=RecoveryState.input_history,
                max_replay_events=RecoveryState.max_events,max_replay_bytes=RecoveryState.max_bytes,
                audio_deduplication='whole completed chunks; unfinished chunks replay',
            detached_sessions_count_toward_admission=True))

    @staticmethod
    def process_resource_sample(workers=None,active_sessions=0):
        """Sample the API and scheduler-owned worker PIDs without host-wide scans.

        Walking children from the API root is blocked by some macOS sandbox
        profiles. The pool already owns its worker PID list, so sample those
        exact processes directly. This reports the API plus known workers;
        descendant discovery remains explicit and can still be incomplete.
        """
        try:
            import os, psutil
        except Exception:
            return dict(available=False,process_tree_complete=False,reason='psutil_unavailable')
        try:
            root=psutil.Process(os.getpid())
            if workers and hasattr(workers,'owned_processes'):
                owned_processes=list(workers.owned_processes())
            else:
                owned_processes=[dict(stage='unknown',pid=pid) for pid in
                    (workers.owned_pids() if workers and hasattr(workers,'owned_pids') else ())]
            known_pids={row['pid'] for row in owned_processes}
            stage_by_pid={row['pid']:row.get('stage','unknown') for row in owned_processes}
            processes=[root];complete=True;error=None;sampled_worker_pids=set()
            worker_samples=[]
            for pid in known_pids:
                try:
                    process=psutil.Process(pid)
                    # We own this PID through ProcessPoolExecutor. Sample it
                    # directly instead of relying on restricted child walks.
                    processes.append(process);sampled_worker_pids.add(pid)
                    times=process.cpu_times();memory=process.memory_info()
                    worker_samples.append(dict(stage=stage_by_pid.get(pid,'unknown'),
                        cpu_user_seconds=times.user,cpu_system_seconds=times.system,rss_bytes=memory.rss))
                except psutil.NoSuchProcess:
                    # A worker can exit between pool inspection and sampling.
                    continue
                except (psutil.AccessDenied,PermissionError) as exc:
                    complete=False;error=type(exc).__name__
                except psutil.Error as exc:
                    complete=False;error=type(exc).__name__
            # Include any additional descendants discoverable from known
            # workers, but do not mark the sample complete when discovery is
            # permission-blocked.
            descendants=[]
            # Include API-owned runtime helpers (for example the spawn
            # resource tracker) when traversal is permitted. Known model
            # workers are still sampled directly if any walk is denied.
            for process in processes:
                try: descendants.extend(process.children(recursive=True))
                except (psutil.AccessDenied,PermissionError) as exc:
                    complete=False;error=error or type(exc).__name__
                except psutil.Error as exc:
                    complete=False;error=error or type(exc).__name__
            processes.extend(p for p in descendants if p.pid not in {x.pid for x in processes})
            if sampled_worker_pids != known_pids:
                complete=False;error=error or 'owned_worker_unavailable'
            cpu_user=cpu_system=rss=0;sampled=0
            for process in processes:
                try:
                    times=process.cpu_times();memory=process.memory_info()
                    cpu_user+=times.user;cpu_system+=times.system;rss+=memory.rss;sampled+=1
                except psutil.Error:
                    complete=False
            return dict(available=True,process_tree_complete=complete,child_discovery_error=error,
                process_count=sampled,cpu_user_seconds=cpu_user,cpu_system_seconds=cpu_system,
                known_owned_worker_count=len(known_pids),sampled_owned_worker_count=len(sampled_worker_pids),
                owned_workers_complete=sampled_worker_pids==known_pids,
                descendant_discovery_complete=complete,
                active_sessions=active_sessions,
                worker_processes=worker_samples,
                worker_rss_bytes=sum(row['rss_bytes'] for row in worker_samples),
                worker_rss_bytes_per_active_session=(sum(row['rss_bytes'] for row in worker_samples)/active_sessions
                    if active_sessions>0 and len(worker_samples)==len(known_pids) else None),
                rss_bytes=rss,rss_scope='API plus scheduler-owned worker processes and observed descendants; summed RSS can double-count shared pages')
        except Exception as exc:
            return dict(available=False,process_tree_complete=False,reason=type(exc).__name__)
