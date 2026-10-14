export const STREAM_PROTOCOL = "smartvoice.stream.v1";
export const SAMPLE_RATE = 16_000;
export const FRAME_BYTES = 640;
export const MAX_FRAME_BYTES = 32_000;

export type Task = "transcription" | "speech";
export type StreamMode = "transcription" | "translated_subtitles" | "spoken_interpretation";
export type ResponseFormat = "json" | "text" | "verbose_json";
export type AudioFormat = "wav" | "mp3";
export type AudioInput = Uint8Array | ArrayBuffer | Blob | string;
export type JsonRecord = Record<string, unknown>;

export class SmartVoiceError extends Error {
  override name = "SmartVoiceError";
}

export class APIError extends SmartVoiceError {
  override name = "APIError";
  constructor(
    readonly statusCode: number,
    readonly code: string,
    message: string,
    readonly requestId?: string,
  ) {
    super(message);
  }
}

export class StreamProtocolError extends SmartVoiceError {
  override name = "StreamProtocolError";
}

export class StreamRecoveryError extends SmartVoiceError {
  override name = "StreamRecoveryError";
}

export class StreamingUnavailableError extends SmartVoiceError {
  override name = "StreamingUnavailableError";
  constructor(readonly chain: StreamingChain) {
    const reason = chain.reason ?? "model_unavailable";
    const missing = chain.missing_models?.length ? ` (missing models: ${chain.missing_models.join(", ")})` : "";
    super(`Streaming chain is unavailable: ${reason}${missing}`);
  }
}

export class ServerError extends SmartVoiceError {
  override name = "ServerError";
  constructor(readonly code: string, message: string, readonly event: StreamEventData) {
    super(message);
  }
}

export interface TaskModelCapability {
  task: Task;
  model: string;
  languages: string[];
  streaming: boolean;
  details: JsonRecord;
}

export interface LanguageRoute {
  installed?: boolean;
  [key: string]: unknown;
}

export interface ServiceCapabilitiesData {
  tasks?: Array<Record<string, unknown>>;
  router?: { tasks?: Partial<Record<Task, Record<string, LanguageRoute[]>>> };
  [key: string]: unknown;
}

export class ServiceCapabilities {
  readonly tasks: TaskModelCapability[];
  constructor(readonly raw: ServiceCapabilitiesData) {
    this.tasks = (Array.isArray(raw.tasks) ? raw.tasks : [])
      .filter((row) => row && (row.task === "transcription" || row.task === "speech"))
      .map((row) => ({
        task: row.task as Task,
        model: String(row.model ?? ""),
        languages: Array.isArray(row.languages) ? row.languages.filter((v): v is string => typeof v === "string") : [],
        streaming: Boolean(row.streaming),
        details: { ...row },
      }));
  }

  modelsFor(task: Task): TaskModelCapability[] {
    validateTask(task);
    return this.tasks.filter((item) => item.task === task);
  }

  languagesFor(task: Task, options: { model?: string; includeAuto?: boolean } = {}): string[] {
    validateTask(task);
    let languages: string[];
    if (options.model === "smartvoice-auto") {
      const routes = this.raw.router?.tasks?.[task] ?? {};
      languages = Object.entries(routes)
        .filter(([, candidates]) => Array.isArray(candidates) && candidates.some((candidate) => candidate?.installed === true))
        .map(([language]) => language);
    } else {
      const models = this.modelsFor(task).filter((item) => !options.model || item.model === options.model);
      languages = models.flatMap((item) => item.languages);
    }
    if (options.includeAuto) languages.push("auto");
    return [...new Set(languages)].sort();
  }

  supportsLanguage(task: Task, language: string, options: { model?: string } = {}): boolean {
    return this.languagesFor(task, options).includes(language);
  }
}

export interface StreamingChain {
  mode: StreamMode;
  source_language: string;
  target_language: string | null;
  available?: boolean;
  reason?: string;
  missing_models?: string[];
  [key: string]: unknown;
}

export class StreamingCapabilities {
  readonly chains: StreamingChain[];
  constructor(readonly raw: JsonRecord) {
    this.chains = (Array.isArray(raw.chains) ? raw.chains : [])
      .filter((row): row is StreamingChain => !!row && typeof row === "object" && !Array.isArray(row));
  }

  chain(mode: StreamMode, sourceLanguage: string, targetLanguage: string | null = null): StreamingChain | undefined {
    return this.chains.find((row) => row.mode === mode && row.source_language === sourceLanguage
      && (row.target_language ?? null) === targetLanguage);
  }

  sourceLanguages(mode: StreamMode, availableOnly = false): string[] {
    return [...new Set(this.chains.filter((row) => row.mode === mode && (!availableOnly || row.available === true))
      .map((row) => row.source_language))].sort();
  }

  languagePairs(mode: StreamMode, availableOnly = false): Array<[string, string | null]> {
    const pairs = this.chains.filter((row) => row.mode === mode && (!availableOnly || row.available === true))
      .map((row) => [row.source_language, row.target_language ?? null] as [string, string | null]);
    const seen = new Set<string>();
    return pairs.filter((pair) => {
      const key = JSON.stringify(pair);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    }).sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b)));
  }
}

export interface TranscriptionResult {
  text: string;
  language: string | null;
  model: string | null;
  requestedModel: string;
  requestedLanguage: string;
  responseFormat: ResponseFormat;
  raw: JsonRecord;
  headers: Headers;
  segments: Array<Record<string, unknown>>;
}

export interface SpeechResult {
  audio: Uint8Array;
  format: AudioFormat;
  contentType: string;
  model: string | null;
  requestedModel: string;
  durationSeconds: number | null;
  sampleRate: number | null;
  segments: number | null;
  headers: Headers;
}

export interface AudioChunk {
  sequence: number;
  eventSequence: number;
  audioBytes: Uint8Array;
  descriptor: JsonRecord;
}

export interface StreamEventData extends JsonRecord {
  type: string;
  event_sequence?: number;
}

export interface SubtitleUnit {
  unitId: string;
  text: string;
  revision: number;
  final: boolean;
  data: StreamEventData;
}

/** Maintains the latest subtitle revision for each source or target unit. */
export class SubtitleState {
  private readonly groups = {
    source: { units: new Map<string, SubtitleUnit>(), order: [] as string[] },
    target: { units: new Map<string, SubtitleUnit>(), order: [] as string[] },
  };

