from __future__ import annotations

import json
import unittest
from collections import deque
from unittest.mock import patch

from websockets.exceptions import ConnectionClosedOK
from websockets.frames import Close

from smartvoice_sdk import StreamEvent, StreamingSession


def closed_error() -> ConnectionClosedOK:
    return ConnectionClosedOK(
        Close(1000, "test disconnect"), Close(1000, "test disconnect"), True,
    )


class FakeSocket:
    def __init__(self, incoming=(), *, closed_on_receive=False, fail_sends=False):
        self.incoming = deque(incoming)
        self.closed_on_receive = closed_on_receive
        self.fail_sends = fail_sends
        self.sent = []
        self.closed = False

    async def send(self, message):
        if self.closed or self.fail_sends:
            raise closed_error()
        self.sent.append(message)

    async def recv(self):
        if self.incoming:
            return self.incoming.popleft()
        if self.closed or self.closed_on_receive:
            raise closed_error()
        raise AssertionError("FakeSocket has no queued message")

    async def close(self):
        self.closed = True


def json_message(kind: str, **fields) -> str:
    return json.dumps({"type": kind, **fields}, separators=(",", ":"))


def resumable_session(socket: FakeSocket) -> StreamingSession:
    session = StreamingSession(
        socket,
        {"type": "configure", "protocol": "smartvoice.stream.v1", "resumable": True},
        base_url="http://localhost:8000",
        resume_attempts=1,
        resume_backoff=0,
    )
    session._set_ready(StreamEvent("session_ready", {
        "session_id": "session-1",
        "event_sequence": 1,
        "recovery": {
            "resume_token": "resume-secret",
            "input_history_frames": 128,
            "grace_seconds": 20,
        },
    }))
    return session


class StreamingRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_disconnect_resumes_and_replays_only_unaccepted_input(self):
        old = FakeSocket(closed_on_receive=True)
        candidate = FakeSocket([
            json_message(
                "session_resumed", session_id="session-1", input_sequence=1,
                accepted_samples=320, last_event_sequence=1, last_audio_played=0,
            ),
            json_message("input_ack", input_sequence=2, accepted_samples=640),
            json_message("source_final", event_sequence=2, text="recovered"),
            json_message("session_complete", event_sequence=3, status="complete"),
        ])
        session = resumable_session(old)
        session._input_sequence = 2
        session._input_samples = 640
        session._pending_input[1] = (0, b"a" * 640)
        session._pending_input[2] = (320, b"b" * 640)

        async def connect(*args, **kwargs):
            return candidate

        with patch("smartvoice_sdk.client.connect", connect):
            events = [event async for event in session.events()]

        self.assertEqual([event.type for event in events], ["source_final", "session_complete"])
        self.assertEqual(session._accepted_input_sequence, 2)
        self.assertEqual(session._accepted_input_samples, 640)
        self.assertFalse(session._pending_input)
        resume = json.loads(candidate.sent[0])
        self.assertEqual(resume["type"], "resume")
        self.assertEqual(resume["last_event_sequence"], 1)
        self.assertEqual(candidate.sent[1], json_message("audio_frame", sequence=2, start_sample=320))
        self.assertEqual(candidate.sent[2], b"b" * 640)
        await session.close()

    async def test_send_failure_reconnects_and_retries_current_frame(self):
        old = FakeSocket(fail_sends=True)
        candidate = FakeSocket([
            json_message(
                "session_resumed", session_id="session-1", input_sequence=0,
                accepted_samples=0, last_event_sequence=1, last_audio_played=0,
            ),
            json_message("input_ack", input_sequence=1, accepted_samples=320),
            json_message("session_complete", event_sequence=2, status="complete"),
        ])
        session = resumable_session(old)

        async def connect(*args, **kwargs):
            return candidate

        with patch("smartvoice_sdk.client.connect", connect):
            await session.send_audio_frame(b"c" * 640)
            events = [event async for event in session.events()]

        self.assertEqual([event.type for event in events], ["session_complete"])
        self.assertEqual(session._accepted_input_sequence, 1)
        self.assertEqual(candidate.sent[1], json_message("audio_frame", sequence=1, start_sample=0))
        self.assertEqual(candidate.sent[2], b"c" * 640)
        await session.close()

    async def test_identical_replayed_audio_chunk_is_accepted(self):
        descriptor = json_message(
            "audio_segment", event_sequence=2, audio_sequence=1,
            audio_bytes=4, format="wav",
        )
        socket = FakeSocket([descriptor, b"WAVE", descriptor, b"WAVE"])
        session = resumable_session(socket)

        first = await session._receive_raw_event()
        replay = await session._receive_raw_event()

        self.assertEqual(first.audio.audio_bytes, b"WAVE")
        self.assertEqual(replay.audio.audio_bytes, b"WAVE")
        self.assertEqual(replay.audio.sequence, 1)
        await session.close()


if __name__ == "__main__":
    unittest.main()
