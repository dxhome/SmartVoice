from __future__ import annotations

import unittest
import io
import json
import os
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from smartvoice.__main__ import _format_model_list, _models, _router, _serve


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
                "id": "tts-kokoro-multilingual-v1-1-zh-en",
                "name": "Kokoro 1.1",
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
        self.assertIn("Not installed (2)", output)
        self.assertLess(output.index("Installed (1)"), output.index("Not installed (2)"))
        self.assertLess(output.index("  STT (1)"), output.index("Not installed (2)"))
        self.assertLess(output.index("  STT (1)", output.index("Not installed (2)")), output.index("  TTS (1)"))
        self.assertIn("    - SenseVoice Small INT8 (stt-sensevoice-small-int8) | zh, en | sherpa-onnx", output)
        self.assertNotIn("Default", output)
        self.assertIn("    - Paraformer Chinese (paraformer-zh-local) | zh | funasr", output)
        self.assertIn("    - Kokoro 1.1 (tts-kokoro-multilingual-v1-1-zh-en) | zh, en | sherpa-onnx", output)
        self.assertIn("zh, en", output)
        self.assertNotRegex(output, r"[\u4e00-\u9fff]")

    def test_install_all_includes_every_catalog_model_and_language_detector(self):
        from smartvoice.services.model_registry import load_catalog
        specs = load_catalog()
        entries = [{'id': spec.id, 'status': 'uninstalled', 'installation_method': spec.installation_method} for spec in specs]
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {'SMARTVOICE_HOME': temporary}), patch(
                'smartvoice.__main__.ModelManagementService') as management, patch(
                'smartvoice.adapters.storage.converted_model.check_dependencies'), patch(
                'smartvoice.__main__._refresh_running_model_availability'), patch(
                'smartvoice.services.spoken_language_identifier.ensure_language_id_model', return_value=temporary) as language_id:
            service = management.return_value
            service.catalog.return_value = {'data': entries}
            service.get_spec.side_effect = lambda mid: next(spec for spec in specs if spec.id == mid)
            service.install.return_value = temporary
            with redirect_stdout(io.StringIO()): _models(['install', 'all'])
            self.assertEqual([call.args[0] for call in service.install.call_args_list], [spec.id for spec in specs])
            language_id.assert_called_once()
        output = _format_model_list([{'id': spec.id, 'name': spec.name, 'task': spec.task,
            'category': spec.category, 'subcategory': spec.subcategory, 'installed': False} for spec in specs])
        self.assertIn('Streaming (5)', output)
        for group in ('STT', 'MT', 'CT'): self.assertIn('    ' + group, output)

    def test_empty_model_list_has_helpful_message(self):
        self.assertEqual(_format_model_list([]), "The model catalog is empty.")

    def test_model_removal_uses_only_the_uninstall_command(self):
        errors = io.StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit) as raised:
            _models(["remove", "stt-sensevoice-small-int8"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("invalid choice", errors.getvalue())

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

    def test_model_refresh_forces_full_integrity_check_on_running_service(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return json.dumps({"status": "refreshed", "count": 1}).encode()

        output = io.StringIO()
        with tempfile.TemporaryDirectory() as data_dir, patch.dict(
            os.environ, {"SMARTVOICE_HOME": data_dir}, clear=True
        ), patch("smartvoice.__main__.urllib.request.urlopen", return_value=Response()) as urlopen:
            with redirect_stdout(output):
                _models(["refresh"])

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8000/v1/models/refresh")
        self.assertEqual(json.loads(request.data), {"force_integrity_check": True})
        self.assertIn("Model availability refreshed", output.getvalue())

    def test_router_reload_rejects_non_loopback_host(self):
        with tempfile.TemporaryDirectory() as data_dir, patch.dict(os.environ, {"SMARTVOICE_HOME": data_dir}, clear=True):
            with self.assertRaises(SystemExit) as raised:
                _router(["--host", "192.168.1.4", "reload"])
        self.assertEqual(raised.exception.code, 2)

    def test_service_accepts_explicit_external_http_bind_and_warns(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as data_dir, patch.dict(
            os.environ, {"SMARTVOICE_HOME": data_dir}, clear=True
        ), patch("smartvoice.__main__.socket.create_connection", side_effect=OSError("not bound")), patch(
            "smartvoice.__main__._print_startup_banner"
        ), patch("smartvoice.app.create_app", return_value=object()), patch(
            "smartvoice.__main__.uvicorn.run"
        ) as run:
            with redirect_stdout(output):
                _serve(["--host", "0.0.0.0", "--port", "8123"])

        self.assertEqual(run.call_args.kwargs["host"], "0.0.0.0")
        self.assertEqual(run.call_args.kwargs["port"], 8123)
        self.assertIn("over HTTP without authentication", output.getvalue())

    def test_service_defaults_to_loopback(self):
        with tempfile.TemporaryDirectory() as data_dir, patch.dict(
            os.environ, {"SMARTVOICE_HOME": data_dir}, clear=True
        ), patch("smartvoice.__main__.socket.create_connection", side_effect=OSError("not bound")), patch(
            "smartvoice.__main__._print_startup_banner"
        ), patch("smartvoice.app.create_app", return_value=object()), patch(
            "smartvoice.__main__.uvicorn.run"
        ) as run:
            _serve([])

        self.assertEqual(run.call_args.kwargs["host"], "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
