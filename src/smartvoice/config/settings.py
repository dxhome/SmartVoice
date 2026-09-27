"""Environment-backed configuration with loopback-safe defaults."""

from __future__ import annotations

import os
import json
from dataclasses import dataclass, fields
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
    server_host: str = "127.0.0.1"
    server_port: int = 8000
    log_level: str = "INFO"
    max_upload_bytes: int = 25 * 1024 * 1024
    max_audio_seconds: float = 600
    max_tts_characters: int = 4000
    max_tts_audio_seconds: float = 180
    max_tts_output_bytes: int = 32 * 1024 * 1024
    num_threads: int = max(1, min(4, os.cpu_count() or 1))
    provider: str = "cpu"
    max_concurrent_inference: int = 1
    max_queued_inference: int = 2
    inference_queue_timeout_seconds: float = 60.0
    inference_execution_timeout_seconds: float = 600.0

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @classmethod
    def from_env(cls, config_path: Path | None = None) -> "Settings":
        selected_config = config_path or (Path(os.environ["SMARTVOICE_CONFIG"]) if os.environ.get("SMARTVOICE_CONFIG") else None)
        config: dict[str, object] = {}
        if selected_config:
            config = json.loads(selected_config.read_text(encoding="utf-8"))
            if not isinstance(config, dict):
                raise ValueError("SmartVoice configuration must be a JSON object")
            allowed = {field.name for field in fields(cls)}
            unknown = sorted(set(config) - allowed)
            if unknown:
                raise ValueError(f"Unknown SmartVoice configuration key(s): {', '.join(unknown)}")

        def setting(key: str, env_name: str, default: object, cast):
            value = os.environ.get(env_name, config.get(key, default))
            return cast(value)

        settings = cls(
            data_dir=Path(setting("data_dir", "SMARTVOICE_HOME", default_data_dir(), Path)).expanduser().resolve(),
            server_host=setting("server_host", "SMARTVOICE_HOST", "127.0.0.1", str),
            server_port=setting("server_port", "SMARTVOICE_PORT", 8000, int),
            log_level=setting("log_level", "SMARTVOICE_LOG_LEVEL", "INFO", str).upper(),
            max_upload_bytes=setting("max_upload_bytes", "SMARTVOICE_MAX_UPLOAD_BYTES", 25 * 1024 * 1024, int),
            max_audio_seconds=setting("max_audio_seconds", "SMARTVOICE_MAX_AUDIO_SECONDS", 600, float),
            max_tts_characters=setting("max_tts_characters", "SMARTVOICE_MAX_TTS_CHARACTERS", 4000, int),
            max_tts_audio_seconds=setting("max_tts_audio_seconds", "SMARTVOICE_MAX_TTS_AUDIO_SECONDS", 180, float),
            max_tts_output_bytes=setting("max_tts_output_bytes", "SMARTVOICE_MAX_TTS_OUTPUT_BYTES", 32 * 1024 * 1024, int),
            num_threads=max(1, setting("num_threads", "SMARTVOICE_NUM_THREADS", max(1, min(4, os.cpu_count() or 1)), int)),
            provider=setting("provider", "SMARTVOICE_DEVICE", "cpu", str),
            max_concurrent_inference=max(1, setting("max_concurrent_inference", "SMARTVOICE_MAX_CONCURRENT_INFERENCE", 1, int)),
            max_queued_inference=max(0, setting("max_queued_inference", "SMARTVOICE_MAX_QUEUED_INFERENCE", 2, int)),
            inference_queue_timeout_seconds=max(0.1, setting("inference_queue_timeout_seconds", "SMARTVOICE_INFERENCE_QUEUE_TIMEOUT", 60, float)),
            inference_execution_timeout_seconds=max(0.1, setting("inference_execution_timeout_seconds", "SMARTVOICE_INFERENCE_EXECUTION_TIMEOUT", 600, float)),
        )
        if settings.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("log_level must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
        if not 1 <= settings.server_port <= 65535:
            raise ValueError("server_port must be between 1 and 65535")
        return settings
