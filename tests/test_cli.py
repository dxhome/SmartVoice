from __future__ import annotations

import unittest
import io
import json
import os
import tempfile
from contextlib import redirect_stdout
from unittest.mock import patch

from smartvoice.__main__ import _format_model_list, _router


class ModelListOutputTests(unittest.TestCase):
    def test_model_list_groups_by_install_state_then_task_compactly(self):
        output = _format_model_list([
            {
                "id": "stt-sensevoice-small-int8",
                "name": "SenseVoice Small INT8",
                "task": "transcription",
                "languages": ["zh", "en"],
                "backend": "sherpa-onnx",
                "installed": True,
            },
            {
                "id": "tts-melo-zh-en",
                "name": "Melo TTS",
                "task": "speech",
                "languages": ["zh", "en"],
                "backend": "sherpa-onnx",
                "installed": False,
            },
            {
                "id": "paraformer-zh-local",
                "name": "Paraformer Chinese",
                "task": "transcription",
                "languages": ["zh"],
                "backend": "funasr",
                "installed": False,
            },
        ])

        self.assertIn("Installed (1)", output)
        self.assertIn("Uninstalled (2)", output)
        self.assertLess(output.index("Installed (1)"), output.index("Uninstalled (2)"))
        self.assertLess(output.index("  STT (1)"), output.index("Uninstalled (2)"))
        self.assertLess(output.index("  STT (1)", output.index("Uninstalled (2)")), output.index("  TTS (1)"))
        self.assertIn("    - SenseVoice Small INT8 (stt-sensevoice-small-int8) | zh, en | sherpa-onnx", output)
        self.assertNotIn("Default", output)
        self.assertIn("    - Paraformer Chinese (paraformer-zh-local) | zh | funasr", output)
        self.assertIn("    - Melo TTS (tts-melo-zh-en) | zh, en | sherpa-onnx", output)
        self.assertIn("zh, en", output)
        self.assertNotRegex(output, r"[\u4e00-\u9fff]")

    def test_empty_model_list_has_helpful_message(self):
        self.assertEqual(_format_model_list([]), "The model catalog is empty.")

    def test_router_reload_notifies_the_running_local_service(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return json.dumps({"status": "reloaded", "sha256": "abc123"}).encode()

        output = io.StringIO()
        with tempfile.TemporaryDirectory() as data_dir, patch.dict(os.environ, {"SMARTVOICE_HOME": data_dir}, clear=True):
            with patch("smartvoice.__main__.urllib.request.urlopen", return_value=Response()) as urlopen:
                with redirect_stdout(output):
                    _router(["reload"])
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8000/v1/router/reload")
        self.assertEqual(request.get_method(), "POST")
        self.assertIn("abc123", output.getvalue())

    def test_router_reload_rejects_non_loopback_host(self):
        with tempfile.TemporaryDirectory() as data_dir, patch.dict(os.environ, {"SMARTVOICE_HOME": data_dir}, clear=True):
            with self.assertRaises(SystemExit) as raised:
                _router(["--host", "192.168.1.4", "reload"])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
