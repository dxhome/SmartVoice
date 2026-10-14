# SmartVoice TypeScript / JavaScript SDK

For the cross-SDK interface reference and additional application integration
examples, see [`../../doc/sdk-spec.md`](../../doc/sdk-spec.md).

An asynchronous Node.js client for SmartVoice transcription (STT), speech
synthesis (TTS), language capability discovery, and all supported Streaming
workflows. It intentionally does not include model or router management.

## Install

From a SmartVoice checkout:

```sh
npm install ./sdk/typescript
```

Node.js 20 or later is required. The SDK uses Fetch, FormData, and Blob from
Node, with `ws` for Streaming connections.

## Discover models and languages

```js
import { SmartVoiceClient } from "@smartvoice/sdk";

const client = new SmartVoiceClient("http://127.0.0.1:8000");
try {
  const capabilities = await client.capabilities();
  console.log(capabilities.modelsFor("transcription"));
  console.log(await client.languages("transcription", { model: "smartvoice-auto" }));

  const streaming = await client.streamingCapabilities();
  console.log(streaming.languagePairs("translated_subtitles", true));
} finally {
  await client.close();
}
```

`languages()` derives STT/TTS languages from `/v1/capabilities`; for
`smartvoice-auto`, it reports languages with an installed runtime route.
Streaming modes and language pairs come from `/v1/audio/stream/capabilities`.

### Connection configuration

Pass `headers` for REST authentication or tracing headers. Node.js clients can
set `webSocketHeaders`; for custom TLS, proxy, or socket behavior, inject a
`webSocketFactory`. The browser runtime cannot set arbitrary WebSocket headers,
so use same-site cookies or a custom application proxy where required. Inject
`fetch` to customize the REST transport.

## STT and TTS

```js
import { writeFile } from "node:fs/promises";
import { SmartVoiceClient } from "@smartvoice/sdk";

const client = new SmartVoiceClient();
const transcript = await client.transcribe("./speech.wav", {
  language: "auto",
  responseFormat: "verbose_json",
  timestamps: true,
});
console.log(transcript.text, transcript.model, transcript.language);

const speech = await client.synthesize("Hello from SmartVoice", {
  language: "en",
  responseFormat: "wav",
});
await writeFile("hello.wav", speech.audio);
await client.close();
```

STT accepts a Node file path, `Uint8Array`, `ArrayBuffer`, or Node's `Blob` / `File` APIs.
Results include actual and requested model/language, response headers, and
timestamped segments when requested. TTS returns audio bytes and available
model, format, duration, and sample-rate metadata.

## Streaming

Streaming input is **16 kHz, mono, PCM16**. Capture and resampling are left to
the application. The SDK provides 20 ms frame defaults, real-time pacing,
heartbeats, ordered event and audio delivery, playback acknowledgements, and
automatic recovery when enabled.

```js
import { SubtitleState } from "@smartvoice/sdk";

const session = await client.startStream({
  mode: "translated_subtitles", // or transcription / spoken_interpretation
  sourceLanguage: "zh",
  targetLanguage: "en",
  resumable: true,
});
const subtitles = new SubtitleState();

const sender = (async () => {
  await session.sendAudioStream(microphonePcmChunks(), { realtime: true });
  await session.finish();
})();

try {
  for await (const event of session) {
    if (event.audio) {
      // event.audio.audioBytes contains one ordered WAV segment.
    } else {
      if (subtitles.apply(event)) {
        console.log(subtitles.text(), subtitles.text("target"));
      }
    }
  }
  await sender;
} finally {
  await session.close();
}

// `microphonePcmChunks()` is an AsyncIterable<Uint8Array> of normalized PCM16.
// For an already-buffered recording, `sendAudio(pcm16Bytes)` remains available.
```

With `resumable: true`, the client retains unacknowledged input up to the
server-advertised replay window, reconnects within the server recovery grace
period, replays accepted audio safely, and resumes output from the last
acknowledged cursor. Keep consuming `session.events()` while sending audio.
Recovery applies only while the same client process and server process remain
alive.
`session.close()` sends `cancel` if no terminal event was received, so early
breaks and exceptions release resumable sessions promptly. Pass
`{ cancelIfIncomplete: false }` only when intentionally detaching for recovery.

`SubtitleState` retains up to 1,000 units per kind by default. Configure the
limit with `new SubtitleState(maxUnits)`, or archive `units()` and call
`clear()` between display windows. The browser WebSocket adapter bounds its
outgoing queue to 4 MiB by default, pauses above the 4 MiB high-water mark,
resumes below the 2 MiB low-water mark, and errors after 10 seconds without
drain. Tune these with `browserSendBufferHighWaterMarkBytes`,
`browserSendBufferLowWaterMarkBytes`, and `browserSendBufferWaitTimeoutMs`.

## Examples and tests

See [`examples/stt-tts.mjs`](examples/stt-tts.mjs) and
[`examples/streaming.mjs`](examples/streaming.mjs). To run them from this
checkout after installing dependencies:

```sh
npm ci --prefix sdk/typescript
npm run build --prefix sdk/typescript
node sdk/typescript/examples/stt-tts.mjs ./speech.wav
node sdk/typescript/examples/streaming.mjs ./speech.pcm
```

`streaming.mjs` expects headerless normalized PCM16 input. The test suite uses
mock HTTP and WebSocket transports and runs with `npm test --prefix
sdk/typescript`.

## Browser entry

Browser applications can import the browser-only build, which has no Node.js
filesystem or `ws` dependency:

```js
import { decodeAudioToPCM16, SmartVoiceClient } from "@smartvoice/sdk/browser";

const client = new SmartVoiceClient("http://127.0.0.1:8000");
const file = document.querySelector("input[type=file]").files[0];
const result = await client.transcribe(file);
console.log(result.text, result.model);
const pcm16 = await decodeAudioToPCM16(file); // 16 kHz mono PCM16 for Streaming
```

The browser entry accepts `File`, `Blob`, `ArrayBuffer`, and `Uint8Array` for
STT. `decodeAudioToPCM16()` converts Web Audio-supported files into normalized
16 kHz mono PCM16 for Streaming. Live microphone capture and audio-device
playback remain app responsibilities; `SubtitleState` handles subtitle
revision tracking. In playback mode, call `markAudioStarted()` and
`markAudioPlayed()` only when the corresponding chunk actually starts and
finishes playing. Serve the application and SmartVoice from an origin permitted
by the service's WebSocket policy.
