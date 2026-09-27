from __future__ import annotations

import unittest
import uuid
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

    def transcribe(self, audio, language="auto"):
        assert audio == b"audio fixture"
        return {
            "text": "测试转写",
            "language": "zh",
            "duration": 1.25,
            "model": "sensevoice-small-local",
            "device": "cpu",
            "segments": [{"text": "测试", "start": 0.1}],
        }

    def synthesize(self, text, voice="default", speed=1.0):
        assert text == "你好"
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
            self.assertIn("model_not_installed:sensevoice-small-local", response.json()["reasons"])

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

    def test_tts_returns_wav_and_metadata_headers(self):
        with self.make_client() as client:
            response = client.post("/v1/audio/speech", json={"input": "你好"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "audio/wav")
            self.assertEqual(response.headers["x-audio-sample-rate"], "24000")
            self.assertTrue(response.content.startswith(b"RIFF"))

    def test_tts_rejects_whitespace_only_input(self):
        with self.make_client() as client:
            response = client.post("/v1/audio/speech", json={"input": "   "})
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["error"]["code"], "validation_error")
            self.assertEqual(response.json()["error"]["details"][0]["field"], "body.input")
            self.assertIn("non-whitespace", response.json()["error"]["details"][0]["message"])

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

    def test_remote_bind_addresses_are_not_allowed(self):
        from smartvoice.__main__ import _is_loopback

        self.assertTrue(_is_loopback("127.0.0.1"))
        self.assertTrue(_is_loopback("::1"))
        self.assertFalse(_is_loopback("0.0.0.0"))


if __name__ == "__main__":
    unittest.main()
