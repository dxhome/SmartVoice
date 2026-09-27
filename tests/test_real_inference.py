"""Optional smoke tests that exercise downloaded local models when available."""

from __future__ import annotations

import importlib.util
import unittest

from fastapi.testclient import TestClient

from smartvoice.app import create_app
from smartvoice.config.settings import Settings


_settings = Settings.from_env()
_models_ready = all(
    (_settings.models_dir / model_id / "smartvoice-model.json").is_file()
    for model_id in ("sensevoice-small-local", "melo-tts-zh-en-local")
)
_dependencies_ready = all(importlib.util.find_spec(name) is not None for name in ("sherpa_onnx", "av", "numpy"))


@unittest.skipUnless(_models_ready and _dependencies_ready, "Install both catalog models and [inference] dependencies")
class RealInferenceTests(unittest.TestCase):
    def test_local_chinese_stt_and_tts(self):
        app = create_app(settings=_settings)
        with TestClient(app) as client:
            for language in ("zh", "en"):
                sample = next((_settings.models_dir / "sensevoice-small-local").rglob(f"{language}.wav"))
                with sample.open("rb") as audio:
                    stt = client.post(
                        "/v1/audio/transcriptions",
                        files={"file": (sample.name, audio, "audio/wav")},
                        data={"language": language, "response_format": "verbose_json", "timestamps": "true"},
                    )
                self.assertEqual(stt.status_code, 200, stt.text)
                self.assertTrue(stt.json()["text"])
                self.assertEqual(stt.json()["language"], language)

            tts = client.post("/v1/audio/speech", json={"input": "你好，这是 SmartVoice 的本地语音合成测试。"})
            self.assertEqual(tts.status_code, 200, tts.text)
            self.assertEqual(tts.headers["content-type"], "audio/wav")
            self.assertTrue(tts.content.startswith(b"RIFF"))
            self.assertGreater(len(tts.content), 44)


if __name__ == "__main__":
    unittest.main()
