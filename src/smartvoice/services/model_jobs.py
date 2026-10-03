"""In-process background jobs for cancellable model downloads."""

from __future__ import annotations

import threading
import uuid
import logging
from typing import Any

from smartvoice.config.settings import Settings
from smartvoice.domain.errors import InvalidRequestError, ResourceNotFoundError
from smartvoice.services.model_registry import get_model_spec
from smartvoice.services.model_download import install_model

logger = logging.getLogger(__name__)


class ModelJobManager:
    def __init__(self, settings: Settings, on_model_change=None):
        self.settings = settings
        self.on_model_change = on_model_change
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def start_download(self, model_id: str) -> dict[str, Any]:
        model_id = get_model_spec(model_id).id
        job_id = uuid.uuid4().hex
        cancel = threading.Event()
        job: dict[str, Any] = {
            "job_id": job_id,
            "model_id": model_id,
            "status": "queued",
            "downloaded_bytes": 0,
            "total_bytes": None,
            "error": None,
            "cancel": cancel,
        }
        with self._lock:
            for existing in self._jobs.values():
                if existing["model_id"] == model_id and existing["status"] in {"queued", "running", "canceling"}:
                    raise InvalidRequestError(f"A download job is already active for model {model_id!r}.")
            self._jobs[job_id] = job
        threading.Thread(target=self._run_download, args=(job_id,), daemon=True, name=f"smartvoice-model-{job_id[:8]}").start()
        return self.get(job_id)

    def _run_download(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job["status"] = "running"

        def update(downloaded: int, total: int | None) -> None:
            with self._lock:
                job["downloaded_bytes"] = downloaded
                job["total_bytes"] = total

        try:
            path = install_model(
                self.settings,
                job["model_id"],
                progress=update,
                cancel_event=job["cancel"],
            )
            if self.on_model_change is not None:
                try:
                    self.on_model_change()
                except Exception:
                    logger.exception("Model download completed, but the in-process availability snapshot could not be refreshed.")
            with self._lock:
                job["status"] = "completed"
                job["result"] = {"model_id": job["model_id"], "path": str(path)}
        except Exception as exc:
            with self._lock:
                job["status"] = "canceled" if job["cancel"].is_set() else "failed"
                job["error"] = str(exc)

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise ResourceNotFoundError(f"Unknown model job ID: {job_id}")
            return {key: value for key, value in job.items() if key != "cancel"}

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise ResourceNotFoundError(f"Unknown model job ID: {job_id}")
            if job["status"] not in {"queued", "running"}:
                return {key: value for key, value in job.items() if key != "cancel"}
            job["cancel"].set()
            job["status"] = "canceling"
            return {key: value for key, value in job.items() if key != "cancel"}

    def cancel_all(self) -> None:
        with self._lock:
            for job in self._jobs.values():
                if job["status"] in {"queued", "running"}:
                    job["cancel"].set()
