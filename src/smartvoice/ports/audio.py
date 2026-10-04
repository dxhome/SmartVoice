"""Canonical audio boundaries; adapters own containers, codecs and storage."""
from dataclasses import dataclass
from typing import Protocol, ContextManager, Iterator
from smartvoice.domain.contracts import SynthesizedSpeech

# Shared offline STT policy; models can share chunks without per-model catalog settings.
DEFAULT_TRANSCRIPTION_WINDOW_SECONDS = 15.0
DEFAULT_TRANSCRIPTION_OVERLAP_SECONDS = 1.0
# sherpa-onnx Whisper language identification truncates inputs at 30 seconds;
# keep the shared sample safely below that strict upper bound.
LANGUAGE_IDENTIFICATION_SAMPLE_SECONDS = 29.0

@dataclass(frozen=True, slots=True)
class AudioWindow:
    audio: bytes
    start_seconds: float
    overlap_seconds: float

class PreparedAudio(Protocol):
    duration: float
    def sample(self, seconds: float) -> bytes: ...
    def windows(self, seconds: float) -> Iterator[AudioWindow]: ...

class AudioInput(Protocol):
    def prepare(self, audio: bytes) -> ContextManager[PreparedAudio]: ...

class AudioEncoding(Protocol):
    def validate(self, target: str) -> None: ...
    def capabilities(self) -> dict[str, object]: ...
    def encode(self, audio: SynthesizedSpeech, target: str) -> SynthesizedSpeech: ...

class AudioAssembly(Protocol):
    def session(self) -> ContextManager: ...
