"""Routes provider-neutral inference calls to model-specific adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Sequence

from smartvoice.domain.contracts import InstalledModel, LanguageIdentificationResult, ProviderCapabilityDocument, SynthesizedSpeech, TranscriptionResult
from smartvoice.domain.errors import UnsupportedFeatureError
from smartvoice.ports.inference import InferenceProvider
from smartvoice.ports.model_repository import ModelRepository

DEFAULT_MODEL_IDS = {
    "transcription": "stt-sensevoice-small-int8",
    "speech": "tts-kokoro-multilingual-v1-1-zh-en",
}


class CompositeInferenceProvider:
    """Combines inference adapters without exposing backend choices to services."""

    def __init__(self, providers: Mapping[str, InferenceProvider], model_repository: ModelRepository) -> None:
        self.providers = dict(providers)
        self.model_repository = model_repository

    def installed_models(self) -> Sequence[InstalledModel]:
        return [model for provider in self.providers.values() for model in provider.installed_models()]

    def runtime(self) -> dict[str, object]:
        runtimes = {backend: provider.runtime() for backend, provider in self.providers.items()}
        primary = next(iter(runtimes.values()), {})
        common = {
            key: value
            for key, value in primary.items()
            if key not in {
                "backend", "backends", "installed_model_count", "actual_device",
                "provider_status", "requested_device", "reason",
            }
        }
        available = [
            runtime for runtime in runtimes.values()
            if runtime.get("provider_status") == "available"
        ]
        actual_devices = {runtime.get("actual_device") for runtime in available}
        requested_devices = {runtime.get("requested_device") for runtime in runtimes.values()}
        reasons = [str(runtime["reason"]) for runtime in runtimes.values() if runtime.get("reason")]
        return {
            **common,
            "backend": "multi-adapter",
            "backends": runtimes,
            "requested_device": next(iter(requested_devices)) if len(requested_devices) == 1 else None,
            "actual_device": next(iter(actual_devices)) if len(actual_devices) == 1 else None,
            "provider_status": "available" if available else "unavailable",
            "reason": None if available else "; ".join(reasons) or "No inference adapter is available.",
            "installed_model_count": len(self.installed_models()),
        }

    def capabilities(self) -> ProviderCapabilityDocument:
        tasks: list[dict[str, object]] = []
        for backend, provider in self.providers.items():
            for task in provider.capabilities().get("tasks", []):
                task_document = dict(task)
                task_document.setdefault("backend", backend)
                tasks.append(task_document)
        return {
            "api_version": "v1",
            "capability_schema_version": "1.0",
            "backend": "multi-adapter",
            "backends": list(self.providers),
            "tasks": tasks,
        }

    def transcribe(
        self, audio: bytes, language: str = "auto", model_id: str | None = None,
    ) -> TranscriptionResult:
        selected_id = model_id or DEFAULT_MODEL_IDS["transcription"]
        return self._provider_for_model(selected_id).transcribe(audio, language, selected_id)

    def synthesize(
        self, text: str, voice: str = "default", speed: float = 1.0,
        model_id: str | None = None, language: str = "auto",
    ) -> SynthesizedSpeech:
        selected_id = model_id or DEFAULT_MODEL_IDS["speech"]
        return self._provider_for_model(selected_id).synthesize(text, voice, speed, selected_id, language)

    def identify_language(self, audio: bytes) -> LanguageIdentificationResult:
        for provider in self.providers.values():
            identify = getattr(provider, "identify_language", None)
            if callable(identify):
                return identify(audio)
        raise UnsupportedFeatureError("Spoken-language identification is not available from the configured adapters.")

    def language_identification_available(self) -> bool:
        return any(
            callable(getattr(provider, "language_identification_available", None))
            and provider.language_identification_available()
            for provider in self.providers.values()
        )

    def is_model_loaded(self, model_id: str) -> bool:
        is_loaded = getattr(self._provider_for_model(model_id), "is_model_loaded", None)
        return bool(is_loaded(model_id)) if callable(is_loaded) else False

    def close(self) -> None:
        """Close managed runtimes owned by the composed adapters."""
        for provider in self.providers.values():
            close = getattr(provider, "close", None)
            if callable(close):
                close()

    def _provider_for_model(self, model_id: str) -> InferenceProvider:
        spec = self.model_repository.get_spec(model_id)
        provider = self.providers.get(spec.backend)
        if provider is None:
            raise UnsupportedFeatureError(
                f"No inference adapter is configured for model backend {spec.backend!r}."
            )
        return provider
