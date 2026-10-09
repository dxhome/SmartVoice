"""Create the configured inference adapters at the application boundary."""

from smartvoice.adapters.inference.composite_provider import CompositeInferenceProvider
from smartvoice.adapters.storage.catalog_model_repository import CatalogModelRepository
from smartvoice.config.settings import Settings
from smartvoice.ports.model_repository import ModelRepository


def create_inference_provider(
    settings: Settings,
    model_repository: ModelRepository | None = None,
    *, compute_budget=None,
) -> CompositeInferenceProvider:
    """Build the standard adapter set while keeping backend wiring in one place."""
    repository = model_repository or CatalogModelRepository(settings)

    # Import concrete adapters here to keep platform/runtime imports at the edge.
    from smartvoice.adapters.inference.qwen_tts.provider import QwenTTSProvider
    from smartvoice.adapters.inference.sherpa_onnx.provider import SherpaOnnxProvider

    from smartvoice.adapters.inference.runtime.pooled_provider import PooledInferenceProvider, PooledSherpaProvider

    metadata = SherpaOnnxProvider(settings, repository)

    def create_sherpa(seed):
        runtime = SherpaOnnxProvider(settings, repository)
        source = seed or metadata
        # Copy only validated file fingerprints, never native objects or locks.
        # The unchanged provider rechecks size/mtime and manifest digest on use.
        with source._cache_lock:
            runtime._verified_files.update(source._verified_files)
        return runtime

    qwen = QwenTTSProvider(settings, repository)
    return CompositeInferenceProvider(
        {
            "sherpa-onnx": PooledSherpaProvider(metadata, create_sherpa, settings, compute_budget=compute_budget),
            "qwen-tts": PooledInferenceProvider(qwen, lambda seed: qwen, settings, shared_backend=True, compute_budget=compute_budget),
        },
        repository,
        model_availability_ttl_seconds=settings.model_availability_ttl_seconds,
    )
