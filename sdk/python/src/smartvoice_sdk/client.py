"""Async SmartVoice SDK for REST speech and the versioned streaming protocol.

The Streaming client accepts already-normalized 16 kHz mono PCM16.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Mapping
from urllib.parse import urlsplit, urlunsplit

import httpx
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from .capabilities import ServiceCapabilities, StreamingCapabilities
from .results import SpeechResult, TranscriptionResult


PROTOCOL = "smartvoice.stream.v1"
SAMPLE_RATE = 16_000
FRAME_BYTES = 640  # 20 ms of mono PCM16 at 16 kHz.
MAX_FRAME_BYTES = 32_000
_MODES = {"transcription", "translated_subtitles", "spoken_interpretation"}


class SmartVoiceError(Exception):
    """Base exception for SDK errors."""


class StreamProtocolError(SmartVoiceError):
    """The server returned a message that violates the streaming contract."""


class StreamRecoveryError(SmartVoiceError):
    """A resumable stream could not be restored without losing client state."""


class ServerError(SmartVoiceError):
    """A terminal server error event."""

    def __init__(self, code: str, message: str, event: Mapping[str, Any]):
        self.code = code
        self.event = dict(event)
        super().__init__(message)


class APIError(SmartVoiceError):
    """An HTTP API error with the server's structured code and request ID."""

    def __init__(self, status_code: int, code: str, message: str, request_id: str | None = None):
        self.status_code = status_code
        self.code = code
        self.request_id = request_id
        super().__init__(message)


@dataclass(frozen=True)
class AudioChunk:
    """A validated WAV payload paired with its ``audio_segment`` descriptor."""

    sequence: int
    event_sequence: int
    audio_bytes: bytes
    descriptor: Mapping[str, Any]


@dataclass(frozen=True)
class StreamEvent:
    """A server event; ``data`` retains protocol-specific fields verbatim."""

    type: str
    data: Mapping[str, Any]
    audio: AudioChunk | None = None

    @property
    def terminal(self) -> bool:
        return self.type in {"session_complete", "error"}


