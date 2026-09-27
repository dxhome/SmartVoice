from __future__ import annotations

import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from smartvoice.app import create_app
from smartvoice.config.settings import Settings
from smartvoice.adapters.inference.sherpa_onnx.provider import SherpaOnnxProvider


class ProviderContractTests(unittest.TestCase):
    def test_tts_text_chunking_preserves_text_and_bounds_chunks(self):
        text = "你好。" + ("长文本测试 " * 75) + "结束！"
        chunks = SherpaOnnxProvider._split_tts_text(text, 80)

        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk) <= 80 for chunk in chunks))

    def test_tts_text_chunking_prefers_sentence_boundaries(self):
        first = "第一句。"
        second = "第二句。"
        chunks = SherpaOnnxProvider._split_tts_text(first + second, 6)
        self.assertEqual(chunks, [first, second])

    def test_public_api_contract_is_shared_by_two_provider_substitutes(self):
        class ProviderA:
            backend = "provider-a"

            def installed_models(self):
                return [
                    {"id": "sensevoice-small-local", "task": "transcription"},
                    {"id": "melo-tts-zh-en-local", "task": "speech"},
                ]

            def runtime(self):
                return {"backend": self.backend, "actual_device": "cpu"}

            def capabilities(self):
                return {"api_version": "v1", "capability_schema_version": "1.0", "tasks": []}

            def transcribe(self, audio, language="auto", model_id=None):
                return {"text": "contract", "language": language, "duration": 1.0, "model": model_id, "device": "cpu"}

            def synthesize(self, text, voice="default", speed=1.0, model_id=None):
                return b"RIFF-test", 24000, 1.0

        class ProviderB(ProviderA):
            backend = "provider-b"

        for provider_type in (ProviderA, ProviderB):
            with self.subTest(provider=provider_type.backend):
                data_dir = Path.cwd() / ".smartvoice-dev" / f"provider-contract-{uuid.uuid4().hex}"
                data_dir.mkdir(parents=True)
                app = create_app(settings=Settings(data_dir=data_dir), provider=provider_type())
                with TestClient(app) as client:
                    models = client.get("/v1/models")
                    runtime = client.get("/v1/runtime")
                    capabilities = client.get("/v1/capabilities")
                    ready = client.get("/ready")
                    self.assertEqual(models.status_code, 200)
                    self.assertEqual(runtime.status_code, 200)
                    self.assertEqual(capabilities.status_code, 200)
                    self.assertEqual(ready.status_code, 200)
                    transcription = client.post(
                        "/v1/audio/transcriptions",
                        files={"file": ("sample.wav", b"fixture", "audio/wav")},
                    )
                    speech = client.post("/v1/audio/speech", json={"input": "Hello"})
                    self.assertEqual(transcription.status_code, 200)
                    self.assertEqual(transcription.json()["text"], "contract")
                    self.assertEqual(speech.status_code, 200)
                    self.assertEqual(speech.headers["content-type"], "audio/wav")
                    self.assertEqual(runtime.json()["backend"], provider_type.backend)


if __name__ == "__main__":
    unittest.main()
