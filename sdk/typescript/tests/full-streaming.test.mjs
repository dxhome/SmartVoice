import assert from "node:assert/strict";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname } from "node:path";
import test from "node:test";
import WebSocket from "ws";
import { SmartVoiceClient } from "../dist/node.js";

const enabled = process.env.SMARTVOICE_TS_FULL === "1";
const baseUrl = process.env.SMARTVOICE_TS_BASE_URL;
const wavPath = process.env.SMARTVOICE_TS_WAV;
const reportPath = process.env.SMARTVOICE_TS_REPORT;

function wavPcm(bytes) {
  if (bytes.toString("ascii", 0, 4) !== "RIFF" || bytes.toString("ascii", 8, 12) !== "WAVE") throw new Error("Expected WAV fixture");
  let format;
  let pcm;
  for (let offset = 12; offset + 8 <= bytes.length;) {
    const name = bytes.toString("ascii", offset, offset + 4);
    const size = bytes.readUInt32LE(offset + 4);
    const start = offset + 8;
    if (name === "fmt ") format = [bytes.readUInt16LE(start), bytes.readUInt16LE(start + 2), bytes.readUInt32LE(start + 4), bytes.readUInt16LE(start + 14)];
    if (name === "data") pcm = bytes.subarray(start, start + size);
    offset = start + size + (size % 2);
  }
  assert.deepEqual(format, [1, 1, 16_000, 16]);
  assert.ok(pcm?.length);
  return new Uint8Array(pcm);
}

function instrumentedWebSocketFactory(targetSeconds) {
  const targets = targetSeconds.map((seconds) => seconds * 16_000);
  const fired = new Set();
  let connections = 0;
  let successfulResumes = 0;
  const factory = (url) => {
    connections += 1;
    const socket = new WebSocket(url);
    const listeners = new Map();
    let frameStart;
    let shouldDrop = false;
    const wrapped = {
      get readyState() { return socket.readyState; },
      send(data, callback) {
        if (typeof data === "string") {
          try {
            const message = JSON.parse(data);
            if (message.type === "audio_frame") {
              frameStart = message.start_sample;
              const target = targets.findIndex((sample, index) => !fired.has(index) && frameStart >= sample);
              shouldDrop = target >= 0;
              if (shouldDrop) fired.add(target);
            }
          } catch { /* Let SDK protocol validation handle malformed JSON. */ }
        }
        socket.send(data, (error) => {
          callback?.(error);
          if (!error && shouldDrop && typeof data !== "string") {
            shouldDrop = false;
            setImmediate(() => socket.terminate());
          }
        });
      },
      close(code, reason) { socket.close(code, reason); },
      on(event, listener) {
        const proxy = (...args) => {
          if (event === "message" && !args[1]) {
            try {
              if (JSON.parse(args[0].toString()).type === "session_resumed") successfulResumes += 1;
            } catch { /* SDK validates server messages. */ }
          }
          listener(...args);
        };
        listeners.set(listener, proxy);
        socket.on(event, proxy);
        return wrapped;
      },
      off(event, listener) {
        const proxy = listeners.get(listener);
        if (proxy) socket.off(event, proxy);
        return wrapped;
      },
    };
    return wrapped;
  };
  return {
    factory,
    connections: () => connections,
    drops: () => fired.size,
    successfulResumes: () => successfulResumes,
  };
}

