"""Filesystem model repository backed by the local SmartVoice catalog."""

from pathlib import Path
import copy
import threading
from contextlib import contextmanager
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
        self._installed_models_lock = threading.RLock()
        self._installed_models_snapshot: tuple[InstalledModel, ...] | None = None
        self._installed_models_stale = False
        self._model_users: dict[str, int] = {}

    @contextmanager
    def hold_models(self, model_ids):
        """Pin verified files for a live session; removal uses the same lock."""
        from smartvoice.domain.errors import InvalidRequestError
        model_ids = tuple(dict.fromkeys(model_ids))
        with self._installed_models_lock:
            installed = {model["id"] for model in self.installed_models()}
            if any(model_id not in installed for model_id in model_ids):
                raise InvalidRequestError("The complete streaming model chain is not installed.")
            for model_id in model_ids:
                self._model_users[model_id] = self._model_users.get(model_id, 0) + 1
        try:
            yield
        finally:
            with self._installed_models_lock:
                for model_id in model_ids:
                    remaining = self._model_users[model_id] - 1
                    if remaining:
                        self._model_users[model_id] = remaining
                    else:
                        self._model_users.pop(model_id)

    def get_spec(self, model_id: str) -> ModelSpec:
        return get_model_spec(model_id)

    def installed_models(self) -> Sequence[InstalledModel]:
        with self._installed_models_lock:
            if self._installed_models_snapshot is None:
                self._installed_models_snapshot = tuple(installed_models(self.settings))
                self._installed_models_stale = False
            return copy.deepcopy(self._installed_models_snapshot)

    def refresh_installed_models(self, *, force_integrity_check: bool = False) -> Sequence[InstalledModel]:
        """Rescan and replace the process-wide verified filesystem snapshot."""
        with self._installed_models_lock:
            if force_integrity_check:
                models = tuple(installed_models(self.settings, force_integrity_check=True))
            else:
                models = tuple(installed_models(self.settings))
            self._installed_models_snapshot = models
            self._installed_models_stale = False
            return copy.deepcopy(models)

    def force_refresh_installed_models(self) -> Sequence[InstalledModel]:
        """Rehash every file listed in installed model manifests and persist results."""
        return self.refresh_installed_models(force_integrity_check=True)

    def invalidate_installed_models(self) -> None:
        with self._installed_models_lock:
            # Keep the last known-good data available to readers while a
            # full refresh is pending or running.
            self._installed_models_stale = True

    def model_directory(self, model_id: str) -> Path:
        return model_directory(self.settings, model_id)

    def catalog_models(self) -> Sequence[dict[str, object]]:
        return catalog_models(self.settings, installed_snapshot=self.installed_models())

    def storage_summary(self) -> dict[str, int]:
        return model_storage(self.settings, installed_snapshot=self.installed_models())

    def install_model(self, model_id: str, progress=None, *, source: str | None = None) -> Path:
        result = install_model(self.settings, model_id, progress=progress, source=source)
        self.invalidate_installed_models()
        return result

    def uninstall_model(self, model_id: str, *, loaded: bool = False) -> int:
        with self._installed_models_lock:
            result = uninstall_model(self.settings, model_id, loaded=loaded or bool(self._model_users.get(model_id)))
            self.invalidate_installed_models()
        return result

    def export_model(self, model_id: str, destination: Path) -> Path:
        return export_model(self.settings, model_id, destination)

    def import_model(self, archive_path: Path) -> Path:
        result = import_model(self.settings, archive_path)
        self.invalidate_installed_models()
        return result
