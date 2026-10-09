"""Composition of registered adapters; shared flows never inspect model families."""
from functools import partial
from importlib.util import find_spec
from smartvoice.domain.streaming import StageError
from smartvoice.adapters.inference.runtime.stream_workers import ProcessAffinityWorker


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

class StageWorkers:
    def __init__(self,settings,compute):self.settings=settings;self.compute=compute
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
        return BudgetedWorker(name,profiler,self.settings.streaming_cancel_grace_seconds,self.compute)
    def binding(self,spec,plan):return partial(construct_stage,self.settings,spec,plan)

class BudgetedWorker(ProcessAffinityWorker):
    def __init__(self,name,profiler,grace,compute):super().__init__(name,profiler,grace);self.compute=compute
    async def call(self,function,*args,**kwargs):
        import time
        from smartvoice.domain.errors import InferenceOverloadedError
        if function.__name__=='close':
            return await super().call(function,*args,**kwargs)
        began=time.monotonic()
        try:
            async with self.compute.async_permit(priority=0 if self.name=='asr' else 1,timeout=kwargs.get('deadline') or 30):
                self.profiler.add(self.name+'.compute_admission_wait',time.monotonic()-began)
                return await super().call(function,*args,**kwargs)
        except InferenceOverloadedError as exc:
            raise StageError('scheduler_overload','Shared compute admission unavailable',self.name) from exc
