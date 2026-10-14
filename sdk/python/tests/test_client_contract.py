"""Deterministic HTTP and streaming contract tests for the public Python SDK."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from smartvoice_sdk import (
    APIError,
    ServerError,
    SmartVoiceClient,
    SmartVoiceError,
    StreamEvent,
    StreamProtocolError,
    StreamingUnavailableError,
    StreamingSession,
    SubtitleState,
)
from smartvoice_sdk.capabilities import ServiceCapabilities, StreamingCapabilities


class ProtocolSocket:
    def __init__(self, incoming=()):
        self.incoming = list(incoming)
        self.sent: list[str | bytes] = []
        self.closed = False

    async def send(self, data):
        if self.closed:
            raise RuntimeError("socket closed")
        self.sent.append(data)

    async def recv(self):
        if not self.incoming:
            raise AssertionError("No queued server message")
        return self.incoming.pop(0)

    async def close(self):
        self.closed = True


class SDKHttpContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = SmartVoiceClient("https://voice.test/prefix/")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def use_handler(self, handler):
        await self.client._http.aclose()
        self.client._http = httpx.AsyncClient(
            base_url=self.client.base_url, transport=httpx.MockTransport(handler),
        )

    async def test_capabilities_filters_models_and_installed_router_languages(self):
        calls = []

        async def handler(request):
            calls.append(request)
            return httpx.Response(200, json={
                "tasks": [
                    {"task": "transcription", "model": "asr-zh", "languages": ["zh"], "streaming": True},
                    {"task": "transcription", "model": "asr-multi", "languages": ["en", "ja"], "streaming": False},
                    {"task": "speech", "model": "tts-zh", "languages": ["zh"], "streaming": False},
                    {"task": "unknown", "model": "ignored", "languages": ["xx"]},
                ],
                "router": {"tasks": {"transcription": {
                    "zh": [{"installed": True}], "en": [{"installed": False}],
                }}},
            })

        await self.use_handler(handler)
        caps = await self.client.capabilities(task="transcription")
        self.assertEqual(calls[0].url.path, "/prefix/v1/capabilities")
        self.assertEqual(calls[0].url.params["task"], "transcription")
        self.assertEqual([row.model for row in caps.models_for("transcription")], ["asr-zh", "asr-multi"])
        self.assertEqual(caps.languages_for("transcription"), ("en", "ja", "zh"))
        self.assertEqual(caps.languages_for("transcription", model="asr-zh"), ("zh",))
        self.assertEqual(caps.languages_for("transcription", model="smartvoice-auto"), ("zh",))
        self.assertEqual(caps.languages_for("speech", include_auto=True), ("auto", "zh"))
        self.assertTrue(caps.supports_language("transcription", "ja", model="asr-multi"))
        self.assertEqual(await self.client.languages("speech", model="tts-zh"), ("zh",))

    async def test_injected_http_client_and_default_headers(self):
        requests = []

        async def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"tasks": []})

        injected = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = SmartVoiceClient("https://voice.test/prefix", http_client=injected, headers={"X-SDK-Test": "yes"})
        await client.capabilities()
        await client.aclose()
        self.assertFalse(injected.is_closed)
        self.assertEqual(requests[0].url.path, "/prefix/v1/capabilities")
        self.assertEqual(requests[0].headers["X-SDK-Test"], "yes")
        await injected.aclose()

    async def test_capabilities_reject_unknown_tasks_and_non_object_json(self):
        with self.assertRaises(ValueError):
            await self.client.capabilities(task="translation")
        await self.use_handler(lambda request: httpx.Response(200, json=["not", "an object"]))
        with self.assertRaises(StreamProtocolError):
            await self.client.capabilities()

    async def test_http_errors_preserve_structured_details_and_request_id(self):
        await self.use_handler(lambda request: httpx.Response(422, json={
            "error": {"code": "invalid_audio", "message": "Cannot decode", "request_id": "req-body"},
        }, headers={"X-Request-ID": "req-header"}))
        with self.assertRaises(APIError) as caught:
            await self.client.streaming_capabilities()
        self.assertEqual((caught.exception.status_code, caught.exception.code), (422, "invalid_audio"))
        self.assertEqual(caught.exception.request_id, "req-header")
        self.assertEqual(str(caught.exception), "Cannot decode")

    async def test_streaming_capabilities_filter_language_pairs_by_availability(self):
        await self.use_handler(lambda request: httpx.Response(200, json={"chains": [
            {"mode": "transcription", "source_language": "zh", "target_language": None, "available": True},
            {"mode": "transcription", "source_language": "en", "target_language": None, "available": False},
            {"mode": "translated_subtitles", "source_language": "ja", "target_language": "en", "available": True},
        ]}))
        caps = await self.client.streaming_capabilities()
        self.assertIsInstance(caps, StreamingCapabilities)
        self.assertEqual(caps.source_languages("transcription"), ("en", "zh"))
        self.assertEqual(caps.source_languages("transcription", available_only=True), ("zh",))
        self.assertEqual(caps.language_pairs("translated_subtitles"), (("ja", "en"),))

    async def test_transcribe_uploads_bytes_and_maps_json_and_text_responses(self):
        requests = []

        async def handler(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(200, json={
                    "text": "hello", "language": "en", "model": "asr-en",
                    "segments": [{"text": "hello"}, None],
                })
            return httpx.Response(200, text="plain transcript", headers={"x-model-id": "asr-auto"})

        await self.use_handler(handler)
        result = await self.client.transcribe(
            bytearray(b"RIFFdata"), model="asr-en", language="en",
            response_format="verbose_json", timestamps=True, filename="clip.wav",
        )
        self.assertEqual(result.text, "hello")
        self.assertEqual(result.segments, ({"text": "hello"},))
        self.assertEqual(result.model, "asr-en")
        self.assertEqual(result.requested_language, "en")
        self.assertIn(b'name="clip.wav"', requests[0].content)
        self.assertIn(b'name="timestamps"', requests[0].content)
        self.assertIn(b"true", requests[0].content)

        text_result = await self.client.transcribe(b"audio", response_format="text")
        self.assertEqual(text_result.text, "plain transcript")
        self.assertEqual(text_result.model, "asr-auto")
        self.assertEqual(text_result.raw, {"text": "plain transcript"})

    async def test_transcribe_reads_path_and_rejects_invalid_format_or_payload(self):
        received = []
        await self.use_handler(lambda request: (received.append(request.content) or httpx.Response(200, json={"text": "ok"})))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.flac"
            path.write_bytes(b"audio-bytes")
            result = await self.client.transcribe(path)
        self.assertEqual(result.text, "ok")
        self.assertIn(b'filename="sample.flac"', received[0])
        with self.assertRaises(ValueError):
            await self.client.transcribe(b"audio", response_format="srt")
        with self.assertRaises(ValueError):
            await self.client.transcribe(b"audio", timestamps=1)
        with self.assertRaises(TypeError):
            await self.client.transcribe(object())
        await self.use_handler(lambda request: httpx.Response(200, content=b"[]", headers={"content-type": "application/json"}))
        with self.assertRaises(StreamProtocolError):
            await self.client.transcribe(b"audio")

    async def test_synthesize_maps_binary_metadata_and_defaults_to_mp3(self):
        captured = []

        def handler(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, content=b"ID3audio", headers={
                "content-type": "audio/mpeg", "x-audio-format": "mp3", "x-model-id": "tts-en",
                "x-audio-duration": "1.25", "x-audio-sample-rate": "24000", "x-audio-segments": "2",
            })

        await self.use_handler(handler)
        result = await self.client.synthesize(" hello ")
        self.assertEqual(captured[0]["response_format"], "mp3")
        self.assertEqual(result.audio, b"ID3audio")
        self.assertEqual((result.format, result.model, result.duration_seconds), ("mp3", "tts-en", 1.25))
        self.assertEqual((result.sample_rate, result.segments), (24000, 2))

    async def test_synthesize_validates_input_and_surfaces_http_errors(self):
        for text in ("", "   "):
            with self.assertRaises(ValueError):
                await self.client.synthesize(text)
        for speed in (0.49, 2.01):
            with self.assertRaises(ValueError):
                await self.client.synthesize("hello", speed=speed)
        with self.assertRaises(ValueError):
            await self.client.synthesize("hello", response_format="ogg")
        await self.use_handler(lambda request: httpx.Response(400, json={"detail": "bad request"}))
        with self.assertRaises(APIError) as caught:
            await self.client.synthesize("hello")
        self.assertEqual((caught.exception.code, str(caught.exception)), ("request_failed", "bad request"))


class SDKStreamingContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_subtitle_state_tracks_revisions_retractions_and_final_units(self):
        state = SubtitleState()
        self.assertTrue(state.apply({"type": "source_unit_partial", "unit_id": "u1", "revision": 1, "text": "hel"}))
        self.assertTrue(state.apply({"type": "source_unit_partial", "unit_id": "u1", "revision": 2, "text": "hello"}))
        self.assertFalse(state.apply({"type": "source_unit_partial", "unit_id": "u1", "revision": 1, "text": "stale"}))
        self.assertTrue(state.apply({"type": "source_unit_final", "unit_id": "u1", "revision": 3, "text": "Hello."}))
        self.assertFalse(state.apply({"type": "source_unit_partial", "unit_id": "u1", "revision": 4, "text": "overwrite"}))
        self.assertTrue(state.apply({"type": "target_partial", "unit_id": "u2", "revision": 1, "text": "draft"}))
        self.assertTrue(state.apply({"type": "target_partial", "unit_id": "u2", "revision": 2, "text": "", "retracted": True}))
        self.assertEqual(state.text(), "Hello.")
        self.assertEqual(state.text("target"), "")

    async def test_subtitle_state_reappearance_is_not_duplicated_and_history_is_bounded(self):
        state = SubtitleState(max_units=2)
        self.assertTrue(state.apply({"type": "source_unit_partial", "unit_id": "u1", "revision": 1, "text": "old"}))
        self.assertTrue(state.apply({"type": "source_unit_partial", "unit_id": "u1", "revision": 2, "text": "", "retracted": True}))
        self.assertTrue(state.apply({"type": "source_unit_partial", "unit_id": "u1", "revision": 3, "text": "again"}))
        self.assertEqual(state.text(), "again")
        self.assertEqual([unit.unit_id for unit in state.units()], ["u1"])
        state.apply({"type": "source_unit_final", "unit_id": "u2", "revision": 1, "text": "second"})
        state.apply({"type": "source_unit_final", "unit_id": "u3", "revision": 1, "text": "third"})
        self.assertEqual([unit.unit_id for unit in state.units()], ["u2", "u3"])
        self.assertEqual(state.text(), "secondthird")
        with self.assertRaises(ValueError):
            SubtitleState(max_units=0)

    async def test_close_cancels_incomplete_session_but_can_leave_it_detached(self):
        canceled = ProtocolSocket()
        session = StreamingSession(canceled, {"type": "configure", "resumable": True})
        await session.close()
        self.assertEqual([json.loads(row)["type"] for row in canceled.sent], ["cancel"])

        detached = ProtocolSocket()
        session = StreamingSession(detached, {"type": "configure", "resumable": True})
        await session.close(cancel_if_incomplete=False)
        self.assertEqual(detached.sent, [])

        completed = ProtocolSocket([json.dumps({"type": "session_complete", "event_sequence": 1})])
        session = StreamingSession(completed, {"type": "configure", "resumable": True})
        async for _event in session.events():
            pass
        await session.close()
        self.assertEqual([json.loads(row)["type"] for row in completed.sent], ["ack"])

    async def test_send_audio_stream_splits_incremental_chunks(self):
        session = StreamingSession(ProtocolSocket(), {"type": "configure"})
        received = []

        async def capture(frame):
            received.append(frame)

        session.send_audio_frame = capture

        async def chunks():
            yield b"12"
            yield b"345678"

        await session.send_audio_stream(chunks(), frame_bytes=4, realtime=False)
        self.assertEqual(received, [b"1234", b"5678"])

    async def test_stream_setup_rejects_known_unavailable_chain_before_connecting(self):
        client = SmartVoiceClient("http://voice.test")
        client.streaming_capabilities = AsyncMock(return_value=StreamingCapabilities.from_dict({"chains": [
            {"mode": "spoken_interpretation", "source_language": "zh", "target_language": "en",
             "available": False, "reason": "model_unavailable", "missing_models": ["streaming-tts-zh"]},
        ]}))
        with self.assertRaises(StreamingUnavailableError) as caught:
            await client.start_stream(mode="spoken_interpretation", source_language="zh", target_language="en")
        self.assertEqual(caught.exception.reason, "model_unavailable")
        self.assertEqual(caught.exception.missing_models, ("streaming-tts-zh",))
        self.assertIn("streaming-tts-zh", str(caught.exception))
        await client.aclose()

    async def test_stream_setup_sends_config_acknowledges_ready_and_closes(self):
        socket = ProtocolSocket([json.dumps({
            "type": "session_ready", "session_id": "s1", "event_sequence": 1,
            "heartbeat_interval_seconds": 3600,
        })])
        connect = AsyncMock(return_value=socket)
        with patch("smartvoice_sdk.client.connect", new=connect):
            client = SmartVoiceClient(
                "https://voice.test/prefix",
                websocket_headers={"Authorization": "Bearer test-token"},
                websocket_options={"ping_interval": 17, "compression": None},
            )
            client.streaming_capabilities = AsyncMock(return_value=StreamingCapabilities.from_dict({"chains": [
                {"mode": "transcription", "source_language": "zh", "target_language": None},
            ]}))
            session = await client.start_stream(mode="transcription", source_language="zh")
            connect.assert_awaited_once()
            self.assertEqual(connect.await_args.args[0], "wss://voice.test/prefix/v1/audio/stream")
            self.assertEqual(connect.await_args.kwargs["additional_headers"], {"Authorization": "Bearer test-token"})
            self.assertEqual(connect.await_args.kwargs["ping_interval"], 17)
            self.assertIsNone(connect.await_args.kwargs["compression"])
            config = json.loads(socket.sent[0])
            self.assertEqual(config["protocol"], "smartvoice.stream.v1")
            self.assertEqual(config["audio"], {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1})
            self.assertEqual(json.loads(socket.sent[1]), {"type": "ack", "event_sequence": 1})
            await session.cancel()
            await session.heartbeat()
            await session.finish()
            self.assertEqual([json.loads(row)["type"] for row in socket.sent[2:]], ["cancel", "heartbeat", "finish"])
            await session.close()
            self.assertTrue(socket.closed)
            await client.aclose()

    async def test_stream_setup_rejects_unsupported_and_malformed_sessions(self):
        client = SmartVoiceClient("http://voice.test")
        client.streaming_capabilities = AsyncMock(return_value=StreamingCapabilities.from_dict({"chains": []}))
        with self.assertRaises(ValueError):
            await client.start_stream(mode="transcription", source_language="xx")
        with self.assertRaises(ValueError):
            SmartVoiceClient._make_config(
                mode="translated_subtitles", source_language="zh", target_language="zh",
                output_consumption="delivery", ack_window=16, resumable=False, glossary=None,
                context=None, models=None, include_source_text=True,
            )
        await client.aclose()

        for first_message, expected in (
            (json.dumps({"type": "error", "code": "unavailable", "message": "No route"}), ServerError),
            (json.dumps({"type": "unexpected"}), StreamProtocolError),
            ("not-json", StreamProtocolError),
        ):
            socket = ProtocolSocket([first_message])
            with patch("smartvoice_sdk.client.connect", new=AsyncMock(return_value=socket)):
                client = SmartVoiceClient("http://voice.test")
                client.streaming_capabilities = AsyncMock(return_value=StreamingCapabilities.from_dict({"chains": [
                    {"mode": "transcription", "source_language": "zh", "target_language": None},
                ]}))
                with self.assertRaises(expected):
                    await client.start_stream(mode="transcription", source_language="zh")
                self.assertTrue(socket.closed)
                await client.aclose()

        invalid_configs = (
            {"mode": "transcription", "source_language": "", "target_language": None,
             "output_consumption": "delivery", "ack_window": 16, "include_source_text": True},
            {"mode": "transcription", "source_language": "zh", "target_language": "en",
             "output_consumption": "delivery", "ack_window": 16, "include_source_text": True},
            {"mode": "transcription", "source_language": "zh", "target_language": None,
             "output_consumption": "delivery", "ack_window": 0, "include_source_text": True},
            {"mode": "transcription", "source_language": "zh", "target_language": None,
             "output_consumption": "playback", "ack_window": 16, "include_source_text": True},
            {"mode": "spoken_interpretation", "source_language": "zh", "target_language": "en",
             "output_consumption": "delivery", "ack_window": 16, "include_source_text": 1},
        )
        for fields in invalid_configs:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                SmartVoiceClient._make_config(
                    target_language=fields.pop("target_language"), resumable=False,
                    glossary=None, context=None, models=None, **fields,
                )

    async def test_stream_audio_playback_ack_order_and_protocol_errors(self):
        descriptor = json.dumps({
            "type": "audio_segment", "event_sequence": 4, "audio_sequence": 1,
            "audio_bytes": 4, "format": "wav",
        })
        socket = ProtocolSocket([descriptor, b"WAVE"])
        session = StreamingSession(socket, {"type": "configure", "mode": "spoken_interpretation"})
        event = await session._receive_raw_event()
        self.assertEqual((event.audio.sequence, event.audio.audio_bytes), (1, b"WAVE"))
        with self.assertRaises(ValueError):
            await session.mark_audio_played(1)
        await session.mark_audio_started(1)
        await session.mark_audio_played(1)
        self.assertEqual([json.loads(row) for row in socket.sent], [
            {"type": "audio_started", "audio_sequence": 1},
            {"type": "audio_played", "audio_sequence": 1},
        ])

        bad = ProtocolSocket([json.dumps({
            "type": "audio_segment", "event_sequence": 5, "audio_sequence": 2, "audio_bytes": 6,
        }), b"short"])
        session._socket = bad
        with self.assertRaises(StreamProtocolError):
            await session._receive_raw_event()

    async def test_terminal_server_errors_and_pcm_frame_boundaries(self):
        socket = ProtocolSocket([json.dumps({"type": "error", "event_sequence": 2, "code": "failed", "message": "boom"})])
        session = StreamingSession(socket, {"type": "configure"})
        events = session.events()
        self.assertEqual((await anext(events)).type, "error")
        with self.assertRaises(ServerError) as caught:
            await anext(events)
        self.assertEqual((caught.exception.code, str(caught.exception)), ("failed", "boom"))
        with self.assertRaises(ValueError):
            await session.send_audio_frame(b"odd")
        with self.assertRaises(ValueError):
            await session.send_audio_frame(b"x" * 32002)
        with self.assertRaises(ValueError):
            await session.send_audio(pcm16=b"1234", frame_bytes=3)
        await session.close()
        with self.assertRaises(SmartVoiceError):
            await session.send_audio_frame(b"\0\0")

    async def test_input_ack_cursors_and_audio_replays_reject_corruption(self):
        ready = StreamEvent("session_ready", {
            "session_id": "s1", "event_sequence": 1,
            "recovery": {"resume_token": "token", "input_history_frames": 4, "grace_seconds": 1},
        })
        session = StreamingSession(ProtocolSocket(), {"type": "configure", "resumable": True})
        session._set_ready(ready)
        session._input_sequence = 1
        session._input_samples = 2
        session._pending_input[1] = (0, b"ab\0\0")
        with self.assertRaises(StreamProtocolError):
            session._ack_input({"input_sequence": 1, "accepted_samples": 1})
        with self.assertRaises(StreamProtocolError):
            session._ack_input({"input_sequence": 2, "accepted_samples": 2})

        first = ProtocolSocket([
            json.dumps({"type": "audio_segment", "event_sequence": 2, "audio_sequence": 1, "audio_bytes": 4}),
            b"WAVE",
        ])
        await session._receive_raw_event(first)
        changed = ProtocolSocket([
            json.dumps({"type": "audio_segment", "event_sequence": 2, "audio_sequence": 1, "audio_bytes": 4}),
            b"NOPE",
        ])
        with self.assertRaises(StreamProtocolError):
            await session._receive_raw_event(changed)
        gap = ProtocolSocket([
            json.dumps({"type": "audio_segment", "event_sequence": 3, "audio_sequence": 3, "audio_bytes": 4}),
            b"WAVE",
        ])
        with self.assertRaises(StreamProtocolError):
            await session._receive_raw_event(gap)


if __name__ == "__main__":
    unittest.main()
