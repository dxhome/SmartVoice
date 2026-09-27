"""Environment-backed configuration with loopback-safe defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def default_data_dir() -> Path:
    explicit = os.environ.get("SMARTVOICE_HOME")
    if explicit:
        return Path(explicit).expanduser().resolve()
    if os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "SmartVoice"
    return Path.home() / ".smartvoice"


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    max_upload_bytes: int = 25 * 1024 * 1024
    max_audio_seconds: float = 600
    max_tts_characters: int = 4000
    num_threads: int = max(1, min(4, os.cpu_count() or 1))
    provider: str = "cpu"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=default_data_dir(),
            max_upload_bytes=int(os.environ.get("SMARTVOICE_MAX_UPLOAD_BYTES", 25 * 1024 * 1024)),
            max_audio_seconds=float(os.environ.get("SMARTVOICE_MAX_AUDIO_SECONDS", 600)),
            max_tts_characters=int(os.environ.get("SMARTVOICE_MAX_TTS_CHARACTERS", 4000)),
            num_threads=max(1, int(os.environ.get("SMARTVOICE_NUM_THREADS", max(1, min(4, os.cpu_count() or 1))))),
            provider=os.environ.get("SMARTVOICE_DEVICE", "cpu"),
        )
