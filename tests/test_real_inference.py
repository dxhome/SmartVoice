"""Optional smoke tests that exercise downloaded local models when available."""

from __future__ import annotations

import importlib.util
import io
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from smartvoice.app import create_app
from smartvoice.config.settings import Settings
from smartvoice.services.model_storage import model_directory
from smartvoice.services.model_registry import load_catalog
from smartvoice.services.spoken_language_identifier import installed_language_id_model_dir


_settings = Settings.from_env()
_dependencies_ready = all(importlib.util.find_spec(name) is not None for name in ("sherpa_onnx", "av", "numpy"))
_models_ready = all(
    (model_directory(_settings, model_id) / "smartvoice-model.json").is_file()
    for model_id in ("stt-sensevoice-small-int8", "tts-kokoro-multilingual-v1-1-zh-en")
) and installed_language_id_model_dir(_settings) is not None
_catalog = load_catalog()


def _encode_audio(raw_wav: bytes, container_format: str, codec: str, rate: int, sample_format: str) -> bytes:
    import av

    source = av.open(io.BytesIO(raw_wav))
    output_buffer = io.BytesIO()
    destination = av.open(output_buffer, mode="w", format=container_format)
    stream = destination.add_stream(codec, rate=rate)
    stream.layout = "mono"
    stream.codec_context.format = sample_format
    resampler = av.AudioResampler(format=sample_format, layout="mono", rate=rate)
    for frame in source.decode(audio=0):
        for converted in resampler.resample(frame):
            for packet in stream.encode(converted):
                destination.mux(packet)
    for converted in resampler.resample(None):
        for packet in stream.encode(converted):
            destination.mux(packet)
    for packet in stream.encode(None):
        destination.mux(packet)
    destination.close()
    source.close()
    return output_buffer.getvalue()


