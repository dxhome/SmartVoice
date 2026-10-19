"""Package membership follows the active router and streaming call planner."""

from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from smartvoice.adapters.storage.catalog_model_repository import CatalogModelRepository
from smartvoice.config.settings import Settings
from smartvoice.services.model_packages import resolve_packages
from smartvoice.services.model_registry import load_catalog
from smartvoice.services.model_router import ModelRouter
from smartvoice.services.spoken_language_identifier import LANGUAGE_ID_RESOURCE_ID
from smartvoice.services.streaming.planning import resolve


ROOT = Path(__file__).resolve().parents[1]


class ModelPackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.settings = Settings(data_dir=Path(self.temporary.name))
        self.repository = CatalogModelRepository(self.settings)
        self.router = ModelRouter(self.settings, default_path=ROOT / "catalog" / "router.json")

    def test_static_package_contains_first_chinese_and_english_router_choices(self):
        packages = {item["id"]: item for item in resolve_packages(self.repository, self.router, self.settings)}
        package = packages["static-speech"]
        expected = {
            candidates[0]
            for task in ("transcription", "speech")
            for language in ("zh", "en")
            if (candidates := self.router.snapshot().tasks.get(task, {}).get(language, ()))
        }
        actual = set(package["model_ids"])
        self.assertEqual(actual, expected)
        self.assertIn(LANGUAGE_ID_RESOURCE_ID, {asset["id"] for asset in package["assets"]})
        self.assertIsInstance(package["estimated_size_bytes"], int)

    def test_streaming_package_matches_all_default_bilingual_workflow_plans(self):
        package = next(item for item in resolve_packages(self.repository, self.router, self.settings)
                       if item["id"] == "streaming")
        expected = set()
        for mode, source, target in (
            ("transcription", "zh", None),
            ("transcription", "en", None),
            ("translated_subtitles", "zh", "en"),
            ("translated_subtitles", "en", "zh"),
            ("spoken_interpretation", "zh", "en"),
            ("spoken_interpretation", "en", "zh"),
        ):
            config = {
                "type": "configure", "protocol": "smartvoice.stream.v1",
                "mode": mode, "source_language": source,
                "audio": {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1},
            }
            if target:
                config["target_language"] = target
            plan, _specs, _resident = resolve(config, self.repository, threads=1)
            expected.update(plan.model_ids)
        self.assertEqual(set(package["model_ids"]), expected)
        self.assertNotIn(LANGUAGE_ID_RESOURCE_ID, {asset["id"] for asset in package["assets"]})
        self.assertIsInstance(package["estimated_size_bytes"], int)

    def test_all_package_is_catalog_union_language_identification(self):
        package = next(item for item in resolve_packages(self.repository, self.router, self.settings)
                       if item["id"] == "all")
        self.assertEqual(set(package["model_ids"]), {spec.id for spec in load_catalog()})
        self.assertIn(LANGUAGE_ID_RESOURCE_ID, {asset["id"] for asset in package["assets"]})
        self.assertEqual(package["estimated_size_bytes"], sum(
            int(asset["estimated_download_size_bytes"]) for asset in package["assets"]
        ))
        self.assertEqual(package["estimated_size_basis"], "download")


if __name__ == "__main__":
    unittest.main()
