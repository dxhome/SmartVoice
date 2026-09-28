"""Optional smoke tests that exercise downloaded local models when available."""

from __future__ import annotations

import importlib.util
import io
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


@unittest.skipUnless(_models_ready and _dependencies_ready, "Install both catalog models and [inference] dependencies")
class RealInferenceTests(unittest.TestCase):
    def test_local_chinese_stt_and_tts(self):
        app = create_app(settings=_settings)
        with TestClient(app) as client:
            runtime = client.get("/v1/runtime")
            self.assertEqual(runtime.status_code, 200)
            self.assertEqual(runtime.json()["actual_device"], "cpu")
            self.assertGreater(runtime.json()["host"]["logical_cpu_count"], 0)
            self.assertGreater(runtime.json()["host"]["total_physical_memory_bytes"], 0)
            process = runtime.json()["process"]
            self.assertGreater(
                process.get("working_set_bytes", process.get("peak_working_set_bytes", 0)), 0
            )
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

            source_wav = next((_settings.models_dir / "sensevoice-small-local").rglob("zh.wav")).read_bytes()
            audio_formats = (
                ("sample.wav", source_wav, "audio/wav"),
                ("sample.mp3", _encode_audio(source_wav, "mp3", "libmp3lame", 44100, "fltp"), "audio/mpeg"),
                ("sample.flac", _encode_audio(source_wav, "flac", "flac", 16000, "s16"), "audio/flac"),
                ("sample.m4a", _encode_audio(source_wav, "mp4", "aac", 44100, "fltp"), "audio/mp4"),
            )
            for filename, audio_bytes, content_type in audio_formats:
                response = client.post(
                    "/v1/audio/transcriptions",
                    files={"file": (filename, audio_bytes, content_type)},
                    data={"language": "zh"},
                )
                self.assertEqual(response.status_code, 200, f"{filename}: {response.text}")
                self.assertTrue(response.json()["text"], filename)

            invalid_audio = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("broken.wav", b"not an audio file", "audio/wav")},
                data={"language": "zh"},
            )
            self.assertEqual(invalid_audio.status_code, 422)
            self.assertEqual(invalid_audio.json()["error"]["code"], "invalid_audio")

            for language, text in (
                ("zh", "你好，这是 SmartVoice 的本地语音合成测试。"),
                ("en", "Hello, this is a local SmartVoice speech synthesis test."),
                ("zh", "这是一个用于验证长文本语音合成分段处理的测试。" * 24),
            ):
                tts = client.post("/v1/audio/speech", json={"input": text, "language": language})
                self.assertEqual(tts.status_code, 200, tts.text)
                self.assertEqual(tts.headers["content-type"], "audio/wav")
                self.assertEqual(tts.headers["x-requested-language"], language)
                self.assertTrue(tts.content.startswith(b"RIFF"))
                self.assertGreater(len(tts.content), 44)


if __name__ == "__main__":
    unittest.main()