def _http_base(base_url: str) -> str:
    parsed = urlsplit(base_url.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url must be an absolute http:// or https:// URL")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _websocket_url(base_url: str) -> str:
    parsed = urlsplit(_http_base(base_url))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    prefix = parsed.path.rstrip("/")
    return urlunsplit((scheme, parsed.netloc, f"{prefix}/v1/audio/stream", "", ""))


def _http_error(response: httpx.Response) -> APIError:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    error = payload.get("error") if isinstance(payload, Mapping) else None
    detail = payload.get("detail") if isinstance(payload, Mapping) else None
    code = error.get("code") if isinstance(error, Mapping) else None
    message = error.get("message") if isinstance(error, Mapping) else error if isinstance(error, str) else None
    if not message:
        message = detail if isinstance(detail, str) else payload.get("message") if isinstance(payload, Mapping) else None
    if not message:
        message = f"SmartVoice request failed (HTTP {response.status_code})"
    request_id = response.headers.get("X-Request-ID")
    if not request_id and isinstance(error, Mapping):
        request_id = error.get("request_id")
    return APIError(response.status_code, str(code or "request_failed"), str(message), request_id)


def _response_json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise StreamProtocolError("SmartVoice returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise StreamProtocolError("SmartVoice response must be a JSON object")
    return payload


def _float_header(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _int_header(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


class SmartVoiceClient:
    """Async STT/TTS/Streaming client; it has no model or router management API."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        *,
        open_timeout: float = 10,
        request_timeout: float = 120,
        resume_attempts: int = 3,
        resume_backoff: float = 0.25,
    ):
        if type(resume_attempts) is not int or resume_attempts < 1:
            raise ValueError("resume_attempts must be a positive integer")
        if not math.isfinite(resume_backoff) or resume_backoff < 0:
            raise ValueError("resume_backoff must be nonnegative")
        self.base_url = _http_base(base_url)
        self.open_timeout = open_timeout
        self.resume_attempts = resume_attempts
        self.resume_backoff = resume_backoff
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=request_timeout)

    async def __aenter__(self) -> "SmartVoiceClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def capabilities(self, *, task: str | None = None) -> ServiceCapabilities:
        """Fetch installed STT/TTS runtime capabilities and language support."""
        if task is not None and task not in {"transcription", "speech"}:
            raise ValueError("task must be 'transcription' (STT), 'speech' (TTS), or None")
        response = await self._http.get("/v1/capabilities", params={"task": task} if task else None)
        if response.is_error:
            raise _http_error(response)
        return ServiceCapabilities.from_dict(_response_json(response))

    async def languages(
        self,
        task: str,
        *,
        model: str | None = None,
        include_auto: bool = False,
    ) -> tuple[str, ...]:
        """Discover installed task languages; ``auto`` is optionally included."""
        return (await self.capabilities(task=task)).languages_for(
            task, model=model, include_auto=include_auto,
        )

    async def streaming_capabilities(self) -> StreamingCapabilities:
        """Fetch supported streaming modes, language pairs, and chain readiness."""
        response = await self._http.get("/v1/audio/stream/capabilities")
        if response.is_error:
            raise _http_error(response)
        return StreamingCapabilities.from_dict(_response_json(response))

    async def transcribe(
        self,
        audio: bytes | bytearray | Path | str,
        *,
        model: str = "smartvoice-auto",
        language: str | None = "auto",
        response_format: str = "json",
        timestamps: bool = False,
        filename: str | None = None,
        content_type: str = "application/octet-stream",
    ) -> TranscriptionResult:
        """Transcribe an audio file using ``POST /v1/audio/transcriptions``.

        ``audio`` may be bytes or a filesystem path. Supported language codes
        can be discovered with ``await client.languages('transcription')``.
        """
        if response_format not in {"json", "text", "verbose_json"}:
            raise ValueError("response_format must be json, text, or verbose_json")
        if type(timestamps) is not bool:
            raise ValueError("timestamps must be a boolean")
        data, upload_name = await self._audio_data(audio, filename)
        requested_language = language or "auto"
        response = await self._http.post(
            "/v1/audio/transcriptions",
            data={
                "model": model,
                "language": requested_language,
                "response_format": response_format,
                "timestamps": str(timestamps).lower(),
            },
            files={"file": (upload_name, data, content_type)},
        )
        if response.is_error:
            raise _http_error(response)
        headers = dict(response.headers)
        if response_format == "text":
            text = response.text
            payload: dict[str, Any] = {"text": text}
        else:
            payload = _response_json(response)
            text = str(payload.get("text", ""))
        return TranscriptionResult(
            text=text,
            language=payload.get("language") or headers.get("x-resolved-language"),
            model=payload.get("model") or headers.get("x-model-id"),
            requested_model=model,
            requested_language=requested_language,
            response_format=response_format,
            raw=payload,
            headers=headers,
        )

    async def synthesize(
        self,
        text: str,
        *,
        model: str = "smartvoice-auto",
        language: str | None = "auto",
        voice: str = "default",
        speed: float = 1.0,
        response_format: str = "mp3",
    ) -> SpeechResult:
        """Generate speech using ``POST /v1/audio/speech`` and return audio bytes."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must contain non-whitespace characters")
        if not 0.5 <= speed <= 2.0:
            raise ValueError("speed must be between 0.5 and 2.0")
        if response_format not in {"mp3", "wav"}:
            raise ValueError("response_format must be mp3 or wav")
        request = {
            "model": model,
            "input": text,
            "voice": voice,
            "language": language or "auto",
            "speed": speed,
            "response_format": response_format,
        }
        response = await self._http.post("/v1/audio/speech", json=request)
        if response.is_error:
            raise _http_error(response)
        headers = dict(response.headers)
        return SpeechResult(
            audio=response.content,
            format=headers.get("x-audio-format", response_format),
            content_type=headers.get("content-type", "application/octet-stream"),
            model=headers.get("x-model-id"),
            requested_model=model,
            duration_seconds=_float_header(headers.get("x-audio-duration")),
            sample_rate=_int_header(headers.get("x-audio-sample-rate")),
            segments=_int_header(headers.get("x-audio-segments")),
            headers=headers,
        )

    @staticmethod
    async def _audio_data(audio: bytes | bytearray | Path | str, filename: str | None) -> tuple[bytes, str]:
        if isinstance(audio, (str, Path)):
            path = Path(audio)
            data = await asyncio.to_thread(path.read_bytes)
            return data, filename or path.name
        if isinstance(audio, (bytes, bytearray)):
            return bytes(audio), filename or "audio.wav"
        raise TypeError("audio must be bytes, bytearray, or a filesystem path")

    async def start_stream(
        self,
        *,
        mode: str,
        source_language: str,
        target_language: str | None = None,
        output_consumption: str = "delivery",
        ack_window: int = 16,
        resumable: bool = False,
        glossary: list[dict[str, str]] | None = None,
        context: Mapping[str, Any] | None = None,
        models: Mapping[str, str] | None = None,
        include_source_text: bool = True,
    ) -> "StreamingSession":
        """Open a stream and wait for its validated ``session_ready`` event."""
        streaming = await self.streaming_capabilities()
        if streaming.chain(mode, source_language, target_language) is None:
            raise ValueError(
                f"Unsupported streaming mode/language combination: "
                f"{mode!r}, {source_language!r} -> {target_language!r}"
            )
        config = self._make_config(
            mode=mode,
            source_language=source_language,
            target_language=target_language,
            output_consumption=output_consumption,
            ack_window=ack_window,
            resumable=resumable,
            glossary=glossary,
            context=context,
            models=models,
            include_source_text=include_source_text,
        )
        socket = await connect(
            _websocket_url(self.base_url),
            open_timeout=self.open_timeout,
            max_size=8 * 1024 * 1024,
            max_queue=4,
        )
        session = StreamingSession(
            socket,
            config,
            base_url=self.base_url,
            open_timeout=self.open_timeout,
            resume_attempts=self.resume_attempts,
            resume_backoff=self.resume_backoff,
        )
        try:
            await socket.send(json.dumps(config, separators=(",", ":")))
            ready = await asyncio.wait_for(session._receive_raw_event(), timeout=self.open_timeout)
            if ready.type == "error":
                raise ServerError(str(ready.data.get("code", "stream_error")), str(ready.data.get("message", "Stream setup failed")), ready.data)
            if ready.type != "session_ready":
                raise StreamProtocolError(f"Expected session_ready, received {ready.type!r}")
            if resumable and not isinstance(ready.data.get("recovery"), Mapping):
                raise StreamProtocolError("The service did not enable recovery for this session")
            session._set_ready(ready)
            await session._ack(ready.data)
            interval = ready.data.get("heartbeat_interval_seconds", 20)
            try:
                interval = max(1.0, float(interval))
            except (TypeError, ValueError):
                interval = 20.0
            session._heartbeat_task = asyncio.create_task(session._heartbeat_loop(interval))
            return session
        except BaseException:
            await socket.close()
            raise

    @staticmethod
    def _make_config(
        *, mode: str, source_language: str, target_language: str | None,
        output_consumption: str, ack_window: int, resumable: bool,
        glossary: list[dict[str, str]] | None, context: Mapping[str, Any] | None,
        models: Mapping[str, str] | None, include_source_text: bool,
    ) -> dict[str, Any]:
        if mode not in _MODES:
            raise ValueError(f"mode must be one of {', '.join(sorted(_MODES))}")
        if not isinstance(source_language, str) or not source_language:
            raise ValueError("source_language must be a non-empty language code")
        if output_consumption not in {"delivery", "playback"}:
            raise ValueError("output_consumption must be 'delivery' or 'playback'")
        if output_consumption == "playback" and mode != "spoken_interpretation":
            raise ValueError("playback acknowledgements are only supported for spoken_interpretation")
        if type(include_source_text) is not bool:
            raise ValueError("include_source_text must be a boolean")
        if type(ack_window) is not int or not 1 <= ack_window <= 32:
            raise ValueError("ack_window must be an integer between 1 and 32")
        translated = mode != "transcription"
        if translated:
            if not isinstance(target_language, str) or not target_language or target_language == source_language:
                raise ValueError("translated workflows require a different target_language")
        elif target_language is not None:
            raise ValueError("transcription does not accept target_language")
        config: dict[str, Any] = {
            "type": "configure",
            "protocol": PROTOCOL,
            "mode": mode,
            "source_language": source_language,
            "audio": {"encoding": "pcm_s16le", "sample_rate": SAMPLE_RATE, "channels": 1},
            "output_consumption": output_consumption,
            "ack_window": ack_window,
            "resumable": resumable,
            "include_source_text": include_source_text,
        }
        if translated:
            config["target_language"] = target_language
        if glossary is not None:
            config["glossary"] = glossary
        if context is not None:
            config["context"] = dict(context)
        if models is not None:
            config["models"] = dict(models)
        return config


class StreamingSession:
    """One configured session with optional in-process automatic recovery."""

    def __init__(
        self,
        socket: ClientConnection,
        config: Mapping[str, Any],
        *,
        base_url: str = "http://127.0.0.1:8000",
        open_timeout: float = 10,
        resume_attempts: int = 3,
        resume_backoff: float = 0.25,
    ):
        self._socket = socket
        self.config = dict(config)
        self.base_url = _http_base(base_url)
        self.open_timeout = open_timeout
        self.resume_attempts = resume_attempts
        self.resume_backoff = resume_backoff
        self.ready: StreamEvent | None = None
        self._recovery: Mapping[str, Any] | None = None
        self._last_event_sequence = 0
        self._last_started = 0
        self._last_played = 0
        self._last_audio_sequence = 0
        self._audio_event_sequences: dict[int, int] = {}
        self._audio_seen: OrderedDict[int, tuple[str, str]] = OrderedDict()
        self._input_sequence = 0
        self._input_samples = 0
        self._accepted_input_sequence = 0
        self._accepted_input_samples = 0
        self._pending_input: OrderedDict[int, tuple[int, bytes]] = OrderedDict()
        self._input_history_limit = 128
        self._closed = False
        self._events_active = False
        self._send_lock = asyncio.Lock()
        self._recovery_lock = asyncio.Lock()
        self._input_window_changed = asyncio.Event()
        self._heartbeat_task: asyncio.Task[None] | None = None

    @property
    def resumable(self) -> bool:
        return self.config.get("resumable") is True

    def _set_ready(self, event: StreamEvent) -> None:
        self.ready = event
        self._last_event_sequence = int(event.data.get("event_sequence", 0))
        if self.resumable:
            recovery = event.data.get("recovery")
            if not isinstance(recovery, Mapping) or not isinstance(recovery.get("resume_token"), str):
                raise StreamProtocolError("The service did not provide a streaming recovery token")
            session_id = event.data.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                raise StreamProtocolError("The service returned an invalid streaming session ID")
            self._recovery = {**recovery, "session_id": session_id}
            limit = recovery.get("input_history_frames", 128)
            if type(limit) is not int or limit < 1:
                raise StreamProtocolError("The service returned an invalid input recovery window")
            self._input_history_limit = limit

    async def __aenter__(self) -> "StreamingSession":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def send_audio_frame(self, pcm16: bytes) -> None:
        """Send one mono PCM16 frame (normally 640 bytes / 20 ms).

        This low-level API does not resample, pace, or accumulate audio.
        """
        if self._closed:
            raise SmartVoiceError("Session is closed")
        if not pcm16 or len(pcm16) % 2:
            raise ValueError("PCM16 frame must be non-empty and have an even byte length")
        if len(pcm16) > MAX_FRAME_BYTES:
            raise ValueError(f"PCM frame exceeds {MAX_FRAME_BYTES} bytes")
        if not self.resumable:
            async with self._send_lock:
                try:
                    await self._socket.send(pcm16)
                except ConnectionClosed as exc:
                    raise SmartVoiceError("WebSocket closed before audio could be sent") from exc
            return
        if self._recovery is None:
            raise StreamProtocolError("The resumable stream has not received session_ready")
        while len(self._pending_input) >= self._input_history_limit:
            if not self._events_active:
                raise StreamRecoveryError(
                    "The input replay window is full; consume session.events() while sending audio"
                )
            self._input_window_changed.clear()
            if len(self._pending_input) < self._input_history_limit:
                continue
            try:
                await asyncio.wait_for(self._input_window_changed.wait(), timeout=self.open_timeout)
            except TimeoutError as exc:
                raise StreamRecoveryError(
                    "Timed out waiting for input acknowledgements; keep consuming session.events()"
                ) from exc
            if self._closed:
                raise SmartVoiceError("Session is closed")
        sequence = self._input_sequence + 1
        start_sample = self._input_samples
        self._pending_input[sequence] = (start_sample, bytes(pcm16))
        self._input_sequence = sequence
        self._input_samples += len(pcm16) // 2
        try:
            await self._send_sequenced_frame(self._socket, sequence, start_sample, pcm16)
            # websockets.send() may complete without yielding while its buffer
            # has room; let the events consumer drain input_ack messages.
            await asyncio.sleep(0)
        except ConnectionClosed:
            await self._recover(self._socket)

    async def _send_sequenced_frame(
        self, socket: ClientConnection, sequence: int, start_sample: int, pcm16: bytes,
    ) -> None:
        async with self._send_lock:
            await self._send_sequenced_frame_unlocked(socket, sequence, start_sample, pcm16)

    async def _send_sequenced_frame_unlocked(
        self, socket: ClientConnection, sequence: int, start_sample: int, pcm16: bytes,
    ) -> None:
        await socket.send(json.dumps({
            "type": "audio_frame", "sequence": sequence, "start_sample": start_sample,
        }, separators=(",", ":")))
        await socket.send(pcm16)

    async def send_audio(self, pcm16: bytes, *, frame_bytes: int = FRAME_BYTES, realtime: bool = True) -> None:
        """Send a complete PCM buffer in bounded frames, optionally at real time."""
        if not pcm16:
            return
        if frame_bytes < 2 or frame_bytes > MAX_FRAME_BYTES or frame_bytes % 2:
            raise ValueError("frame_bytes must be even and between 2 and 32000")
        started = time.monotonic()
        sample_cursor = 0
        for offset in range(0, len(pcm16), frame_bytes):
            frame = pcm16[offset:offset + frame_bytes]
            await self.send_audio_frame(frame)
            sample_cursor += len(frame) // 2
            if realtime:
                deadline = started + sample_cursor / SAMPLE_RATE
                delay = deadline - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)

    async def finish(self) -> None:
        await self._send_control({"type": "finish"})

    async def cancel(self) -> None:
        if not self._closed:
            await self._send_control({"type": "cancel"})

    async def heartbeat(self) -> None:
        await self._send_control({"type": "heartbeat"})

    async def mark_audio_started(self, sequence: int) -> None:
        """Tell the service that playback began for this ordered audio chunk."""
        if sequence > self._last_audio_sequence:
            raise ValueError("Cannot acknowledge playback for an audio chunk not yet received")
        if sequence != self._last_started + 1:
            raise ValueError("Audio chunks must be started in consecutive sequence order")
        if sequence > self._last_played + 1:
            raise ValueError("Finish the previous audio chunk before starting another")
        await self._send_control({"type": "audio_started", "audio_sequence": sequence})
        self._last_started = sequence

    async def mark_audio_played(self, sequence: int) -> None:
        """Acknowledge actual playback completion for playback-mode sessions."""
        if sequence != self._last_played + 1 or sequence != self._last_started:
            raise ValueError("Audio chunks must be reported played in order after starting")
        self._last_played = sequence
        self._last_event_sequence = max(
            self._last_event_sequence, self._audio_event_sequences.get(sequence, 0),
        )
        await self._send_control({"type": "audio_played", "audio_sequence": sequence})

    async def events(self) -> AsyncIterator[StreamEvent]:
        """Yield validated events and ACK them after the caller advances the iterator.

        Audio bytes are paired with their preceding descriptor. In playback mode,
        call ``mark_audio_started`` and ``mark_audio_played`` after actual playback.
        """
        if self._events_active:
            raise SmartVoiceError("Only one events() consumer may read a streaming session")
        self._events_active = True
        try:
            while True:
                socket = self._socket
                try:
                    event = await self._receive_raw_event(socket)
                except ConnectionClosed as exc:
                    if not self.resumable or self._closed:
                        raise SmartVoiceError("WebSocket closed before a terminal event") from exc
                    await self._recover(socket)
                    continue
                if event.type == "input_ack":
                    self._ack_input(event.data)
                    continue
                yield event
                await self._ack(event.data)
                if event.terminal:
                    if event.type == "error":
                        raise ServerError(str(event.data.get("code", "stream_error")), str(event.data.get("message", "Streaming failed")), event.data)
                    return
        finally:
            self._events_active = False

    async def _receive_raw_event(self, socket: ClientConnection | None = None) -> StreamEvent:
        source = socket or self._socket
        raw = await source.recv()
        if not isinstance(raw, str):
            raise StreamProtocolError("Expected a JSON event before binary audio")
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StreamProtocolError("Server sent invalid JSON") from exc
        if not isinstance(row, dict) or not isinstance(row.get("type"), str):
            raise StreamProtocolError("Server event must be an object with a type")
        audio = None
        if row["type"] == "audio_segment":
            binary = await source.recv()
            if not isinstance(binary, bytes):
                raise StreamProtocolError("audio_segment must be followed by a binary WAV payload")
            expected = row.get("audio_bytes")
            sequence = row.get("audio_sequence")
            event_sequence = row.get("event_sequence")
            if type(expected) is not int or len(binary) != expected:
                raise StreamProtocolError("Audio payload length does not match audio_bytes")
            if type(sequence) is not int or type(event_sequence) is not int:
                raise StreamProtocolError("Audio descriptor is missing integer sequence fields")
            descriptor = json.dumps(row, sort_keys=True, separators=(",", ":"))
            fingerprint = hashlib.sha256(binary).hexdigest()
            previous = self._audio_seen.get(sequence)
            if sequence == self._last_audio_sequence + 1:
                self._last_audio_sequence = sequence
                self._audio_seen[sequence] = (descriptor, fingerprint)
                self._audio_event_sequences[sequence] = event_sequence
                while len(self._audio_seen) > 128:
                    stale, _ = self._audio_seen.popitem(last=False)
                    self._audio_event_sequences.pop(stale, None)
            elif sequence <= self._last_audio_sequence and previous == (descriptor, fingerprint):
                # An unacknowledged descriptor/binary pair can be replayed after reconnect.
                self._audio_seen.move_to_end(sequence)
            else:
                raise StreamProtocolError("Audio chunk sequence is missing, duplicated, or out of order")
            audio = AudioChunk(sequence, event_sequence, binary, dict(row))
        return StreamEvent(row["type"], dict(row), audio)

    async def _ack(self, event: Mapping[str, Any]) -> None:
        sequence = event.get("event_sequence")
        if type(sequence) is int:
            self._last_event_sequence = max(self._last_event_sequence, sequence)
            await self._send_control({"type": "ack", "event_sequence": sequence})

    def _ack_input(self, event: Mapping[str, Any]) -> None:
        sequence = event.get("input_sequence")
        samples = event.get("accepted_samples")
        if type(sequence) is not int or type(samples) is not int:
            raise StreamProtocolError("Input acknowledgement is missing sequence fields")
        if sequence < self._accepted_input_sequence or sequence > self._input_sequence:
            raise StreamProtocolError("Input acknowledgement sequence is outside the sent range")
        if sequence == self._accepted_input_sequence:
            expected_samples = self._accepted_input_samples
        else:
            pending = self._pending_input.get(sequence)
            following = self._pending_input.get(sequence + 1)
            if pending is not None:
                expected_samples = pending[0] + len(pending[1]) // 2
            elif following is not None:
                expected_samples = following[0]
            elif sequence == self._input_sequence:
                expected_samples = self._input_samples
            else:
                raise StreamProtocolError("Input acknowledgement is outside the retained replay window")
        if samples != expected_samples:
            raise StreamProtocolError("Input acknowledgement sample offset does not match the sent audio")
        self._accepted_input_sequence = sequence
        self._accepted_input_samples = samples
        for old_sequence in tuple(self._pending_input):
            if old_sequence <= sequence:
                del self._pending_input[old_sequence]
        self._input_window_changed.set()

    async def _send_control(self, message: Mapping[str, Any]) -> None:
        if self._closed:
            raise SmartVoiceError("Session is closed")
        payload = json.dumps(dict(message), separators=(",", ":"))
        async with self._send_lock:
            socket = self._socket
            try:
                await socket.send(payload)
                return
            except ConnectionClosed:
                pass
        if not self.resumable:
            raise SmartVoiceError("WebSocket closed while sending a control message")
        await self._recover(socket)
        async with self._send_lock:
            try:
                await self._socket.send(payload)
            except ConnectionClosed as exc:
                raise StreamRecoveryError("The stream disconnected again while sending control") from exc

    async def _recover(self, observed_socket: ClientConnection) -> None:
        if not self.resumable or self._closed or self._recovery is None:
            raise StreamRecoveryError("This stream was not configured for recovery")
        async with self._recovery_lock:
            if self._closed:
                raise SmartVoiceError("Session is closed")
            # Another task may already have restored the transport.
            if observed_socket is not self._socket:
                return
            await observed_socket.close()
            last_error: BaseException | None = None
            grace = self._recovery.get("grace_seconds", self.open_timeout)
            try:
                deadline = time.monotonic() + max(0.0, float(grace))
            except (TypeError, ValueError):
                deadline = time.monotonic() + self.open_timeout
            for attempt in range(self.resume_attempts):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if attempt and self.resume_backoff:
                    await asyncio.sleep(min(self.resume_backoff * (2 ** (attempt - 1)), remaining))
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                attempt_timeout = min(self.open_timeout, remaining)
                candidate: ClientConnection | None = None
                try:
                    candidate = await connect(
                        _websocket_url(self.base_url),
                        open_timeout=attempt_timeout,
                        max_size=8 * 1024 * 1024,
                        max_queue=4,
                    )
                    request = {
                        "type": "resume",
                        "protocol": PROTOCOL,
                        "session_id": self._recovery["session_id"],
                        "resume_token": self._recovery["resume_token"],
                        "last_event_sequence": self._last_event_sequence,
                        "last_audio_played": self._last_played,
                    }
                    await candidate.send(json.dumps(request, separators=(",", ":")))
                    response = await asyncio.wait_for(
                        self._receive_raw_event(candidate), timeout=attempt_timeout,
                    )
                    if response.type == "error":
                        raise ServerError(
                            str(response.data.get("code", "resume_failed")),
                            str(response.data.get("message", "Streaming recovery failed")),
                            response.data,
                        )
                    if response.type != "session_resumed":
                        raise StreamProtocolError(f"Expected session_resumed, received {response.type!r}")
                    if response.data.get("session_id") != self._recovery["session_id"]:
                        raise StreamProtocolError("The service resumed a different streaming session")
                    self._reconcile_input(response.data)
                    self._socket = candidate
                    async with self._send_lock:
                        for sequence, (start_sample, pcm) in tuple(self._pending_input.items()):
                            if sequence > self._accepted_input_sequence:
                                await self._send_sequenced_frame_unlocked(candidate, sequence, start_sample, pcm)
                    return
                except (OSError, TimeoutError, ConnectionClosed) as exc:
                    last_error = exc
                    if candidate is not None:
                        await candidate.close()
                    # Give the service a chance to observe the close and detach the session.
                    await asyncio.sleep(0)
                except BaseException:
                    if candidate is not None:
                        await candidate.close()
                    raise
            raise StreamRecoveryError(
                f"Could not resume streaming session after {self.resume_attempts} attempts"
            ) from last_error

    def _reconcile_input(self, response: Mapping[str, Any]) -> None:
        sequence = response.get("input_sequence")
        samples = response.get("accepted_samples")
        event_sequence = response.get("last_event_sequence")
        played = response.get("last_audio_played")
        if any(type(value) is not int for value in (sequence, samples, event_sequence, played)):
            raise StreamProtocolError("Streaming resume response is missing cursor fields")
        if sequence < self._accepted_input_sequence or sequence > self._input_sequence:
            raise StreamProtocolError("Service input cursor is outside the sent audio range")
        if event_sequence < self._last_event_sequence or played < self._last_played:
            raise StreamProtocolError("Service recovery cursor moved backwards")
        if sequence == self._input_sequence:
            expected_samples = self._input_samples
        elif sequence == self._accepted_input_sequence:
            expected_samples = self._accepted_input_samples
        else:
            pending = self._pending_input.get(sequence)
            following = self._pending_input.get(sequence + 1)
            if pending is not None:
                expected_samples = pending[0] + len(pending[1]) // 2
            elif following is not None:
                expected_samples = following[0]
            else:
                raise StreamProtocolError("Service resume cursor is outside the retained input window")
        if samples != expected_samples:
            raise StreamProtocolError("Service resume sample cursor does not match sent audio")
        self._accepted_input_sequence = sequence
        self._accepted_input_samples = samples
        for old_sequence in tuple(self._pending_input):
            if old_sequence <= sequence:
                del self._pending_input[old_sequence]
        self._last_event_sequence = event_sequence
        self._last_played = played
        self._last_started = played
        self._input_window_changed.set()

    async def _heartbeat_loop(self, interval: float) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(interval)
                if not self._closed:
                    await self.heartbeat()
        except (ConnectionClosed, SmartVoiceError, asyncio.CancelledError):
            return

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
                await asyncio.gather(self._heartbeat_task, return_exceptions=True)
            await self._socket.close()
