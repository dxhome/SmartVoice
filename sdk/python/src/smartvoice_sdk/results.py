"""Response objects for STT and TTS REST calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    language: str | None
    model: str | None
    requested_model: str
    requested_language: str
    response_format: str
    raw: Mapping[str, Any]
    headers: Mapping[str, str]

    @property
    def segments(self) -> tuple[Mapping[str, Any], ...]:
        rows = self.raw.get("segments", ())
        return tuple(row for row in rows if isinstance(row, Mapping)) if isinstance(rows, (list, tuple)) else ()


@dataclass(frozen=True)
class SpeechResult:
    audio: bytes
    format: str
    content_type: str
    model: str | None
    requested_model: str
    duration_seconds: float | None
    sample_rate: int | None
    segments: int | None
    headers: Mapping[str, str]
