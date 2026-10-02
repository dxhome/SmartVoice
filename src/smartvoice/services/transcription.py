"""Application use case for direct and language-routed transcription."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence

from smartvoice.domain.contracts import InstalledModel, TranscriptionResult
from smartvoice.domain.errors import InvalidAudioError, ModelUnavailableError, UnsupportedFeatureError
from smartvoice.ports.inference import InferenceAvailability, InferenceProvider, LanguageIdentifier
from smartvoice.ports.model_repository import ModelRepository
from smartvoice.services.model_router import ModelRouter, RouterConfig

logger = logging.getLogger("smartvoice.application.transcription")


@dataclass(frozen=True, slots=True)
class TranscriptionOutcome:
    result: TranscriptionResult
    model_id: str
    language: str | None
    candidates: tuple[dict[str, object], ...]
    language_source: str


class TranscriptionService:
    """Select a supported model and execute transcription without HTTP dependencies."""

    def __init__(
        self,
        provider: InferenceProvider,
        model_router: ModelRouter,
        model_repository: ModelRepository,
        language_identifier: LanguageIdentifier | None = None,
    ) -> None:
        self.provider = provider
        self.model_router = model_router
        self.model_repository = model_repository
        self.language_identifier = language_identifier

    def execute(
        self,
        audio: bytes,
        requested_model: str,
        language: str,
        router_config: RouterConfig | None = None,
    ) -> TranscriptionOutcome:
        routed = requested_model == "smartvoice-auto"
        installed: Sequence[InstalledModel] = (
            self.provider.inference_models() if isinstance(self.provider, InferenceAvailability)
            else self.provider.installed_models()
        )
        candidates: tuple[dict[str, object], ...] = ()
        language_source = "request" if language != "auto" else "model_detection"

        if not routed:
            spec = self.model_repository.get_spec(requested_model)
            self._validate_model(spec, "transcription", installed)
            if language not in {"auto", *spec.languages}:
                raise UnsupportedFeatureError(f"The selected STT model does not support language {language!r}.")
            result = self.provider.transcribe(audio, language, spec.id)
            resolved = (result.get("language") if language == "auto" else language) or None
            return TranscriptionOutcome(result, spec.id, str(resolved) if resolved else None, candidates, language_source)

        if router_config is None:
            router_config = self.model_router.snapshot()
        if language != "auto":
            selected, states = self.model_router.choose("transcription", language, list(installed), config=router_config)
            result = self.provider.transcribe(audio, language, selected)
            return TranscriptionOutcome(result, selected, language, tuple(states), "request")

        detected_language = ""
        detection_failure = ""
        runtime_wait = 0.0
        if self.language_identifier is not None:
            try:
                detected = self.language_identifier.identify_language(audio)
                detected_language = str(detected.get("language") or "").lower()
                runtime_wait += float(detected.get("runtime_wait_seconds") or 0.0)
                logger.debug("language_detection_completed model=%s language=%s", detected.get("model"), detected_language or "unknown")
            except InvalidAudioError:
                raise
            except Exception as exc:
                detection_failure = f"language identifier failed ({type(exc).__name__})"
        else:
            detection_failure = "language identifier is not installed"

        if not detection_failure and (
            not router_config.tasks.get("transcription", {}).get(detected_language)
        ):
            detection_failure = f"detected language {detected_language or 'unknown'!r} has no configured route"

        if detection_failure:
            auto_spec = None
            for item in installed:
                if item.get("task") != "transcription":
                    continue
                spec = self.model_repository.get_spec(str(item["id"]))
                if "auto" in spec.languages:
                    auto_spec = spec
                    break
            if auto_spec is None:
                raise ModelUnavailableError(
                    f"STT language detection failed ({detection_failure}); no installed and verified STT model supports auto mode."
                )
            result = self.provider.transcribe(audio, "auto", auto_spec.id)
            result["runtime_wait_seconds"] = round(
                runtime_wait + float(result.get("runtime_wait_seconds") or 0.0), 4
            )
            resolved = result.get("language")
            states = ({"model": auto_spec.id, "installed": True},)
            logger.warning("language_detection_fallback model=%s reason=%s", auto_spec.id, detection_failure)
            return TranscriptionOutcome(result, auto_spec.id, str(resolved).lower() if resolved else None, states, "model_detection")

        selected, states = self.model_router.choose("transcription", detected_language, list(installed), config=router_config)
        result = self.provider.transcribe(audio, detected_language, selected)
        result["runtime_wait_seconds"] = round(
            runtime_wait + float(result.get("runtime_wait_seconds") or 0.0), 4
        )
        return TranscriptionOutcome(result, selected, detected_language, tuple(states), "model_detection")

    @staticmethod
    def _validate_model(spec, task: str, installed: Sequence[InstalledModel]) -> None:
        if spec.task != task:
            raise UnsupportedFeatureError(f"Model {spec.id!r} does not support the {task} task.")
        if spec.id not in {str(model.get("id")) for model in installed if model.get("task") == task}:
            raise ModelUnavailableError(f"Model {spec.id!r} is not installed or not available in the active provider.")
