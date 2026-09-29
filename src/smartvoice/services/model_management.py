"""Application use cases for listing and managing locally installed models."""

from pathlib import Path

from smartvoice.ports.inference import ModelLifecycle
from smartvoice.ports.model_repository import ModelRepository
from smartvoice.services.model_jobs import ModelJobManager


class ModelManagementService:
    def __init__(
        self,
        jobs: ModelJobManager,
        model_repository: ModelRepository,
        lifecycle: ModelLifecycle | None = None,
    ) -> None:
        self.jobs = jobs
        self.model_repository = model_repository
        self.lifecycle = lifecycle

    def catalog(self, task: str | None = None) -> dict[str, object]:
        entries = list(self.model_repository.catalog_models())
        if task is not None:
            entries = [model for model in entries if model.get("task") == task]
        return {"data": entries, "storage": self.model_repository.storage_summary()}

    def get_spec(self, model_id: str):
        return self.model_repository.get_spec(model_id)

    def install(self, model_id: str, progress=None) -> Path:
        return self.model_repository.install_model(model_id, progress=progress)

    def start_download(self, model_id: str) -> dict[str, object]:
        return self.jobs.start_download(self.model_repository.get_spec(model_id).id)

    def get_job(self, job_id: str) -> dict[str, object]:
        return self.jobs.get(job_id)

    def cancel_job(self, job_id: str) -> dict[str, object]:
        return self.jobs.cancel(job_id)

    def uninstall(self, model_id: str) -> dict[str, object]:
        canonical_id = self.model_repository.get_spec(model_id).id
        loaded = self.lifecycle is not None and self.lifecycle.is_model_loaded(canonical_id)
        size = self.model_repository.uninstall_model(canonical_id, loaded=loaded)
        return {"id": canonical_id, "removed_bytes": size}

    def export(self, model_id: str, destination: Path) -> Path:
        return self.model_repository.export_model(model_id, destination)

    def import_archive(self, archive_path: Path) -> dict[str, str]:
        destination = self.model_repository.import_model(archive_path)
        return {"id": destination.name, "status": "installed"}
