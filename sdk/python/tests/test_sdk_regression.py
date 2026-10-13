"""Regression-tier SDK tests against real installed models and a local service."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import unittest
import wave
from pathlib import Path

from smartvoice.config.settings import Settings
from smartvoice.services.model_storage import model_directory
from smartvoice.services.model_registry import get_model_spec
from smartvoice.services.streaming.planning import resolve
from smartvoice_sdk import SmartVoiceClient

from tests.streaming_long_audio import configuration, ROOT
from ._real_service import RealService


SETTINGS = Settings.from_env()
STT_MODEL = "stt-sensevoice-small-int8"
TTS_MODEL = "tts-kokoro-multilingual-v1-1-zh-en"
SAMPLE = ROOT / "tests" / "fixtures" / "zh.wav"


class _Catalog:
    get_spec = staticmethod(get_model_spec)


def _streaming_models() -> tuple[str, ...]:
    plan, _, _ = resolve(configuration("transcription", "zh"), _Catalog())
    return plan.model_ids


def _installed(model_id: str) -> bool:
    return (model_directory(SETTINGS, model_id) / "smartvoice-model.json").is_file()


_DEPENDENCIES_READY = all(importlib.util.find_spec(name) for name in ("sherpa_onnx", "av", "numpy"))
_STREAM_MODELS = _streaming_models()
_SDK_MODELS = tuple(dict.fromkeys((STT_MODEL, TTS_MODEL, *_STREAM_MODELS)))
_AVAILABLE_MODELS = tuple(model_id for model_id in _SDK_MODELS if _installed(model_id))
_API_MODELS_READY = _installed(STT_MODEL) and _installed(TTS_MODEL)
_STREAM_MODELS_READY = all(_installed(model_id) for model_id in _STREAM_MODELS)


@unittest.skipUnless(
    _DEPENDENCIES_READY and (_API_MODELS_READY or _STREAM_MODELS_READY),
    "Install SenseVoice/Kokoro or the zh streaming chain to run SDK real-inference regression",
)
class SDKRegressionTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.service = RealService(_AVAILABLE_MODELS).start()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.service.close()
        finally:
            super().tearDownClass()

    @unittest.skipUnless(_API_MODELS_READY, "Install the SenseVoice and Kokoro models for SDK STT/TTS regression")
    async def test_sdk_discovers_languages_and_runs_real_stt_and_tts(self):
        async with SmartVoiceClient(self.service.base_url, request_timeout=180) as client:
            capabilities = await client.capabilities()
            self.assertIn(STT_MODEL, {item.model for item in capabilities.models_for("transcription")})
            self.assertIn(TTS_MODEL, {item.model for item in capabilities.models_for("speech")})
            self.assertIn("zh", await client.languages("transcription", model=STT_MODEL))
            self.assertIn("zh", await client.languages("speech", model=TTS_MODEL))

            transcription = await client.transcribe(
                SAMPLE, model=STT_MODEL, language="zh", response_format="verbose_json", timestamps=True,
            )
            self.assertEqual(transcription.model, STT_MODEL)
            self.assertEqual(transcription.language, "zh")
            self.assertTrue(transcription.text.strip())
            self.assertTrue(transcription.segments)

            speech = await client.synthesize(
                "你好，这是通过 Python SDK 执行的真实语音合成回归测试。",
                model=TTS_MODEL, language="zh", response_format="wav",
            )
            self.assertEqual(speech.model, TTS_MODEL)
            self.assertEqual(speech.format, "wav")
            self.assertEqual(speech.content_type.split(";", 1)[0], "audio/wav")
            self.assertTrue(speech.audio.startswith(b"RIFF"))
            self.assertGreater(len(speech.audio), 44)
            self.assertGreater(speech.duration_seconds or 0, 0)

    @unittest.skipUnless(_STREAM_MODELS_READY, "Install the zh streaming transcription chain for SDK stream regression")
    async def test_sdk_streams_real_audio_and_recovers_after_transport_drop(self):
        with wave.open(str(SAMPLE), "rb") as reader:
            self.assertEqual((reader.getframerate(), reader.getnchannels(), reader.getsampwidth()), (16000, 1, 2))
            pcm = reader.readframes(reader.getnframes())

        async with SmartVoiceClient(
            self.service.base_url, request_timeout=180, resume_attempts=5, resume_backoff=0.1,
        ) as client:
            capabilities = await client.streaming_capabilities()
            chain = capabilities.chain("transcription", "zh")
            self.assertIsNotNone(chain)
            self.assertTrue(chain["available"], chain)
            async with await client.start_stream(
                mode="transcription", source_language="zh", resumable=True,
            ) as session:
                old_socket = session._socket
                disconnected = False
                observed = []

                async def send():
                    await session.send_audio(pcm, realtime=True)
                    await session.finish()

                async def receive():
                    nonlocal disconnected
                    async for event in session.events():
                        observed.append(event)
                        if not disconnected and event.type.startswith("source_") and event.data.get("text", "").strip():
                            disconnected = True
                            await old_socket.close()

                async with asyncio.timeout(180):
                    sender = asyncio.create_task(send())
                    await receive()
                    await sender

                self.assertTrue(disconnected, "The real stream produced no source text to trigger recovery")
                self.assertIsNot(session._socket, old_socket, "The SDK did not reconnect to the live service")
                self.assertEqual(observed[-1].type, "session_complete")
                self.assertEqual(observed[-1].data.get("status"), "complete")
                sequences = [event.data["event_sequence"] for event in observed if "event_sequence" in event.data]
                self.assertEqual(sequences, sorted(set(sequences)), "Recovered events must remain ordered and deduplicated")
                self.assertTrue(any(event.type == "source_unit_final" for event in observed))


if __name__ == "__main__":
    unittest.main()
