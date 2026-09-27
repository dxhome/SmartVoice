from __future__ import annotations

import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from smartvoice.app import create_app
from smartvoice.config.settings import Settings
from smartvoice.adapters.inference.sherpa_onnx.provider import SherpaOnnxProvider


class ProviderContractTests(unittest.TestCase):
    def test_whisper_model_uses_whisper_recognizer_factory(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        provider = SherpaOnnxProvider(Settings(data_dir=Path.cwd() / ".smartvoice-dev" / "whisper-contract"))
        factory = Mock(return_value=object())
        provider._sherpa = lambda: SimpleNamespace(OfflineRecognizer=SimpleNamespace(from_whisper=factory))
        recognizer = provider._get_recognizer("whisper", Path("encoder.onnx"), Path("decoder.onnx"), Path("tokens.txt"), "fr")
        self.assertIsNotNone(recognizer)
        self.assertEqual(factory.call_args.kwargs["language"], "fr")
        self.assertEqual(factory.call_args.kwargs["task"], "transcribe")

    def test_supertonic_config_uses_all_required_assets(self):
        from types import SimpleNamespace

        created = {}

        class SupertonicConfig:
            def __init__(self, **kwargs):
                created["assets"] = kwargs

        class ModelConfig:
            def __init__(self, **kwargs):
                created["model"] = kwargs

        class TtsConfig:
            def __init__(self, **kwargs):
                created["tts"] = kwargs

            def validate(self):
                return True

        class Tts:
            def __init__(self, _config):
                pass

        provider = SherpaOnnxProvider(Settings(data_dir=Path.cwd() / ".smartvoice-dev" / "supertonic-contract"))
        provider._sherpa = lambda: SimpleNamespace(
            OfflineTtsSupertonicModelConfig=SupertonicConfig,
            OfflineTtsModelConfig=ModelConfig,
            OfflineTtsConfig=TtsConfig,
            OfflineTts=Tts,
        )
        asset_names = ("duration_predictor.int8.onnx", "text_encoder.int8.onnx", "vector_estimator.int8.onnx",
                       "vocoder.int8.onnx", "tts.json", "unicode_indexer.bin", "voice.bin")
        provider._get_tts("supertonic", "supertonic", {name: Path(name) for name in asset_names}, None, None, None)
        self.assertEqual(set(created["assets"]), {"duration_predictor", "text_encoder", "vector_estimator", "vocoder", "tts_json", "unicode_indexer", "voice_style"})

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

            def synthesize(self, text, voice="default", speed=1.0, model_id=None, language="auto"):
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
