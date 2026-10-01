"""CPU adapter for the catalogued Qwen3-TTS CustomVoice model."""

from __future__ import annotations

import io
import wave

from smartvoice.adapters.inference.qwen_tts.native_runtime import QwenNativeRuntime
from smartvoice.config.settings import Settings
from smartvoice.domain.contracts import InstalledModel, SynthesizedSpeech
from smartvoice.domain.errors import (
    InferenceError,
    ModelUnavailableError,
    SpeechOutputTooLargeError,
    UnsupportedFeatureError,
)
from smartvoice.ports.model_repository import ModelRepository
from smartvoice.adapters.storage.catalog_model_repository import CatalogModelRepository

MODEL_ID = "tts-qwen3-0-6b-customvoice"
BACKEND = "qwen-tts"
NATIVE_ENGINE_VERSION = "ef339be58a778b062e1c14382347964552eae007"
SPEAKERS = {
    "aiden": "Aiden",
    "dylan": "Dylan",
    "eric": "Eric",
    "ono_anna": "Ono_Anna",
    "ryan": "Ryan",
    "serena": "Serena",
    "sohee": "Sohee",
    "uncle_fu": "Uncle_Fu",
    "vivian": "Vivian",
}
LANGUAGES = {
    "de": "German",
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "pt": "Portuguese",
    "ru": "Russian",
    "zh": "Chinese",
}
DEFAULT_SPEAKER = "Vivian"


class QwenTTSProvider:
    """Lazily loads Qwen3-TTS and translates it to SmartVoice's speech contract."""

    backend = BACKEND

    def __init__(self, settings: Settings, model_repository: ModelRepository | None = None) -> None:
        self.settings = settings
        self.model_repository = model_repository or CatalogModelRepository(settings)
        self._native_runtime = QwenNativeRuntime(settings, self.model_repository, backend=self.backend)

    def installed_models(self) -> list[InstalledModel]:
        if self.settings.device != "cpu":
            return []
        if self._native_runtime.binary_path() is None:
            return []
        return self._catalogued_models()

    def _catalogued_models(self) -> list[InstalledModel]:
        return [
            model for model in self.model_repository.installed_models()
            if model.get("backend") == self.backend
        ]

    def runtime(self) -> dict[str, object]:
        if self.settings.device != "cpu":
            return {
                "backend": self.backend,
                "requested_device": self.settings.device,
                "actual_device": None,
                "provider_status": "unsupported",
                "runtime_version": None,
                "installed_model_count": 0,
                "reason": "Qwen3-TTS currently supports CPU inference in SmartVoice.",
            }
        binary = self._native_runtime.binary_path()
        available = binary is not None
        return {
            "backend": self.backend,
            "requested_device": "cpu",
            "actual_device": "cpu" if available else None,
            "provider_status": "available" if available else "dependency_missing",
            "runtime_version": f"native-c:{NATIVE_ENGINE_VERSION[:12]}" if available else None,
            "installed_model_count": len(self.installed_models()),
            "reason": None if available else "The native C INT8 Qwen3-TTS runtime is not installed for this platform.",
        }

    def capabilities(self) -> dict[str, object]:
        tasks = []
        runtime_available = bool(self.installed_models())
        for model in self._catalogued_models():
            tasks.append({
                "task": model["task"],
                "model": model["id"],
                "backend": self.backend,
                "languages": model["languages"],
                "streaming": False,
                "voices": ["default", *SPEAKERS.values()],
                "voice_count": len(SPEAKERS),
                "speed_control": False,
                "available": runtime_available,
            })
        return {
            "api_version": "v1",
            "capability_schema_version": "1.0",
            "backend": self.backend,
            "tasks": tasks,
        }

    def transcribe(self, audio: bytes, language: str = "auto", model_id: str | None = None):
        raise UnsupportedFeatureError("Qwen3-TTS does not support transcription.")

    def synthesize(
        self,
        text: str,
        voice: str = "default",
        speed: float = 1.0,
        model_id: str | None = None,
        language: str = "auto",
    ) -> SynthesizedSpeech:
        canonical_id = model_id or MODEL_ID
        spec = self.model_repository.get_spec(canonical_id)
        if spec.backend != self.backend or spec.model_type != "qwen3_tts":
            raise UnsupportedFeatureError(f"Model {canonical_id!r} is not supported by the Qwen3-TTS adapter.")
        if self.settings.device != "cpu":
            raise UnsupportedFeatureError("Qwen3-TTS currently supports CPU inference in SmartVoice.")
        if speed != 1.0:
            raise UnsupportedFeatureError("Qwen3-TTS does not support speed adjustment; use speed 1.0.")
        qwen_language = "Auto" if language == "auto" else LANGUAGES.get(language)
        if qwen_language is None:
            raise UnsupportedFeatureError(f"Qwen3-TTS does not support language {language!r}.")
        speaker = DEFAULT_SPEAKER if voice == "default" else SPEAKERS.get(voice.casefold())
        if speaker is None:
            available = ", ".join(SPEAKERS.values())
            raise UnsupportedFeatureError(f"Qwen3-TTS voice must be default or one of: {available}.")

        return self._synthesize_native(canonical_id, text, speaker, qwen_language)

    def is_model_loaded(self, model_id: str) -> bool:
        return model_id == MODEL_ID and self._native_runtime.is_running()

    def close(self) -> None:
        """Stop the lazily started native engine during application shutdown."""
        self._native_runtime.close()

    def _synthesize_native(self, model_id: str, text: str, speaker: str, language: str) -> SynthesizedSpeech:
        if model_id not in {str(model.get("id")) for model in self.installed_models()}:
            raise ModelUnavailableError(
                f"Model {model_id!r} is not installed or failed integrity validation. "
                f"Install it with `python -m smartvoice models install {model_id}`."
            )
        audio, runtime_wait = self._native_runtime.synthesize(model_id, text, speaker, language)
        if len(audio) > self.settings.max_tts_output_bytes:
            raise SpeechOutputTooLargeError("Synthesized speech exceeds the configured audio output limit.")
        try:
            with wave.open(io.BytesIO(audio), "rb") as wav:
                sample_rate = wav.getframerate()
                if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or sample_rate <= 0:
                    raise ValueError("The native runtime returned an unsupported WAV format.")
                duration = wav.getnframes() / sample_rate
        except (wave.Error, EOFError, ValueError, ZeroDivisionError) as exc:
            raise InferenceError(
                "The native Qwen3-TTS engine returned invalid WAV audio.", detail=type(exc).__name__
            ) from exc
        if duration > self.settings.max_tts_audio_seconds:
            raise SpeechOutputTooLargeError("Synthesized speech exceeds the configured audio duration limit.")
        return SynthesizedSpeech(
            audio=audio, sample_rate=sample_rate, duration=duration,
            runtime_wait_seconds=runtime_wait,
        )
