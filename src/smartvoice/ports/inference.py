"""Inference boundary implemented by runtime-specific adapters."""

from typing import Protocol


class InferenceProvider(Protocol):
    """Operations an inference adapter must expose to the domain layer."""

    def installed_models(self) -> list[dict[str, object]]: ...

    def runtime(self) -> dict[str, object]: ...

    def transcribe(self, audio: bytes, language: str = "auto", model_id: str | None = None) -> dict[str, object]: ...

    def synthesize(
        self, text: str, voice: str = "default", speed: float = 1.0, model_id: str | None = None
    ) -> tuple[bytes, int, float]: ...

    def capabilities(self) -> dict[str, object]: ...
