import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";
import { BrowserWebSocketAdapter, decodeAudioToPCM16, SmartVoiceClient as BrowserSmartVoiceClient } from "../dist/browser.js";
import {
  APIError,
  ServerError,
  ServiceCapabilities,
  SmartVoiceClient,
  SmartVoiceError,
  StreamRecoveryError,
  StreamProtocolError,
  StreamingCapabilities,
  StreamingUnavailableError,
  SubtitleState,
} from "../dist/node.js";

test("capability helpers expose installed languages and ready routes", () => {
  const capabilities = new ServiceCapabilities({
    tasks: [
      { task: "transcription", model: "asr-zh", languages: ["zh"], streaming: true },
      { task: "transcription", model: "asr-multi", languages: ["en", "ja"], streaming: false },
      { task: "speech", model: "tts-en", languages: ["en"], streaming: false },
    ],
    router: { tasks: { transcription: { zh: [{ installed: true }], en: [{ installed: false }], ja: [{ installed: true }] } } },
  });
  assert.deepEqual(capabilities.languagesFor("transcription"), ["en", "ja", "zh"]);
  assert.deepEqual(capabilities.languagesFor("transcription", { model: "asr-multi" }), ["en", "ja"]);
  assert.deepEqual(capabilities.languagesFor("transcription", { model: "smartvoice-auto" }), ["ja", "zh"]);
  assert.deepEqual(capabilities.languagesFor("speech", { model: "tts-en", includeAuto: true }), ["auto", "en"]);
  assert.equal(capabilities.supportsLanguage("transcription", "zh", { model: "asr-zh" }), true);
  assert.throws(() => capabilities.languagesFor("streaming"), TypeError);

  const streaming = new StreamingCapabilities({ chains: [
    { mode: "transcription", source_language: "zh", target_language: null, available: true },
    { mode: "transcription", source_language: "en", target_language: null, available: false },
    { mode: "translated_subtitles", source_language: "zh", target_language: "en", available: false },
    { mode: "spoken_interpretation", source_language: "ja", target_language: "en", available: true },
  ] });
  assert.deepEqual(streaming.sourceLanguages("transcription"), ["en", "zh"]);
  assert.deepEqual(streaming.sourceLanguages("transcription", true), ["zh"]);
  assert.deepEqual(streaming.languagePairs("translated_subtitles"), [["zh", "en"]]);
  assert.deepEqual(streaming.sourceLanguages("transcription", true), ["zh"]);
  assert.deepEqual(streaming.languagePairs("spoken_interpretation", true), [["ja", "en"]]);
});

