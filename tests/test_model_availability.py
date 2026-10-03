from __future__ import annotations

import copy
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from smartvoice.adapters.inference.composite_provider import CompositeInferenceProvider
from smartvoice.adapters.storage.catalog_model_repository import CatalogModelRepository
from smartvoice.config.settings import Settings


MODEL = {
    "id": "stt-sensevoice-small-int8",
    "task": "transcription",
    "backend": "test",
    "languages": ["zh"],
}


class Repository:
    def __init__(self):
        self.refresh_calls = 0

    def refresh_installed_models(self):
        self.refresh_calls += 1


class Provider:
    def __init__(self):
        self.models = [copy.deepcopy(MODEL)]
        self.scan_calls = 0
        self.failures_remaining = 0

    def installed_models(self):
        self.scan_calls += 1
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise OSError("scan failed")
        return copy.deepcopy(self.models)

    def runtime_for_models(self, models):
        return {"backend": "test", "provider_status": "available", "actual_device": "cpu",
                "installed_model_count": len(models)}

    def capabilities_for_models(self, models):
        return {"tasks": [{"task": m["task"], "model": m["id"]} for m in models]}


class ThreadStub:
    def __init__(self, *, target, args, **kwargs):
        self.target = target
        self.args = args
        self.kwargs = kwargs

    def start(self):
        pass


class ModelAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.repository = Repository()
        self.backend = Provider()
        self.provider = CompositeInferenceProvider(
            {"test": self.backend}, self.repository, model_availability_ttl_seconds=600,
        )
        self.provider.refresh_model_availability()

    def test_one_snapshot_is_shared_and_returned_as_a_copy(self):
        before = self.backend.scan_calls
        models = self.provider.installed_models()
        models[0]["id"] = "mutated-by-caller"

        self.assertEqual(self.provider.inference_models()[0]["id"], MODEL["id"])
        self.assertEqual(self.provider.capabilities()["tasks"][0]["model"], MODEL["id"])
        self.assertEqual(self.provider.runtime()["installed_model_count"], 1)
        self.assertEqual(self.backend.scan_calls, before)
        self.assertEqual(self.repository.refresh_calls, 1)

    def test_expired_snapshot_is_served_while_only_one_background_refresh_starts(self):
        thread_class = Mock(side_effect=ThreadStub)
        with patch(
            "smartvoice.adapters.inference.composite_provider.threading.Thread", thread_class,
        ), patch(
            "smartvoice.adapters.inference.composite_provider.time.monotonic", return_value=100.0,
        ) as clock:
            self.provider.refresh_model_availability()
            before = self.backend.scan_calls

            clock.return_value = 699.0
            self.assertEqual(list(self.provider.installed_models()), [MODEL])
            thread_class.assert_not_called()

            clock.return_value = 700.0
            self.assertEqual(list(self.provider.installed_models()), [MODEL])
            self.assertEqual(list(self.provider.installed_models()), [MODEL])
            self.assertEqual(self.backend.scan_calls, before)

            thread_class.assert_called_once()
            self.assertTrue(self.provider._refresh_in_progress)
            self.backend.models = [{**MODEL, "id": "updated-model"}]
            target = thread_class.call_args.kwargs["target"]
            args = thread_class.call_args.kwargs["args"]
            target(*args)
            self.assertEqual(self.provider.installed_models()[0]["id"], "updated-model")
            self.assertFalse(self.provider._refresh_in_progress)
            self.assertEqual(self.backend.scan_calls, before + 1)
            thread_class.assert_called_once()

    def test_ttl_refresh_retries_after_configured_delays_then_publishes(self):
        self.backend.failures_remaining = 3
        self.backend.models = [{**MODEL, "id": "updated-model"}]
        self.provider._refresh_generation = 10

        with patch("smartvoice.adapters.inference.composite_provider.time.sleep") as sleep:
            self.provider._refresh_expired_snapshot(10)

        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5.0, 10.0, 15.0])
        self.assertEqual(self.backend.scan_calls, 5)  # initial snapshot plus four TTL attempts
        self.assertEqual(self.provider.installed_models()[0]["id"], "updated-model")
        self.assertFalse(self.provider._refresh_retries_exhausted)

    def test_exhausted_ttl_refresh_keeps_last_snapshot_and_manual_refresh_recovers(self):
        self.backend.failures_remaining = 4
        self.provider._refresh_generation = 11
        with patch("smartvoice.adapters.inference.composite_provider.time.sleep") as sleep:
            with self.assertLogs("smartvoice.adapters.inference.composite_provider", level="ERROR"):
                self.provider._refresh_expired_snapshot(11)

        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5.0, 10.0, 15.0])
        self.assertTrue(self.provider._refresh_retries_exhausted)
        self.assertEqual(list(self.provider.installed_models()), [MODEL])

        self.backend.models = [{**MODEL, "id": "recovered-model"}]
        self.provider.refresh_model_availability()
        self.assertFalse(self.provider._refresh_retries_exhausted)
        self.assertEqual(self.provider.installed_models()[0]["id"], "recovered-model")

    def test_force_refresh_preserves_last_snapshot_if_scan_fails(self):
        self.backend.failures_remaining = 1
        with self.assertRaisesRegex(OSError, "scan failed"):
            self.provider.refresh_model_availability()
        self.assertEqual(list(self.provider.installed_models()), [MODEL])
        self.assertFalse(self.provider._refresh_in_progress)

    def test_repository_caches_last_good_scan_until_explicit_refresh(self):
        from smartvoice.adapters.storage import catalog_model_repository as repository_module

        next_models = [{**MODEL}]
        with patch.object(repository_module, "installed_models", side_effect=lambda _settings: next_models) as scan:
            repository = CatalogModelRepository(Settings(data_dir=Path("/tmp/smartvoice-test")))
            first = repository.installed_models()
            first[0]["id"] = "caller-mutation"
            self.assertEqual(repository.installed_models()[0]["id"], MODEL["id"])
            self.assertEqual(scan.call_count, 1)

            repository.invalidate_installed_models()
            next_models = [{**MODEL, "id": "after-refresh"}]
            self.assertEqual(repository.installed_models()[0]["id"], MODEL["id"])
            self.assertEqual(scan.call_count, 1)
            repository.refresh_installed_models()
            self.assertEqual(repository.installed_models()[0]["id"], "after-refresh")
            self.assertEqual(scan.call_count, 2)


if __name__ == "__main__":
    unittest.main()
