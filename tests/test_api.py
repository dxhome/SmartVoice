from __future__ import annotations

import unittest
import uuid
import hashlib
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from smartvoice.app import create_app
from smartvoice.config.settings import Settings


class FakeProvider:
    def installed_models(self):
        return [
            {"id": "sensevoice-small-local", "task": "transcription"},
            {"id": "melo-tts-zh-en-local", "task": "speech"},
        ]

    def runtime(self):
        return {"backend": "test-double", "actual_device": "cpu"}

    def capabilities(self):
        return {"api_version": "v1", "tasks": ["transcription", "speech"]}

    def transcribe(self, audio, language="auto", model_id=None):
        assert audio == b"audio fixture"
        return {
            "text": "测试转写",
            "language": "zh",
            "duration": 1.25,
            "model": "sensevoice-small-local",
            "device": "cpu",
            "segments": [{"text": "测试", "start": 0.1}],
        }

    def synthesize(self, text, voice="default", speed=1.0, model_id=None):
        assert text in {"你好", "Hello"}
        return b"RIFF-test-wav", 24000, 0.8


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.data_dir = Path.cwd() / ".smartvoice-dev" / f"api-test-{uuid.uuid4().hex}"
        self.data_dir.mkdir(parents=True)

    def make_client(self, max_upload_bytes=100):
        settings = Settings(data_dir=self.data_dir, max_upload_bytes=max_upload_bytes)
        return TestClient(create_app(settings=settings, provider=FakeProvider()))

    def test_health_and_readiness(self):
        with self.make_client() as client:
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.get("/health").json()["status"], "ok")
            self.assertEqual(client.get("/ready").json()["status"], "ready")

    def test_readiness_reports_missing_models(self):
        class EmptyProvider(FakeProvider):
            def installed_models(self):
                return []

        settings = Settings(data_dir=self.data_dir)
        with TestClient(create_app(settings=settings, provider=EmptyProvider())) as client:
            response = client.get("/ready")
            self.assertEqual(response.status_code, 503)
            self.assertIn("model_not_available_for_task:transcription", response.json()["reasons"])
            self.assertIn("model_not_available_for_task:speech", response.json()["reasons"])

    def test_readiness_is_task_based_not_tied_to_model_ids(self):
        class DifferentCatalogProvider(FakeProvider):
            def installed_models(self):
                return [
                    {"id": "another-asr", "task": "transcription"},
                    {"id": "another-tts", "task": "speech"},
                ]

        settings = Settings(data_dir=self.data_dir)
        with TestClient(create_app(settings=settings, provider=DifferentCatalogProvider())) as client:
            response = client.get("/ready")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["available_models"], ["another-asr", "another-tts"])

    def test_catalog_reports_installed_state_and_storage(self):
        with self.make_client() as client:
            response = client.get("/v1/catalog")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["data"])
            self.assertIn("available_disk_bytes", response.json()["storage"])

    def test_runtime_includes_cpu_host_and_process_resource_metrics(self):
        from smartvoice.adapters.inference.sherpa_onnx.provider import SherpaOnnxProvider

        provider = SherpaOnnxProvider(Settings(data_dir=self.data_dir))
        with TestClient(create_app(settings=provider.settings, provider=provider)) as client:
            response = client.get("/v1/runtime")
        self.assertEqual(response.status_code, 200)
        runtime = response.json()
        self.assertEqual(runtime["actual_device"], "cpu")
        self.assertIn("processor", runtime["host"])
        self.assertGreater(runtime["process"]["pid"], 0)
        self.assertGreaterEqual(runtime["process"]["cpu_time_seconds"], 0)

    def test_uninstall_route_refuses_a_loaded_model(self):
        class LoadedProvider(FakeProvider):
            def is_model_loaded(self, model_id):
                return model_id == "sensevoice-small-local"

        with TestClient(create_app(
            settings=Settings(data_dir=self.data_dir), provider=LoadedProvider()
        )) as client:
            response = client.delete("/v1/models/sensevoice-small-local")
        self.assertEqual(response.status_code, 400)
        self.assertIn("loaded by the running service", response.json()["error"]["message"])

    def test_model_download_job_routes_delegate_to_manager(self):
        class Jobs:
            def start_download(self, model_id):
                return {"job_id": "job-123", "model_id": model_id, "status": "running"}

            def get(self, job_id):
                return {"job_id": job_id, "status": "completed"}

            def cancel(self, job_id):
                return {"job_id": job_id, "status": "canceling"}

            def cancel_all(self):
                return None

        with self.make_client() as client:
            client.app.state.model_jobs = Jobs()
            started = client.post("/v1/models/sensevoice-small-local/download")
            self.assertEqual(started.status_code, 202)
            self.assertEqual(started.json()["job_id"], "job-123")
            self.assertEqual(client.get("/v1/jobs/job-123").json()["status"], "completed")
            self.assertEqual(client.delete("/v1/jobs/job-123").json()["status"], "canceling")

    def test_model_activation_export_uninstall_and_import_routes(self):
        from smartvoice.services.model_catalog import get_model_spec

        with self.make_client() as client:
            settings = client.app.state.settings
            spec = get_model_spec("sensevoice-small-local")
            model_dir = settings.models_dir / spec.id
            model_dir.mkdir(parents=True)
            files = {}
            file_hashes = {}
            for name in spec.required_files:
                content = f"fixture:{name}".encode()
                (model_dir / name).write_bytes(content)
                files[name] = name
                file_hashes[name] = hashlib.sha256(content).hexdigest()
            (model_dir / "smartvoice-model.json").write_text(json.dumps({
                "schema_version": "1.0",
                "id": spec.id,
                "task": spec.task,
                "source": spec.source,
                "archive_sha256": spec.archive_sha256,
                "files": files,
                "file_sha256": file_hashes,
            }), encoding="utf-8")

            activated = client.put(f"/v1/models/{spec.id}/activation")
            self.assertEqual(activated.status_code, 200, activated.text)
            self.assertEqual(activated.json()["active_models"][spec.task], spec.id)
            exported = client.get(f"/v1/models/{spec.id}/export")
            self.assertEqual(exported.status_code, 200)
            self.assertEqual(exported.headers["content-type"], "application/zip")
            removed = client.delete(f"/v1/models/{spec.id}")
            self.assertEqual(removed.status_code, 200, removed.text)
            imported = client.post(
                "/v1/models/import",
                files={"file": ("offline.zip", exported.content, "application/zip")},
            )
            self.assertEqual(imported.status_code, 201, imported.text)
            self.assertEqual(imported.json()["id"], spec.id)

    def test_transcription_response_and_request_id(self):
        with self.make_client() as client:
            response = client.post(
                "/v1/audio/transcriptions",
                headers={"X-Request-ID": "test-123"},
                files={"file": ("sample.wav", b"audio fixture", "audio/wav")},
                data={"language": "zh", "timestamps": "true", "response_format": "verbose_json"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["X-Request-ID"], "test-123")
            self.assertEqual(response.json()["text"], "测试转写")
            self.assertTrue(response.json()["segments"])

    def test_transcription_rejects_unknown_fields(self):
        with self.make_client() as client:
            response = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("sample.wav", b"audio fixture", "audio/wav")},
                data={"unexpected": "value"},
            )
            self.assertEqual(response.status_code, 400)
            self.assertIn("unexpected", response.json()["error"]["message"])

    def test_transcription_can_select_catalog_model(self):
        with self.make_client() as client:
            response = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("sample.wav", b"audio fixture", "audio/wav")},
                data={"model": "sensevoice-small-local", "language": "en"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["model"], "sensevoice-small-local")

    def test_tts_returns_wav_and_metadata_headers(self):
        with self.make_client() as client:
            response = client.post("/v1/audio/speech", json={"input": "你好"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "audio/wav")
            self.assertEqual(response.headers["x-audio-sample-rate"], "24000")
            self.assertIn("x-inference-time-seconds", response.headers)
            self.assertEqual(response.headers["x-requested-language"], "auto")
            self.assertIn("x-process-working-set-bytes", response.headers)
            self.assertTrue(response.content.startswith(b"RIFF"))

    def test_tts_language_hint_is_returned_in_metadata(self):
        with self.make_client() as client:
            response = client.post("/v1/audio/speech", json={"input": "Hello", "language": "en"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["x-requested-language"], "en")
            self.assertEqual(response.headers["x-text-language"], "en")

    def test_tts_rejects_language_hint_that_conflicts_with_input_script(self):
        with self.make_client() as client:
            response = client.post("/v1/audio/speech", json={"input": "你好", "language": "en"})
            self.assertEqual(response.status_code, 400)
            self.assertIn("conflicts", response.json()["error"]["message"])

    def test_tts_rejects_whitespace_only_input(self):
        with self.make_client() as client:
            response = client.post("/v1/audio/speech", json={"input": "   "})
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["error"]["code"], "validation_error")
            self.assertEqual(response.json()["error"]["details"][0]["field"], "body.input")
            self.assertIn("non-whitespace", response.json()["error"]["details"][0]["message"])

    def test_inference_execution_timeout_maps_to_gateway_timeout(self):
        class SlowProvider(FakeProvider):
            def transcribe(self, audio, language="auto", model_id=None):
                time.sleep(0.08)
                return super().transcribe(audio, language, model_id)

        settings = Settings(data_dir=self.data_dir, inference_execution_timeout_seconds=0.01)
        with TestClient(create_app(settings=settings, provider=SlowProvider())) as client:
            response = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("sample.wav", b"audio fixture", "audio/wav")},
            )
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["error"]["code"], "inference_timeout")

    def test_request_logs_include_request_id_status_and_duration(self):
        with self.make_client() as client, self.assertLogs("smartvoice.api", level="INFO") as captured:
            response = client.get("/health", headers={"X-Request-ID": "log-test-123"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(any(
            "request_completed" in line
            and "request_id=log-test-123" in line
            and "status=200" in line
            and "duration_ms=" in line
            for line in captured.output
        ))

    def test_upload_size_is_bounded(self):
        with self.make_client(max_upload_bytes=4) as client:
            response = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("sample.wav", b"too large", "audio/wav")},
            )
            self.assertEqual(response.status_code, 413)
            self.assertEqual(response.json()["error"]["code"], "file_too_large")
            self.assertIn("request_id", response.json()["error"])

    def test_remote_bind_addresses_are_not_allowed(self):
        from smartvoice.__main__ import _is_loopback

        self.assertTrue(_is_loopback("127.0.0.1"))
        self.assertTrue(_is_loopback("::1"))
        self.assertFalse(_is_loopback("0.0.0.0"))


if __name__ == "__main__":
    unittest.main()
