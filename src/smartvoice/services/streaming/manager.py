"""Session admission, complete-plan validation and installed-model use leases."""
from smartvoice.domain.streaming import StageError
from .planning import resolve
from .session import StreamSession

class StreamingManager:
    def __init__(self, settings, repository, workers, profiler_factory, gate_factory, packager):
        self.settings=settings;self.repository=repository;self.workers=workers
        self.profiler_factory=profiler_factory;self.gate_factory=gate_factory;self.packager=packager
        self.active={};self._resident=0;self.closing=False

    @property
    def resident(self):
        return self.workers.resident_mib if getattr(self.workers,'shared_models',False) else self._resident

    def open(self, config):
        if self.closing or len(self.active)>=self.settings.streaming_max_sessions:
            raise StageError('session_overload','Streaming session capacity reached')
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
            return session
        except BaseException:
            self._resident-=resident;lease.__exit__(None,None,None);raise

    async def close(self):
        import asyncio
        self.closing=True
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
                max_model_workers=self.settings.streaming_max_model_workers,max_pending_jobs=self.settings.streaming_max_pending_jobs,
                estimated_model_memory_mib=self.settings.streaming_memory_mib),
            active_sessions=len(self.active),estimated_resident_mib=self.resident,
            worker_pool=self.workers.metrics() if hasattr(self.workers,'metrics') else {},
            heartbeat={"message":{"type":"heartbeat"},"recommended_interval_seconds":self.settings.streaming_idle_seconds/3},
            verified_platforms=['macOS arm64 (sandbox)'],product_quality_accepted=False,
            performance_targets=dict(source_partial_p90_seconds=1,target_partial_p90_seconds=2,
                speech_ttfo_onset_p90_seconds=3),
            acknowledgement=dict(delivery='ack(event_sequence); accepts audio delivery without playback',
                playback='ack plus audio_started/audio_played(audio_sequence)'))
