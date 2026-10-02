"""Application use case for direct and language-routed speech synthesis."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from smartvoice.domain.errors import InvalidRequestError, UnsupportedFeatureError
from smartvoice.ports.inference import InferenceAvailability, InferenceProvider
from smartvoice.services.language_detection import detect_text_language, prepare_text_for_language_detection, script_language
from smartvoice.ports.model_repository import ModelRepository
from smartvoice.services.model_registry import supported_language_codes
from smartvoice.services.model_router import ModelRouter, RouterConfig

logger = logging.getLogger("smartvoice.application.speech")


@dataclass(frozen=True, slots=True)
class SpeechOutcome:
    audio: bytes
    sample_rate: int
    duration: float
    model_id: str
    language: str
    language_source: str
    language_confidence: float | None
    candidates: tuple[dict[str, object], ...]
    text_language: str | None
    runtime_wait_seconds: float


class SpeechService:
    """Resolve language and model policy, then synthesize speech."""

    def __init__(self, provider: InferenceProvider, model_router: ModelRouter, model_repository: ModelRepository) -> None:
        self.provider = provider
        self.model_router = model_router
        self.model_repository = model_repository

    def execute(
        self,
        text: str,
        requested_model: str,
        language: str,
        voice: str,
        speed: float,
        router_config: RouterConfig | None = None,
    ) -> SpeechOutcome:
        routed = requested_model == "smartvoice-auto"
        text_language = script_language(text)
        candidates: tuple[dict[str, object], ...] = ()
        confidence: float | None = None
        if language != "auto" and language not in supported_language_codes("speech"):
            raise UnsupportedFeatureError(f"Unsupported TTS language code: {language!r}.")

        if routed:
            if router_config is None:
                router_config = self.model_router.snapshot()
            if language == "auto":
                detected, confidence = detect_text_language(prepare_text_for_language_detection(text))
                logger.debug("language_detection_completed task=speech language=%s confidence=%.3f", detected, confidence)
                resolved = detected
                source = "text_detection"
            else:
                resolved = language
                source = "request"
            model_id = ""
        else:
            spec = self.model_repository.get_spec(requested_model)
            if spec.task != "speech":
                raise UnsupportedFeatureError(f"Model {requested_model!r} does not support the speech task.")
            available = (
                self.provider.inference_models() if isinstance(self.provider, InferenceAvailability)
                else self.provider.installed_models()
            )
            installed = {str(item.get("id")) for item in available if item.get("task") == "speech"}
            if spec.id not in installed:
                from smartvoice.domain.errors import ModelUnavailableError

                raise ModelUnavailableError(f"Model {requested_model!r} is not installed or not available in the active provider.")
            script = text_language
            if language not in {"auto", *spec.languages}:
                raise UnsupportedFeatureError(f"The selected TTS model supports: {', '.join(spec.languages)}.")
            if language != "auto" and script in {"zh", "ja", "ko"} and language != script:
                raise InvalidRequestError(f"The input text appears to be {script}, which conflicts with requested language {language}.")
            if language == "auto":
                resolved = script or (spec.languages[0] if len(spec.languages) == 1 else "auto")
                if resolved == "auto" and spec.model_type == "kokoro":
                    detected, _ = detect_text_language(prepare_text_for_language_detection(text))
                    resolved = detected if detected in spec.languages else "en"
            else:
                resolved = language
            if resolved != "auto" and resolved not in spec.languages:
                raise UnsupportedFeatureError(f"The selected TTS model does not support language {resolved!r}.")
            model_id = spec.id
            source = "request" if language != "auto" else "model_inference"

        if routed:
            assert router_config is not None
            available = (
                self.provider.inference_models() if isinstance(self.provider, InferenceAvailability)
                else self.provider.installed_models()
            )
            model_id, states = self.model_router.choose("speech", resolved, available, config=router_config)
            candidates = tuple(states)
        synthesized = self.provider.synthesize(text, voice, speed, model_id, resolved)
        return SpeechOutcome(
            synthesized.audio,
            synthesized.sample_rate,
            synthesized.duration,
            model_id,
            resolved,
            source,
            confidence,
            candidates,
            text_language,
            synthesized.runtime_wait_seconds,
        )