test("REST methods use multipart STT, structured errors, and binary TTS results", async () => {
  const calls = [];
  const responses = [
    new Response(JSON.stringify({ text: "hello", language: "en", model: "asr-en", segments: [{ text: "hello" }] }), {
      status: 200, headers: { "content-type": "application/json" },
    }),
    new Response(JSON.stringify({ error: { code: "bad_audio", message: "Audio could not be decoded", request_id: "req-1" } }), {
      status: 422, headers: { "content-type": "application/json" },
    }),
    new Response(new Uint8Array([82, 73, 70, 70, 1, 2, 3, 4]), {
      status: 200, headers: { "content-type": "audio/wav", "x-audio-format": "wav", "x-model-id": "tts-en", "x-audio-duration": "1.25", "x-audio-sample-rate": "24000" },
    }),
  ];
  const client = new SmartVoiceClient("http://voice.test/base/", {
    fetch: async (url, init) => { calls.push({ url: String(url), init }); return responses.shift(); },
  });
  const transcription = await client.transcribe(new Uint8Array([1, 2]), {
    language: "en", responseFormat: "verbose_json", timestamps: true,
  });
  assert.equal(transcription.text, "hello");
  assert.equal(transcription.model, "asr-en");
  assert.equal(transcription.segments.length, 1);
  const form = calls[0].init.body;
  assert.equal(form.get("language"), "en");
  assert.equal(form.get("timestamps"), "true");
  assert.equal(form.get("file").name, "audio.wav");

  await assert.rejects(client.transcribe(new Uint8Array([0])), (error) => {
    assert.ok(error instanceof APIError);
    assert.equal(error.statusCode, 422);
    assert.equal(error.code, "bad_audio");
    assert.equal(error.message, "Audio could not be decoded");
    assert.equal(error.requestId, "req-1");
    return true;
  });

  const speech = await client.synthesize("Hello", { language: "en", responseFormat: "wav" });
  assert.equal(speech.format, "wav");
  assert.equal(speech.model, "tts-en");
  assert.equal(speech.durationSeconds, 1.25);
  assert.deepEqual([...speech.audio.slice(0, 4)], [82, 73, 70, 70]);
  assert.match(calls[2].init.body, /"response_format":"wav"/);

  const textClient = new SmartVoiceClient("http://voice.test", {
    fetch: async (url, init) => {
      calls.push({ url: String(url), init });
      if (String(url).endsWith("/v1/audio/transcriptions")) {
        return new Response("plain words", { status: 200, headers: { "x-resolved-language": "en" } });
      }
      return new Response(new Uint8Array([73, 68, 51]), { status: 200, headers: {
        "content-type": "audio/mpeg", "x-audio-duration": "bad", "x-audio-sample-rate": "24000",
      } });
    },
  });
  const plain = await textClient.transcribe(new Uint8Array([1]), { responseFormat: "text" });
  assert.equal(plain.text, "plain words");
  assert.equal(plain.language, "en");
  const defaultSpeech = await textClient.synthesize("Hello");
  assert.equal(JSON.parse(calls.at(-1).init.body).response_format, "mp3");
  assert.equal(defaultSpeech.format, "mp3");
  assert.equal(defaultSpeech.durationSeconds, null);
  assert.equal(defaultSpeech.sampleRate, 24000);
  await textClient.close();
  await client.close();
});

test("HTTP errors and input validation keep stable SDK error shapes", async () => {
  const client = new SmartVoiceClient("http://voice.test", {
    fetch: async () => new Response("upstream unavailable", { status: 503 }),
  });
  await assert.rejects(client.capabilities(), (error) => {
    assert.ok(error instanceof APIError);
    assert.equal(error.statusCode, 503);
    assert.equal(error.code, "request_failed");
    assert.match(error.message, /HTTP 503/);
    return true;
  });
  await assert.rejects(client.transcribe(new Uint8Array([1]), { responseFormat: "srt" }), TypeError);
  await assert.rejects(client.transcribe(new Uint8Array([1]), { timestamps: "yes" }), TypeError);
  await assert.rejects(client.synthesize("  "), TypeError);
  await assert.rejects(client.synthesize("hello", { speed: 2.1 }), RangeError);
  await assert.rejects(client.synthesize("hello", { responseFormat: "ogg" }), TypeError);
  await client.close();
});

test("REST client applies default headers to SDK requests", async () => {
  const requests = [];
  const client = new SmartVoiceClient("http://voice.test", {
    headers: { Authorization: "Bearer test-token", "X-SDK-Client": "typescript" },
    fetch: async (url, init) => {
      requests.push({ url: String(url), headers: new Headers(init.headers) });
      return new Response(JSON.stringify({ tasks: [] }), {
        status: 200, headers: { "content-type": "application/json" },
      });
    },
  });
  await client.capabilities();
  assert.equal(requests[0].headers.get("authorization"), "Bearer test-token");
  assert.equal(requests[0].headers.get("x-sdk-client"), "typescript");
  await client.close();
});

test("stream config rejects unsupported playback and invalid frame sizes", async () => {
  const client = new SmartVoiceClient("http://voice.test", {
    fetch: async () => new Response(JSON.stringify({ chains: [] }), { status: 200 }),
  });
  await assert.rejects(client.startStream({ mode: "transcription", sourceLanguage: "zh", outputConsumption: "playback" }), TypeError);
  await assert.rejects(client.startStream({ mode: "spoken_interpretation", sourceLanguage: "zh", targetLanguage: "zh" }), TypeError);
  await assert.rejects(client.startStream({ mode: "transcription", sourceLanguage: "zh", targetLanguage: "en" }), TypeError);
  await assert.rejects(client.startStream({ mode: "transcription", sourceLanguage: "zh", ackWindow: 0 }), RangeError);
  await assert.rejects(client.startStream({ mode: "transcription", sourceLanguage: "zh", includeSourceText: 1 }), TypeError);
  await client.close();
});

