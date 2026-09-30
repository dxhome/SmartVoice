from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from smartvoice.config.settings import Settings
from smartvoice.domain.errors import UnsupportedFeatureError
from smartvoice.services.model_router import ModelRouter, RouterConfigError


class ModelRouterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.settings = Settings(data_dir=self.root / "data")
        self.config_path = self.root / "router.json"
        self.default_path = Path(__file__).resolve().parents[1] / "catalog" / "router.json"
        self.router = ModelRouter(
            self.settings, config_path=self.config_path, default_path=self.default_path
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_config(self, value):
        self.config_path.write_text(json.dumps(value), encoding="utf-8")

    def test_first_installed_candidate_wins_in_static_order(self):
        selected, states = self.router.choose("transcription", "zh", [
            {"id": "stt-whisper-base-multilingual-int8", "task": "transcription"},
            {"id": "stt-sensevoice-small-int8", "task": "transcription"},
        ])
        self.assertEqual(selected, "stt-sensevoice-small-int8")
        self.assertEqual(states[0], {"model": "stt-sensevoice-small-int8", "installed": True})

    def test_uninstalled_candidates_are_skipped_without_download_or_inference_fallback(self):
        selected, states = self.router.choose("speech", "fr", [
            {"id": "tts-supertonic-v3-multilingual-int8", "task": "speech"},
        ])
        self.assertEqual(selected, "tts-supertonic-v3-multilingual-int8")
        self.assertEqual(states, [
            {"model": "tts-supertonic-v3-multilingual-int8", "installed": True},
            {"model": "tts-qwen3-0-6b-customvoice", "installed": False},
        ])

    def test_no_installed_candidate_returns_actionable_model_error(self):
        with self.assertRaisesRegex(UnsupportedFeatureError, "Install a configured model"):
            self.router.choose("transcription", "zh", [])

    def test_initialization_copies_builtin_config_to_user_data_dir(self):
        self.assertTrue(self.config_path.is_file())
        self.assertEqual(self.router.snapshot().digest, ModelRouter(
            self.settings, config_path=self.config_path, default_path=self.default_path
        ).snapshot().digest)

    def test_invalid_reload_keeps_last_valid_snapshot(self):
        original = self.router.snapshot()
        invalid = json.loads(self.config_path.read_text(encoding="utf-8"))
        invalid["tasks"]["transcription"]["zh"] = [
            "stt-sensevoice-small-int8", "stt-whisper-base-multilingual-int8",
            "stt-sensevoice-small-int8", "stt-whisper-base-multilingual-int8",
        ]
        self.write_config(invalid)
        with self.assertRaises(RouterConfigError):
            self.router.reload()
        self.assertEqual(self.router.snapshot(), original)

    def test_invalid_saved_file_uses_builtin_snapshot_and_surfaces_warning(self):
        broken_path = self.root / "broken-router.json"
        broken_path.write_text("not json", encoding="utf-8")
        router = ModelRouter(self.settings, config_path=broken_path, default_path=self.default_path)
        self.assertIn("built-in configuration is active", router.public_status([])["warning"])
        broken_path.write_bytes(self.default_path.read_bytes())
        result = router.reload()
        self.assertEqual(result["status"], "reloaded")
        self.assertIsNone(router.public_status([])["warning"])

    def test_valid_reload_atomically_updates_priority(self):
        updated = json.loads(self.config_path.read_text(encoding="utf-8"))
        updated["tasks"]["transcription"]["zh"] = [
            "stt-whisper-base-multilingual-int8", "stt-sensevoice-small-int8",
        ]
        self.write_config(updated)
        result = self.router.reload()
        selected, _ = self.router.choose("transcription", "zh", [
            {"id": "stt-sensevoice-small-int8", "task": "transcription"},
            {"id": "stt-whisper-base-multilingual-int8", "task": "transcription"},
        ])
        self.assertEqual(selected, "stt-whisper-base-multilingual-int8")
        self.assertEqual(result["sha256"], self.router.snapshot().digest)

    def test_router_config_rejects_unknown_language_duplicate_and_wrong_task(self):
        original = json.loads(self.config_path.read_text(encoding="utf-8"))
        cases = []
        unknown_language = json.loads(json.dumps(original))
        unknown_language["tasks"]["speech"]["en-US"] = ["tts-kokoro-multilingual-v1-1-zh-en"]
        cases.append(unknown_language)
        duplicate = json.loads(json.dumps(original))
        duplicate["tasks"]["speech"]["en"] = ["tts-kokoro-multilingual-v1-1-zh-en", "tts-kokoro-multilingual-v1-1-zh-en"]
        cases.append(duplicate)
        wrong_task = json.loads(json.dumps(original))
        wrong_task["tasks"]["speech"]["zh"] = ["stt-sensevoice-small-int8"]
        cases.append(wrong_task)
        unknown_model = json.loads(json.dumps(original))
        unknown_model["tasks"]["speech"]["zh"] = ["tts-not-in-catalog"]
        cases.append(unknown_model)
        for value in cases:
            with self.subTest(value=value):
                self.write_config(value)
                with self.assertRaises(RouterConfigError):
                    self.router.reload()

    def test_router_config_rejects_unknown_root_fields_and_duplicate_json_keys(self):
        original = json.loads(self.config_path.read_text(encoding="utf-8"))
        original["unexpected"] = True
        self.write_config(original)
        with self.assertRaises(RouterConfigError):
            self.router.reload()
        self.config_path.write_text(
            '{"schema_version":"1.0","schema_version":"1.0","tasks":{}}',
            encoding="utf-8",
        )
        with self.assertRaises(RouterConfigError):
            self.router.reload()


if __name__ == "__main__":
    unittest.main()
