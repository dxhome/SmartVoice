import { SAMPLE_RATE, SmartVoiceError } from "./index.js";

/** Decode a browser-supported audio file and convert it to SmartVoice's 16 kHz mono PCM16 stream format. */
export async function decodeAudioToPCM16(input: Blob | ArrayBuffer | ArrayBufferView): Promise<Uint8Array> {
  if (!globalThis.AudioContext || !globalThis.OfflineAudioContext) {
    throw new SmartVoiceError("Audio decoding is unavailable; use a browser with Web Audio support");
  }
  const bytes = input instanceof Blob
    ? await input.arrayBuffer()
    : input instanceof ArrayBuffer
      ? input.slice(0)
      : input.buffer.slice(input.byteOffset, input.byteOffset + input.byteLength) as ArrayBuffer;
  const decoder = new globalThis.AudioContext();
  try {
    const decoded = await decoder.decodeAudioData(bytes);
    const mono = decoder.createBuffer(1, decoded.length, decoded.sampleRate);
    const monoSamples = mono.getChannelData(0);
    for (let channel = 0; channel < decoded.numberOfChannels; channel += 1) {
      const source = decoded.getChannelData(channel);
      for (let frame = 0; frame < decoded.length; frame += 1) monoSamples[frame] += source[frame] / decoded.numberOfChannels;
    }

    const outputFrames = Math.max(1, Math.ceil(decoded.duration * SAMPLE_RATE));
    const renderer = new globalThis.OfflineAudioContext(1, outputFrames, SAMPLE_RATE);
    const source = renderer.createBufferSource();
    source.buffer = mono;
    source.connect(renderer.destination);
    source.start();
    const rendered = await renderer.startRendering();
    const samples = rendered.getChannelData(0);
    const pcm = new Uint8Array(samples.length * 2);
    const view = new DataView(pcm.buffer);
    for (let index = 0; index < samples.length; index += 1) {
      const sample = Math.max(-1, Math.min(1, samples[index]));
      view.setInt16(index * 2, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
    }
    return pcm;
  } catch (error) {
    throw new SmartVoiceError("Could not decode audio for SmartVoice streaming", { cause: error });
  } finally {
    await decoder.close();
  }
}