test("stream startup rejects a known unavailable model chain before opening a WebSocket", async () => {
  const client = new SmartVoiceClient("http://voice.test", {
    fetch: async () => new Response(JSON.stringify({ chains: [
      { mode: "spoken_interpretation", source_language: "zh", target_language: "en", available: false,
        reason: "model_unavailable", missing_models: ["streaming-tts-zh"] },
    ] }), { status: 200, headers: { "content-type": "application/json" } }),
    webSocketFactory: () => { throw new Error("WebSocket must not be opened for an unavailable chain"); },
  });
  await assert.rejects(
    client.startStream({ mode: "spoken_interpretation", sourceLanguage: "zh", targetLanguage: "en" }),
    (error) => error instanceof StreamingUnavailableError
      && error.chain.reason === "model_unavailable"
      && error.chain.missing_models?.[0] === "streaming-tts-zh",
  );
  await client.close();
});

test("browser entry adapts native WebSocket events and rejects filesystem paths", async () => {
  const NativeWebSocket = globalThis.WebSocket;
  class FakeBrowserSocket extends EventTarget {
    binaryType = "blob";
    readyState = 1;
    bufferedAmount = 0;
    constructor(readonlyUrl) { super(); this.url = readonlyUrl; }
    send(data) { this.bufferedAmount += data.byteLength ?? data.length ?? 0; }
    close() { this.readyState = 3; }
  }
  globalThis.WebSocket = FakeBrowserSocket;
  try {
    const socket = new BrowserWebSocketAdapter("ws://voice.test/v1/audio/stream");
    assert.equal(socket.socket?.binaryType, "arraybuffer");
    let message;
    const listener = (...args) => { message = args; };
    socket.on("message", listener);
    socket.socket.dispatchEvent(new MessageEvent("message", { data: new Uint8Array([1, 2]).buffer }));
    assert.deepEqual([...new Uint8Array(message[0])], [1, 2]);
    assert.equal(message[1], true);
    socket.off("message", listener);

    const client = new BrowserSmartVoiceClient("http://voice.test", { fetch: async () => new Response("{}") });
    await assert.rejects(client.transcribe("./local.wav"), /File paths are only supported by the Node.js SDK/);
    await client.close();
  } finally {
    globalThis.WebSocket = NativeWebSocket;
  }
});

test("browser WebSocket adapter waits for its send buffer and fails on drain timeout", async () => {
  const NativeWebSocket = globalThis.WebSocket;
  class FakeBrowserSocket extends EventTarget {
    readyState = 1;
    bufferedAmount = 8;
    sent = [];
    constructor(url) { super(); this.url = url; }
    send(data) { this.sent.push(data); this.bufferedAmount += data.byteLength; }
    close() { this.readyState = 3; }
  }
  globalThis.WebSocket = FakeBrowserSocket;
  try {
    const adapter = new BrowserWebSocketAdapter("ws://voice.test", {
      highWaterMarkBytes: 8, lowWaterMarkBytes: 2, waitTimeoutMs: 200,
    });
    setTimeout(() => { adapter.socket.bufferedAmount = 0; }, 30);
    const completed = await new Promise((resolve) => adapter.send(new Uint8Array([1, 2]), (error) => resolve(error)));
    assert.equal(completed, undefined);
    assert.equal(adapter.socket.sent.length, 1);

    const blocked = new BrowserWebSocketAdapter("ws://voice.test", {
      highWaterMarkBytes: 8, lowWaterMarkBytes: 2, waitTimeoutMs: 20,
    });
    const failure = await new Promise((resolve) => blocked.send(new Uint8Array([1, 2]), (error) => resolve(error)));
    assert.match(failure.message, /did not drain/);
    assert.equal(blocked.socket.sent.length, 0);
  } finally {
    globalThis.WebSocket = NativeWebSocket;
  }
});

