# SmartVoice SDK Specification

This document describes the public Python and TypeScript/JavaScript clients in
this repository. Both SDKs connect to a running SmartVoice service; neither
installs models, changes the router, nor manages the service process. The
service's REST and WebSocket contracts remain the source of truth; see the
[HTTP API specification](api-spec.md) and [streaming contract](streaming.md).

## Choose an integration

| Interface | Best fit | Install / access |
|---|---|---|
| REST API | Any language with HTTP support; OpenAI-style audio clients | `http://127.0.0.1:8000/v1` by default |
| Python SDK | Asynchronous Python applications | `python -m pip install -e ./sdk/python` from the repository root |
| Node.js SDK | Node.js applications using TypeScript or JavaScript | `npm install ./sdk/typescript` from the repository root; Node.js 20+ |
| Browser SDK | Browser applications using `File`, `Blob`, and Web Audio | Import `@smartvoice/sdk/browser` from the built SDK package |

The standalone SDK packages are versioned with the product, but the current
release workflow does not publish `smartvoice-sdk` to PyPI or `@smartvoice/sdk`
to npm. Install them from a SmartVoice checkout. Python requires 3.11 or newer.

Use the Console to try workflows and copy REST examples without writing an
SDK client. The Console, REST endpoints, and SDKs all use the same local
service and installed model set.

## Shared behavior

- The default service URL is `http://127.0.0.1:8000` (without the `/v1`
  suffix). For an OpenAI-compatible application setting, use
  `http://127.0.0.1:8000/v1`.
- `smartvoice-auto` selects an installed runtime model using the configured
  task/language route. Pass an installed model ID to request direct inference.
- STT accepts the service's supported audio formats. Streaming input is
  normalized **16 kHz, mono, signed PCM16 little-endian**, not a WAV/MP3
  container.
- TTS returns MP3 by default; request WAV with `response_format="wav"`.
- Capability discovery reports runtime/model availability. It is read-only;
  model installation and router edits use the Console, CLI, or application
  model-management APIs.
- Streaming clients should query `/v1/audio/stream/capabilities` before
  opening a session and select a chain whose `available` value is true.

## Python SDK

Import the public API from `smartvoice_sdk`:

```python
from smartvoice_sdk import (
    APIError,
    ServerError,
    SmartVoiceClient,
    StreamingUnavailableError,
    StreamRecoveryError,
    StreamProtocolError,
    SubtitleState,
)
```

### Client methods

| Method | Purpose |
|---|---|
| `capabilities(task=None)` | Read installed STT/TTS model capabilities. |
| `languages(task, model=None, include_auto=False)` | Get available languages for a task/model; `task` is `transcription` or `speech`. |
| `streaming_capabilities()` | Read workflow/language chains and current availability. |
| `transcribe(audio, model="smartvoice-auto", language="auto", response_format="json", timestamps=False)` | Transcribe a filesystem path or bytes; formats are `json`, `text`, and `verbose_json`. |
| `synthesize(text, model="smartvoice-auto", language="auto", voice="default", speed=1.0, response_format="mp3")` | Return audio bytes and available response metadata; formats are `mp3` and `wav`. |
| `start_stream(...)` | Open a streaming session after checking the selected live capability chain. |
| `aclose()` | Close the SDK-owned HTTP client. An injected `httpx.AsyncClient` remains caller-owned. |

`TranscriptionResult` exposes text, resolved/requested model and language,
response format, raw payload, response headers, and any verbose segments.
`SpeechResult` exposes audio bytes, format, content type, model, duration,
sample rate, segment count, and response headers.

```python
import asyncio
from pathlib import Path
from smartvoice_sdk import SmartVoiceClient

async def main():
    async with SmartVoiceClient("http://127.0.0.1:8000") as client:
        caps = await client.capabilities()
        print(caps.languages_for("transcription", model="smartvoice-auto"))

        transcript = await client.transcribe(
            "speech.wav", language="auto", response_format="verbose_json", timestamps=True,
        )
        print(transcript.text, transcript.model, transcript.language)

        speech = await client.synthesize("你好，来自 SmartVoice。", language="zh")
        Path("speech.mp3").write_bytes(speech.audio)

asyncio.run(main())
```

### Python streaming

The three modes are `transcription`, `translated_subtitles`, and
`spoken_interpretation`. The last two require a distinct target language.
`send_audio()` accepts an in-memory PCM16 byte buffer; `send_audio_stream()`
accepts an `AsyncIterable[bytes]` and frames it incrementally. Both default to
20 ms frames and real-time pacing.

Pass already-normalized PCM16 bytes from your capture or audio-decoding
pipeline into this asynchronous function:

```python
import asyncio
from smartvoice_sdk import SmartVoiceClient, SubtitleState

async def stream_pcm16(pcm16: bytes):
    async with SmartVoiceClient("http://127.0.0.1:8000") as client:
        caps = await client.streaming_capabilities()
        chain = caps.chain("translated_subtitles", "zh", "en")
        if not chain or not chain.get("available"):
            raise RuntimeError(f"Streaming chain is unavailable: {chain}")

        async with await client.start_stream(
            mode="translated_subtitles", source_language="zh", target_language="en",
            resumable=True,
        ) as session:
            subtitles = SubtitleState()

            async def send():
                await session.send_audio(pcm16, realtime=True)
                await session.finish()

            sender = asyncio.create_task(send())
            async for event in session.events():
                if subtitles.apply(event):
                    print(subtitles.text(), subtitles.text("target"))
            await sender

```

