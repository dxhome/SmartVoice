"""Typed, provider-neutral model capability contracts.

Adapters construct these values from catalog metadata plus runtime-specific
support. The API layer is the only place that serializes them to JSON.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypedDict

TaskName = Literal["transcription", "speech"]


class SegmentLimitsDocument(TypedDict, total=False):
    characters: int
    audio_seconds: float


class TaskCapabilityDocument(TypedDict, total=False):
    task: TaskName
    model: str
    backend: str
    languages: list[str]
    streaming: bool
    available: bool
    voices: list[str]
    voice_count: int
    speed_control: bool
    generation_formats: list[str]
    segmentation: SegmentLimitsDocument


class ProviderCapabilityDocument(TypedDict):
    api_version: str
    capability_schema_version: str
    backend: str
    backends: list[str]
    tasks: list[TaskCapabilityDocument]


@dataclass(frozen=True, slots=True)
class SegmentLimits:
    characters: int | None = None
    audio_seconds: float | None = None

    def to_document(self) -> SegmentLimitsDocument:
        document: SegmentLimitsDocument = {}
        if self.characters is not None and self.characters > 0:
            document["characters"] = self.characters
        if self.audio_seconds is not None and self.audio_seconds > 0:
            document["audio_seconds"] = self.audio_seconds
        return document


@dataclass(frozen=True, slots=True)
class TaskCapability:
    task: TaskName
    model_id: str
    backend: str
    languages: tuple[str, ...]
    streaming: bool = False
    available: bool | None = None
    voices: tuple[str, ...] | None = None
    voice_count: int | None = None
    speed_control: bool | None = None
    generation_formats: tuple[str, ...] = ()
    segmentation: SegmentLimits | None = None

    @classmethod
    def from_model_spec(cls, spec, **overrides) -> "TaskCapability":
        """Build the provider-neutral portion from catalog metadata."""
        if spec.task not in ("transcription", "speech"):
            raise ValueError(f"Unsupported model task: {spec.task!r}")
        return cls(
            task=spec.task, model_id=spec.id, backend=spec.backend,
            languages=tuple(spec.languages), **overrides,
        )

    def supports_language(self, language: str) -> bool:
        return language in self.languages

    def to_document(self) -> TaskCapabilityDocument:
        document: TaskCapabilityDocument = {
            "task": self.task,
            "model": self.model_id,
            "backend": self.backend,
            "languages": list(self.languages),
            "streaming": self.streaming,
        }
        if self.available is not None:
            document["available"] = self.available
        if self.voices is not None:
            document["voices"] = list(self.voices)
        if self.voice_count is not None:
            document["voice_count"] = self.voice_count
        if self.speed_control is not None:
            document["speed_control"] = self.speed_control
        if self.generation_formats:
            document["generation_formats"] = list(self.generation_formats)
        if self.segmentation is not None:
            limits = self.segmentation.to_document()
            if limits:
                document["segmentation"] = limits
        return document


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    backend: str
    tasks: tuple[TaskCapability, ...]
    backends: tuple[str, ...] = ()
    schema_version: str = "1.0"

    def to_document(self) -> ProviderCapabilityDocument:
        return {
            "api_version": "v1",
            "capability_schema_version": self.schema_version,
            "backend": self.backend,
            "backends": list(self.backends or (self.backend,)),
            "tasks": [task.to_document() for task in self.tasks],
        }
