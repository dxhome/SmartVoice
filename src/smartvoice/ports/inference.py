"""Inference boundary implemented by runtime-specific adapters."""

from typing import Protocol, Sequence, runtime_checkable

from smartvoice.domain.contracts import (
    InstalledModel,
    LanguageIdentificationResult,
    ProviderCapabilityDocument,
    SynthesizedSpeech,
    TranscriptionResult,
)


class InferenceProvider(Protocol):
    """Required operations an inference adapter exposes to application services."""

    def installed_models(self) -> Sequence[InstalledModel]: ...

    def runtime(self) -> dict[str, object]: ...

    def transcribe(self, audio: bytes, language: str = "auto", model_id: str | None = None) -> TranscriptionResult: ...

    def synthesize(
        self, text: str, voice: str = "default", speed: float = 1.0, model_id: str | None = None,
        language: str = "auto",
    ) -> SynthesizedSpeech: ...

    def capabilities(self) -> ProviderCapabilityDocument: ...


@runtime_checkable
class LanguageIdentifier(Protocol):
    """Optional adapter capability for dedicated spoken-language detection."""

    def identify_language(self, audio: bytes) -> LanguageIdentificationResult: ...


@runtime_checkable
class LanguageIdentifierStatus(Protocol):
    """Optional status capability for adapters with installable detector assets."""

    def language_identification_available(self) -> bool: ...


@runtime_checkable
class ModelLifecycle(Protocol):
    """Optional provider capability for querying loaded models."""

    def is_model_loaded(self, model_id: str) -> bool: ...


@runtime_checkable
class RuntimeLifecycle(Protocol):
    """Optional provider capability for releasing resources owned by its runtime."""

    def close(self) -> None: ...


@runtime_checkable
class ManagedAdmission(Protocol):
    """Optional bounded adapter admission; reports transport reservation capacity."""

    def request_capacity(self) -> int | None: ...


@runtime_checkable
class InferenceAvailability(Protocol):
    """Optional inexpensive routing snapshot; execution still verifies assets."""

    def inference_models(self) -> Sequence[InstalledModel]: ...
