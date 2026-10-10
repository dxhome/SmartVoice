"""Transport-neutral stage records and ports. No native/runtime objects in records."""
from dataclasses import dataclass, asdict, field
from typing import Mapping

class StageError(Exception):
    def __init__(self, code: str, message: str, stage: str | None=None):
        self.code,self.message,self.stage=code,message,stage
        super().__init__(message)

@dataclass(frozen=True)
class AudioBlock:
    sequence: int
    pcm: bytes
    start_sample: int
    received_at: float

@dataclass(frozen=True)
class Hypothesis:
    kind: str
    utterance_id: int
    revision: int
    text: str
    language: str
    start_sample: int
    end_sample: int
    final: bool
    reason: str|None=None
    context_version: int=0

    @classmethod
    def from_adapter(cls, row: Mapping, language: str):
        return cls(row['type'],row.get('utterance_id',0),row.get('revision',0),row.get('text',''),
                   row.get('language',language),row.get('start_sample',0),row.get('end_sample',0),
                   row.get('final',row['type']=='source_final'),row.get('reason'),row.get('context_version',0))

    def event(self):
        row=asdict(self);row['type']=row.pop('kind');return row

@dataclass(frozen=True)
class SourceRef:
    utterance_id: int
    revision: int
    event_sequence: int
    text_start: int
    text_end: int
    start_sample: int
    end_sample: int

@dataclass(frozen=True)
class TextUnit:
    unit_id: int
    revision: int
    raw_text: str
    text: str
    language: str
    committed: bool
    refs: tuple[SourceRef,...]
    reason: str
    incomplete: bool=False

    def event(self):
        row=asdict(self)
        row.update(type='source_unit_final' if self.committed else 'source_unit_partial',
                   final=self.committed,start_sample=min(r.start_sample for r in self.refs),
                   end_sample=max(r.end_sample for r in self.refs))
        return row

@dataclass(frozen=True)
class TranslationRequest:
    unit: TextUnit
    source_language: str
    target_language: str
    submitted_at: float

@dataclass(frozen=True)
class TranslationResult:
    text: str
    source_tokens: int|None=None
    target_tokens: int|None=None
    translation_input: str|None=None
    normalizations: tuple=()
    quality_issues: tuple=()

@dataclass(frozen=True)
class CommittedTarget:
    """Future synthesis input: an immutable accepted translation, never a draft."""
    session_id: str
    translation_id: str
    unit_id: int
    revision: int
    language: str
    text: str
    refs: tuple[SourceRef,...]
    committed: bool=True
    source_language: str='zh'
    source_unit_revision: int=1
    boundary_reason: str='semantic_final'
    incomplete: bool=False

    def __post_init__(self):
        if not self.committed or not self.text.strip() or self.revision < 1 or self.source_unit_revision < 1 or not self.refs:
            raise StageError('tts_input_not_committed','Synthesis requires nonempty committed text and source alignment')

@dataclass(frozen=True)
class SynthesizedAudio:
    translation_id: str
    language: str
    pcm: bytes
    sample_rate: int
    channels: int
    committed: bool=True
    profile: str|None=None
