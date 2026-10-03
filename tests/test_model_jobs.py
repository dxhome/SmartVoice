from __future__ import annotations

import time
import uuid
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from smartvoice.config.settings import Settings
from smartvoice.domain.errors import InvalidRequestError
from smartvoice.services.model_download import ModelDownloadCancelled
from smartvoice.services.model_jobs import ModelJobManager


class ModelJobTests(unittest.TestCase):
    def test_download_job_reports_completion(self):
        directory = Path.cwd() / ".smartvoice-dev" / f"job-{uuid.uuid4().hex}"
        directory.mkdir(parents=True)
        on_model_change = Mock()
        manager = ModelJobManager(Settings(data_dir=directory), on_model_change=on_model_change)
        with patch("smartvoice.services.model_jobs.install_model", return_value=directory / "installed"):
            job = manager.start_download("stt-sensevoice-small-int8")
            deadline = time.monotonic() + 2
            while manager.get(job["job_id"])["status"] not in {"completed", "failed"} and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(manager.get(job["job_id"])["status"], "completed")
        on_model_change.assert_called_once_with()

    def test_duplicate_download_is_rejected_and_cancel_is_exposed(self):
        directory = Path.cwd() / ".smartvoice-dev" / f"job-cancel-{uuid.uuid4().hex}"
        directory.mkdir(parents=True)
        manager = ModelJobManager(Settings(data_dir=directory))

        def blocked(_settings, _model_id, progress=None, cancel_event=None):
            cancel_event.wait(timeout=2)
            raise ModelDownloadCancelled("canceled")

        with patch("smartvoice.services.model_jobs.install_model", side_effect=blocked):
            job = manager.start_download("stt-sensevoice-small-int8")
            with self.assertRaises(InvalidRequestError):
                manager.start_download("stt-sensevoice-small-int8")
            manager.cancel(job["job_id"])
            deadline = time.monotonic() + 2
            while manager.get(job["job_id"])["status"] not in {"canceled", "failed"} and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(manager.get(job["job_id"])["status"], "canceled")


if __name__ == "__main__":
    unittest.main()
