"""Real bilingual STT functional and duration-boundary regressions."""
import importlib.util
from contextlib import ExitStack
import io
import os
import unittest
import wave

from fastapi.testclient import TestClient

from smartvoice.app import create_app
from smartvoice.config.settings import Settings
from tests.stt_regression_audio import build_audio, short_samples
from tests.inference_environment import isolated_runtime


_SETTINGS = Settings.from_env()
_DEPS_READY = all(importlib.util.find_spec(name) is not None for name in ("sherpa_onnx", "av", "numpy"))
_FULL = os.environ.get("SMARTVOICE_TEST_SUITE") == "full"
_MODELS = (
    "stt-sensevoice-small-int8",
    "stt-qwen3-asr-600m-int8",
    "stt-whisper-base-multilingual-int8",
)
if os.environ.get('SMARTVOICE_TEST_STT_MODELS'):
    _MODELS=tuple(m for m in _MODELS if m in os.environ['SMARTVOICE_TEST_STT_MODELS'].split(','))
_WINDOWS = {
    "stt-sensevoice-small-int8": 15,
    "stt-qwen3-asr-600m-int8": 15,
    "stt-whisper-base-multilingual-int8": 25,
}

@unittest.skipUnless(_DEPS_READY, "Install SmartVoice runtime dependencies to run real STT regressions")
class RealSttAudioRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if _FULL and "stt-whisper-base-multilingual-int8" in _MODELS:
            import sherpa_onnx
            if sherpa_onnx.__version__ != "1.13.8+smartvoice.whisper2":
                raise RuntimeError("Full STT regression requires the validated Whisper whisper2 repair wheel")
        environment = ExitStack()
        cls.addClassCleanup(environment.close)
        settings, provider = environment.enter_context(isolated_runtime(_SETTINGS))
        cls.client = TestClient(create_app(settings=settings, provider=provider))
        cls.client.__enter__()
        cls.provider = cls.client.app.state.provider
        cls.installed = {str(item["id"]) for item in cls.provider.installed_models()}
        if _FULL:
            missing = sorted(set(_MODELS) - cls.installed)
            if missing:
                cls.client.__exit__(None, None, None)
                raise RuntimeError(
                    "Full STT regression requires runtime-available models: " + ", ".join(missing)
                )

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def _transcribe(self, model, language, filename, audio, duration):
        if model not in self.installed:
            self.skipTest(f"{model} is not installed and runtime-available")
        if model == "stt-whisper-base-multilingual-int8":
            import sherpa_onnx
            if sherpa_onnx.__version__ != "1.13.8+smartvoice.whisper2":
                self.skipTest("Whisper regression requires the validated sherpa-onnx whisper2 repair wheel")
        window = float(self.provider.segment_limits(model)["audio_seconds"])
        response = self.client.post(
            "/v1/audio/transcriptions",
            files={"file": (filename, audio, "audio/wav")},
            data={"model": model, "language": language, "response_format": "verbose_json"},
        )
        self.assertEqual(response.status_code, 200, f"{model}/{language}/{duration}s returned {response.status_code}")
        result = response.json()
        self.assertEqual(result["model"], model)
        self.assertEqual(result["language"], language)
        self.assertIsInstance(result.get("text"), str)
        self.assertTrue(result["text"].strip())
        self.assertAlmostEqual(result["duration"], duration, delta=0.01)
        if duration > window:
            self.assertGreaterEqual(result.get("chunk_count", 1), 2)
        else:
            self.assertLessEqual(result.get("chunk_count", 1), 1)


def _make_case(model, language, kind, duration=None, fixture=None):
    def test(self):
        if kind == "short":
            filename, audio = fixture
            with wave.open(io.BytesIO(audio), "rb") as source:
                actual_duration = source.getnframes()/source.getframerate()
            self._transcribe(model, language, filename, audio, actual_duration)
        else:
            audio = build_audio(duration, language)
            if kind == "long":
                self.assertLess(len(audio), _SETTINGS.max_upload_bytes)
            self._transcribe(model, language, f"{kind}-{duration:g}s.wav", audio, duration)
    suffix = fixture[0].replace(".", "_") if fixture else str(duration).replace(".", "_")
    test.__name__ = f"test_{model.replace('-', '_')}_{language}_{kind}_{suffix}"
    return test


# Separate cases keep every model/language/duration result visible in unittest.
for _model in _MODELS:
    for _language in ("zh", "en"):
        for _fixture in short_samples(_language):
            setattr(RealSttAudioRegressionTests,
                    f"test_{_model.replace('-', '_')}_{_language}_short_{_fixture[0].replace('.', '_')}",
                    _make_case(_model, _language, "short", fixture=_fixture))
        _window = _WINDOWS[_model]
        for _duration in (_window-0.1, _window, _window+0.1):
            setattr(RealSttAudioRegressionTests,
                    f"test_{_model.replace('-', '_')}_{_language}_boundary_{str(_duration).replace('.', '_')}",
                    _make_case(_model, _language, "boundary", duration=_duration))
        setattr(RealSttAudioRegressionTests,
                f"test_{_model.replace('-', '_')}_{_language}_medium_75s",
                _make_case(_model, _language, "medium", duration=75))
if _FULL:
    for _model in _MODELS:
        for _language in ("zh", "en"):
            for _duration in (300, 590):
                setattr(RealSttAudioRegressionTests,
                        f"test_{_model.replace('-', '_')}_{_language}_long_{_duration}s",
                        _make_case(_model, _language, "long", duration=_duration))
