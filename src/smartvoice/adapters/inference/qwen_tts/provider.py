"""CPU adapter for the catalogued Qwen3-TTS CustomVoice model."""

from __future__ import annotations

import importlib.metadata
import io
import threading
import time
import wave

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
MAX_CODEC_TOKENS = 8192


class QwenTTSProvider:
    """Lazily loads Qwen3-TTS and translates it to SmartVoice's speech contract."""

    backend = BACKEND

    def __init__(self, settings: Settings, model_repository: ModelRepository | None = None) -> None:
        self.settings = settings
        self.model_repository = model_repository or CatalogModelRepository(settings)
        self._load_lock = threading.Lock()
        self._inference_lock = threading.Lock()
        self._model = None

    def installed_models(self) -> list[InstalledModel]:
        if self.settings.provider != "cpu" or self._runtime_version() is None:
            return []
        return self._catalogued_models()

    def _catalogued_models(self) -> list[InstalledModel]:
        return [
            model for model in self.model_repository.installed_models()
            if model.get("backend") == self.backend
        ]

    @staticmethod
    def _runtime_version() -> str | None:
        try:
            return importlib.metadata.version("qwen-tts")
        except importlib.metadata.PackageNotFoundError:
            return None

    def runtime(self) -> dict[str, object]:
        runtime_version = self._runtime_version()
        available = self.settings.provider == "cpu" and runtime_version is not None
        if runtime_version is None:
            status = "dependency_missing"
            reason = "Install the optional Qwen3-TTS dependencies with `pip install 'smartvoice[qwen-tts]'`."
        elif self.settings.provider != "cpu":
            status = "unsupported"
            reason = "Qwen3-TTS currently supports CPU inference in SmartVoice."
        else:
            status = "available"
            reason = None
        return {
            "backend": self.backend,
            "requested_device": self.settings.provider,
            "actual_device": "cpu" if available else None,
            "provider_status": status,
            "runtime_version": runtime_version,
            "installed_model_count": len(self.installed_models()),
            "reason": reason,
        }

    def capabilities(self) -> dict[str, object]:
        tasks = []
        runtime_available = self.settings.provider == "cpu" and self._runtime_version() is not None
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
        if self.settings.provider != "cpu":
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

        model = self._get_model(canonical_id)
        runtime_wait_started = time.perf_counter()
        self._inference_lock.acquire()
        runtime_wait = time.perf_counter() - runtime_wait_started
        try:
            max_new_tokens = min(
                MAX_CODEC_TOKENS,
                max(32, int(self.settings.max_tts_audio_seconds * 12.5) + 16),
            )
            try:
                waveforms, sample_rate = model.generate_custom_voice(
                    text=text,
                    speaker=speaker,
                    language=qwen_language,
                    max_new_tokens=max_new_tokens,
                )
            except Exception as exc:
                raise InferenceError("Qwen3-TTS speech synthesis failed.", detail=type(exc).__name__) from exc
        finally:
            self._inference_lock.release()

        if not waveforms:
            raise InferenceError("Qwen3-TTS returned no audio.")
        try:
            import numpy as np

            samples = np.asarray(waveforms[0], dtype=np.float32).reshape(-1)
            sample_rate = int(sample_rate)
            if samples.size == 0 or sample_rate <= 0:
                raise ValueError("The runtime returned empty audio or an invalid sample rate.")
            duration = samples.size / sample_rate
            pcm_bytes = samples.size * 2 + 44
            if duration > self.settings.max_tts_audio_seconds or pcm_bytes > self.settings.max_tts_output_bytes:
                raise SpeechOutputTooLargeError("Synthesized speech exceeds the configured audio output limit.")
            pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2", copy=False)
            output = io.BytesIO()
            with wave.open(output, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(sample_rate)
                wav.writeframes(pcm.tobytes())
        except SpeechOutputTooLargeError:
            raise
        except Exception as exc:
            raise InferenceError("Could not encode Qwen3-TTS audio output.", detail=type(exc).__name__) from exc

        return SynthesizedSpeech(
            audio=output.getvalue(),
            sample_rate=sample_rate,
            duration=duration,
            runtime_wait_seconds=runtime_wait,
        )

    def is_model_loaded(self, model_id: str) -> bool:
        return model_id == MODEL_ID and self._model is not None

    def _get_model(self, model_id: str):
        if self._model is not None:
            return self._model
        if model_id not in {str(model.get("id")) for model in self.installed_models()}:
            raise ModelUnavailableError(
                f"Model {model_id!r} is not installed or failed integrity validation. "
                f"Install it with `python -m smartvoice models install {model_id}`."
            )
        model_dir = self.model_repository.model_directory(model_id)
        with self._load_lock:
            if self._model is not None:
                return self._model
            try:
                import torch
                from qwen_tts import Qwen3TTSModel
            except ImportError as exc:
                raise UnsupportedFeatureError(
                    "Qwen3-TTS dependencies are not installed. Install SmartVoice with the [qwen-tts] extra."
                ) from exc
            try:
                torch.set_num_threads(self.settings.num_threads)
                self._model = Qwen3TTSModel.from_pretrained(
                    str(model_dir),
                    device_map="cpu",
                    dtype=torch.float32,
                )
            except Exception as exc:
                raise InferenceError("Could not load the installed Qwen3-TTS model.", detail=type(exc).__name__) from exc
            return self._model
