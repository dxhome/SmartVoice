import {
  SmartVoiceClient as CoreSmartVoiceClient,
  type SmartVoiceClientOptions,
  type WebSocketLike,
} from "./index.js";
export { decodeAudioToPCM16 } from "./browser-audio.js";

/** Browser WebSocket adapter with the event shape expected by the shared client. */
export class BrowserWebSocketAdapter implements WebSocketLike {
  private readonly listeners = new Map<string, Map<(...args: unknown[]) => void, EventListener>>();
  private readonly socket: WebSocket;
  private readonly highWaterMarkBytes: number;
  private readonly lowWaterMarkBytes: number;
  private readonly waitTimeoutMs: number;

  constructor(url: string, options: { highWaterMarkBytes?: number; lowWaterMarkBytes?: number; waitTimeoutMs?: number } = {}) {
    if (!globalThis.WebSocket) throw new Error("This browser does not provide WebSocket support");
    this.highWaterMarkBytes = options.highWaterMarkBytes ?? 4 * 1024 * 1024;
    this.lowWaterMarkBytes = options.lowWaterMarkBytes ?? 2 * 1024 * 1024;
    this.waitTimeoutMs = options.waitTimeoutMs ?? 10_000;
    if (!Number.isSafeInteger(this.highWaterMarkBytes) || this.highWaterMarkBytes < 1
      || !Number.isSafeInteger(this.lowWaterMarkBytes) || this.lowWaterMarkBytes < 0
      || this.lowWaterMarkBytes >= this.highWaterMarkBytes
      || !Number.isFinite(this.waitTimeoutMs) || this.waitTimeoutMs <= 0) {
      throw new RangeError("Invalid browser WebSocket send-buffer limits");
    }
    this.socket = new globalThis.WebSocket(url);
    this.socket.binaryType = "arraybuffer";
  }

  get readyState(): number { return this.socket.readyState; }
  get bufferedAmount(): number { return this.socket.bufferedAmount; }

  send(data: string | Uint8Array, callback?: (error?: Error) => void): void {
    const byteLength = typeof data === "string" ? new TextEncoder().encode(data).byteLength : data.byteLength;
    if (byteLength > this.highWaterMarkBytes) {
      callback?.(new RangeError(`WebSocket message exceeds the ${this.highWaterMarkBytes}-byte send-buffer limit`));
      return;
    }
    void this.sendWhenWritable(data, byteLength).then(
      () => callback?.(),
      (error: unknown) => callback?.(error instanceof Error ? error : new Error(String(error))),
    );
  }

  private async sendWhenWritable(data: string | Uint8Array, byteLength: number): Promise<void> {
    const deadline = Date.now() + this.waitTimeoutMs;
    let waitForLowWaterMark = this.socket.bufferedAmount > this.lowWaterMarkBytes;
    while (waitForLowWaterMark || this.socket.bufferedAmount + byteLength > this.highWaterMarkBytes) {
      if (this.socket.readyState !== 1) throw new Error("WebSocket closed while waiting for send-buffer capacity");
      if (Date.now() >= deadline) throw new Error("WebSocket send buffer did not drain before the configured timeout");
      if (this.socket.bufferedAmount <= this.lowWaterMarkBytes) waitForLowWaterMark = false;
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    if (this.socket.readyState !== 1) throw new Error("WebSocket is not open");
    this.socket.send(data);
  }

  close(code?: number, reason?: string): void { this.socket.close(code, reason); }

  on(event: string, listener: (...args: unknown[]) => void): this {
    const wrapper: EventListener = (rawEvent) => {
      if (event === "message") {
        const message = rawEvent as MessageEvent;
        listener(message.data, typeof message.data !== "string");
      } else {
        listener(rawEvent);
      }
    };
    const map = this.listeners.get(event) ?? new Map();
    map.set(listener, wrapper);
    this.listeners.set(event, map);
    this.socket.addEventListener(event, wrapper);
    return this;
  }

  off(event: string, listener: (...args: unknown[]) => void): this {
    const map = this.listeners.get(event);
    const wrapper = map?.get(listener);
    if (wrapper) {
      this.socket.removeEventListener(event, wrapper);
      map!.delete(listener);
      if (map!.size === 0) this.listeners.delete(event);
    }
    return this;
  }
}

/** Browser client. Pass File/Blob/ArrayBuffer/Uint8Array; filesystem paths are Node-only. */
export class SmartVoiceClient extends CoreSmartVoiceClient {
  constructor(baseUrl = "http://127.0.0.1:8000", options: SmartVoiceClientOptions = {}) {
    super(baseUrl, {
      ...options,
      webSocketFactory: options.webSocketFactory ?? ((url) => new BrowserWebSocketAdapter(url, {
        highWaterMarkBytes: options.browserSendBufferHighWaterMarkBytes,
        lowWaterMarkBytes: options.browserSendBufferLowWaterMarkBytes,
        waitTimeoutMs: options.browserSendBufferWaitTimeoutMs,
      })),
    });
  }
}

export * from "./index.js";