  constructor(readonly maxUnits = 1_000) {
    if (!Number.isSafeInteger(maxUnits) || maxUnits < 1) throw new RangeError("maxUnits must be a positive safe integer");
  }

  apply(event: StreamEvent | StreamEventData | JsonRecord): boolean {
    const data = "data" in event && event.data && typeof event.data === "object"
      ? event.data as StreamEventData : event as StreamEventData;
    const type = String(data.type ?? "");
    const kind = type.startsWith("source_unit_") ? "source" : type.startsWith("target_") ? "target" : undefined;
    if (!kind || data.unit_id === undefined || !Number.isInteger(data.revision) || Number(data.revision) < 0) return false;
    const group = this.groups[kind];
    const unitId = String(data.unit_id);
    const revision = Number(data.revision);
    const current = group.units.get(unitId);
    if (current && (current.final || revision <= current.revision)) return false;
    if (data.retracted === true) {
      if (!current) return false;
      group.units.delete(unitId);
      group.order.splice(group.order.indexOf(unitId), 1);
      return true;
    }
    const final = type.endsWith("_final") || data.final === true || data.committed === true;
    if (!current) group.order.push(unitId);
    group.units.set(unitId, { unitId, text: String(data.text ?? ""), revision, final, data: { ...data } });
    while (group.order.length > this.maxUnits) {
      const oldest = group.order.shift()!;
      group.units.delete(oldest);
    }
    return true;
  }

  units(kind: "source" | "target" = "source"): SubtitleUnit[] {
    const group = this.groups[kind];
    return group.order.flatMap((id) => {
      const unit = group.units.get(id);
      return unit ? [unit] : [];
    });
  }

  text(kind: "source" | "target" = "source"): string {
    return this.units(kind).map((unit) => unit.text).join("");
  }

  clear(): void {
    for (const group of Object.values(this.groups)) {
      group.units.clear();
      group.order.length = 0;
    }
  }
}

export interface StreamEvent {
  type: string;
  data: StreamEventData;
  audio?: AudioChunk;
  readonly terminal: boolean;
}

export interface TranscribeOptions {
  model?: string;
  language?: string | null;
  responseFormat?: ResponseFormat;
  timestamps?: boolean;
  filename?: string;
  contentType?: string;
}

export interface SynthesizeOptions {
  model?: string;
  language?: string | null;
  voice?: string;
  speed?: number;
  responseFormat?: AudioFormat;
}

export interface StartStreamOptions {
  mode: StreamMode;
  sourceLanguage: string;
  targetLanguage?: string;
  outputConsumption?: "delivery" | "playback";
  ackWindow?: number;
  resumable?: boolean;
  glossary?: Array<Record<string, string>>;
  context?: JsonRecord;
  models?: Record<string, string>;
  includeSourceText?: boolean;
}

export interface SmartVoiceClientOptions {
  openTimeoutMs?: number;
  requestTimeoutMs?: number;
  resumeAttempts?: number;
  resumeBackoffMs?: number;
  fetch?: typeof globalThis.fetch;
  /** Default headers for REST requests (for example, authorization headers). */
  headers?: HeadersInit;
  /** Node transport headers for WebSocket connections; browsers cannot set these. */
  webSocketHeaders?: Record<string, string>;
  /** Browser adapter: stop queueing when buffered frames exceed this byte count. */
  browserSendBufferHighWaterMarkBytes?: number;
  /** Browser adapter: resume queueing after the buffer drains to this byte count. */
  browserSendBufferLowWaterMarkBytes?: number;
  /** Browser adapter: fail a send if the WebSocket buffer does not drain in time. */
  browserSendBufferWaitTimeoutMs?: number;
  webSocketFactory?: (url: string) => WebSocketLike;
  /** Node adapter hook; browser clients should pass Blob/File or bytes instead of paths. */
  readAudioFile?: (path: string) => Promise<Uint8Array>;
}

export interface WebSocketLike {
  readonly readyState: number;
  send(data: string | Uint8Array, callback?: (error?: Error) => void): void;
  close(code?: number, reason?: string): void;
  on(event: string, listener: (...args: unknown[]) => void): this;
  off(event: string, listener: (...args: unknown[]) => void): this;
}

type WireMessage = { binary: boolean; data: string | Uint8Array };
const OPEN = 1;
const DEFAULT_REPLAY_WINDOW = 128;
const MAX_AUDIO_HISTORY = 128;
const MAX_SOCKET_QUEUE_MESSAGES = 128;
const MAX_SOCKET_QUEUE_BYTES = 8 * 1024 * 1024;

class SocketClosedError extends Error {}

class SocketChannel {
  private queue: WireMessage[] = [];
  private queuedBytes = 0;
  private waiters: Array<{ resolve: (message: WireMessage) => void; reject: (error: Error) => void }> = [];
  private failure?: Error;

  constructor(readonly socket: WebSocketLike) {
    socket.on("message", (raw: unknown, isBinary: unknown) => {
      const binary = Boolean(isBinary);
      const data = toWireData(raw, binary);
      if (this.waiters.length) {
        this.waiters.shift()!.resolve({ binary, data });
        return;
      }
      this.queuedBytes += wireSize(data);
      this.queue.push({ binary, data });
      if (this.queue.length > MAX_SOCKET_QUEUE_MESSAGES || this.queuedBytes > MAX_SOCKET_QUEUE_BYTES) {
        this.fail(new SocketClosedError("Streaming receive buffer exceeded its bound"));
        socket.close(1013, "client receive buffer full");
      }
    });
    socket.on("close", () => this.fail(new SocketClosedError("Streaming WebSocket closed")));
    socket.on("error", (error: unknown) => this.fail(error instanceof Error ? error : new SocketClosedError("Streaming WebSocket failed")));
  }

  async send(data: string | Uint8Array): Promise<void> {
    if (this.socket.readyState !== OPEN) throw new SocketClosedError("Streaming WebSocket is closed");
    await new Promise<void>((resolve, reject) => {
      this.socket.send(data, (error) => {
        if (!error) { resolve(); return; }
        reject(this.socket.readyState !== OPEN
          ? new SocketClosedError("Streaming WebSocket closed while sending")
          : error);
      });
    });
  }

