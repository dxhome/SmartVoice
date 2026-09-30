from __future__ import annotations

import json
import os
import sys
import uuid
import unittest
from pathlib import Path
from unittest.mock import patch

from smartvoice.config.settings import Settings


class SettingsTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "macOS data-directory semantics are tested on macOS")
    def test_macos_default_data_dir_uses_application_support(self):
        from smartvoice.config.settings import default_data_dir

        with patch.dict(os.environ, {}, clear=True), patch(
            "smartvoice.config.settings.Path.home", return_value=Path("/Users/example")
        ):
            self.assertEqual(default_data_dir(), Path("/Users/example/Library/Application Support/SmartVoice"))

    def test_json_configuration_and_environment_overrides(self):
        directory = Path.cwd() / ".smartvoice-dev" / f"settings-{uuid.uuid4().hex}"
        directory.mkdir(parents=True)
        config = directory / "smartvoice.json"
        config.write_text(json.dumps({
            "data_dir": str(directory / "models-home"),
            "server_host": "127.0.0.1",
            "server_port": 8123,
            "num_threads": 2,
            "max_queued_inference": 4,
        }), encoding="utf-8")
        with patch.dict(os.environ, {"SMARTVOICE_NUM_THREADS": "3"}, clear=False):
            settings = Settings.from_env(config)
        self.assertEqual(settings.server_port, 8123)
        self.assertEqual(settings.num_threads, 3)
        self.assertEqual(settings.max_queued_inference, 4)

    def test_unknown_config_keys_are_rejected(self):
        directory = Path.cwd() / ".smartvoice-dev" / f"settings-bad-{uuid.uuid4().hex}"
        directory.mkdir(parents=True)
        config = directory / "bad.json"
        config.write_text('{"typo_option": true}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unknown SmartVoice configuration"):
            Settings.from_env(config)

    def test_first_start_creates_editable_user_config_and_next_start_reads_it(self):
        directory = Path.cwd() / ".smartvoice-dev" / f"settings-bootstrap-{uuid.uuid4().hex}"
        with patch.dict(os.environ, {"SMARTVOICE_HOME": str(directory)}, clear=True):
            settings = Settings.from_env(initialize_user_config=True)
            config = directory / "smartvoice.json"
            self.assertTrue(config.is_file())
            defaults = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(defaults["server_host"], "127.0.0.1")
            self.assertEqual(settings.server_port, 8000)
            defaults["server_port"] = 8129
            config.write_text(json.dumps(defaults), encoding="utf-8")
            restarted = Settings.from_env(initialize_user_config=True)
        self.assertEqual(restarted.server_port, 8129)

    def test_environment_overrides_user_config_without_rewriting_it(self):
        directory = Path.cwd() / ".smartvoice-dev" / f"settings-precedence-{uuid.uuid4().hex}"
        directory.mkdir(parents=True)
        config = directory / "smartvoice.json"
        config.write_text('{"server_port": 8129}', encoding="utf-8")
        with patch.dict(os.environ, {"SMARTVOICE_HOME": str(directory), "SMARTVOICE_PORT": "8130"}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.server_port, 8130)
        self.assertEqual(json.loads(config.read_text(encoding="utf-8"))["server_port"], 8129)

    def test_cli_data_dir_selects_its_user_config(self):
        directory = Path.cwd() / ".smartvoice-dev" / f"settings-cli-home-{uuid.uuid4().hex}"
        settings = Settings.from_env(data_dir_override=directory, initialize_user_config=True)
        self.assertEqual(settings.data_dir, directory.resolve())
        self.assertTrue((directory / "smartvoice.json").is_file())


if __name__ == "__main__":
    unittest.main()
