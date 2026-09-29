"""Filesystem model repository backed by the local SmartVoice catalog."""

from pathlib import Path
from typing import Sequence

from smartvoice.config.settings import Settings
from smartvoice.domain.contracts import InstalledModel
from smartvoice.domain.models import ModelSpec
from smartvoice.ports.model_repository import ModelRepository
from smartvoice.services.model_download import install_model
from smartvoice.services.model_registry import get_model_spec
from smartvoice.services.model_storage import (
    catalog_models,
    export_model,
    import_model,
    installed_models,
    model_directory,
    model_storage,
    uninstall_model,
)


class CatalogModelRepository(ModelRepository):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def get_spec(self, model_id: str) -> ModelSpec:
        return get_model_spec(model_id)

    def installed_models(self) -> Sequence[InstalledModel]:
        return installed_models(self.settings)

    def model_directory(self, model_id: str) -> Path:
        return model_directory(self.settings, model_id)

    def catalog_models(self) -> Sequence[dict[str, object]]:
        return catalog_models(self.settings)

    def storage_summary(self) -> dict[str, int]:
        return model_storage(self.settings)

    def install_model(self, model_id: str, progress=None, *, source: str | None = None) -> Path:
        return install_model(self.settings, model_id, progress=progress, source=source)

    def uninstall_model(self, model_id: str, *, loaded: bool = False) -> int:
        return uninstall_model(self.settings, model_id, loaded=loaded)

    def export_model(self, model_id: str, destination: Path) -> Path:
        return export_model(self.settings, model_id, destination)

    def import_model(self, archive_path: Path) -> Path:
        return import_model(self.settings, archive_path)
