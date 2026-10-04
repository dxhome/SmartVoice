"""Routes provider-neutral inference calls to model-specific adapters."""

from __future__ import annotations

from collections.abc import Mapping
import copy
import logging
import math
import threading
import time
from typing import Sequence

from smartvoice.domain.contracts import InstalledModel, LanguageIdentificationResult, ProviderCapabilityDocument, SynthesizedSpeech, TranscriptionResult
from smartvoice.domain.errors import UnsupportedFeatureError
from smartvoice.ports.inference import (
    InferenceProvider,
    LanguageIdentifier,
    LanguageIdentifierStatus,
    ModelLifecycle,
    ManagedAdmission,
    RuntimeLifecycle,
)
from smartvoice.ports.model_repository import ModelRepository

DEFAULT_MODEL_IDS = {
    "transcription": "stt-sensevoice-small-int8",
    "speech": "tts-kokoro-multilingual-v1-1-zh-en",
}

logger = logging.getLogger(__name__)
MODEL_AVAILABILITY_RETRY_DELAYS_SECONDS = (5.0, 10.0, 15.0)


class CompositeInferenceProvider:
    """Combines inference adapters without exposing backend choices to services."""

    def __init__(
        self,
        providers: Mapping[str, InferenceProvider],
        model_repository: ModelRepository,
        *,
        model_availability_ttl_seconds: float = 600.0,
    ) -> None:
        if not math.isfinite(model_availability_ttl_seconds) or model_availability_ttl_seconds <= 0:
            raise ValueError("model_availability_ttl_seconds must be finite and positive")
        self.providers = dict(providers)
        self.model_repository = model_repository
        self.model_availability_ttl_seconds = model_availability_ttl_seconds
        self._availability_lock = threading.RLock()
        self._scan_lock = threading.Lock()
        self._installed_snapshot: tuple[InstalledModel, ...] | None = None
        self._snapshot_refreshed_at: float | None = None
        self._refresh_generation = 0
        self._refresh_in_progress = False
        self._refresh_retries_exhausted = False
        self._last_refresh_error: str | None = None

    def refresh_model_availability(self, *, force_integrity_check: bool = False) -> list[InstalledModel]:
        """Force a full scan and atomically publish it as the shared snapshot."""
        with self._scan_lock:
            with self._availability_lock:
                self._refresh_generation += 1
                generation = self._refresh_generation
                self._refresh_in_progress = True
                self._refresh_retries_exhausted = False
                self._last_refresh_error = None
            try:
                models = self._scan_model_availability(force_integrity_check=force_integrity_check)
            except Exception:
                with self._availability_lock:
                    if generation == self._refresh_generation:
                        self._refresh_in_progress = False
                raise
            with self._availability_lock:
                if generation == self._refresh_generation:
                    self._publish_snapshot(models)
                    self._refresh_in_progress = False
                    self._refresh_retries_exhausted = False
                    self._last_refresh_error = None
                return copy.deepcopy(self._installed_snapshot or ())

    def _scan_model_availability(self, *, force_integrity_check: bool = False) -> list[InstalledModel]:
        refresh_repository = getattr(self.model_repository, "refresh_installed_models", None)
        if force_integrity_check:
            force_refresh = getattr(self.model_repository, "force_refresh_installed_models", None)
            if callable(force_refresh):
                force_refresh()
            elif callable(refresh_repository):
                refresh_repository()
        elif callable(refresh_repository):
            refresh_repository()
        if force_integrity_check:
            for provider in self.providers.values():
                reload_cache = getattr(provider, "reload_integrity_cache", None)
                if callable(reload_cache):
                    reload_cache()
        return [
            model
            for provider in self.providers.values()
            for model in provider.installed_models()
        ]

    def _publish_snapshot(self, models: Sequence[InstalledModel]) -> None:
        self._installed_snapshot = tuple(copy.deepcopy(models))
        self._snapshot_refreshed_at = time.monotonic()

    def _start_ttl_refresh_locked(self) -> None:
        if self._refresh_in_progress or self._refresh_retries_exhausted:
            return
        self._refresh_generation += 1
        generation = self._refresh_generation
        self._refresh_in_progress = True
        self._last_refresh_error = None
        worker = threading.Thread(
            target=self._refresh_expired_snapshot,
            args=(generation,),
            name="smartvoice-model-availability-refresh",
            daemon=True,
        )
        worker.start()

    def _refresh_expired_snapshot(self, generation: int) -> None:
        retry_delays = (0.0, *MODEL_AVAILABILITY_RETRY_DELAYS_SECONDS)
        for attempt, delay in enumerate(retry_delays):
            if delay:
                time.sleep(delay)
            with self._scan_lock:
                with self._availability_lock:
                    if generation != self._refresh_generation:
                        return
                try:
                    models = self._scan_model_availability()
                except Exception as exc:
                    error = exc
                else:
                    with self._availability_lock:
                        if generation == self._refresh_generation:
                            self._publish_snapshot(models)
                            self._refresh_in_progress = False
                            self._refresh_retries_exhausted = False
                            self._last_refresh_error = None
                    return

            with self._availability_lock:
                if generation != self._refresh_generation:
                    return
                self._last_refresh_error = f"{type(error).__name__}: {error}"
                if attempt == len(retry_delays) - 1:
                    self._refresh_in_progress = False
                    self._refresh_retries_exhausted = True
                    logger.error(
                        "Model availability refresh failed after %d retries; keeping the last successful snapshot. error=%s",
                        len(MODEL_AVAILABILITY_RETRY_DELAYS_SECONDS), self._last_refresh_error,
                        exc_info=(type(error), error, error.__traceback__),
                    )
                else:
                    next_delay = retry_delays[attempt + 1]
                    logger.warning(
                        "Model availability refresh attempt %d failed; retrying in %.0f seconds and keeping the last successful snapshot. error=%s",
                        attempt + 1, next_delay, self._last_refresh_error,
                        exc_info=(type(error), error, error.__traceback__),
                    )

    def request_capacity(self) -> int | None:
        if not self.providers or not all(isinstance(provider, ManagedAdmission) for provider in self.providers.values()):
            return None
        capacities = [provider.request_capacity() for provider in self.providers.values()]
        return sum(capacities) if all(capacity is not None for capacity in capacities) else None

    def installed_models(self) -> Sequence[InstalledModel]:
        with self._availability_lock:
            if self._installed_snapshot is not None:
                refreshed_at = self._snapshot_refreshed_at
                if (
                    refreshed_at is not None
                    and time.monotonic() - refreshed_at >= self.model_availability_ttl_seconds
                ):
                    self._start_ttl_refresh_locked()
                return copy.deepcopy(self._installed_snapshot)

        # This is only the pre-startup fallback. Normal service startup builds
        # the first snapshot before accepting requests.
        with self._scan_lock:
            with self._availability_lock:
                if self._installed_snapshot is not None:
                    return copy.deepcopy(self._installed_snapshot)
                self._refresh_generation += 1
                generation = self._refresh_generation
                self._refresh_in_progress = True
            try:
                models = self._scan_model_availability()
            except Exception:
                with self._availability_lock:
                    if generation == self._refresh_generation:
                        self._refresh_in_progress = False
                raise
            with self._availability_lock:
                if generation == self._refresh_generation:
                    self._publish_snapshot(models)
                    self._refresh_in_progress = False
                return copy.deepcopy(self._installed_snapshot or ())

    def inference_models(self) -> Sequence[InstalledModel]:
        return self.installed_models()

    def runtime(self) -> dict[str, object]:
        models = self.installed_models()
        runtimes = {}
        for backend, provider in self.providers.items():
            backend_models = [model for model in models if model.get("backend") == backend]
            runtime_for_models = getattr(provider, "runtime_for_models", None)
            runtime = runtime_for_models(backend_models) if callable(runtime_for_models) else provider.runtime()
            runtimes[backend] = {**runtime, "installed_model_count": len(backend_models)}
        primary = next(iter(runtimes.values()), {})
        common = {
            key: value
            for key, value in primary.items()
            if key not in {
                "backend", "backends", "installed_model_count", "actual_device",
                "provider_status", "requested_device", "reason", "instance_pools", "pool_limits",
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
            "installed_model_count": len(models),
        }

    def capabilities(self) -> ProviderCapabilityDocument:
        tasks: list[dict[str, object]] = []
        models = self.installed_models()
        for backend, provider in self.providers.items():
            backend_models = [model for model in models if model.get("backend") == backend]
            capabilities_for_models = getattr(provider, "capabilities_for_models", None)
            capabilities = (
                capabilities_for_models(backend_models)
                if callable(capabilities_for_models)
                else provider.capabilities()
            )
            for task in capabilities.get("tasks", []):
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
            if isinstance(provider, LanguageIdentifier):
                return provider.identify_language(audio)
        raise UnsupportedFeatureError("Spoken-language identification is not available from the configured adapters.")

    def language_identification_available(self) -> bool:
        return any(
            isinstance(provider, LanguageIdentifierStatus)
            and provider.language_identification_available()
            for provider in self.providers.values()
        )

    def is_model_loaded(self, model_id: str) -> bool:
        provider = self._provider_for_model(model_id)
        return provider.is_model_loaded(model_id) if isinstance(provider, ModelLifecycle) else False

    def close(self) -> None:
        """Close managed runtimes owned by the composed adapters."""
        for provider in self.providers.values():
            if isinstance(provider, RuntimeLifecycle):
                provider.close()

    def _provider_for_model(self, model_id: str) -> InferenceProvider:
        spec = self.model_repository.get_spec(model_id)
        provider = self.providers.get(spec.backend)
        if provider is None:
            raise UnsupportedFeatureError(
                f"No inference adapter is configured for model backend {spec.backend!r}."
            )
        return provider