While a resumable session is sending audio, consume `session.events()` so
event acknowledgements advance and the bounded input replay window can drain.
`events()` acknowledges an event after the caller advances the iterator.
For `spoken_interpretation` with `output_consumption="playback"`, call
`mark_audio_started(sequence)` and `mark_audio_played(sequence)` only when
playback actually starts and finishes. `SubtitleState` applies source/target
unit revisions and retractions and bounds retained history.

`resumable=True` enables automatic recovery only within the same live SDK
object, service process, and server-advertised recovery grace period. The SDK
retains unacknowledged input up to the server's replay window and raises
`StreamRecoveryError` if it cannot safely continue. Recovery does not survive
client or service restarts and does not move a session between service
instances. Leaving a session context before its terminal event sends cancel by
default; `close(cancel_if_incomplete=False)` intentionally leaves a detached
session for its recovery grace period.

### Python transport configuration and errors

`SmartVoiceClient` accepts `headers`, `websocket_headers`,
`websocket_options`, timeouts, and recovery retry settings. Inject an
`httpx.AsyncClient` to control REST transport/TLS/proxy behavior; the SDK does
not close an injected client. HTTP failures raise `APIError` with status,
service error code, message, and request ID. Streaming setup and server event
errors raise `StreamingUnavailableError`, `ServerError`,
`StreamProtocolError`, or `StreamRecoveryError` as appropriate.

See the [Python SDK README](../sdk/python/README.md) and runnable programs in
[`sdk/python/examples`](../sdk/python/examples/).

## TypeScript / JavaScript SDK

The Node package exports from `@smartvoice/sdk`. Its public API uses
camelCase method and option names.

| Method | Purpose |
|---|---|
| `capabilities({ task })` / `languages(task, options)` | Read task models and supported languages. |
| `streamingCapabilities()` | Read streaming mode/language chains and readiness. |
| `transcribe(audio, options)` | STT from Node file paths, `Uint8Array`, `ArrayBuffer`, `Blob`, or `File`. |
| `synthesize(text, options)` | TTS audio bytes plus response metadata. |
| `startStream(options)` | Start a validated streaming session. |
| `close()` | Close the client; the Fetch-based HTTP layer owns no persistent handle. |

```js
import { writeFile } from "node:fs/promises";
import { SmartVoiceClient } from "@smartvoice/sdk";

const client = new SmartVoiceClient("http://127.0.0.1:8000");
try {
  const transcript = await client.transcribe("speech.wav", {
    language: "auto", responseFormat: "verbose_json", timestamps: true,
  });
  console.log(transcript.text, transcript.model, transcript.language);

  const speech = await client.synthesize("Hello from SmartVoice", { language: "en" });
  await writeFile("speech.mp3", speech.audio);
} finally {
  await client.close();
}
```

`StreamingSession` implements `AsyncIterable<StreamEvent>` and provides
`sendAudioFrame()`, `sendAudio()`, `sendAudioStream()`, `finish()`,
`cancel()`, `heartbeatControl()`, `ack()`, `markAudioStarted()`,
`markAudioPlayed()`, and `close()`. `SubtitleState` handles revision-aware
source/target text. The Node entry uses the `ws` package for its WebSocket
transport; custom `fetch` and `webSocketFactory` implementations can be
injected. Errors include `APIError`, `StreamingUnavailableError`,
`ServerError`, `StreamProtocolError`, and `StreamRecoveryError`.

### Browser entry

Browser apps import `@smartvoice/sdk/browser`. It accepts browser `File` and
`Blob` values for STT, and exports `decodeAudioToPCM16()` to convert audio
supported by Web Audio into 16 kHz mono PCM16 for Streaming:

```js
import { decodeAudioToPCM16, SmartVoiceClient } from "@smartvoice/sdk/browser";

const client = new SmartVoiceClient("http://127.0.0.1:8000");
const file = document.querySelector('input[type="file"]').files[0];
const transcript = await client.transcribe(file, { language: "auto" });
console.log(transcript.text);

const pcm16 = await decodeAudioToPCM16(file);
const session = await client.startStream({
  mode: "transcription", sourceLanguage: "zh", resumable: true,
});
const sender = session.sendAudio(pcm16, { realtime: true }).then(() => session.finish());
for await (const event of session) console.log(event.type, event.data);
await sender;
await session.close();
```

The browser adapter bounds its outgoing WebSocket buffer and waits for it to
drain. Browser microphone capture, output-device playback, and audio UI remain
application responsibilities. Browser WebSocket connections must satisfy the
service's origin policy; browser WebSocket APIs cannot attach arbitrary custom
headers. Serve the app and service from an allowed same origin or use an
application proxy.

See the [TypeScript/JavaScript SDK README](../sdk/typescript/README.md), its
[examples](../sdk/typescript/examples/), and the [testing guide](testing.md).