  async next(timeoutMs?: number): Promise<WireMessage> {
    const message = this.queue.shift();
    if (message) {
      this.queuedBytes -= wireSize(message.data);
      return message;
    }
    if (this.failure) throw this.failure;
    let waiter: { resolve: (message: WireMessage) => void; reject: (error: Error) => void };
    const promise = new Promise<WireMessage>((resolve, reject) => {
      waiter = { resolve, reject };
      this.waiters.push(waiter);
    });
    if (timeoutMs === undefined) return promise;
    let timer: ReturnType<typeof setTimeout>;
    return Promise.race([
      promise,
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => {
          this.waiters = this.waiters.filter((entry) => entry !== waiter!);
          reject(new Error("Timed out waiting for streaming event"));
        }, timeoutMs);
      }),
    ]).finally(() => clearTimeout(timer!));
  }

  private fail(error: Error): void {
    if (this.failure) return;
    this.failure = error;
    for (const waiter of this.waiters.splice(0)) waiter.reject(error);
  }
}

class Mutex {
  private tail: Promise<void> = Promise.resolve();
  async run<T>(operation: () => Promise<T>): Promise<T> {
    const previous = this.tail;
    let release!: () => void;
    this.tail = new Promise<void>((resolve) => { release = resolve; });
    await previous;
    try { return await operation(); } finally { release(); }
  }
}

function toWireData(raw: unknown, binary: boolean): string | Uint8Array {
  if (typeof raw === "string") return raw;
  let bytes: Uint8Array;
  if (raw instanceof ArrayBuffer) bytes = new Uint8Array(raw);
  else if (ArrayBuffer.isView(raw)) bytes = new Uint8Array(raw.buffer, raw.byteOffset, raw.byteLength);
  else if (Array.isArray(raw)) bytes = concatBytes(raw.map((item) => toWireData(item, true) as Uint8Array));
  else if (raw && typeof raw === "object" && "buffer" in raw) {
    const value = raw as { buffer: ArrayBuffer; byteOffset?: number; byteLength?: number };
    bytes = new Uint8Array(value.buffer, value.byteOffset ?? 0, value.byteLength);
  } else {
    if (!binary) return String(raw);
    throw new StreamProtocolError("Unsupported WebSocket frame type");
  }
  return binary ? bytes : new TextDecoder().decode(bytes);
}

function concatBytes(chunks: Uint8Array[]): Uint8Array {
  const size = chunks.reduce((sum, chunk) => sum + chunk.byteLength, 0);
  const output = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { output.set(chunk, offset); offset += chunk.byteLength; }
  return output;
}

function wireSize(data: string | Uint8Array): number {
  return typeof data === "string" ? new TextEncoder().encode(data).byteLength : data.byteLength;
}

function httpBase(baseUrl: string): string {
  const parsed = new URL(baseUrl);
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") throw new TypeError("baseUrl must use http:// or https://");
  parsed.search = "";
  parsed.hash = "";
  parsed.pathname = parsed.pathname.replace(/\/+$/, "");
  return parsed.toString().replace(/\/$/, "");
}

function wsUrl(baseUrl: string): string {
  const url = new URL(baseUrl);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = `${url.pathname.replace(/\/+$/, "")}/v1/audio/stream`;
  return url.toString();
}

async function parseJson(response: Response): Promise<JsonRecord> {
  let value: unknown;
  try { value = await response.json(); } catch { throw new StreamProtocolError("SmartVoice returned invalid JSON"); }
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new StreamProtocolError("SmartVoice response must be a JSON object");
  return value as JsonRecord;
}

async function assertOk(response: Response): Promise<void> {
  if (response.ok) return;
  let body: JsonRecord = {};
  try { body = await response.json() as JsonRecord; } catch { /* Human-readable HTTP fallback below. */ }
  const error = body.error && typeof body.error === "object" ? body.error as JsonRecord : undefined;
  const message = typeof error?.message === "string" ? error.message
    : typeof body.detail === "string" ? body.detail
      : typeof body.message === "string" ? body.message
        : `SmartVoice request failed (HTTP ${response.status})`;
  const requestId = response.headers.get("x-request-id")
    ?? (typeof error?.request_id === "string" ? error.request_id : undefined);
  throw new APIError(response.status, String(error?.code ?? "request_failed"), message, requestId);
}

