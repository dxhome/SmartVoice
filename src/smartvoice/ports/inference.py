"""Inference boundary implemented by runtime-specific adapters."""

from typing import Protocol

from smartvoice.domain.capabilities import ProviderCapabilities


class InferenceProvider(Protocol):
    """Operations an inference adapter must expose to the domain layer."""

    @property
    def capabilities(self) -> ProviderCapabilities: ...

    def installed_models(self) -> list[dict[str, object]]: ...

    def runtime(self) -> dict[str, object]: ...

    def transcribe(self, audio: bytes, language: str = "auto") -> dict[str, object]: ...

    def synthesize(self, text: str, voice: str = "default", speed: float = 1.0) -> tuple[bytes, int, float]: ...

    def capabilities(self) -> dict[str, object]: ...
