import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import WebSocket from "ws";
import { SmartVoiceClient } from "../dist/node.js";

const baseUrl = process.env.SMARTVOICE_TS_BASE_URL;
const wavPath = process.env.SMARTVOICE_TS_WAV;
const sttModel = process.env.SMARTVOICE_TS_STT_MODEL;
const ttsModel = process.env.SMARTVOICE_TS_TTS_MODEL;
const runApi = process.env.SMARTVOICE_TS_API === "1";
const runStreaming = process.env.SMARTVOICE_TS_STREAMING === "1";

function decodePcm16Wav(bytes) {
  if (bytes.toString("ascii", 0, 4) !== "RIFF" || bytes.toString("ascii", 8, 12) !== "WAVE") {
    throw new Error("Expected a PCM WAV fixture");
  }
  let format;
  let data;
  for (let offset = 12; offset + 8 <= bytes.length;) {
    const name = bytes.toString("ascii", offset, offset + 4);
    const size = bytes.readUInt32LE(offset + 4);
    const start = offset + 8;
    if (start + size > bytes.length) throw new Error("WAV chunk extends beyond file length");
    if (name === "fmt ") {
      format = {
        encoding: bytes.readUInt16LE(start),
        channels: bytes.readUInt16LE(start + 2),
        sampleRate: bytes.readUInt32LE(start + 4),
        bitsPerSample: bytes.readUInt16LE(start + 14),
      };
    } else if (name === "data") data = bytes.subarray(start, start + size);
    offset = start + size + (size % 2);
  }
  assert.deepEqual(format, { encoding: 1, channels: 1, sampleRate: 16_000, bitsPerSample: 16 });
  assert.ok(data?.length, "WAV fixture has PCM data");
  return new Uint8Array(data);
}

function disconnectOnFirstSourceText() {
  let disconnected = false;
  let socketCount = 0;
  const factory = (socketUrl) => {
    socketCount += 1;
    const socket = new WebSocket(socketUrl);
    const wrappers = new Map();
    const wrapped = {
      get readyState() { return socket.readyState; },
      send(data, callback) { socket.send(data, callback); },
      close(code, reason) { socket.close(code, reason); },
      on(event, listener) {
        const proxy = (...args) => {
          listener(...args);
          if (event === "message" && !disconnected && !args[1]) {
            try {
              const message = JSON.parse(args[0].toString());
              if (message.type?.startsWith("source_") && message.text?.trim()) {
                disconnected = true;
                setImmediate(() => socket.terminate());
              }
            } catch { /* Non-JSON messages are reported by the SDK parser. */ }
          }
        };
        wrappers.set(listener, proxy);
        socket.on(event, proxy);
        return wrapped;
      },
      off(event, listener) {
        const proxy = wrappers.get(listener);
        if (proxy) socket.off(event, proxy);
        return wrapped;
      },
    };
    return wrapped;
  };
  return { factory, wasDisconnected: () => disconnected, socketCount: () => socketCount };
}

test("JavaScript SDK discovers languages and performs real STT/TTS", {
  skip: !runApi || !baseUrl || !wavPath || !sttModel || !ttsModel,
  timeout: 240_000,
}, async () => {
  const client = new SmartVoiceClient(baseUrl, { requestTimeoutMs: 180_000 });
  try {
    const capabilities = await client.capabilities();
    assert.ok(capabilities.modelsFor("transcription").some((item) => item.model === sttModel));
    assert.ok(capabilities.modelsFor("speech").some((item) => item.model === ttsModel));
    assert.ok((await client.languages("transcription", { model: sttModel })).includes("zh"));
    assert.ok((await client.languages("speech", { model: ttsModel })).includes("zh"));

    const transcription = await client.transcribe(wavPath, {
      model: sttModel, language: "zh", responseFormat: "verbose_json", timestamps: true,
    });
    assert.equal(transcription.model, sttModel);
    assert.equal(transcription.language, "zh");
    assert.ok(transcription.text.trim());
    assert.ok(transcription.segments.length);

    const speech = await client.synthesize("你好，这是通过 JavaScript SDK 执行的真实语音合成回归测试。", {
      model: ttsModel, language: "zh", responseFormat: "wav",
    });
    assert.equal(speech.model, ttsModel);
    assert.equal(speech.format, "wav");
    assert.equal(speech.contentType.split(";", 1)[0], "audio/wav");
    assert.equal(Buffer.from(speech.audio).toString("ascii", 0, 4), "RIFF");
    assert.ok(speech.audio.length > 44);
    assert.ok(speech.durationSeconds > 0);
  } finally {
    await client.close();
  }
});

test("JavaScript SDK streams real audio and recovers after a WebSocket drop", {
  skip: !runStreaming || !baseUrl || !wavPath,
  timeout: 240_000,
}, async () => {
  const pcm = decodePcm16Wav(await readFile(wavPath));
  const transport = disconnectOnFirstSourceText();
  const client = new SmartVoiceClient(baseUrl, {
    requestTimeoutMs: 180_000,
    resumeAttempts: 5,
    resumeBackoffMs: 100,
    webSocketFactory: transport.factory,
  });
  let session;
  try {
    const capabilities = await client.streamingCapabilities();
    const chain = capabilities.chain("transcription", "zh");
    assert.ok(chain, "zh transcription chain is advertised");
    assert.equal(chain.available, true, JSON.stringify(chain));
    session = await client.startStream({ mode: "transcription", sourceLanguage: "zh", resumable: true });
    const sender = (async () => {
      await session.sendAudio(pcm, { realtime: true });
      await session.finish();
    })();
    const events = [];
    for await (const event of session) events.push(event);
    await sender;
    assert.ok(transport.wasDisconnected(), "the test did not inject a transport drop");
    assert.ok(transport.socketCount() >= 2, "the SDK did not reconnect");
    assert.equal(events.at(-1)?.type, "session_complete");
    assert.equal(events.at(-1)?.data.status, "complete");
    assert.ok(events.some((event) => event.type === "source_unit_final"));
    const sequences = events.flatMap((event) => Number.isInteger(event.data.event_sequence) ? [event.data.event_sequence] : []);
    assert.deepEqual(sequences, [...new Set(sequences)].sort((a, b) => a - b));
  } finally {
    await session?.close();
    await client.close();
  }
});
