import { readFile } from "node:fs/promises";
import WebSocket from "ws";
import {
  SmartVoiceClient as CoreSmartVoiceClient,
  type SmartVoiceClientOptions,
  type WebSocketLike,
} from "./index.js";

/** Node.js client with filesystem audio paths and the ws WebSocket transport. */
export class SmartVoiceClient extends CoreSmartVoiceClient {
  constructor(baseUrl = "http://127.0.0.1:8000", options: SmartVoiceClientOptions = {}) {
    super(baseUrl, {
      ...options,
      webSocketFactory: options.webSocketFactory ?? ((url) => new WebSocket(url, { headers: options.webSocketHeaders }) as unknown as WebSocketLike),
      readAudioFile: options.readAudioFile ?? (async (path) => new Uint8Array(await readFile(path))),
    });
  }
}

export {
  APIError,
  ServerError,
  ServiceCapabilities,
  SmartVoiceError,
  StreamProtocolError,
  StreamRecoveryError,
  StreamingCapabilities,
  StreamingSession,
  StreamingUnavailableError,
  SubtitleState,
  TaskModelCapability,
} from "./index.js";
export type {
  AudioChunk,
  AudioFormat,
  AudioInput,
  JsonRecord,
  ResponseFormat,
  SmartVoiceClientOptions,
  SpeechResult,
  StartStreamOptions,
  StreamEvent,
  StreamEventData,
  StreamMode,
  SubtitleUnit,
  SynthesizeOptions,
  Task,
  TranscribeOptions,
  TranscriptionResult,
  WebSocketLike,
} from "./index.js";
export {
  FRAME_BYTES,
  MAX_FRAME_BYTES,
  SAMPLE_RATE,
  STREAM_PROTOCOL,
} from "./index.js";

