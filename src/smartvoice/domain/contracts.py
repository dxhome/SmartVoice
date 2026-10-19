"""Typed, provider-neutral values exchanged between the application and adapters."""

from dataclasses import dataclass
from typing import TypedDict

from smartvoice.domain.capabilities import ProviderCapabilityDocument


class InstalledModel(TypedDict, total=False):
    id: str
    name: str
    task: str
    category: str
    subcategory: str
    languages: list[str]
    backend: str
    installed: bool
    installed_size_bytes: int
    estimated_size_bytes: int
    license_note: str


class TranscriptionResult(TypedDict, total=False):
    text: str
    language: str | None
    duration: float
    model: str
    device: str
    processing_seconds: float
    runtime_wait_seconds: float
    rtf: float | None
    segments: list[dict[str, object]]
    chunk_count: int


@dataclass(frozen=True, slots=True)
class SynthesizedSpeech:
    """Native adapters emit mono PCM16 WAV; centralized encoding sets the final format."""
    audio: bytes
    sample_rate: int
    duration: float
    runtime_wait_seconds: float = 0.0
    audio_format: str = "wav"


class LanguageIdentificationResult(TypedDict, total=False):
    language: str
    model: str
    processing_seconds: float
    runtime_wait_seconds: float
    confidence: float