test("browser audio adapter downmixes/resamples to little-endian PCM16 samples", async () => {
  const originalAudioContext = globalThis.AudioContext;
  const originalOfflineContext = globalThis.OfflineAudioContext;
  class FakeAudioContext {
    async decodeAudioData() {
      return {
        length: 3, duration: 3 / 8_000, sampleRate: 8_000, numberOfChannels: 1,
        getChannelData: () => new Float32Array([-1, 0, 1]),
      };
    }
    createBuffer() { return { getChannelData: () => new Float32Array(3) }; }
    async close() {}
  }
  class FakeOfflineAudioContext {
    constructor() { this.destination = {}; }
    createBufferSource() { return { connect() {}, start() {} }; }
    async startRendering() { return { getChannelData: () => new Float32Array([-1, 0, 1]) }; }
  }
  globalThis.AudioContext = FakeAudioContext;
  globalThis.OfflineAudioContext = FakeOfflineAudioContext;
  try {
    const pcm = await decodeAudioToPCM16(new Uint8Array([1]));
    const view = new DataView(pcm.buffer, pcm.byteOffset, pcm.byteLength);
    assert.equal(pcm.byteLength, 6);
    assert.equal(view.getInt16(0, true), -32768);
    assert.equal(view.getInt16(2, true), 0);
    assert.equal(view.getInt16(4, true), 32767);
  } finally {
    globalThis.AudioContext = originalAudioContext;
    globalThis.OfflineAudioContext = originalOfflineContext;
  }
});

class FakeSocket extends EventEmitter {
  readyState = 0;
  sent = [];
  constructor(role) {
    super();
    this.role = role;
    setImmediate(() => { this.readyState = 1; this.emit("open"); });
  }
  send(data, callback) {
    this.sent.push(data);
    if (typeof data === "string") {
      const message = JSON.parse(data);
      if (message.type === "configure") {
        this.message({ type: "session_ready", session_id: "session-1", event_sequence: 1, heartbeat_interval_seconds: 120,
          recovery: { resume_token: "test-token", input_history_frames: 128, grace_seconds: 5 } });
      } else if (message.type === "resume") {
        this.message({ type: "session_resumed", session_id: "session-1", input_sequence: 1, accepted_samples: 320, last_event_sequence: 1, last_audio_played: 0 });
      } else if (message.type === "audio_frame") this.header = message;
    } else if (this.role === "recovery" && this.header) {
      this.message({ type: "input_ack", input_sequence: this.header.sequence, accepted_samples: this.header.start_sample + data.byteLength / 2 });
      this.message({ type: "source_final", event_sequence: 2, text: "recovered" });
      this.message({ type: "session_complete", event_sequence: 3, status: "complete" });
      this.header = undefined;
    }
    callback?.();
  }
  message(data, isBinary = false) { this.emit("message", isBinary ? data : JSON.stringify(data), isBinary); }
  close() { this.readyState = 3; this.emit("close"); }
  abort() { this.readyState = 3; this.emit("close"); }
}

test("resumable Streaming reconnects and replays unacknowledged input", async () => {
  const sockets = [];
  const client = new SmartVoiceClient("http://voice.test", {
    resumeAttempts: 2,
    resumeBackoffMs: 0,
    fetch: async () => new Response(JSON.stringify({ chains: [
      { mode: "transcription", source_language: "zh", target_language: null, available: true },
    ] }), { status: 200, headers: { "content-type": "application/json" } }),
    webSocketFactory: () => {
      const socket = new FakeSocket(sockets.length ? "recovery" : "initial");
      sockets.push(socket);
      return socket;
    },
  });
  const session = await client.startStream({ mode: "transcription", sourceLanguage: "zh", resumable: true });
  await session.sendAudioFrame(new Uint8Array(640));
  await session.sendAudioFrame(new Uint8Array(640));
  sockets[0].abort();
  const events = [];
  for await (const event of session.events()) events.push(event);
  assert.deepEqual(events.map((event) => event.type), ["source_final", "session_complete"]);
  assert.equal(events[0].data.text, "recovered");
  assert.deepEqual(JSON.parse(sockets[1].sent[1]), { type: "audio_frame", sequence: 2, start_sample: 320 });
  assert.equal(sockets[1].sent[2].byteLength, 640);
  assert.equal(session.pendingInputFrames, 0);
  await session.close();
  await client.close();
});

