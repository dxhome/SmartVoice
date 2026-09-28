from __future__ import annotations

import json
import os
import uuid
import unittest
from pathlib import Path
from unittest.mock import patch

from smartvoice.config.settings import Settings


class SettingsTests(unittest.TestCase):
    def test_macos_default_data_dir_uses_application_support(self):
        from smartvoice.config.settings import default_data_dir

        with patch.dict(os.environ, {}, clear=True), patch("smartvoice.config.settings.sys.platform", "darwin"), patch("smartvoice.config.settings.Path.home", return_value=Path("/Users/example")):
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


if __name__ == "__main__":
    unittest.main()
