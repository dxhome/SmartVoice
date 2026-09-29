"""Typed, provider-neutral values exchanged between the application and adapters."""

from dataclasses import dataclass
from typing import TypedDict


class InstalledModel(TypedDict, total=False):
    id: str
    name: str
    task: str
    languages: list[str]
    backend: str
    installed: bool
    installed_size_bytes: int
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


class ProviderCapabilityDocument(TypedDict, total=False):
    api_version: str
    capability_schema_version: str
    backend: str
    backends: list[str]
    tasks: list[dict[str, object]]
    language_identification: dict[str, object]
    router: dict[str, object]


@dataclass(frozen=True, slots=True)
class SynthesizedSpeech:
    audio: bytes
    sample_rate: int
    duration: float
    runtime_wait_seconds: float = 0.0


class LanguageIdentificationResult(TypedDict, total=False):
    language: str
    model: str
    processing_seconds: float
    runtime_wait_seconds: float
    confidence: float
