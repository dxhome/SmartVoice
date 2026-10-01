"""Create the configured inference adapters at the application boundary."""

from smartvoice.adapters.inference.composite_provider import CompositeInferenceProvider
from smartvoice.adapters.storage.catalog_model_repository import CatalogModelRepository
from smartvoice.config.settings import Settings
from smartvoice.ports.model_repository import ModelRepository


def create_inference_provider(
    settings: Settings,
    model_repository: ModelRepository | None = None,
) -> CompositeInferenceProvider:
    """Build the standard adapter set while keeping backend wiring in one place."""
    repository = model_repository or CatalogModelRepository(settings)

    # Import concrete adapters here to keep platform/runtime imports at the edge.
    from smartvoice.adapters.inference.qwen_tts.provider import QwenTTSProvider
    from smartvoice.adapters.inference.sherpa_onnx.provider import SherpaOnnxProvider

    return CompositeInferenceProvider(
        {
            "sherpa-onnx": SherpaOnnxProvider(settings, repository),
            "qwen-tts": QwenTTSProvider(settings, repository),
        },
        repository,
    )
