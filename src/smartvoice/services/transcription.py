"""Application use case for direct and language-routed transcription."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence

from smartvoice.domain.contracts import InstalledModel, TranscriptionResult
from smartvoice.domain.errors import InvalidAudioError, ModelUnavailableError, UnsupportedFeatureError
from smartvoice.ports.inference import InferenceAvailability, InferenceProvider, LanguageIdentifier, SegmentPlanning
from smartvoice.ports.model_repository import ModelRepository
from smartvoice.ports.audio import (
    AudioInput,
    DEFAULT_TRANSCRIPTION_WINDOW_SECONDS,
    LANGUAGE_IDENTIFICATION_SAMPLE_SECONDS,
)
from smartvoice.ports.inference_context import check_execution, segment_scope
from smartvoice.services.audio_planning import merge
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
        audio_input: AudioInput | None = None,
    ) -> None:
        self.provider = provider
        self.model_router = model_router
        self.model_repository = model_repository
        self.language_identifier = language_identifier
        self.audio_input = audio_input

    def execute(
        self,
        audio: bytes,
        requested_model: str,
        language: str,
        router_config: RouterConfig | None = None,
    ) -> TranscriptionOutcome:
        check_execution()
        installed = (
            self.provider.inference_models() if isinstance(self.provider, InferenceAvailability)
            else self.provider.installed_models()
        )
        if requested_model != "smartvoice-auto":
            spec = self.model_repository.get_spec(requested_model)
            self._validate_model(spec, "transcription", installed)
            if language not in {"auto", *spec.languages}:
                raise UnsupportedFeatureError(f"The selected STT model does not support language {language!r}.")

        if self.audio_input is not None:
            with self.audio_input.prepare(audio) as prepared:
                return self._execute(audio, requested_model, language, router_config, prepared, installed)
        return self._execute(audio, requested_model, language, router_config, installed=installed)

    def _transcribe(self, audio: bytes, language: str, model_id: str, prepared) -> TranscriptionResult:
        if prepared is None:
            return self.provider.transcribe(audio, language, model_id)
        maximum = DEFAULT_TRANSCRIPTION_WINDOW_SECONDS
        if isinstance(self.provider, SegmentPlanning):
            declared = self.provider.segment_limits(model_id).get("audio_seconds", 0)
            if declared:
                maximum = float(declared)
        if prepared.duration <= maximum:
            return self.provider.transcribe(audio, language, model_id)
        text = ""
        segments = []
        wait = 0.0
        processing = 0.0
        count = 0
        first = None
        chunk_language = language
        supported_languages = set(self.model_repository.get_spec(model_id).languages)
        for index, window in enumerate(prepared.windows(float(maximum))):
            with segment_scope(index):
                result = self.provider.transcribe(window.audio, chunk_language, model_id)
            if first is None:
                first = result
            if language == "auto" and chunk_language == "auto":
                detected = str(result.get("language") or "").lower()
                if detected in supported_languages and detected != "auto":
                    chunk_language = detected
            count += 1
            text = merge(text, str(result.get("text", "")), window.overlap_seconds)
            wait += float(result.get("runtime_wait_seconds") or 0)
            processing += float(result.get("processing_seconds") or 0)
            for item in result.get("segments", []):
                start = float(item["start"]) + window.start_seconds
                if start < window.start_seconds + window.overlap_seconds:
                    continue
                shifted = {**item, "start": round(start, 3)}
                if "end" in item:
                    shifted["end"] = round(float(item["end"]) + window.start_seconds, 3)
                segments.append(shifted)
        check_execution()
        output = {**(first or {}), "text": text, "duration": round(prepared.duration, 3),
                  "processing_seconds": round(processing, 3), "runtime_wait_seconds": round(wait, 4),
                  "rtf": round(processing/prepared.duration, 4), "chunk_count": count}
        if chunk_language != "auto":
            output["language"] = chunk_language
        output.pop("segments", None)
        if segments:
            output["segments"] = segments
        return output

    def _execute(
        self,
        audio: bytes,
        requested_model: str,
        language: str,
        router_config: RouterConfig | None = None,
        prepared=None,
        installed: Sequence[InstalledModel] = (),
    ) -> TranscriptionOutcome:
        routed = requested_model == "smartvoice-auto"
        candidates: tuple[dict[str, object], ...] = ()
        language_source = "request" if language != "auto" else "model_detection"

        if not routed:
            spec = self.model_repository.get_spec(requested_model)
            # An explicitly selected model handles auto language itself. The
            # dedicated language identifier is reserved for smartvoice-auto.
            result = self._transcribe(audio, language, spec.id, prepared)
            resolved = (result.get("language") if language == "auto" else language) or None
            return TranscriptionOutcome(result, spec.id, str(resolved) if resolved else None, candidates, language_source)

        if router_config is None:
            router_config = self.model_router.snapshot()
        if language != "auto":
            selected, states = self.model_router.choose("transcription", language, list(installed), config=router_config)
            result = self._transcribe(audio, language, selected, prepared)
            return TranscriptionOutcome(result, selected, language, tuple(states), "request")

        detected_language = ""
        detection_failure = ""
        runtime_wait = 0.0
        if self.language_identifier is not None and prepared is not None:
            try:
                sample = prepared.sample(LANGUAGE_IDENTIFICATION_SAMPLE_SECONDS)
                detected = self.language_identifier.identify_language(sample)
                detected_language = str(detected.get("language") or "").lower()
                runtime_wait += float(detected.get("runtime_wait_seconds") or 0.0)
                logger.debug("language_detection_completed model=%s language=%s", detected.get("model"), detected_language or "unknown")
            except InvalidAudioError:
                raise
            except Exception as exc:
                detection_failure = f"language identifier failed ({type(exc).__name__})"
        elif self.language_identifier is not None:
            detection_failure = "bounded audio preparation is unavailable"
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
            result = self._transcribe(audio, "auto", auto_spec.id, prepared)
            result["runtime_wait_seconds"] = round(
                runtime_wait + float(result.get("runtime_wait_seconds") or 0.0), 4
            )
            resolved = result.get("language")
            states = ({"model": auto_spec.id, "installed": True},)
            logger.warning("language_detection_fallback model=%s reason=%s", auto_spec.id, detection_failure)
            return TranscriptionOutcome(result, auto_spec.id, str(resolved).lower() if resolved else None, states, "model_detection")

        selected, states = self.model_router.choose("transcription", detected_language, list(installed), config=router_config)
        result = self._transcribe(audio, detected_language, selected, prepared)
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
