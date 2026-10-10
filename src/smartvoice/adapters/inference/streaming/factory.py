"""Composition of registered adapters; shared flows never inspect model families."""
from dataclasses import dataclass
from copy import copy
from importlib.util import find_spec
from smartvoice.domain.streaming import StageError


def construct_stage(settings,spec,plan):
    capability=spec.streaming or {}
    adapter=capability.get('adapter')
    if adapter=='sherpa-online':
        from .asr import OnlineASR
        return OnlineASR(settings,spec,plan)
    if adapter=='sherpa-punctuation':
        from .formatting import PunctuationAdapter
        return PunctuationAdapter(settings,spec,plan)
    if adapter=='ctranslate2':
        from .translation import TextTranslation
        return TextTranslation(settings,spec,plan)
    if adapter=='sherpa-tts':
        from .synthesis import SpeechAdapter
        return SpeechAdapter(settings,spec,plan)
    raise StageError('model_unavailable','No registered adapter for this stage')

@dataclass(frozen=True)
class StageBinding:
    settings: object
    spec: object
    plan: object
    key: tuple
    resident: int
    model_ids: tuple
    @property
    def stage(self): return self.spec.streaming['stage']
    def factory(self): return construct_stage(self.settings,self.spec,self.plan)
    def spawn(self,template):
        if (self.spec.streaming or {}).get('adapter')=='sherpa-online':
            from .asr import OnlineASR
            return OnlineASR(self.settings,self.spec,self.plan,recognizer=template.recognizer)
        adapter=copy(template)
        # Native model/tokenizer objects are shared; session metadata is not.
        if hasattr(adapter,'plan'): adapter.plan=dict(template.plan)
        if hasattr(adapter,'projection_repairs'): adapter.projection_repairs=0
        if hasattr(adapter,'configure_quality'):
            adapter.source_language=self.plan.source_language
            adapter.target_language=self.plan.target_language
            adapter.configure_quality(self.plan.glossary)
        if (self.spec.streaming or {}).get('adapter')=='sherpa-tts':
            adapter.language=self.plan.target_language
            adapter.speed=self.plan.tts_speed
            adapter.sid=self.plan.tts_speaker_id
            adapter.plan['chunk_max_chars']=self.plan.tts_chunk_chars
        return adapter

class StageWorkers:
    shared_models=True
    @property
    def resident_mib(self): return self.pool.resident_mib
    def __init__(self,settings,compute,repository=None):
        from smartvoice.adapters.inference.runtime.stream_pool import SharedPool
        self.settings=settings;self.compute=compute
        self.pool=SharedPool(settings,compute,repository);self.reaper=None
    def preflight(self,specs):
        if self.settings.device!='cpu':raise StageError('runtime_unavailable','Streaming currently requires CPU')
        if find_spec('websockets') is None:raise StageError('runtime_unavailable','Install the streaming extra for WebSocket support')
        for spec in specs.values():
            if (spec.streaming or {}).get('adapter') not in ('sherpa-online','sherpa-punctuation','ctranslate2','sherpa-tts'):
                raise StageError('runtime_unavailable','No registered stage adapter')
            if spec.backend=='ctranslate2' and (find_spec('ctranslate2') is None or find_spec('sentencepiece') is None):
                raise StageError('runtime_unavailable','Install SmartVoice with the streaming extra for translation')
            if spec.backend in ('sherpa-online','sherpa-punctuation','sherpa-onnx') and find_spec('sherpa_onnx') is None:
                raise StageError('runtime_unavailable','Speech runtime dependency is missing')
    def create(self,name,profiler):
        from smartvoice.adapters.inference.runtime.stream_pool import PooledWorker
        return PooledWorker(self.pool,name,profiler)
    def binding(self,spec,plan):
        import asyncio
        if self.reaper is None:self.reaper=asyncio.create_task(self._reap())
        stage=spec.streaming['stage']
        threads=getattr(plan,stage+'_threads',1)
        key=(spec.id,stage,threads)
        if stage=='tts':key+=(plan.tts_fallback_model_id,plan.tts_fallback_speaker_id)
        ids=(spec.id,);resident=spec.streaming['resident_mib']
        if stage=='tts' and plan.tts_fallback_model_id:
            ids+=(plan.tts_fallback_model_id,)
            fallback=self.pool.repository.get_spec(plan.tts_fallback_model_id) if self.pool.repository else None
            resident+=(fallback.streaming or {}).get('resident_mib',800) if fallback else 800
        return StageBinding(self.settings,spec,plan,key,resident,ids)
    async def _reap(self):
        import asyncio
        while True:
            await asyncio.sleep(min(5,self.settings.streaming_worker_idle_seconds))
            await self.pool.reap()
    async def aclose(self):
        import asyncio
        if self.reaper:
            self.reaper.cancel();await asyncio.gather(self.reaper,return_exceptions=True)
        await self.pool.close()
    def metrics(self):return self.pool.metrics()