test("streaming validates and starts each supported workflow mode", async () => {
  const sockets = [];
  const client = new SmartVoiceClient("http://voice.test", {
    fetch: async () => new Response(JSON.stringify({ chains: [
      { mode: "transcription", source_language: "zh", target_language: null },
      { mode: "translated_subtitles", source_language: "zh", target_language: "en" },
      { mode: "spoken_interpretation", source_language: "zh", target_language: "en" },
    ] }), { status: 200, headers: { "content-type": "application/json" } }),
    webSocketFactory: () => {
      const socket = new FakeSocket("initial");
      sockets.push(socket);
      return socket;
    },
  });
  const modes = [
    { mode: "transcription", sourceLanguage: "zh" },
    { mode: "translated_subtitles", sourceLanguage: "zh", targetLanguage: "en" },
    { mode: "spoken_interpretation", sourceLanguage: "zh", targetLanguage: "en", outputConsumption: "playback" },
  ];
  for (const options of modes) {
    const session = await client.startStream(options);
    const configure = JSON.parse(sockets.at(-1).sent[0]);
    assert.equal(configure.mode, options.mode);
    assert.equal(configure.source_language, "zh");
    if (options.targetLanguage) assert.equal(configure.target_language, "en");
    assert.equal(configure.output_consumption, options.outputConsumption ?? "delivery");
    await session.close();
  }
  await client.close();
});

test("audio replay accepts identical data and rejects changed duplicates", async () => {
  const socket = new FakeSocket("initial");
  const client = new SmartVoiceClient("http://voice.test", {
    fetch: async () => new Response(JSON.stringify({ chains: [
      { mode: "transcription", source_language: "zh", target_language: null },
    ] }), { status: 200 }),
    webSocketFactory: () => socket,
  });
  const session = await client.startStream({ mode: "transcription", sourceLanguage: "zh" });
  const descriptor = { type: "audio_segment", event_sequence: 2, audio_sequence: 1, audio_bytes: 4, format: "wav" };
  socket.message(descriptor);
  socket.message(Buffer.from("WAVE"), true);
  assert.equal((await session.receiveRawEvent()).audio.sequence, 1);
  socket.message(descriptor);
  socket.message(Buffer.from("WAVE"), true);
  assert.equal((await session.receiveRawEvent()).audio.sequence, 1);
  socket.message(descriptor);
  socket.message(Buffer.from("NOPE"), true);
  await assert.rejects(session.receiveRawEvent(), StreamProtocolError);
  await session.close();
  await client.close();
});

test("playback acknowledgements are ordered and controls are sent explicitly", async () => {
  const socket = new FakeSocket("initial");
  const client = new SmartVoiceClient("http://voice.test", {
    fetch: async () => new Response(JSON.stringify({ chains: [
      { mode: "spoken_interpretation", source_language: "zh", target_language: "en" },
    ] }), { status: 200 }),
    webSocketFactory: () => socket,
  });
  const session = await client.startStream({
    mode: "spoken_interpretation", sourceLanguage: "zh", targetLanguage: "en", outputConsumption: "playback",
  });
  const descriptor = { type: "audio_segment", event_sequence: 2, audio_sequence: 1, audio_bytes: 4, format: "wav" };
  socket.message(descriptor);
  socket.message(Buffer.from("WAVE"), true);
  const event = await session.receiveRawEvent();
  assert.equal(event.audio.sequence, 1);
  await assert.rejects(session.markAudioPlayed(1), RangeError);
  await session.markAudioStarted(1);
  await session.markAudioPlayed(1);
  await session.cancel();
  await session.heartbeatControl();
  await session.finish();
  const controls = socket.sent.filter((item) => typeof item === "string").map((item) => JSON.parse(item).type);
  assert.ok(controls.includes("audio_started"));
  assert.ok(controls.includes("audio_played"));
  assert.ok(controls.includes("cancel"));
  assert.ok(controls.includes("heartbeat"));
  assert.ok(controls.includes("finish"));
  await session.close();
  await assert.rejects(session.sendAudioFrame(new Uint8Array([0, 0])), SmartVoiceError);
  await client.close();
});