@unittest.skipUnless(
    _models_ready and _dependencies_ready,
    "Install SenseVoice, Kokoro, Whisper Tiny language-ID assets, and [inference] dependencies to run routed integration scenarios",
)
class RealInferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = TestClient(create_app(settings=_settings))
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        super().tearDownClass()

    @classmethod
    def _audio_sample(cls, language: str) -> tuple[str, bytes]:
        sample = next(model_directory(_settings, "stt-sensevoice-small-int8").rglob(f"{language}.wav"))
        return sample.name, sample.read_bytes()

    def test_service_discovery_reports_real_models_and_cpu_runtime(self):
        health = self.client.get("/health")
        ready = self.client.get("/ready")
        runtime = self.client.get("/v1/runtime")
        models = self.client.get("/v1/models")
        capabilities = self.client.get("/v1/capabilities")

        self.assertEqual(health.status_code, 200)
        self.assertEqual(ready.status_code, 200, ready.text)
        self.assertEqual(runtime.status_code, 200)
        self.assertEqual(runtime.json()["actual_device"], "cpu")
        self.assertGreater(runtime.json()["host"]["logical_cpu_count"], 0)
        self.assertGreater(runtime.json()["host"]["total_physical_memory_bytes"], 0)
        self.assertGreater(runtime.json()["process"].get("working_set_bytes", runtime.json()["process"].get("peak_working_set_bytes", 0)), 0)
        self.assertEqual(models.status_code, 200)
        model_ids = {item["id"] for item in models.json()["data"]}
        self.assertIn("stt-sensevoice-small-int8", model_ids)
        self.assertIn("tts-kokoro-multilingual-v1-1-zh-en", model_ids)
        self.assertTrue(capabilities.json()["language_identification"]["available"])

    def test_real_stt_transcribes_chinese_and_english_with_segments(self):
        for language in ("zh", "en"):
            filename, audio_bytes = self._audio_sample(language)
            response = self.client.post(
                "/v1/audio/transcriptions",
                files={"file": (filename, audio_bytes, "audio/wav")},
                data={
                    "model": "smartvoice-auto", "language": language,
                    "response_format": "verbose_json", "timestamps": "true",
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["text"])
            self.assertEqual(response.json()["language"], language)
            self.assertTrue(response.json()["segments"])
            self.assertEqual(response.json()["model_mode"], "router")

    def test_real_audio_language_detection_routes_chinese_and_english(self):
        for language in ("zh", "en"):
            filename, audio_bytes = self._audio_sample(language)
            response = self.client.post(
                "/v1/audio/transcriptions",
                files={"file": (filename, audio_bytes, "audio/wav")},
                data={"model": "smartvoice-auto", "language": "auto"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["text"])
            self.assertEqual(response.json()["language_source"], "model_detection")
            self.assertEqual(response.json()["language"], language)

    def test_real_stt_supports_direct_model_selection_and_audio_formats(self):
        filename, source_wav = self._audio_sample("zh")
        direct = self.client.post(
            "/v1/audio/transcriptions",
            files={"file": (filename, source_wav, "audio/wav")},
            data={"model": "stt-sensevoice-small-int8", "language": "zh"},
        )
        self.assertEqual(direct.status_code, 200, direct.text)
        self.assertEqual(direct.json()["model_mode"], "direct")
        self.assertTrue(direct.json()["text"])

        audio_formats = (
            ("sample.wav", source_wav, "audio/wav"),
            ("sample.mp3", _encode_audio(source_wav, "mp3", "libmp3lame", 44100, "fltp"), "audio/mpeg"),
            ("sample.flac", _encode_audio(source_wav, "flac", "flac", 16000, "s16"), "audio/flac"),
            ("sample.m4a", _encode_audio(source_wav, "mp4", "aac", 44100, "fltp"), "audio/mp4"),
        )
        for audio_name, audio_bytes, content_type in audio_formats:
            response = self.client.post(
                "/v1/audio/transcriptions",
                files={"file": (audio_name, audio_bytes, content_type)},
                data={"model": "smartvoice-auto", "language": "zh"},
            )
            self.assertEqual(response.status_code, 200, f"{audio_name}: {response.text}")
            self.assertTrue(response.json()["text"], audio_name)

    def test_real_stt_rejects_invalid_audio(self):
        response = self.client.post(
            "/v1/audio/transcriptions",
            files={"file": ("broken.wav", b"not an audio file", "audio/wav")},
            data={"model": "smartvoice-auto", "language": "zh"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "invalid_audio")

    def test_real_tts_supports_direct_and_routed_chinese_english_and_long_text(self):
        cases = (
            ("你好，这是 SmartVoice 的本地语音合成测试。", "zh", "smartvoice-auto"),
            ("Hello, this is a local SmartVoice speech synthesis test.", "en", "tts-kokoro-multilingual-v1-1-zh-en"),
            ("这是一个用于验证长文本语音合成分段处理的测试。" * 24, "zh", "smartvoice-auto"),
        )
        for text, language, model in cases:
            response = self.client.post("/v1/audio/speech", json={
                "model": model, "input": text, "language": language,
            })
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["content-type"], "audio/wav")
            self.assertEqual(response.headers["x-requested-language"], language)
            self.assertTrue(response.content.startswith(b"RIFF"))
            self.assertGreater(len(response.content), 44)

    def test_real_tts_detects_text_language_when_language_is_automatic(self):
        response = self.client.post("/v1/audio/speech", json={
            "model": "smartvoice-auto", "input": "Hello, this is an automatic language routing test.",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["x-resolved-language"], "en")
        self.assertEqual(response.headers["x-language-source"], "text_detection")
        self.assertTrue(response.content.startswith(b"RIFF"))


@unittest.skipUnless(
    _dependencies_ready,
    "Install [inference] dependencies to run model inference tests",
)
class InstalledCatalogModelInferenceTests(unittest.TestCase):
    """Directly smoke-test each catalog model that is installed and runtime-available."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = TestClient(create_app(settings=_settings))
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        super().tearDownClass()

    def _run_model_smoke_test(self, spec):
        installed_ids = {str(model["id"]) for model in self.client.app.state.provider.installed_models()}
        if spec.id not in installed_ids:
            self.skipTest(f"{spec.id} is not installed or its inference runtime is unavailable")

        # Prefer Chinese for this project's primary use case; use English for
        # multilingual models without Chinese, then the first concrete language.
        language = next(
            (code for code in ("zh", "en") if code in spec.languages),
            next((code for code in spec.languages if code != "auto"), None),
        )
        self.assertIsNotNone(language, f"{spec.id} has no concrete language to test")

        if spec.task == "transcription":
            sample = Path(__file__).parent / "fixtures" / "zh.wav"
            if not sample.is_file():
                self.skipTest("shared Chinese STT sample is unavailable")
            response = self.client.post(
                "/v1/audio/transcriptions",
                files={"file": (sample.name, sample.read_bytes(), "audio/wav")},
                data={"model": spec.id, "language": language},
            )
            self.assertEqual(response.status_code, 200, f"{spec.id}: {response.text}")
            result = response.json()
            self.assertEqual(result["model"], spec.id)
            self.assertEqual(result["model_mode"], "direct")
            self.assertTrue(result["text"])
        else:
            text = "你好，这是 SmartVoice 语音合成测试。" if language == "zh" else "Hello, this is a SmartVoice speech test."
            response = self.client.post("/v1/audio/speech", json={
                "model": spec.id, "input": text, "language": language,
            })
            self.assertEqual(response.status_code, 200, f"{spec.id}: {response.text}")
            self.assertEqual(response.headers["content-type"], "audio/wav")
            self.assertEqual(response.headers["x-model-id"], spec.id)
            self.assertTrue(response.content.startswith(b"RIFF"))
            self.assertGreater(len(response.content), 44)


def _model_test(spec):
    def test_model_inference(self):
        self._run_model_smoke_test(spec)

    test_model_inference.__name__ = f"test_{spec.id.replace('-', '_')}"
    test_model_inference.__doc__ = f"Run a one-language inference smoke test for {spec.id}."
    return test_model_inference


for _model_spec in _catalog:
    setattr(
        InstalledCatalogModelInferenceTests,
        f"test_{_model_spec.id.replace('-', '_')}",
        _model_test(_model_spec),
    )


if __name__ == "__main__":
    unittest.main()