test("JavaScript SDK streams one hour of real-time audio and recovers twice", {
  skip: !enabled || !baseUrl || !wavPath || !reportPath,
  timeout: 4_250_000,
}, async () => {
  const pcm = wavPcm(await readFile(wavPath));
  const transport = instrumentedWebSocketFactory([10, 1800]);
  const client = new SmartVoiceClient(baseUrl, {
    requestTimeoutMs: 120_000,
    resumeAttempts: 5,
    resumeBackoffMs: 200,
    webSocketFactory: transport.factory,
  });
  const started = performance.now();
  const audioStarted = performance.now();
  const targetSamples = 3600 * 16_000;
  let samplesSent = 0;
  let eventsSeen = 0;
  let sourceFinals = 0;
  let lastSourceEndSample = 0;
  let maxPendingInputFrames = 0;
  let maxRetainedAudioEventFingerprints = 0;
  let peakClientRssBytes = process.memoryUsage.rss();
  let lastRssSample = started;
  let terminal;
  let session;
  let failure;
  try {
    const capabilities = await client.streamingCapabilities();
    const chain = capabilities.chain("transcription", "zh");
    assert.ok(chain?.available, JSON.stringify(chain));
    session = await client.startStream({ mode: "transcription", sourceLanguage: "zh", resumable: true });
    const recoveryWindow = Number(session.ready?.data.recovery?.input_history_frames);
    assert.ok(Number.isInteger(recoveryWindow) && recoveryWindow > 0);

    const sender = (async () => {
      let cursor = 0;
      while (samplesSent < targetSamples) {
        const sampleCount = Math.min(16_000, targetSamples - samplesSent);
        const byteCount = sampleCount * 2;
        const frame = new Uint8Array(byteCount);
        for (let i = 0; i < byteCount;) {
          const count = Math.min(byteCount - i, pcm.length - cursor);
          frame.set(pcm.subarray(cursor, cursor + count), i);
          i += count;
          cursor = (cursor + count) % pcm.length;
        }
        await session.sendAudioFrame(frame);
        samplesSent += sampleCount;
        maxPendingInputFrames = Math.max(maxPendingInputFrames, session.pendingInputFrames);
        const deadline = audioStarted + samplesSent / 16_000 * 1_000;
        const wait = deadline - performance.now();
        if (wait > 0) await new Promise((resolve) => setTimeout(resolve, wait));
      }
      await session.finish();
    })();

    const receiver = (async () => {
      for await (const event of session) {
        eventsSeen += 1;
        if (event.type === "source_final" || event.type === "source_unit_final") sourceFinals += 1;
        if (event.type === "source_final" || event.type === "utterance_complete") {
          lastSourceEndSample = Math.max(lastSourceEndSample, Number(event.data.end_sample ?? 0));
        }
        maxRetainedAudioEventFingerprints = Math.max(maxRetainedAudioEventFingerprints, session.retainedAudioEvents);
        if (event.terminal) terminal = { type: event.type, status: event.data.status };
        if (performance.now() - lastRssSample >= 30_000) {
          peakClientRssBytes = Math.max(peakClientRssBytes, process.memoryUsage.rss());
          lastRssSample = performance.now();
        }
      }
    })();
    await Promise.all([sender, receiver]);
    peakClientRssBytes = Math.max(peakClientRssBytes, process.memoryUsage.rss());

    assert.equal(samplesSent, targetSamples);
    assert.equal(terminal?.type, "session_complete", JSON.stringify(terminal));
    assert.equal(terminal?.status, "complete");
    assert.ok(sourceFinals > 0);
    assert.equal(lastSourceEndSample, targetSamples);
    assert.equal(transport.drops(), 2);
    assert.ok(transport.connections() >= 3, `expected two resumptions, got ${transport.connections()} connections`);
    assert.equal(transport.successfulResumes(), 2);
    assert.ok(maxPendingInputFrames <= recoveryWindow);
    assert.ok(maxRetainedAudioEventFingerprints <= 128);
  } catch (error) {
    failure = `${error?.name ?? "Error"}: ${String(error?.message ?? error).slice(0, 1500)}`;
    throw error;
  } finally {
    await session?.close();
    await client.close();
    const report = {
      schema: "smartvoice.javascript-sdk.full-hour.v1",
      mode: "transcription",
      source_language: "zh",
      audio_seconds_sent: samplesSent / 16_000,
      wall_seconds: (performance.now() - started) / 1_000,
      events_seen: eventsSeen,
      source_finals: sourceFinals,
      last_source_end_sample: lastSourceEndSample,
      requested_disconnect_audio_seconds: [10, 1800],
      successful_resumes: transport.successfulResumes(),
      websocket_connections: transport.connections(),
      max_pending_input_frames: maxPendingInputFrames,
      input_replay_window_frames: session?.ready?.data.recovery?.input_history_frames ?? null,
      max_retained_audio_event_fingerprints: maxRetainedAudioEventFingerprints,
      client_peak_sampled_rss_bytes: peakClientRssBytes,
      terminal: terminal ?? null,
      failure: failure ?? null,
      scope: "One hour of real-time pinned PCM through the public JavaScript SDK and live local service, with two injected WebSocket interruptions. Client RSS is sampled in this Node.js process only.",
    };
    await mkdir(dirname(reportPath), { recursive: true });
    await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`);
    console.log(`JavaScript SDK full streaming report: ${reportPath}`);
  }
});