function validateTask(task: string): asserts task is Task {
  if (task !== "transcription" && task !== "speech") throw new TypeError("task must be 'transcription' (STT) or 'speech' (TTS)");
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    const row = value as Record<string, unknown>;
    return `{${Object.keys(row).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(row[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function parseFloatHeader(headers: Headers, name: string): number | null {
  const value = headers.get(name);
  if (value === null || !Number.isFinite(Number(value))) return null;
  return Number(value);
}

async function sha256Hex(bytes: Uint8Array): Promise<string> {
  if (!globalThis.crypto?.subtle) throw new SmartVoiceError("This runtime must provide Web Crypto SHA-256 for resumable streaming");
  const copy = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
  const digest = await globalThis.crypto.subtle.digest("SHA-256", copy);
  return [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, "0")).join("");
}

export class SmartVoiceClient {
  readonly baseUrl: string;
  private readonly fetchImpl: typeof globalThis.fetch;
  private readonly webSocketFactory: (url: string) => WebSocketLike;
  private readonly readAudioFile?: (path: string) => Promise<Uint8Array>;
  private readonly headers: Headers;
  readonly webSocketHeaders: Record<string, string>;
  readonly openTimeoutMs: number;
  readonly requestTimeoutMs: number;
  readonly resumeAttempts: number;
  readonly resumeBackoffMs: number;

  constructor(baseUrl = "http://127.0.0.1:8000", options: SmartVoiceClientOptions = {}) {
    this.baseUrl = httpBase(baseUrl);
    this.fetchImpl = options.fetch ?? globalThis.fetch;
    if (!this.fetchImpl) throw new Error("A Fetch API implementation is required");
    this.webSocketFactory = options.webSocketFactory ?? ((url) => {
      if (!globalThis.WebSocket) throw new SmartVoiceError("No WebSocket implementation is available in this runtime");
      return new globalThis.WebSocket(url) as unknown as WebSocketLike;
    });
    this.readAudioFile = options.readAudioFile;
    this.headers = new Headers(options.headers);
    this.webSocketHeaders = { ...(options.webSocketHeaders ?? {}) };
    this.openTimeoutMs = options.openTimeoutMs ?? 10_000;
    this.requestTimeoutMs = options.requestTimeoutMs ?? 120_000;
    this.resumeAttempts = options.resumeAttempts ?? 3;
    this.resumeBackoffMs = options.resumeBackoffMs ?? 250;
    if (!Number.isInteger(this.resumeAttempts) || this.resumeAttempts < 1) throw new RangeError("resumeAttempts must be a positive integer");
    if (!Number.isFinite(this.resumeBackoffMs) || this.resumeBackoffMs < 0) throw new RangeError("resumeBackoffMs must be nonnegative");
  }

  async capabilities(options: { task?: Task } = {}): Promise<ServiceCapabilities> {
    if (options.task !== undefined) validateTask(options.task);
    const url = new URL(`${this.baseUrl}/v1/capabilities`);
    if (options.task) url.searchParams.set("task", options.task);
    const response = await this.request(url.toString());
    await assertOk(response);
    return new ServiceCapabilities(await parseJson(response) as ServiceCapabilitiesData);
  }

  async languages(task: Task, options: { model?: string; includeAuto?: boolean } = {}): Promise<string[]> {
    return (await this.capabilities({ task })).languagesFor(task, options);
  }

  async streamingCapabilities(): Promise<StreamingCapabilities> {
    const response = await this.request(`${this.baseUrl}/v1/audio/stream/capabilities`);
    await assertOk(response);
    return new StreamingCapabilities(await parseJson(response));
  }

  async transcribe(audio: AudioInput, options: TranscribeOptions = {}): Promise<TranscriptionResult> {
    const model = options.model ?? "smartvoice-auto";
    const language = options.language || "auto";
    const responseFormat = options.responseFormat ?? "json";
    if (!["json", "text", "verbose_json"].includes(responseFormat)) throw new TypeError("responseFormat must be json, text, or verbose_json");
    if (options.timestamps !== undefined && typeof options.timestamps !== "boolean") throw new TypeError("timestamps must be a boolean");
    const { bytes, filename, contentType } = await this.audioBytes(audio, options.filename, options.contentType);
    const form = new FormData();
    form.append("model", model);
    form.append("language", language);
    form.append("response_format", responseFormat);
    form.append("timestamps", String(options.timestamps ?? false));
    const blobBuffer = new ArrayBuffer(bytes.byteLength);
    new Uint8Array(blobBuffer).set(bytes);
    form.append("file", new Blob([blobBuffer], { type: contentType }), filename);
    const response = await this.request(`${this.baseUrl}/v1/audio/transcriptions`, { method: "POST", body: form });
    await assertOk(response);
    const headers = response.headers;
    let raw: JsonRecord;
    let text: string;
    if (responseFormat === "text") {
      text = await response.text();
      raw = { text };
    } else {
      raw = await parseJson(response);
      text = String(raw.text ?? "");
    }
    const segments = Array.isArray(raw.segments) ? raw.segments.filter((item): item is Record<string, unknown> => !!item && typeof item === "object") : [];
    return {
      text,
      language: typeof raw.language === "string" ? raw.language : headers.get("x-resolved-language"),
      model: typeof raw.model === "string" ? raw.model : headers.get("x-model-id"),
      requestedModel: model,
      requestedLanguage: language,
      responseFormat,
      raw,
      headers,
      segments,
    };
  }

  async synthesize(text: string, options: SynthesizeOptions = {}): Promise<SpeechResult> {
    if (typeof text !== "string" || !text.trim()) throw new TypeError("text must contain non-whitespace characters");
    const speed = options.speed ?? 1;
    if (!Number.isFinite(speed) || speed < 0.5 || speed > 2) throw new RangeError("speed must be between 0.5 and 2.0");
    const format = options.responseFormat ?? "mp3";
    if (format !== "mp3" && format !== "wav") throw new TypeError("responseFormat must be mp3 or wav");
    const model = options.model ?? "smartvoice-auto";
    const response = await this.request(`${this.baseUrl}/v1/audio/speech`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        model, input: text, voice: options.voice ?? "default", language: options.language || "auto",
        speed, response_format: format,
      }),
    });
    await assertOk(response);
    const headers = response.headers;
    return {
      audio: new Uint8Array(await response.arrayBuffer()),
      format: (headers.get("x-audio-format") ?? format) as AudioFormat,
      contentType: headers.get("content-type") ?? "application/octet-stream",
      model: headers.get("x-model-id"),
      requestedModel: model,
      durationSeconds: parseFloatHeader(headers, "x-audio-duration"),
      sampleRate: parseFloatHeader(headers, "x-audio-sample-rate"),
      segments: parseFloatHeader(headers, "x-audio-segments"),
      headers,
    };
  }

  async startStream(options: StartStreamOptions): Promise<StreamingSession> {
    const target = options.targetLanguage ?? null;
    const config = makeStreamConfig(options);
    const caps = await this.streamingCapabilities();
    const chain = caps.chain(options.mode, options.sourceLanguage, target);
    if (!chain) {
      throw new TypeError(`Unsupported streaming mode/language combination: ${options.mode}, ${options.sourceLanguage} -> ${target ?? "none"}`);
    }
    if (chain.available === false) throw new StreamingUnavailableError(chain);
    const channel = await this.connectChannel();
    const session = new StreamingSession(channel, config, this, this.resumeAttempts, this.resumeBackoffMs, this.openTimeoutMs);
    try {
      await channel.send(JSON.stringify(config));
      const ready = await session.receiveRawEvent(this.openTimeoutMs);
      if (ready.type === "error") throw new ServerError(String(ready.data.code ?? "stream_error"), String(ready.data.message ?? "Stream setup failed"), ready.data);
      if (ready.type !== "session_ready") throw new StreamProtocolError(`Expected session_ready, received ${ready.type}`);
      if (options.resumable && (!ready.data.recovery || typeof ready.data.recovery !== "object")) {
        throw new StreamProtocolError("The service did not enable streaming recovery");
      }
      session.setReady(ready);
      await session.ack(ready.data);
      session.startHeartbeat(Number(ready.data.heartbeat_interval_seconds ?? 20));
      return session;
    } catch (error) {
      channel.socket.close();
      throw error;
    }
  }

  async close(): Promise<void> { /* HTTP uses the runtime Fetch implementation and owns no persistent handle. */ }

  async connectChannel(): Promise<SocketChannel> {
    const socket = this.webSocketFactory(wsUrl(this.baseUrl));
    await new Promise<void>((resolve, reject) => {
      const timeout = setTimeout(() => finish(new Error("Timed out connecting to SmartVoice streaming")), this.openTimeoutMs);
      const onOpen = () => finish();
      const onError = (error: unknown) => finish(error instanceof Error ? error : new Error("WebSocket connection failed"));
      const finish = (error?: Error) => {
        clearTimeout(timeout);
        socket.off("open", onOpen).off("error", onError);
        if (error) { socket.close(); reject(error); } else resolve();
      };
      socket.on("open", onOpen).on("error", onError);
    });
    return new SocketChannel(socket);
  }

  private async request(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.requestTimeoutMs);
    const headers = new Headers(this.headers);
    new Headers(init.headers).forEach((value, key) => headers.set(key, value));
    try { return await this.fetchImpl(input, { ...init, headers, signal: controller.signal }); }
    catch (error) {
      if (controller.signal.aborted) throw new SmartVoiceError(`Request timed out after ${this.requestTimeoutMs} ms`, { cause: error });
      throw error;
    } finally { clearTimeout(timer); }
  }

  private async audioBytes(audio: AudioInput, filename?: string, contentType?: string): Promise<{ bytes: Uint8Array; filename: string; contentType: string }> {
    if (typeof audio === "string") {
      if (!this.readAudioFile) throw new TypeError("File paths are only supported by the Node.js SDK; pass a Blob, File, or byte array in browsers");
      const bytes = await this.readAudioFile(audio);
      const pathName = audio.split(/[\\/]/).filter(Boolean).at(-1) ?? "audio.wav";
      return { bytes, filename: filename ?? pathName, contentType: contentType ?? "application/octet-stream" };
    }
    if (audio instanceof Blob) {
      return { bytes: new Uint8Array(await audio.arrayBuffer()), filename: filename ?? ("name" in audio ? String(audio.name) : "audio.wav"), contentType: contentType ?? (audio.type || "application/octet-stream") };
    }
    if (audio instanceof ArrayBuffer) return { bytes: new Uint8Array(audio), filename: filename ?? "audio.wav", contentType: contentType ?? "application/octet-stream" };
    if (audio instanceof Uint8Array) return { bytes: audio, filename: filename ?? "audio.wav", contentType: contentType ?? "application/octet-stream" };
    throw new TypeError("audio must be bytes, an ArrayBuffer, Blob/File, or a file path");
  }
}

function makeStreamConfig(options: StartStreamOptions): JsonRecord {
  const modes: StreamMode[] = ["transcription", "translated_subtitles", "spoken_interpretation"];
  if (!modes.includes(options.mode)) throw new TypeError(`mode must be one of ${modes.join(", ")}`);
  if (!options.sourceLanguage) throw new TypeError("sourceLanguage must be a non-empty language code");
  const outputConsumption = options.outputConsumption ?? "delivery";
  if (!["delivery", "playback"].includes(outputConsumption)) throw new TypeError("outputConsumption must be delivery or playback");
  if (outputConsumption === "playback" && options.mode !== "spoken_interpretation") throw new TypeError("playback acknowledgements are only supported for spoken_interpretation");
  const ackWindow = options.ackWindow ?? 16;
  if (!Number.isInteger(ackWindow) || ackWindow < 1 || ackWindow > 32) throw new RangeError("ackWindow must be an integer between 1 and 32");
  if (typeof (options.includeSourceText ?? true) !== "boolean") throw new TypeError("includeSourceText must be a boolean");
  const translated = options.mode !== "transcription";
  if (translated && (!options.targetLanguage || options.targetLanguage === options.sourceLanguage)) throw new TypeError("translated workflows require a different targetLanguage");
  if (!translated && options.targetLanguage !== undefined) throw new TypeError("transcription does not accept targetLanguage");
  const config: JsonRecord = {
    type: "configure", protocol: STREAM_PROTOCOL, mode: options.mode,
    source_language: options.sourceLanguage,
    audio: { encoding: "pcm_s16le", sample_rate: SAMPLE_RATE, channels: 1 },
    output_consumption: outputConsumption, ack_window: ackWindow,
    resumable: options.resumable ?? false, include_source_text: options.includeSourceText ?? true,
  };
  if (translated) config.target_language = options.targetLanguage;
  if (options.glossary !== undefined) config.glossary = options.glossary;
  if (options.context !== undefined) config.context = options.context;
  if (options.models !== undefined) config.models = options.models;
  return config;
}

function makeEvent(row: JsonRecord, audio?: AudioChunk): StreamEvent {
  const data = row as StreamEventData;
  return { type: String(row.type), data, audio, get terminal() { return this.type === "session_complete" || this.type === "error"; } };
}

export class StreamingSession implements AsyncIterable<StreamEvent> {
  private channel: SocketChannel;
  private recovery?: JsonRecord & { session_id: string; resume_token: string };
  private recoveryTask?: Promise<void>;
  private readonly sendMutex = new Mutex();
  private readonly pendingInput = new Map<number, { startSample: number; pcm: Uint8Array }>();
  private readonly audioSeen = new Map<number, { descriptor: string; hash: string }>();
  private readonly audioEventSequences = new Map<number, number>();
  private eventCursor = 0;
  private inputSequence = 0;
  private inputSamples = 0;
  private acceptedInputSequence = 0;
  private acceptedInputSamples = 0;
  private inputWindow = DEFAULT_REPLAY_WINDOW;
  private lastAudioSequence = 0;
  private lastStarted = 0;
  private lastPlayed = 0;
  private eventsActive = false;
  private closed = false;
  private terminalReceived = false;
  private heartbeat?: ReturnType<typeof setInterval>;
  private inputWaiters: Array<() => void> = [];
  ready?: StreamEvent;

  constructor(
    channel: SocketChannel,
    readonly config: JsonRecord,
    private readonly client: SmartVoiceClient,
    private readonly resumeAttempts: number,
    private readonly resumeBackoffMs: number,
    private readonly openTimeoutMs: number,
  ) { this.channel = channel; }

  get resumable(): boolean { return this.config.resumable === true; }
  get pendingInputFrames(): number { return this.pendingInput.size; }
  get retainedAudioEvents(): number { return this.audioSeen.size; }

  setReady(event: StreamEvent): void {
    this.ready = event;
    this.eventCursor = Number(event.data.event_sequence ?? 0);
    if (!this.resumable) return;
    const recovery = event.data.recovery as JsonRecord | undefined;
    const sessionId = event.data.session_id;
    if (!recovery || typeof recovery.resume_token !== "string" || typeof sessionId !== "string") {
      throw new StreamProtocolError("The service returned invalid streaming recovery metadata");
    }
    this.recovery = { ...recovery, session_id: sessionId, resume_token: recovery.resume_token };
    const limit = recovery.input_history_frames ?? DEFAULT_REPLAY_WINDOW;
    if (!Number.isInteger(limit) || Number(limit) < 1) throw new StreamProtocolError("Invalid input replay window");
    this.inputWindow = Number(limit);
  }

  startHeartbeat(intervalSeconds: number): void {
    const interval = Math.max(1_000, Number.isFinite(intervalSeconds) ? intervalSeconds * 1_000 : 20_000);
    this.heartbeat = setInterval(() => { void this.heartbeatControl().catch(() => {}); }, interval);
    this.heartbeat.unref?.();
  }

  async sendAudioFrame(pcm16: Uint8Array | ArrayBuffer): Promise<void> {
    if (this.closed) throw new SmartVoiceError("Streaming session is closed");
    const frame = pcm16 instanceof Uint8Array ? pcm16 : new Uint8Array(pcm16);
    if (!frame.byteLength || frame.byteLength % 2) throw new RangeError("PCM16 frame must be non-empty and even length");
    if (frame.byteLength > MAX_FRAME_BYTES) throw new RangeError(`PCM frame exceeds ${MAX_FRAME_BYTES} bytes`);
    if (!this.resumable) {
      try { await this.sendMutex.run(() => this.channel.send(frame)); }
      catch (error) { if (error instanceof SocketClosedError) throw new SmartVoiceError("WebSocket closed before audio could be sent"); throw error; }
      return;
    }
    while (this.pendingInput.size >= this.inputWindow) {
      if (!this.eventsActive) throw new StreamRecoveryError("Input replay window is full; consume session.events() while sending audio");
      await this.waitForInputWindow();
      if (this.closed) throw new SmartVoiceError("Streaming session is closed");
    }
    const sequence = this.inputSequence + 1;
    const startSample = this.inputSamples;
    const frameCopy = frame.slice();
    this.pendingInput.set(sequence, { startSample, pcm: frameCopy });
    this.inputSequence = sequence;
    this.inputSamples += frame.byteLength / 2;
    const channel = this.channel;
    try {
      await this.sendMutex.run(() => this.sendSequencedUnlocked(channel, sequence, startSample, frameCopy));
      await new Promise<void>((resolve) => setTimeout(resolve, 0));
    } catch (error) {
      if (!(error instanceof SocketClosedError)) throw error;
      await this.recover(channel);
    }
  }

  async sendAudio(pcm16: Uint8Array | ArrayBuffer, options: { frameBytes?: number; realtime?: boolean } = {}): Promise<void> {
    const bytes = pcm16 instanceof Uint8Array ? pcm16 : new Uint8Array(pcm16);
    if (!bytes.byteLength) return;
    const frameBytes = options.frameBytes ?? FRAME_BYTES;
    if (!Number.isInteger(frameBytes) || frameBytes < 2 || frameBytes > MAX_FRAME_BYTES || frameBytes % 2) {
      throw new RangeError("frameBytes must be even and between 2 and 32000");
    }
    const started = performance.now();
    let samples = 0;
    for (let offset = 0; offset < bytes.byteLength; offset += frameBytes) {
      const frame = bytes.subarray(offset, Math.min(offset + frameBytes, bytes.byteLength));
      await this.sendAudioFrame(frame);
      samples += frame.byteLength / 2;
      if (options.realtime ?? true) {
        const wait = started + samples / SAMPLE_RATE * 1_000 - performance.now();
        if (wait > 0) await new Promise((resolve) => setTimeout(resolve, wait));
      }
    }
  }

  /** Send asynchronously produced PCM16 chunks without buffering the full recording. */
  async sendAudioStream(chunks: AsyncIterable<Uint8Array | ArrayBuffer>, options: { frameBytes?: number; realtime?: boolean } = {}): Promise<void> {
    const frameBytes = options.frameBytes ?? FRAME_BYTES;
    if (!Number.isInteger(frameBytes) || frameBytes < 2 || frameBytes > MAX_FRAME_BYTES || frameBytes % 2) {
      throw new RangeError("frameBytes must be even and between 2 and 32000");
    }
    const realtime = options.realtime ?? true;
    const started = performance.now();
    let samples = 0;
    let pending = new Uint8Array(0);
    const send = async (frame: Uint8Array): Promise<void> => {
      await this.sendAudioFrame(frame);
      samples += frame.byteLength / 2;
      if (realtime) {
        const wait = started + samples / SAMPLE_RATE * 1_000 - performance.now();
        if (wait > 0) await new Promise((resolve) => setTimeout(resolve, wait));
      }
    };
    for await (const chunk of chunks) {
      const bytes = chunk instanceof Uint8Array ? chunk : new Uint8Array(chunk);
      if (!bytes.byteLength) continue;
      const combined = new Uint8Array(pending.byteLength + bytes.byteLength);
      combined.set(pending);
      combined.set(bytes, pending.byteLength);
      let offset = 0;
      while (combined.byteLength - offset >= frameBytes) {
        await send(combined.subarray(offset, offset + frameBytes));
        offset += frameBytes;
      }
      pending = combined.slice(offset);
    }
    if (pending.byteLength % 2) throw new RangeError("PCM16 stream ended with an incomplete sample");
    if (pending.byteLength) await send(pending);
  }

  async finish(): Promise<void> { await this.sendControl({ type: "finish" }); }
  async cancel(): Promise<void> { if (!this.closed) await this.sendControl({ type: "cancel" }); }
  async heartbeatControl(): Promise<void> { await this.sendControl({ type: "heartbeat" }); }

  async markAudioStarted(sequence: number): Promise<void> {
    if (sequence > this.lastAudioSequence) throw new RangeError("Cannot acknowledge playback for an audio chunk not received");
    if (sequence !== this.lastStarted + 1) throw new RangeError("Audio chunks must be started consecutively");
    if (sequence > this.lastPlayed + 1) throw new RangeError("Finish the previous audio chunk before starting another");
    await this.sendControl({ type: "audio_started", audio_sequence: sequence });
    this.lastStarted = sequence;
  }

  async markAudioPlayed(sequence: number): Promise<void> {
    if (sequence !== this.lastPlayed + 1 || sequence !== this.lastStarted) throw new RangeError("Audio chunks must be reported played in order after starting");
    this.lastPlayed = sequence;
    this.eventCursor = Math.max(this.eventCursor, this.audioEventSequences.get(sequence) ?? 0);
    await this.sendControl({ type: "audio_played", audio_sequence: sequence });
  }

  async *events(): AsyncGenerator<StreamEvent> {
    if (this.eventsActive) throw new SmartVoiceError("Only one events() consumer may read a streaming session");
    this.eventsActive = true;
    try {
      while (true) {
        let event: StreamEvent;
        try { event = await this.receiveRawEvent(undefined, this.channel); }
        catch (error) {
          if (!(error instanceof SocketClosedError) || !this.resumable || this.closed) {
            throw new SmartVoiceError("WebSocket closed before a terminal event", { cause: error });
          }
          await this.recover(this.channel);
          continue;
        }
        if (event.type === "input_ack") { this.ackInput(event.data); continue; }
        const sequence = event.data.event_sequence;
        if (typeof sequence === "number" && sequence <= this.eventCursor) {
          await this.ack(event.data);
          continue;
        }
        if (event.terminal) this.terminalReceived = true;
        yield event;
        await this.ack(event.data);
        if (event.terminal) {
          if (event.type === "error") throw new ServerError(String(event.data.code ?? "stream_error"), String(event.data.message ?? "Streaming failed"), event.data);
          return;
        }
      }
    } finally { this.eventsActive = false; }
  }

  [Symbol.asyncIterator](): AsyncIterator<StreamEvent> { return this.events(); }

  async close(options: { cancelIfIncomplete?: boolean } = {}): Promise<void> {
    if (this.closed) return;
    if ((options.cancelIfIncomplete ?? true) && !this.terminalReceived) {
      try { await this.sendControl({ type: "cancel" }, false); } catch { /* best-effort cleanup */ }
    }
    this.closed = true;
    if (this.heartbeat) clearInterval(this.heartbeat);
    this.channel.socket.close();
    this.wakeInputWaiters();
  }

  async receiveRawEvent(timeoutMs?: number, channel = this.channel): Promise<StreamEvent> {
    const message = await channel.next(timeoutMs);
    if (message.binary || typeof message.data !== "string") throw new StreamProtocolError("Expected JSON event before binary audio");
    let row: JsonRecord;
    try { row = JSON.parse(message.data) as JsonRecord; }
    catch { throw new StreamProtocolError("Server sent invalid JSON"); }
    if (!row || typeof row.type !== "string") throw new StreamProtocolError("Server event must include a type");
    let audio: AudioChunk | undefined;
    if (row.type === "audio_segment") {
      const binary = await channel.next(timeoutMs);
      if (!binary.binary || typeof binary.data === "string") throw new StreamProtocolError("audio_segment must be followed by binary WAV payload");
      const expected = row.audio_bytes;
      const sequence = row.audio_sequence;
      const eventSequence = row.event_sequence;
      if (!Number.isInteger(expected) || binary.data.byteLength !== expected) throw new StreamProtocolError("Audio payload length does not match audio_bytes");
      if (!Number.isInteger(sequence) || !Number.isInteger(eventSequence)) throw new StreamProtocolError("Audio descriptor is missing sequence fields");
      const seq = Number(sequence);
      const descriptor = canonicalJson(row);
      const hash = await sha256Hex(binary.data);
      const prior = this.audioSeen.get(seq);
      if (seq === this.lastAudioSequence + 1) {
        this.lastAudioSequence = seq;
        this.audioSeen.set(seq, { descriptor, hash });
        this.audioEventSequences.set(seq, Number(eventSequence));
        while (this.audioSeen.size > MAX_AUDIO_HISTORY) {
          const oldest = this.audioSeen.keys().next().value as number;
          this.audioSeen.delete(oldest);
          this.audioEventSequences.delete(oldest);
        }
      } else if (seq <= this.lastAudioSequence && prior?.descriptor === descriptor && prior.hash === hash) {
        this.audioSeen.delete(seq);
        this.audioSeen.set(seq, { descriptor, hash });
      } else throw new StreamProtocolError("Audio chunk sequence is missing, duplicated, or out of order");
      audio = { sequence: seq, eventSequence: Number(eventSequence), audioBytes: binary.data, descriptor: row };
    }
    return makeEvent(row, audio);
  }

  async ack(event: JsonRecord): Promise<void> {
    const sequence = event.event_sequence;
    if (typeof sequence !== "number" || !Number.isInteger(sequence)) return;
    this.eventCursor = Math.max(this.eventCursor, sequence);
    await this.sendControl({ type: "ack", event_sequence: sequence });
  }

  private async sendControl(message: JsonRecord, recover = true): Promise<void> {
    if (this.closed) throw new SmartVoiceError("Streaming session is closed");
    const channel = this.channel;
    try { await this.sendMutex.run(() => channel.send(JSON.stringify(message))); }
    catch (error) {
      if (!(error instanceof SocketClosedError) || !recover || !this.resumable) throw new SmartVoiceError("WebSocket closed while sending control", { cause: error });
      await this.recover(channel);
      await this.sendMutex.run(() => this.channel.send(JSON.stringify(message)));
    }
  }

  private async sendSequencedUnlocked(channel: SocketChannel, sequence: number, startSample: number, pcm: Uint8Array): Promise<void> {
    await channel.send(JSON.stringify({ type: "audio_frame", sequence, start_sample: startSample }));
    await channel.send(pcm);
  }

  private async recover(observed: SocketChannel): Promise<void> {
    if (!this.resumable || !this.recovery || this.closed) throw new StreamRecoveryError("This stream is not configured for recovery");
    if (this.recoveryTask) return this.recoveryTask;
    if (observed !== this.channel) return;
    this.recoveryTask = this.performRecovery(observed).finally(() => { this.recoveryTask = undefined; });
    return this.recoveryTask;
  }

  private async performRecovery(observed: SocketChannel): Promise<void> {
    observed.socket.close();
    const graceMs = Math.max(0, Number(this.recovery!.grace_seconds ?? this.openTimeoutMs / 1000) * 1_000);
    const deadline = Date.now() + graceMs;
    let lastError: unknown;
    for (let attempt = 0; attempt < this.resumeAttempts; attempt++) {
      const remaining = deadline - Date.now();
      if (remaining <= 0) break;
      if (attempt && this.resumeBackoffMs) await new Promise((resolve) => setTimeout(resolve, Math.min(this.resumeBackoffMs * 2 ** (attempt - 1), remaining)));
      let candidate: SocketChannel | undefined;
      try {
        candidate = await this.client.connectChannel();
        await candidate.send(JSON.stringify({
          type: "resume", protocol: STREAM_PROTOCOL,
          session_id: this.recovery!.session_id, resume_token: this.recovery!.resume_token,
          last_event_sequence: this.eventCursor, last_audio_played: this.lastPlayed,
        }));
        const response = await this.receiveRawEvent(Math.min(this.openTimeoutMs, deadline - Date.now()), candidate);
        if (response.type === "error") throw new ServerError(String(response.data.code ?? "resume_failed"), String(response.data.message ?? "Streaming recovery failed"), response.data);
        if (response.type !== "session_resumed") throw new StreamProtocolError(`Expected session_resumed, received ${response.type}`);
        if (response.data.session_id !== this.recovery!.session_id) throw new StreamProtocolError("The service resumed a different streaming session");
        this.reconcileInput(response.data);
        this.channel = candidate;
        await this.sendMutex.run(async () => {
          for (const [sequence, input] of this.pendingInput) {
            if (sequence > this.acceptedInputSequence) await this.sendSequencedUnlocked(candidate!, sequence, input.startSample, input.pcm);
          }
        });
        return;
      } catch (error) {
        lastError = error;
        candidate?.socket.close();
        if (error instanceof ServerError || error instanceof StreamProtocolError) throw error;
        await new Promise((resolve) => setTimeout(resolve, 0));
      }
    }
    throw new StreamRecoveryError(`Could not resume streaming session after ${this.resumeAttempts} attempts`, { cause: lastError });
  }

  private reconcileInput(row: JsonRecord): void {
    const sequence = row.input_sequence;
    const samples = row.accepted_samples;
    const eventSequence = row.last_event_sequence;
    const played = row.last_audio_played;
    if (![sequence, samples, eventSequence, played].every(Number.isInteger)) throw new StreamProtocolError("Resume response is missing cursor fields");
    const seq = Number(sequence);
    const acceptedSamples = Number(samples);
    if (seq < this.acceptedInputSequence || seq > this.inputSequence) throw new StreamProtocolError("Service input cursor is outside the sent audio range");
    if (Number(eventSequence) < this.eventCursor || Number(played) < this.lastPlayed) throw new StreamProtocolError("Service recovery cursor moved backwards");
    let expectedSamples: number;
    if (seq === this.inputSequence) expectedSamples = this.inputSamples;
    else if (seq === this.acceptedInputSequence) expectedSamples = this.acceptedInputSamples;
    else {
      const pending = this.pendingInput.get(seq);
      const following = this.pendingInput.get(seq + 1);
      if (pending) expectedSamples = pending.startSample + pending.pcm.byteLength / 2;
      else if (following) expectedSamples = following.startSample;
      else throw new StreamProtocolError("Service resume cursor is outside the retained input window");
    }
    if (acceptedSamples !== expectedSamples) throw new StreamProtocolError("Service resume sample cursor does not match sent audio");
    this.acceptedInputSequence = seq;
    this.acceptedInputSamples = acceptedSamples;
    for (const pendingSeq of this.pendingInput.keys()) if (pendingSeq <= seq) this.pendingInput.delete(pendingSeq);
    this.eventCursor = Number(eventSequence);
    this.lastPlayed = Number(played);
    this.lastStarted = Number(played);
    this.wakeInputWaiters();
  }

  private ackInput(row: JsonRecord): void {
    const sequence = row.input_sequence;
    const samples = row.accepted_samples;
    if (!Number.isInteger(sequence) || !Number.isInteger(samples)) throw new StreamProtocolError("Input ACK is missing cursor fields");
    const seq = Number(sequence);
    const acceptedSamples = Number(samples);
    if (seq < this.acceptedInputSequence || seq > this.inputSequence) throw new StreamProtocolError("Input ACK is outside the sent range");
    let expectedSamples: number;
    if (seq === this.acceptedInputSequence) expectedSamples = this.acceptedInputSamples;
    else {
      const pending = this.pendingInput.get(seq);
      const following = this.pendingInput.get(seq + 1);
      if (pending) expectedSamples = pending.startSample + pending.pcm.byteLength / 2;
      else if (following) expectedSamples = following.startSample;
      else if (seq === this.inputSequence) expectedSamples = this.inputSamples;
      else throw new StreamProtocolError("Input ACK is outside the retained replay window");
    }
    if (acceptedSamples !== expectedSamples) throw new StreamProtocolError("Input ACK sample offset does not match sent audio");
    this.acceptedInputSequence = seq;
    this.acceptedInputSamples = acceptedSamples;
    for (const pendingSeq of this.pendingInput.keys()) if (pendingSeq <= seq) this.pendingInput.delete(pendingSeq);
    this.wakeInputWaiters();
  }

  private waitForInputWindow(): Promise<void> {
    return new Promise((resolve, reject) => {
      const done = () => { clearTimeout(timer); resolve(); };
      const timer = setTimeout(() => {
        this.inputWaiters = this.inputWaiters.filter((waiter) => waiter !== done);
        reject(new StreamRecoveryError("Timed out waiting for input acknowledgements; keep consuming session.events()"));
      }, this.openTimeoutMs);
      this.inputWaiters.push(done);
    });
  }

  private wakeInputWaiters(): void {
    for (const wake of this.inputWaiters.splice(0)) wake();
  }
}
