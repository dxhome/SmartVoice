import { writeFile } from "node:fs/promises";
import { SmartVoiceClient } from "../dist/node.js";

const input = process.argv[2];
if (!input) throw new Error("Usage: node stt-tts.mjs <audio-file> [base-url]");

const client = new SmartVoiceClient(process.argv[3] ?? "http://127.0.0.1:8000");
try {
  const capabilities = await client.capabilities();
  console.log("STT languages:", await client.languages("transcription", { model: "smartvoice-auto" }));
  console.log("TTS languages:", await client.languages("speech", { model: "smartvoice-auto" }));
  console.log("STT models:", capabilities.modelsFor("transcription").map((item) => item.model));

  const transcription = await client.transcribe(input, {
    language: "auto",
    responseFormat: "verbose_json",
    timestamps: true,
  });
  console.log(`Transcribed with ${transcription.model ?? "unknown model"}: ${transcription.text}`);

  const speech = await client.synthesize(transcription.text, {
    language: transcription.language ?? "auto",
    responseFormat: "wav",
  });
  const output = "smartvoice-output.wav";
  await writeFile(output, speech.audio);
  console.log(`Synthesized with ${speech.model ?? "unknown model"}: ${output}`);
} finally {
  await client.close();
}
