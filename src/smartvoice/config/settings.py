"""Environment-backed configuration with loopback defaults."""

from __future__ import annotations

import os
import json
import math
import sys
from dataclasses import dataclass, fields
from pathlib import Path

from smartvoice.services.builtin_resources import read_builtin_json


def _ensure_user_config(path: Path) -> None:
    """Create the editable per-user settings file once without replacing user data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(read_builtin_json("smartvoice.json"))
    except FileExistsError:
        pass


def default_data_dir() -> Path:
    explicit = os.environ.get("SMARTVOICE_HOME")
    if explicit:
        return Path(explicit).expanduser().resolve()
    if os.name == "posix" and sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "SmartVoice"
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
    max_tts_internal_bytes: int = 32 * 1024 * 1024
    max_tts_json_bytes: int = 64 * 1024
    tts_mp3_bitrate: int = 96000
    num_threads: int = max(1, min(2, os.cpu_count() or 1))
    provider: str = "cpu"
    max_concurrent_inference: int = 1
    max_queued_inference: int = 4
    min_instances: int = 1
    max_instances: int = 2
    model_availability_ttl_seconds: float = 600.0
    instance_idle_seconds: float = 300.0
    inference_queue_timeout_seconds: float = 60.0
    inference_execution_timeout_seconds: float = 600.0

    def __post_init__(self):
        if self.tts_mp3_bitrate not in {64000, 96000, 128000}:
            raise ValueError("tts_mp3_bitrate must be 64000, 96000 or 128000")
        for name in ("max_upload_bytes", "max_audio_seconds", "max_tts_characters", "max_tts_audio_seconds", "max_tts_output_bytes", "max_tts_internal_bytes", "max_tts_json_bytes"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not 1 <= self.min_instances <= self.max_instances:
            raise ValueError("Require 1 <= min_instances <= max_instances")
        if not math.isfinite(self.instance_idle_seconds) or self.instance_idle_seconds <= 0:
            raise ValueError("instance_idle_seconds must be finite and positive")
        if not math.isfinite(self.model_availability_ttl_seconds) or self.model_availability_ttl_seconds <= 0:
            raise ValueError("model_availability_ttl_seconds must be finite and positive")
        if self.max_concurrent_inference not in (0, 1):
            raise ValueError("max_concurrent_inference must be 0 (serial) or 1 (parallel enabled)")

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def device(self) -> str:
        """Inference execution device (kept under the legacy ``provider`` config key)."""
        return self.provider

    @classmethod
    def from_env(
        cls,
        config_path: Path | None = None,
        data_dir_override: Path | None = None,
        *,
        initialize_user_config: bool = False,
    ) -> "Settings":
        explicit_config = config_path or (Path(os.environ["SMARTVOICE_CONFIG"]) if os.environ.get("SMARTVOICE_CONFIG") else None)
        initial_data_dir = (
            data_dir_override
            or (Path(os.environ["SMARTVOICE_HOME"]).expanduser() if os.environ.get("SMARTVOICE_HOME") else default_data_dir())
        ).expanduser().resolve()
        selected_config = explicit_config or initial_data_dir / "smartvoice.json"
        if explicit_config is None and (selected_config.is_file() or initialize_user_config):
            _ensure_user_config(selected_config)
        config: dict[str, object] = {}
        if explicit_config is not None or selected_config.is_file():
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
            data_dir=(data_dir_override or Path(setting("data_dir", "SMARTVOICE_HOME", default_data_dir(), Path))).expanduser().resolve(),
            server_host=setting("server_host", "SMARTVOICE_HOST", "127.0.0.1", str),
            server_port=setting("server_port", "SMARTVOICE_PORT", 8000, int),
            log_level=setting("log_level", "SMARTVOICE_LOG_LEVEL", "INFO", str).upper(),
            max_upload_bytes=setting("max_upload_bytes", "SMARTVOICE_MAX_UPLOAD_BYTES", 25 * 1024 * 1024, int),
            max_audio_seconds=setting("max_audio_seconds", "SMARTVOICE_MAX_AUDIO_SECONDS", 600, float),
            max_tts_characters=setting("max_tts_characters", "SMARTVOICE_MAX_TTS_CHARACTERS", 4000, int),
            max_tts_audio_seconds=setting("max_tts_audio_seconds", "SMARTVOICE_MAX_TTS_AUDIO_SECONDS", 180, float),
            max_tts_output_bytes=setting("max_tts_output_bytes", "SMARTVOICE_MAX_TTS_OUTPUT_BYTES", 32 * 1024 * 1024, int),
            max_tts_internal_bytes=setting("max_tts_internal_bytes", "SMARTVOICE_MAX_TTS_INTERNAL_BYTES", config.get("max_tts_output_bytes", 32 * 1024 * 1024), int),
            max_tts_json_bytes=setting("max_tts_json_bytes", "SMARTVOICE_MAX_TTS_JSON_BYTES", 64 * 1024, int),
            tts_mp3_bitrate=setting("tts_mp3_bitrate", "SMARTVOICE_TTS_MP3_BITRATE", 96000, int),
            num_threads=max(1, setting("num_threads", "SMARTVOICE_NUM_THREADS", max(1, min(2, os.cpu_count() or 1)), int)),
            min_instances=setting("min_instances", "SMARTVOICE_MIN_INSTANCES", 1, int),
            max_instances=setting("max_instances", "SMARTVOICE_MAX_INSTANCES", 2, int),
            model_availability_ttl_seconds=setting(
                "model_availability_ttl_seconds", "SMARTVOICE_MODEL_AVAILABILITY_TTL_SECONDS", 600.0, float,
            ),
            instance_idle_seconds=setting("instance_idle_seconds", "SMARTVOICE_INSTANCE_IDLE_SECONDS", 300.0, float),
            provider=setting("provider", "SMARTVOICE_DEVICE", "cpu", str),
            max_concurrent_inference=setting("max_concurrent_inference", "SMARTVOICE_MAX_CONCURRENT_INFERENCE", 1, int),
            max_queued_inference=max(0, setting("max_queued_inference", "SMARTVOICE_MAX_QUEUED_INFERENCE", 4, int)),
            inference_queue_timeout_seconds=max(0.1, setting("inference_queue_timeout_seconds", "SMARTVOICE_INFERENCE_QUEUE_TIMEOUT", 60, float)),
            inference_execution_timeout_seconds=max(0.1, setting("inference_execution_timeout_seconds", "SMARTVOICE_INFERENCE_EXECUTION_TIMEOUT", 600, float)),
        )
        if settings.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("log_level must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
        if not 1 <= settings.server_port <= 65535:
            raise ValueError("server_port must be between 1 and 65535")
        if initialize_user_config:
            _ensure_user_config(settings.data_dir / "smartvoice.json")
        return settings
