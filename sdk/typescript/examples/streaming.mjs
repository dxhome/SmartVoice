import { readFile, writeFile } from "node:fs/promises";
import { SmartVoiceClient } from "../dist/node.js";

const input = process.argv[2];
if (!input) throw new Error("Usage: node streaming.mjs <16k-mono-pcm16.pcm> [base-url] [mode]");
const baseUrl = process.argv[3] ?? "http://127.0.0.1:8000";
const mode = process.argv[4] ?? "transcription";
const sourceLanguage = "zh";
const targetLanguage = mode === "transcription" ? undefined : "en";
const pcm16 = new Uint8Array(await readFile(input));
if (pcm16.byteLength % 2) throw new Error("PCM16 input must have an even byte length");

const client = new SmartVoiceClient(baseUrl);
let session;
try {
  const capabilities = await client.streamingCapabilities();
  const chain = capabilities.chain(mode, sourceLanguage, targetLanguage ?? null);
  if (!chain?.available) throw new Error(`Streaming chain is unavailable: ${chain?.reason ?? "not reported"}`);

  session = await client.startStream({ mode, sourceLanguage, targetLanguage, resumable: true });
  const sender = (async () => {
    await session.sendAudio(pcm16, { realtime: true });
    await session.finish();
  })();
  try {
    for await (const event of session) {
      if (event.audio) {
        const output = `smartvoice-audio-${String(event.audio.sequence).padStart(4, "0")}.wav`;
        await writeFile(output, event.audio.audioBytes);
        console.log(`Audio segment ${event.audio.sequence}: ${output}`);
      } else {
        console.log(JSON.stringify(event.data));
      }
    }
    await sender;
  } finally {
    if (session) await session.close();
  }
} finally {
  await client.close();
}
