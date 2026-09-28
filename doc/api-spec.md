# SmartVoice API Specification

This document describes the HTTP API implemented by the current source code. The interactive OpenAPI UI is available at `/docs` while the service is running. The service has no API authentication in the current release and binds to loopback by default.

## Conventions

- Base URL: `http://127.0.0.1:8000` by default.
- API routes are versioned under `/v1`; service routes are `/health` and `/ready`.
- JSON request bodies use `Content-Type: application/json`. Audio upload and model import use `multipart/form-data`.
- Responses include `X-Request-ID`. A caller may provide `X-Request-ID` containing 1–80 ASCII letters, digits, `.`, `_`, or `-`; otherwise the server generates one.
- Inference responses may also include `X-Process-CPU-Time-Seconds`, `X-Process-Working-Set-Bytes`, and `X-Process-Peak-Working-Set-Bytes` when those measurements are available.
- Errors from the service use the JSON shape described in [Errors](#errors).

## Service

| Method and path | Success response |
|---|---|
| `GET /health` | `200`; `{"status":"ok","version":"<version>"}`. Confirms that the HTTP process responds. |
| `GET /ready` | `200` when at least one verified installed model is available for both transcription and speech; otherwise `503`. Returns `status`, `inference_backend`, `available_tasks`, `available_models`, and `reasons`. |

## Runtime and model discovery

| Method and path | Query parameters | Success response |
|---|---|---|
| `GET /v1/capabilities` | Optional `task`: `transcription` or `speech` | Provider capability document. Current fields: `api_version`, `capability_schema_version`, `backend`, and `tasks`. Each task entry includes task, model, languages, `streaming` (currently false), and `voices` for speech models; Kokoro also reports `voice_count`. |
| `GET /v1/runtime` | None | Runtime document with backend/device/provider status, installed model count, router status/candidates, runtime version, host, system memory and process metrics. Some metrics can be `null` or omitted on unsupported platforms. |
| `GET /v1/models` | None | OpenAI-style `{ "object": "list", "data": [...] }` list of installed models and virtual routing models, extended with SmartVoice metadata. Results are grouped by task (transcription, then speech); each task's virtual model appears first, followed by installed models sorted alphabetically by name. |
| `GET /v1/models/{model_id}` | Path: installed or virtual model ID | One OpenAI-style model object. Unknown or uninstalled concrete IDs return `404`. |
| `GET /v1/catalog` | Optional `task`: `transcription` or `speech` | `{ "data": [...], "storage": {...} }`. Catalog entries include model ID, task, languages, backend, install status, source/archive metadata, installed size, required files and license note. Storage reports installed, free and total bytes. |
| `POST /v1/router/reload` | Empty JSON object | Reloads `router.json`; invalid configuration returns an error and leaves the active configuration unchanged. Intended for local CLI use. |

The `task` query parameter is a SmartVoice extension; omit it to list all tasks. Task names are `transcription` for STT and `speech` for TTS.

The router reads `<data_dir>/router.json`, initializing it from the built-in `catalog/router.json` on first run. Edit the JSON directly, then run `python -m smartvoice router reload`. The command accepts the same `--config` option as the service and optional `--host`/`--port` overrides. Routing candidates are ordered per task and Whisper language code; the first installed and verified candidate is selected. Requests using `stt-smartvoice-auto` or `tts-smartvoice-auto` use routing. Requests using a concrete model ID continue to invoke that model directly. A model inference failure does not cause a second candidate to be tried.

## Model management

| Method and path | Request | Success response |
|---|---|---|
| `POST /v1/models/{model_id}/download` | No body; `{model_id}` must be in the built-in catalog. | `202`; model job document. A download for the same model cannot be started while another is queued, running or canceling. |
| `GET /v1/jobs/{job_id}` | No body. | `200`; job document containing `job_id`, `model_id`, `status`, `downloaded_bytes`, `total_bytes`, `error`, and, on success, `result`. Status is `queued`, `running`, `canceling`, `completed`, `failed`, or `canceled`. |
| `DELETE /v1/jobs/{job_id}` | No body. | `200`; updated job document. A completed or otherwise inactive job is returned unchanged. |
| `DELETE /v1/models/{model_id}` | No body; model must be in the catalog and not loaded by this service process. | `200`; `{ "id": "<model_id>", "removed_bytes": <integer> }`. |
| `GET /v1/models/{model_id}/export` | No body; model must be installed and valid. | `200`; verified model package as `application/zip`, named `<model_id>.smartvoice.zip`. |
| `POST /v1/models/import` | Multipart field `file`: exported `.smartvoice.zip` package. Maximum upload size is 2 GiB. | `201`; `{ "id": "<model_id>", "status": "installed" }`. |

## Audio

### `POST /v1/audio/transcriptions`

Send `multipart/form-data`:

| Field | Type | Default | Description |
|---|---|---|---|
| `file` | file | Required | Audio in WAV, MP3, M4A or FLAC format. Default upload limit: 25 MiB. Default duration limit: 600 seconds. |
| `model` | string | Required | `stt-smartvoice-auto` routes by language, or specify an installed STT model ID for direct inference. |
| `language` | string | `auto` | `auto` or a Whisper language code. `auto` uses a supported installed model for language detection before routing. STT automatic detection makes at most three attempts to obtain a language with a configured route. TTS text detection accepts confidence >= 0.70 immediately; after three lower-confidence results, it routes using the highest-confidence result. |
| `response_format` | string | `json` | `json`, `text` or `verbose_json`. |
| `timestamps` | boolean | `false` | Include token-level `segments` when true and when the model returns aligned tokens/timestamps. |

Other form fields are rejected. Success is `200`. `text` returns UTF-8 plain text with model metadata headers. `json` returns an object with `text`, `language`, `duration`, `model` (actual model ID), `requested_model`, `model_mode` (`router` or `direct`), `language_source`, `router_sha256`, `route_candidates`, `device`, `processing_seconds`, `rtf`, `request_processing_seconds`, and `queue_wait_seconds`. `verbose_json` uses the same object; `segments` is included only when `timestamps=true` and aligned segment data is available. With `response_format=json`, segments are omitted regardless of `timestamps`.

### `POST /v1/audio/speech`

Send a JSON object. Unknown fields are rejected.

| Field | Type | Default | Constraints |
|---|---|---|---|
| `input` | string | Required | Non-whitespace; 1–4,000 characters. |
| `model` | string | Required | `tts-smartvoice-auto` routes by text/request language, or specify an installed TTS model ID for direct inference. |
| `voice` | string | `default` | Most models accept `default` or `0`. Kokoro accepts `default` or a speaker ID from `0` to `102`; its default speaker follows the resolved language. |
| `language` | string or null | `auto` | `auto` detects text language offline. Explicit Whisper language codes take precedence over detected text script. |
| `response_format` | string | `wav` | Only `wav` is accepted. |
| `speed` | number | `1.0` | Inclusive range 0.5–2.0. |

Success is `200` with `audio/wav` mono WAV data. Response headers include `X-Audio-Sample-Rate`, `X-Audio-Duration`, `X-Model-Id` (actual model ID), `X-Requested-Model`, `X-Model-Mode`, `X-Resolved-Language`, `X-Language-Source`, `X-Language-Confidence`, `X-Router-SHA256`, `X-Route-Candidates`, `X-Inference-Time-Seconds`, `X-Queue-Wait-Seconds`, `X-Real-Time-Factor`, `X-Requested-Language`, and `X-Text-Language`. Default generated-audio limits are 180 seconds and 32 MiB; exceeding either returns `413`.

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
| `400` | `invalid_request` | Malformed request, invalid operation, unknown job ID, or invalid/unavailable catalog operation. |
| `400` | `router_config_invalid` | Router configuration is invalid or could not be read during reload. |
| `400` | `language_detection_failed` | TTS language detection is unavailable, for example because the offline language detector dependency is missing. |
| `413` | `file_too_large` | STT audio upload exceeds the configured byte limit. |
| `413` | `payload_too_large` | Imported model package exceeds 2 GiB. |
| `413` | `speech_output_too_large` | Generated TTS audio exceeds its configured duration or byte limit. |
| `422` | `invalid_audio` | STT audio cannot be decoded, is empty, or exceeds the configured duration limit. |
| `422` | `validation_error` | Request shape, field value, or field limit is invalid. `details` is an array of `{ "field", "message", "type" }`. |
| `500` | `inference_failed` | Inference failed. |
| `500` | `internal_error` | Unexpected server error. |
| `501` | `not_implemented` | The requested language, task, model capability, voice, response format, device, or request option is not supported by this release. A valid language without a configured route also returns this status. |
| `503` | `model_unavailable` | A supported model or automatic language detector is configured but not installed, verified, or available in the active provider. |
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
| TTS output size | 32 MiB | `max_tts_output_bytes` / `SMARTVOICE_MAX_TTS_OUTPUT_BYTES` |
| Model import package | 2 GiB | Fixed by the model catalog implementation |

For inference overload and timeout behavior, see `max_concurrent_inference`, `max_queued_inference`, `inference_queue_timeout_seconds`, and `inference_execution_timeout_seconds` in the service configuration.