test("terminal stream errors and malformed audio descriptors are surfaced", async () => {
  const socket = new FakeSocket("initial");
  const client = new SmartVoiceClient("http://voice.test", {
    fetch: async () => new Response(JSON.stringify({ chains: [
      { mode: "transcription", source_language: "zh", target_language: null },
    ] }), { status: 200 }),
    webSocketFactory: () => socket,
  });
  const session = await client.startStream({ mode: "transcription", sourceLanguage: "zh" });
  socket.message({ type: "error", event_sequence: 2, code: "inference_failed", message: "Model failed" });
  const iterator = session.events();
  const first = await iterator.next();
  assert.equal(first.value.type, "error");
  await assert.rejects(iterator.next(), (error) => {
    assert.ok(error instanceof ServerError);
    assert.equal(error.code, "inference_failed");
    return true;
  });
  await session.close();
  await client.close();

  const protocolSocket = new FakeSocket("initial");
  const protocolClient = new SmartVoiceClient("http://voice.test", {
    fetch: async () => new Response(JSON.stringify({ chains: [
      { mode: "transcription", source_language: "zh", target_language: null },
    ] }), { status: 200 }),
    webSocketFactory: () => protocolSocket,
  });
  const protocolSession = await protocolClient.startStream({ mode: "transcription", sourceLanguage: "zh" });
  protocolSocket.message({ type: "audio_segment", event_sequence: 2, audio_sequence: 1, audio_bytes: 4 });
  protocolSocket.message(Buffer.from("bad"), true);
  await assert.rejects(protocolSession.receiveRawEvent(), /Audio payload length/);
  await protocolSession.close();
  await protocolClient.close();
});

test("input acknowledgement offsets and audio sequence gaps are rejected", async () => {
  const socket = new FakeSocket("initial");
  const client = new SmartVoiceClient("http://voice.test", {
    fetch: async () => new Response(JSON.stringify({ chains: [
      { mode: "transcription", source_language: "zh", target_language: null },
    ] }), { status: 200 }),
    webSocketFactory: () => socket,
  });
  const session = await client.startStream({ mode: "transcription", sourceLanguage: "zh", resumable: true });
  await session.sendAudioFrame(new Uint8Array(640));
  socket.message({ type: "input_ack", input_sequence: 1, accepted_samples: 1 });
  const events = session.events();
  await assert.rejects(events.next(), /sample offset/);
  await session.close();
  await client.close();

  const gapSocket = new FakeSocket("initial");
  const gapClient = new SmartVoiceClient("http://voice.test", {
    fetch: async () => new Response(JSON.stringify({ chains: [
      { mode: "transcription", source_language: "zh", target_language: null },
    ] }), { status: 200 }),
    webSocketFactory: () => gapSocket,
  });
  const gapSession = await gapClient.startStream({ mode: "transcription", sourceLanguage: "zh" });
  gapSocket.message({ type: "audio_segment", event_sequence: 2, audio_sequence: 2, audio_bytes: 4 });
  gapSocket.message(Buffer.from("WAVE"), true);
  await assert.rejects(gapSession.receiveRawEvent(), /missing, duplicated, or out of order/);
  await gapSession.close();
  await gapClient.close();
});

