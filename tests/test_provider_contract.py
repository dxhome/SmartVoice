from __future__ import annotations

import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from smartvoice.app import create_app
from smartvoice.config.settings import Settings
from smartvoice.domain.contracts import SynthesizedSpeech
from smartvoice.adapters.inference.sherpa_onnx.provider import SherpaOnnxProvider


class ProviderContractTests(unittest.TestCase):
    def test_spoken_language_identifier_uses_sherpa_whisper_tiny_api(self):
        from types import ModuleType, SimpleNamespace
        from unittest.mock import Mock, patch
        import numpy as np

        provider = SherpaOnnxProvider(Settings(data_dir=Path.cwd() / ".smartvoice-dev" / "lid-contract"))
        calls = {}

        class WhisperConfig:
            def __init__(self, **kwargs):
                calls["whisper"] = kwargs

        class IdentifierConfig:
            def __init__(self, **kwargs):
                calls["config"] = kwargs

        stream = SimpleNamespace(accept_waveform=Mock())
        identifier = SimpleNamespace(create_stream=Mock(return_value=stream), compute=Mock(return_value="fr"))
        sherpa = SimpleNamespace(
            SpokenLanguageIdentificationWhisperConfig=WhisperConfig,
            SpokenLanguageIdentificationConfig=IdentifierConfig,
            SpokenLanguageIdentification=Mock(return_value=identifier),
        )
        provider._sherpa = lambda: sherpa
        provider._decode_audio = lambda *_args: np.zeros(1600, dtype=np.float32)
        with patch.dict("sys.modules", {"av": ModuleType("av")}):
            with patch("smartvoice.adapters.inference.sherpa_onnx.provider.installed_language_id_model_dir", return_value=Path("/lid")):
                result = provider.identify_language(b"fixture")

        self.assertEqual(calls["whisper"], {
            "encoder": str(Path("/lid") / "tiny-encoder.int8.onnx"),
            "decoder": str(Path("/lid") / "tiny-decoder.int8.onnx"),
        })
        self.assertEqual(calls["config"]["num_threads"], provider.settings.num_threads)
        self.assertEqual(result["language"], "fr")
        self.assertEqual(result["model"], "sherpa-onnx-whisper-tiny-int8-language-id")
        stream.accept_waveform.assert_called_once()
        identifier.compute.assert_called_once_with(stream)

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
                    {"id": "stt-sensevoice-small-int8", "task": "transcription"},
                    {"id": "tts-kokoro-multilingual-v1-1-zh-en", "task": "speech"},
                ]

            def runtime(self):
                return {"backend": self.backend, "actual_device": "cpu"}

            def capabilities(self):
                return {"api_version": "v1", "capability_schema_version": "1.0", "tasks": []}

            def transcribe(self, audio, language="auto", model_id=None):
                return {"text": "contract", "language": language, "duration": 1.0, "model": model_id, "device": "cpu"}

            def synthesize(self, text, voice="default", speed=1.0, model_id=None, language="auto"):
                return SynthesizedSpeech(audio=b"RIFF-test", sample_rate=24000, duration=1.0)

        class ProviderB(ProviderA):
            backend = "provider-b"

        for provider_type in (ProviderA, ProviderB):
            with self.subTest(provider=provider_type.backend):
                data_dir = Path.cwd() / ".smartvoice-dev" / f"provider-contract-{uuid.uuid4().hex}"
                data_dir.mkdir(parents=True)
                (data_dir / "router.json").write_text(
                    '{"schema_version":"1.0","tasks":'
                    '{"transcription":{"zh":["stt-sensevoice-small-int8"]},'
                    '"speech":{"en":["tts-kokoro-multilingual-v1-1-zh-en"]}}}',
                    encoding="utf-8",
                )
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
                        data={"model": "smartvoice-auto", "language": "zh"},
                    )
                    speech = client.post("/v1/audio/speech", json={
                        "model": "smartvoice-auto", "input": "Hello", "language": "en",
                    })
                    self.assertEqual(transcription.status_code, 200)
                    self.assertEqual(transcription.json()["text"], "contract")
                    self.assertEqual(speech.status_code, 200)
                    self.assertEqual(speech.headers["content-type"], "audio/wav")
                    self.assertEqual(runtime.json()["backend"], provider_type.backend)


if __name__ == "__main__":
    unittest.main()
