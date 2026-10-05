# SmartVoice API Specification

This document describes the HTTP API implemented by the current source code. The interactive OpenAPI UI is available at `/docs` while the service is running. The service has no API authentication in the current release and binds to loopback by default. An operator can explicitly configure a non-loopback HTTP bind address; such access is unencrypted and unauthenticated. HTTPS is not supported.

## Conventions

- Base URL: `http://127.0.0.1:8000` by default.
- Set `server_host` in `<data_dir>/smartvoice.json`, `SMARTVOICE_HOST`, or `--host` to choose another bind address (for example, `0.0.0.0` to listen on all IPv4 interfaces). External binding exposes the API and model-management endpoints to reachable clients without authentication; use only on a trusted network.
- API routes are versioned under `/v1`; service routes are `/health` and `/ready`.
- JSON request bodies use `Content-Type: application/json`. Audio upload and model import use `multipart/form-data`.
- Responses include `X-Request-ID`. A caller may provide `X-Request-ID` containing 1–80 ASCII letters, digits, `.`, `_`, or `-`; otherwise the server generates one.
- Inference responses may also include `X-Process-CPU-Time-Seconds`, `X-Process-Working-Set-Bytes`, and `X-Process-Peak-Working-Set-Bytes` when those measurements are available.
- Errors from the service use the JSON shape described in [Errors](#errors).

## Service

| Method and path | Success response |
|---|---|
| `GET /health` | `200`; `{"status":"ok","version":"<version>"}`. Confirms that the HTTP process responds. |
| `GET /ready` | `200` when at least one verified installed model is available for both transcription and speech; otherwise `503`. Returns `status`, `version`, `inference_backend`, `available_tasks`, `available_models`, and `reasons`. |

## Runtime and model discovery

| Method and path | Query parameters | Success response |
|---|---|---|
| `GET /v1/capabilities` | Optional `task`: `transcription` or `speech` | Provider capability document with `api_version`, `capability_schema_version`, `backend`, `backends`, `tasks`, router status, and `language_identification`. The latter reports whether spoken-language detection is supported by the provider and whether optional assets are installed; it includes an install command when applicable. Each task entry includes task, model, backend, languages, `available`, and `streaming` (currently false); speech entries include `voices` and may report `speed_control`. |
| `GET /v1/runtime` | None | Runtime document with top-level backend and per-adapter runtime status, installed model count, router status/candidates, runtime version, host, system memory and process metrics. Some metrics can be `null` or omitted on unsupported platforms. |
| `GET /v1/models` | None | OpenAI-style `{ "object": "list", "data": [...] }` list of models available through the active inference adapters. Concrete models include `availability: "available"` and SmartVoice metadata such as `estimated_size_bytes` and `installed_size_bytes`. Unavailable models are omitted. `smartvoice-auto` is included first when at least one routed task is available, and its `tasks` list reflects those available tasks; concrete models follow grouped by task and sorted alphabetically by name. |
| `POST /v1/models/refresh` | Empty JSON object | Rebuilds the process-wide verified model-availability snapshot after model files are changed outside the service. |
| `GET /v1/models/{model_id}` | Path: available or virtual model ID | One OpenAI-style model object. Unknown, uninstalled, or currently unavailable concrete IDs return `404`. |

The service builds one process-wide verified availability snapshot at startup.
Model list, readiness, runtime, and capability queries share that snapshot.
After the configured `model_availability_ttl_seconds` (default 600), the first
query starts a background refresh and continues using the last successful
snapshot. If the refresh fails, it retries after 5, 10, and 15 seconds. After
three failed retries, automatic retries stop until a manual refresh or a
model-management operation triggers another scan. The matching environment
variable is `SMARTVOICE_MODEL_AVAILABILITY_TTL_SECONDS`.
| `GET /v1/catalog` | Optional `task`: `transcription` or `speech` | `{ "data": [...], "storage": {...} }`. Catalog entries include model ID, task, languages, backend, install status, source/archive metadata, estimated and installed sizes, required files and license note. Storage reports installed, free and total bytes. |
| `POST /v1/router/reload` | Empty JSON object | Reloads `router.json`; invalid configuration returns an error and leaves the active configuration unchanged. Intended for local CLI use. |

The `task` query parameter is a SmartVoice extension; omit it to list all tasks. Task names are `transcription` for STT and `speech` for TTS.

`GET /v1/models` only returns models available through the active inference adapters; catalog entries that are uninstalled or unavailable are omitted. Use `/v1/catalog` to inspect installation status for every catalog entry.

`estimated_size_bytes` is the catalog's approximate unpacked model payload size. `installed_size_bytes`, when present, is measured from the installed model directory.

The router reads `<data_dir>/router.json`, initializing it from the built-in `catalog/router.json` on first run. Edit the JSON directly, then run `python -m smartvoice router reload`. The command accepts the same `--config` option as the service and optional `--host`/`--port` overrides. Routing candidates are ordered per task and language code declared by the model catalog; the first installed and verified candidate is selected. Requests using `smartvoice-auto` use routing for either audio endpoint. Requests using a concrete model ID continue to invoke that model directly. A model inference failure does not cause a second candidate to be tried.

The dedicated Whisper Tiny spoken-language detector is installed by `python -m smartvoice models install all` and by the Quick Start model installation steps. The service does not download it during startup. When it is unavailable, routed STT falls back to an installed model that supports automatic language detection, if one is available.

The total inference deadline and client cancellation also apply during language identification. A deadline expiry returns `504` and does not start fallback transcription; a client disconnect stops future work. An already-running native call retains its lease until it finishes.

For a concrete STT model with `language=auto`, every audio window retains `auto`; the service does not force later windows to the first window's language. A segmented response reports one language only when all windows report the same supported language; differing or unknown window languages produce `language: null`. This summarizes model-reported languages, not proof that the recording is monolingual. `smartvoice-auto + auto` still runs LID once and uses its resolved language for routing and every window in that request.

## Model management

The running process initializes a shared, verified model-availability snapshot at startup. Successful model installation jobs, imports, and uninstallations refresh it automatically. If model files are changed outside the service, call `POST /v1/models/refresh` or run `python -m smartvoice models refresh` to rebuild the snapshot. Model listing and automatic routing use the same process-wide availability state.

| Method and path | Request | Success response |
|---|---|---|
| `POST /v1/models/{model_id}/download` | No body; `{model_id}` must be in the built-in catalog. | `202`; model job document. A download for the same model cannot be started while another is queued, running or canceling. |
| `GET /v1/jobs/{job_id}` | No body. | `200`; job document containing `job_id`, `model_id`, `status`, `downloaded_bytes`, `total_bytes`, `error`, and, on success, `result`. Status is `queued`, `running`, `canceling`, `completed`, `failed`, or `canceled`. |
| `DELETE /v1/jobs/{job_id}` | No body. | `200`; updated job document. A completed or otherwise inactive job is returned unchanged. |
| `DELETE /v1/models/{model_id}` | No body; model must be in the catalog and not loaded by this service process. | `200`; `{ "id": "<model_id>", "removed_bytes": <integer> }`. |
| `GET /v1/models/{model_id}/export` | No body; model must be installed and valid. | `200`; verified model package as `application/zip`, named `<model_id>.smartvoice.zip`. |
| `POST /v1/models/import` | Multipart field `file`: exported `.smartvoice.zip` package. Maximum upload size is 3 GiB. | `201`; `{ "id": "<model_id>", "status": "installed" }`. |

## Audio

### `POST /v1/audio/transcriptions`

Send `multipart/form-data`:

| Field | Type | Default | Description |
|---|---|---|---|
| `file` | file | Required | Audio in WAV, MP3, M4A or FLAC format. Default upload limit: 25 MiB. Default duration limit: 600 seconds. |
| `model` | string | Required | `smartvoice-auto` routes by language, or specify an installed STT model ID for direct inference. |
| `language` | string | `auto` | Empty or `auto` lets a concrete model handle language itself on every window. For `smartvoice-auto + auto`, the optional Whisper Tiny identifier runs once before routing; an unavailable detector or unrouted language can fall back to an installed auto-capable ASR model. The identifier is never downloaded during service startup. |
| `response_format` | string | `json` | `json`, `text` or `verbose_json`. |
| `timestamps` | boolean | `false` | Include token-level `segments` when true and when the model returns aligned tokens/timestamps. |

Other form fields are rejected. Success is `200`. `text` returns UTF-8 plain text with model metadata headers. `json` returns an object with `text`, `language`, `duration`, `model` (actual model ID), `requested_model`, `model_mode` (`router` or `direct`), `language_source`, `router_sha256`, `route_candidates`, `device`, `processing_seconds`, `rtf`, `request_processing_seconds`, `queue_wait_seconds`, and `runtime_wait_seconds` (time waiting for a provider runtime instance; zero when there is no wait). `verbose_json` uses the same object; `segments` is included only when `timestamps=true` and aligned segment data is available. With `response_format=json`, segments are omitted regardless of `timestamps`.

### `POST /v1/audio/speech`

Send a JSON object. Unknown fields are rejected.

| Field | Type | Default | Constraints |
|---|---|---|---|
| `input` | string | Required | Non-whitespace; 1–4,000 characters. |
| `model` | string | Required | `smartvoice-auto` routes by text/request language, or specify an installed TTS model ID for direct inference. |
| `voice` | string | `default` | Most models accept `default` or `0`. Kokoro accepts `default` or a speaker ID from `0` to `102`; its default speaker follows the resolved language. Qwen3-TTS accepts `default` or one of its nine named preset speakers. |
| `language` | string or null | `auto` | `auto` detects text language offline using text and script cues. Short text is repeated for detection when needed; synthesis always uses the original input. An explicit catalog-supported language code takes precedence over detected text script. |
| `response_format` | string | `mp3` | `mp3` (96 kbps by default) or explicit `wav`. Unsupported/unavailable formats fail before inference. |
| `speed` | number | `1.0` | Inclusive range 0.5–2.0. Qwen3-TTS currently supports only `1.0`; other values return `501 not_implemented`. |

Success is `200` with `audio/mpeg` mono MP3 data by default, or `audio/wav` when `response_format="wav"`. WAV is passed through byte-for-byte by the encoder. Response headers include `X-Audio-Format`, `X-Audio-Segments`, `X-Audio-Sample-Rate`, `X-Audio-Duration`, `X-Model-Id` (actual model ID), `X-Requested-Model`, `X-Model-Mode`, `X-Resolved-Language`, `X-Language-Source`, `X-Language-Confidence`, `X-Router-SHA256`, `X-Route-Candidates`, `X-Inference-Time-Seconds`, `X-Queue-Wait-Seconds`, `X-Runtime-Wait-Seconds` (time waiting for a provider runtime instance), `X-Real-Time-Factor`, `X-Requested-Language`, and `X-Text-Language`. Default generated-audio limits are 180 seconds, 32 MiB canonical PCM WAV, and 32 MiB final response, enforced separately; exceeding a limit returns `413`. Speech JSON is capped at 64 KiB before parsing. MP3 cannot bypass the duration or internal PCM budget.

## Errors

Application errors have the following shape (validation errors additionally include `details`):

```json
{
  "error": {
    "code": "invalid_request",
    "message": "A human-readable description.",
    "request_id": "request-id"
  }
}
```

| HTTP status | `error.code` | Meaning |
|---:|---|---|
| `400` | `invalid_request` | Malformed request, invalid operation, or invalid/unavailable catalog operation. |
| `404` | `not_found` | A model job ID does not exist. Unknown or uninstalled model IDs on the model detail endpoint return `404` with the standard HTTP error body. |
| `400` | `router_config_invalid` | Router configuration is invalid or could not be read during reload. |
| `413` | `file_too_large` | STT audio upload exceeds the configured byte limit. |
| `413` | `payload_too_large` | Imported model package exceeds 3 GiB. |
| `413` | `speech_output_too_large` | Generated TTS audio exceeds its configured duration or byte limit. |
| `422` | `invalid_audio` | STT audio cannot be decoded, is empty, or exceeds the configured duration limit. |
| `422` | `validation_error` | Request shape, field value, or field limit is invalid. `details` is an array of `{ "field", "message", "type" }`. |
| `500` | `inference_failed` | Inference failed. |
| `500` | `internal_error` | Unexpected server error. |
| `501` | `not_implemented` | The requested language, task, model capability, voice, response format, device, or request option is not supported by this release. This also applies when no installed and verified model is available for a routed language. |
| `503` | `model_unavailable` | A directly requested supported model, or a required runtime resource, is not installed, verified, or available in the active provider. |
| `503` | `inference_overloaded` | The inference queue is full. |
| `504` | `inference_timeout` | Queue wait or inference exceeded its configured timeout. |

Some standard HTTP errors, such as a missing installed model on `GET /v1/models/{model_id}` (`404`), use `{ "detail": "..." }` instead of the application error envelope.

## Default limits and configuration

On first startup, SmartVoice creates `<data_dir>/smartvoice.json` with all default service settings. The service reads this file on later startups, so users can edit it directly. An explicit `--config` path (or `SMARTVOICE_CONFIG`) selects a separate JSON file; environment variables override values from JSON. `SMARTVOICE_HOME` or the serve command's `--data-dir` selects the user data directory. The tracked `config/smartvoice.example.json` is an example of the generated settings file.

| Setting | Default | Configuration key / environment variable |
|---|---:|---|
| STT upload | 25 MiB | `max_upload_bytes` / `SMARTVOICE_MAX_UPLOAD_BYTES` |
| STT audio duration | 600 seconds | `max_audio_seconds` / `SMARTVOICE_MAX_AUDIO_SECONDS` |
| TTS input | 4,000 characters | `max_tts_characters` / `SMARTVOICE_MAX_TTS_CHARACTERS`; the API field also has a fixed 4,000-character maximum |
| TTS output duration | 180 seconds | `max_tts_audio_seconds` / `SMARTVOICE_MAX_TTS_AUDIO_SECONDS` |
| TTS final response size | 32 MiB | `max_tts_output_bytes` / `SMARTVOICE_MAX_TTS_OUTPUT_BYTES` |
| TTS canonical WAV size | 32 MiB | `max_tts_internal_bytes` / `SMARTVOICE_MAX_TTS_INTERNAL_BYTES` |
| TTS JSON body size | 64 KiB | `max_tts_json_bytes` / `SMARTVOICE_MAX_TTS_JSON_BYTES` |
| MP3 bitrate | 96 kbps | `tts_mp3_bitrate` / `SMARTVOICE_TTS_MP3_BITRATE`; 64000, 96000 or 128000 |
| Model import package | 3 GiB | Fixed by the model catalog implementation |

For model instance limits, adapter admission, overload and timeout behavior, see [inference concurrency in the architecture summary](architecture-guidelines.md#inference-concurrency-lifecycle-and-limits) and `max_concurrent_inference`, `max_queued_inference`, `inference_queue_timeout_seconds`, and `inference_execution_timeout_seconds` in the service configuration.


### Audio output and long input policy (2026-10-04)

`/v1/capabilities` schema 1.1 adds `speech_output` (default format, bitrate, per-format availability/reason) and active `limits`. Model generation formats remain separate: inference adapters emit canonical mono PCM16 WAV; an injected audio encoder converts every TTS backend's output uniformly. If MP3 encoding is unavailable, a default request returns `501`; request explicit WAV to use it. There is no silent format fallback. Clients that assumed WAV must now send `response_format="wav"` and choose the file extension from the actual response format.

Capabilities describe segmentation policy by task: TTS may report model-specific `characters`, while STT reports the shared `audio_seconds` window. Supertonic defaults to a 200-character application plan, releases its instance between chunks and joins bounded canonical WAV before final encoding. Qwen3-TTS long-text segmentation remains deferred; its output still uses the same encoder.

Long STT inputs use bounded windows declared by the model capability (Whisper Base: 25 seconds; default: 15 seconds), with quiet cuts preferred and a 1-second overlap at every cut. The service decodes once to private temporary storage and retains the 600-second input limit. Explicit language and routed LID decisions apply to every window; direct-model auto stays auto in every window. `chunk_count` is added to segmented transcription results. Native timestamps receive actual window offsets; unavailable token timestamps are not fabricated. Start-only tokens in overlapping audio are retained unless the previous window supplied identical text at the same rounded absolute start. Returned segments are ordered by start; uncertain overlap may include alternative or repeated tokens and does not establish word durations. Text overlap removal is bounded exact matching, not guaranteed alignment or lossless recognition. Long-text/audio requests share a single execution deadline; cancellation prevents future segments while current native work retains its lease until completion.

For routed auto-language requests, only recoverable identifier inference failures or unavailable identifier assets permit language fallback. Identifier overload remains HTTP 503, execution timeout remains HTTP 504, and cancellation ends the request. These paths do not begin a fallback STT operation. Transport and model admission capacity remain occupied until already-running native work returns, even if the caller disconnects. Validation-only internal stage measurements add no public response fields or headers.

The shared policy addresses bounded long-input handling, not equal model accuracy. Digital silence may produce an empty result; this is not a general noise-rejection claim. See [validation and rollout decisions](archive/stt/audio-optimization-validation.md).