test("subtitle state ignores stale revisions, handles retractions, and preserves final text", () => {
  const state = new SubtitleState();
  assert.equal(state.apply({ type: "source_unit_partial", unit_id: "u1", revision: 1, text: "hel" }), true);
  assert.equal(state.apply({ type: "source_unit_partial", unit_id: "u1", revision: 2, text: "hello" }), true);
  assert.equal(state.apply({ type: "source_unit_partial", unit_id: "u1", revision: 1, text: "stale" }), false);
  assert.equal(state.apply({ type: "source_unit_final", unit_id: "u1", revision: 3, text: "Hello." }), true);
  assert.equal(state.apply({ type: "source_unit_partial", unit_id: "u1", revision: 4, text: "overwrite" }), false);
  assert.equal(state.apply({ type: "target_partial", unit_id: "u2", revision: 1, text: "draft" }), true);
  assert.equal(state.apply({ type: "target_partial", unit_id: "u2", revision: 2, text: "", retracted: true }), true);
  assert.equal(state.text(), "Hello.");
  assert.equal(state.text("target"), "");

  const bounded = new SubtitleState(2);
  bounded.apply({ type: "source_unit_partial", unit_id: "u1", revision: 1, text: "old" });
  bounded.apply({ type: "source_unit_partial", unit_id: "u1", revision: 2, text: "", retracted: true });
  bounded.apply({ type: "source_unit_partial", unit_id: "u1", revision: 3, text: "again" });
  assert.equal(bounded.text(), "again");
  assert.deepEqual(bounded.units().map((unit) => unit.unitId), ["u1"]);
  bounded.apply({ type: "source_unit_final", unit_id: "u2", revision: 1, text: "second" });
  bounded.apply({ type: "source_unit_final", unit_id: "u3", revision: 1, text: "third" });
  assert.deepEqual(bounded.units().map((unit) => unit.unitId), ["u2", "u3"]);
  assert.equal(bounded.text(), "secondthird");
  assert.throws(() => new SubtitleState(0), /maxUnits/);
});

test("closing an incomplete stream cancels it, while terminal streams close cleanly", async () => {
  const socket = new FakeSocket("initial");
  const client = new SmartVoiceClient("http://voice.test", {
    fetch: async () => new Response(JSON.stringify({ chains: [
      { mode: "transcription", source_language: "zh", target_language: null },
    ] }), { status: 200 }),
    webSocketFactory: () => socket,
  });
  const session = await client.startStream({ mode: "transcription", sourceLanguage: "zh", resumable: true });
  await session.close();
  assert.deepEqual(socket.sent.filter((item) => typeof item === "string").map((item) => JSON.parse(item).type), ["configure", "ack", "cancel"]);
  await client.close();

  const terminalSocket = new FakeSocket("initial");
  const terminalClient = new SmartVoiceClient("http://voice.test", {
    fetch: async () => new Response(JSON.stringify({ chains: [
      { mode: "transcription", source_language: "zh", target_language: null },
    ] }), { status: 200 }),
    webSocketFactory: () => terminalSocket,
  });
  const terminalSession = await terminalClient.startStream({ mode: "transcription", sourceLanguage: "zh" });
  terminalSocket.message({ type: "session_complete", event_sequence: 2, status: "complete" });
  for await (const _event of terminalSession.events()) { /* drain through terminal event */ }
  await terminalSession.close();
  assert.equal(terminalSocket.sent.filter((item) => typeof item === "string").some((item) => JSON.parse(item).type === "cancel"), false);
  await terminalClient.close();
});

test("sendAudioStream frames asynchronous PCM chunks incrementally", async () => {
  const socket = new FakeSocket("initial");
  const client = new SmartVoiceClient("http://voice.test", {
    fetch: async () => new Response(JSON.stringify({ chains: [
      { mode: "transcription", source_language: "zh", target_language: null },
    ] }), { status: 200 }),
    webSocketFactory: () => socket,
  });
  const session = await client.startStream({ mode: "transcription", sourceLanguage: "zh" });
  async function* chunks() { yield new Uint8Array([49, 50]); yield new Uint8Array([51, 52, 53, 54]); }
  await session.sendAudioStream(chunks(), { frameBytes: 4, realtime: false });
  assert.deepEqual(socket.sent.filter((data) => data instanceof Uint8Array).map((data) => [...data]), [[49, 50, 51, 52], [53, 54]]);
  await session.close();
  await client.close();
});
