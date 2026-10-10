"""Scenario workflows compose stage capabilities; model selection stays in plans."""
from dataclasses import dataclass

@dataclass(frozen=True)
class Workflow:
    mode: str
    stages: tuple[str, ...]
    first_output: str
    def validate(self, specs):
        from smartvoice.domain.streaming import StageError
        if any(stage not in specs for stage in self.stages):
            raise StageError('invalid_plan','A workflow stage is missing')

    def text_pipeline(self, owner, translator, plan):
        from .incremental import IncrementalPipeline
        return IncrementalPipeline(owner,translator,plan)

    def speech_pipeline(self, owner, packager):
        if 'tts' not in self.stages: return None
        from .speech import SpeechPipeline
        return SpeechPipeline(owner,packager)

WORKFLOWS={
    'transcription':Workflow('transcription',('asr','formatting'),'source_unit_partial'),
    'translation':Workflow('translation',('asr','formatting','translation'),'target_partial'),
    'speech':Workflow('speech',('asr','formatting','translation','tts'),'audio_segment'),
}
