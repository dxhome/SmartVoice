# SmartVoice Implementation Plan

> **Planning record, not a current support matrix.** This document preserves the original phased plan and design decisions. Some early-phase targets and proposals below have been superseded by implementation. For current supported platforms, models, and commands, see [README.md](../README.md); for current API behavior, see [api-spec.md](api-spec.md). Headings marked as pending confirmation are historical prototype material, not outstanding release decisions.

## Original Goals and Constraints

These bullets record the plan's initial assumptions. The current implementation status and support matrix may differ; see the notice above and [README.md](../README.md).

- The first release is a local service that can be run from source on Windows x64. An installer or standalone packaged application is not currently planned.
- Chinese and English are the first priorities. The initial models, runtime, and performance targets must be confirmed through evaluation.
- The current runnable prototype uses sherpa-onnx. The final Phase 1 decision must still consider model quality, performance, hardware, and licensing. STT and TTS may use different backends later.
- Frontends and callers depend only on versioned HTTP APIs and capability contracts. Adapters handle platform and inference differences.
- By default, bind only to loopback and do not log request text or audio.

## Phase 0: Freeze the First-release Scope

Confirm the minimum Windows version/architecture, reference hardware, acceptable latency and audio duration, first-phase GPU requirements, model licensing policy and sources, OpenAI Audio API compatibility scope, and the boundaries of the CLI/REST/management UI.

**Deliverable:** First-release scope and measurable acceptance baseline. Do not invent performance thresholds without reference hardware and a corpus.

## Phase 1: Evaluate Inference Backends and Models

Compare sherpa-onnx with relevant alternatives. Evaluate Chinese and English STT/TTS candidate models separately. Record pinned revisions, file hashes, licenses, formats, sources, model and dependency sizes, cold start, RTF/latency, peak RAM/VRAM, actual device, and quality results.

**Deliverable:** Backend decision record, initial model catalog, and compatibility matrix. Mark a model as verified only after measurement and license review.

**Initial prototype choice:** sherpa-onnx was the first runnable backend, with SenseVoice Small INT8 for STT and Melo VITS ONNX for Chinese/English TTS. Melo support has since been removed from the catalog; the current TTS catalog is documented in [README.md](../README.md). The prototype results do not establish final performance, model quality, or redistribution rights. Reference: [SenseVoice Python API](https://k2-fsa.github.io/sherpa/onnx/sense-voice/python-api.html).

## Phase 2: Domain Architecture and API Contracts

Freeze domain data structures, OpenAPI, stable error structures, capability schema, model metadata, and adapter interfaces. The domain layer must not depend on Windows, GPU vendors, or inference frameworks. Windows file/device/lifecycle capabilities belong in platform adapters.

**Deliverable:** Architecture documentation, initial OpenAPI specification, and versioning/compatibility policy.

## Phase 3: CPU Source-deployment Vertical Slice

Run the service from source and provide health/readiness, runtime/capabilities, model listing, file-based STT, and WAV TTS. Complete input boundaries, error mapping, request IDs, temporary audio cleanup, and redacted-by-default logs.

**Deliverable:** First speech release that can run on Windows CPU and be called over REST.

## Phase 4: Model Catalog and Lifecycle

The current Windows CPU scope provides catalog queries, background download jobs, cancellation and HTTP Range resume, file hash/path checks, temporary directories and atomic installation, activation/deactivation, removal, disk-space queries, and verified offline import/export. Sources are restricted to catalog-registered HTTPS URLs; offline imports accept only packages for registered model IDs.

**Deliverable:** A local lifecycle covering discovery, activation, use, removal, and offline transfer for catalog models.

## Phase 5: GPU, Diagnostics, and First-release Acceptance

Add GPU providers according to the verification matrix and report the actual device and fallback reason. Complete resource limits, concurrency/cancellation/timeout semantics, configuration migration, operating instructions, API documentation, and third-party notices.

**Deliverable:** A Windows source release that meets the first-release baseline, with a reproducible acceptance report.

### sherpa-onnx Concurrency (Staged Implementation)

The application-level `InferenceQueue` controls request admission and queuing; inference adapters control safe use of runtime instances. `num_threads` controls compute threads for one runtime instance and does not define API request concurrency. Do not pass sherpa-onnx streams, recognizers, or TTS objects into application services or API routes.

The first stage separates provider cache-initialization protection from per-runtime-instance inference protection. API responses and concurrency reports expose `queue_wait_seconds` separately from `runtime_wait_seconds`, which measures waiting for an adapter runtime instance. Different models/tasks can proceed independently when the configured admission limit permits; calls sharing the same recognizer or TTS instance remain serialized until same-instance safety is verified.

Next, verify same-instance concurrency for the sherpa-onnx `OfflineRecognizer`, each TTS type, and target CPU platforms, or evaluate independent instance pools and their memory cost. ASR `decode_streams()` micro-batching remains a throughput candidate whose added batching latency must be measured. Use a fixed-arrival-rate workload or the repository concurrency benchmark to compare throughput, tail latency, rejection rate, CPU, and peak RSS. **Defaults remain `max_concurrent_inference=1` and `max_queued_inference=2` until target-device evidence supports changing them.**

**Exploratory evidence (2026-09-29, macOS arm64, CPU, sherpa-onnx 1.13.8):** In three paired closed-loop runs with two workers alternating SenseVoice English ASR and Melo English TTS (`num_threads=1`, admission limit 2, two requests per worker), the per-run median throughput was 0.630 req/s on the pre-lock-split baseline and 0.514 req/s after the lock split; median round p95 was 3.64 s and 4.77 s respectively. All requests succeeded, but the lock split did not consistently improve cross-task interactive latency in this small workload. This supersedes the earlier single-run directional result; keep the current lock boundaries for isolation, but do not claim a latency win or raise concurrency defaults from it. Separately, a direct SenseVoice probe compared eight cached English FLEURS clips decoded serially and through `decode_streams()` over five iterations: serial decode averaged 3.60 s per eight clips, while the batch call averaged 5.22 s. Some transcripts differed by small token substitutions between the two runs, so this is not yet a correctness acceptance. This is an exploratory English-only result, not a target-device or Chinese quality evaluation; retain batching as an opt-in research candidate and do not route production requests through it.

## Phase 6: Smart Routing and Future Extensions

Smart routing described below is implemented. The streaming/session capabilities that follow remain future work.

### Core Feature: Dynamic STT/TTS Model Routing (`smartvoice-auto`)

STT and TTS share one virtual model ID, `smartvoice-auto`. The audio API path determines the task; a static routing table then chooses the actual model by language. A concrete model ID continues to invoke that model directly. The virtual model is not a weight-bearing catalog model and is pinned to the top of the model list. Smart routing replaces the per-task default-model mechanism; concrete models remain managed through the catalog.

- **Model name:** Use `smartvoice-auto` as the single public virtual model ID. Do not treat the virtual ID as a catalog model with weights; the request API path supplies the STT/TTS context.
- **Language identifiers:** API `language` values and JSON routing keys use OpenAI/Whisper language codes. SmartVoice also uses Whisper codes internally as its common language identifiers. Common values include `en`, `zh`, `ja`, `ko`, `fr`, and `de`. Do not use BCP 47 region/script extensions such as `zh-Hans` or `fr-FR`. Validate against Whisper codes and retain `yue` as a catalog extension for Cantonese. Catalog metadata, provider detection results, and language identifier output must map to the same codes. Model-declared capabilities remain authoritative for supported languages.
- **Language sources and priority:** An explicit request `language` takes precedence. For `auto`, missing, or empty values, STT uses sherpa-onnx's dedicated Spoken Language Identification API and multilingual Whisper Tiny INT8 to identify language, then selects an ASR model from the routing table. Do not use ASR inference for language detection or retry. If identification fails, returns an unknown language, or yields a language without a route, select an installed and verified STT model that supports `auto` and let it transcribe directly. Tiny INT8 is a SmartVoice-managed built-in runtime resource, not a user-selectable ASR model. On first service startup, download it from a pinned source to the user model directory and verify SHA-256; reuse verified files on later starts. Startup must fail with a clear error if download or verification fails. TTS detection runs once using Unicode script rules, a small set of high-precision word cues, and a lightweight text classifier. Restrict the detected candidate to languages supported by the selected TTS model. For input shorter than 10 characters, repeat separated copies until the minimum length is reached; pass the original text to synthesis. Confidence is output/debug information, not a routing threshold. Record detection and confidence for 1–2 character and approximately 12-character regression samples; rerun them whenever this logic changes. Evaluate mixed-language chunked synthesis separately.
- **Core selection logic:** Use a static JSON routing configuration with one to three ordered model IDs per task/language. Array order fully determines selection priority; do not reorder at runtime using quality scores, performance metrics, or extra policies. Choose the first candidate that is installed and verified. Do not download automatically and do not fall through to another candidate after inference failure. Log diagnostics and return the inference error so the user can adjust the JSON. If no candidate for that language is installed and verified, return a clear error listing candidates and installation guidance. Do not add implicit per-task defaults or generic fallbacks for unconfigured languages.
- **Default configuration and user edits:** Provide a built-in default routing JSON. The service reads the user's routing JSON from the data directory, initialized from the built-in file on first launch (the exact path was TBD at planning time). Users edit the JSON directly; no CLI edit command is required. Allow at most three candidates per language. Validate schema, tasks, language labels, model ID prefix/task, duplicates, and candidate count. Reject a concrete model that does not match the request task or language; do not offer a force override that bypasses capability checks.
- **Hot-reload command:** After editing the JSON, users invoke `python -m smartvoice router reload` to ask the running service to reload without restarting. The command reports success or validation errors. The service atomically replaces the active configuration only after full validation and keeps the previous valid configuration on failure. The command does not edit JSON or trigger downloads. Service address discovery, config path discovery, and the corresponding local API/IPC contract must be defined in detailed design.
- **Remove default-model mechanism:** Smart routing replaces the current single default model per task. The implementation should remove default-model CLI/REST operations, `default_models.json` runtime behavior, and the default-model field from runtime status. Update the “Default” marker in the model list accordingly. No backward compatibility or migration for the old mechanism is required. Preserve direct inference by concrete model ID alongside virtual routing.
- **Request/response contract:** Callers request routing with `smartvoice-auto`; the audio API path identifies STT/TTS. Concrete model IDs retain direct inference. Responses report requested and actual model IDs and distinguish routed/direct mode. Routed requests report normalized language and its source. Show one virtual model, pinned to the top of the model list. TTS detection runs once; for input shorter than 10 characters, only the repeated copy goes to the detector while synthesis receives the original text. Confidence is not a threshold; keep language and confidence in debug logs.
- **Error semantics:** Define stable error codes, HTTP statuses, and actionable messages for invalid JSON, failed reload, unidentified/unconfigured languages, missing routes, candidates that are all uninstalled or unverified, direct model/task/language/installation mismatch, and inference failure. Routing must not try another candidate, silently change language/model, or download automatically. Direct model errors follow existing model-call semantics.
- **Resource management:** Load the final statically selected model on demand. Define capacity and eviction for multi-model caches, concurrency, and memory use. Verify protection against removing models in use and consistency between route hot reload and concurrent requests.
- **Acceptance:** Cover naming, OpenAI/Whisper language validation and model-language mapping, explicit-language priority, STT/TTS automatic language detection, static candidate order, maximum of three candidates, installed-and-verified filtering, no automatic download or inference fallback, direct concrete-model use, JSON edits/hot reload, retention of the previous configuration after invalid updates, removal of default-model behavior, error/log diagnostics, cache, and concurrency behavior.

#### Smart-routing Table Prototype (Historical draft; superseded by implementation)

This JSON illustrates a possible structure and semantics; it does not define the final model ranking. Language keys use Whisper language codes. Configuration loading must validate the code and verify that each candidate model declares support for the corresponding language.

```json
{
  "schema_version": "1.0",
  "tasks": {
    "transcription": {
      "zh": [
        "stt-sensevoice-small-int8",
        "stt-whisper-base-multilingual-int8"
      ],
      "en": [
        "stt-sensevoice-small-int8",
        "stt-whisper-base-multilingual-int8"
      ],
      "yue": [
        "stt-sensevoice-small-int8"
      ],
      "ja": [
        "stt-sensevoice-small-int8",
        "stt-whisper-base-multilingual-int8"
      ]
    },
    "speech": {
      "zh": [
        "tts-matcha-zh-baker",
        "tts-kokoro-multilingual-v1-1-zh-en"
      ],
      "en": [
        "tts-supertonic-v3-multilingual-int8"
      ],
      "fr": [
        "tts-supertonic-v3-multilingual-int8"
      ],
      "de": [
        "tts-supertonic-v3-multilingual-int8"
      ]
    }
  }
}
```

#### Historical Open Questions for Smart Routing (resolved or superseded by implementation)

1. **STT automatic-detection boundary:** Which models return reusable language results? Is detection available before the first inference? How are confidence and low-confidence results represented? What error is returned when detection is unreliable?
2. **TTS detection boundary:** Select a lightweight text classifier and confirm its license, size, and offline behavior. Define confidence thresholds, short-text handling, mixed-language behavior, and conflicts between script and lexical cues.
3. **Whisper code table vs. catalog capabilities:** Confirm the language-code table version and validation rules. Decide how catalog extensions such as `yue` relate to that table. Ensure every route candidate declares the corresponding code.
4. **Routing JSON lifecycle:** Define file location, built-in defaults, user initialization/recovery, schema version, unknown model behavior, and how the CLI discovers the running service's config and address.
5. **Response compatibility and privacy:** Decide whether requested model, actual model, mode, language source, and selection reason belong in body, headers, or both. Decide whether logs and config digests are sufficient for diagnosis.
6. **Resources and acceptance targets:** Quantify cache capacity/eviction, model switching overhead, detection latency, and consistency requirements for hot reload during concurrent requests.

#### Original Recommended Implementation Order (completed; retained as design history)

1. Confirm virtual ID naming, Whisper language table, and model capability validation. Freeze the semantics: virtual ID triggers routing; concrete IDs call a model directly.
2. Confirm the JSON schema, routing file location, and default initialization. Define errors for unknown and unconfigured languages.
3. Freeze the provider language-detection result contract. Confirm STT model detection feasibility, select an offline TTS text classifier, and define low-confidence behavior.
4. Freeze candidate filtering and execution: select the first installed, verified candidate in JSON order; on inference failure, log and return without trying another candidate.
5. Design the control channel for `router reload`; define full validation, atomic hot swap, retention of the previous config on failure, and concurrent request semantics.
6. Plan removal of old default-model REST/CLI/runtime capabilities without backward compatibility or migration. Preserve direct inference for concrete model IDs.
7. Define errors, response metadata, capabilities/runtime, redacted logs, and cache/resource limits. Begin implementation after the acceptance matrix is complete.

**Implementation status (2026-09-28):** Static JSON routing, startup initialization and validation, runtime hot reload, virtual model ID, explicit/automatic language routing, installed-model filtering, request diagnostics metadata, and removal of the default-model mechanism are complete. Unit, API, and real-inference tests were added. First-release requests still select one model each; mixed-language chunked synthesis is not included.

### Future Core Capability: Streaming Speech Processing (Transcription, Translation Subtitles, and Interpretation)

**Goal:** Provide a session capability that continuously receives audio and returns incremental results in three modes:

1. **Transcription in the source language (speech → text):** Display source-language text while audio arrives, for same-language subtitles. Distinguish revisable partial results from committed final results.
2. **Translated subtitles (speech → target-language text):** Return target-language text while source-language audio arrives. Also return the source transcript when useful for bilingual subtitles, diagnostics, and source/translation alignment.
3. **Spoken interpretation in the target language (speech → target-language speech):** Return target-language text and chunked synthesized audio. Send only committed, stable translations to TTS because already-played audio cannot be retracted.

**Overall architecture:** Use streaming sessions as the product/protocol boundary rather than turning the existing full-file transcription endpoint into a long-lived connection. A session receives and validates audio, maintains recognition state, manages buffering/backpressure/cancellation, and runs streaming ASR, translation, and optional TTS according to the output mode. Support a logical ASR → translation → TTS cascade while also allowing an adapter for an end-to-end speech translation model. Model implementation details must not leak into the session contract. Keep `POST /v1/audio/transcriptions` for full-file transcription. A WebSocket extension API is recommended for real-time bidirectional audio and results; freeze its path and event schema during detailed design.

**Session and event contract:** Session creation declares mode, source language (explicit or auto), required target language for translation, whether to return source text, and model/routing selection. Server events should include session-ready (with actual processing chain), source-language partial/final, target translation partial/final, target audio chunks linked to translations, utterance end, session completion, and error. Events carry session ID, utterance/segment ID, language, finality, and available time ranges. Link translations to source segments and audio chunks to translation segments. Define audio chunk format/sample rate/channels, finish/cancel, disconnect cleanup, heartbeat/idle timeout, recoverability, input/output backpressure, queue limits, and slow-client behavior.

**Incremental commitment and revision:** Streaming recognition may change as new audio arrives, and partial translations may change when the source text is revised. Define translation stabilization/commit policy, permitted edits to text translations, and when a translation becomes final. Speech mode speaks only committed segments. Balance translation trigger frequency, acceptable revision rate, utterance endpointing, context, quality, latency, and compute cost. Do not trigger unlimited retranslations or TTS for every ASR partial.

**VAD and endpointing:** VAD detects speech/silence regions for silence suppression or segmentation. Streaming ASR endpoint detection decides when the current utterance can be committed; these are different responsibilities. First evaluate endpoint detection built into online ASR; a separate VAD is not mandatory. Compare VAD as optional preprocessing/real-time enhancement, measuring missed quiet speech, truncation, noise false positives, and CPU overhead. Endpoint policy affects final recognition, translation stability, and TTS start time.

**Models, capabilities, and routing:** Whisper, SenseVoice, and Qwen3-ASR are currently cataloged as offline models; this does not establish streaming support. New model metadata must describe online/offline type, input/output modality, streaming support, source/target languages or language pairs, required files, platform/device requirements, and license. Streaming ASR, text translation, speech translation, and TTS may be provided by one model or a compatible model chain. Select a complete feasible chain by session mode and language pair, then pin actual models/policy for the session and report the selection. If automatic source-language detection cannot finish in time, define a waiting state or require an explicit language; never switch language/model silently after output has begun. Extend catalog, in-use model protection, and capability/runtime descriptions to represent these features.

**Concurrency, resources, and failures:** The existing queue serves finite one-shot requests. Calls sharing one sherpa-onnx runtime instance are serialized by its adapter lock, while independent runtime instances may proceed concurrently subject to queue limits. Long-lived WebSocket sessions must not occupy ordinary inference slots indefinitely, and audio chunks must not be queued without bounds. Design per-session recognizer state, fair scheduling, concurrent session quotas, model sharing/isolation, audio buffer/output queue limits, input backpressure, idle/maximum session lifetime, cancellation/disconnect cleanup, shutdown, overload, and TTS lag behavior. Measure end-to-end sustained real-time factor, queue latency, CPU/memory, and session count.

**Implementation and acceptance order:**

1. Freeze session semantics, event structure, language inputs, limits, and errors for the three modes. Establish target devices and representative corpora.
2. Evaluate sherpa-onnx online ASR candidates and platform compatibility. Measure first-partial latency, endpoint latency, WER/CER, RTF, resources, and licenses; build session infrastructure and implement mode 1.
3. Evaluate cascaded text translation and end-to-end speech-to-text translation. Measure quality, translation latency, and revision rate per language pair; implement mode 2.
4. Implement chunked TTS for committed translations, audio ordering/linkage, playback cancellation, and backlog control; implement mode 3.
5. Compare model endpoint detection against VAD+ASR separately. Add VAD as an optional capability/configuration only if it clearly improves quality or product experience.
6. Accept each mode separately: mode 1 by partial/final latency and recognition quality; mode 2 by translation latency/quality/revision rate/alignment; mode 3 by first-audio latency, playback order, and backlog. Jointly accept sustained real-time factor, concurrency, resource limits, timeouts, cancellation, disconnect cleanup, and privacy logging.

**Current status:** This is a future design/research plan only. Do not implement streaming APIs, models, or VAD in the current phase. Evaluate model candidates, language coverage, target-device performance, licenses, protocol path/schema, and latency/quality thresholds before implementation. Streaming APIs and a management UI remain outside the current release scope.

Continue expanding the model catalog in stages, prioritizing evaluation of larger parameter counts and non-INT8 precision to improve recognition and synthesis quality. Candidates include Whisper Small/Medium/Large for STT (approximately 244M/769M/1.55B parameters; start by validating Medium) and Kokoro-82M for TTS. Also evaluate FP32/FP16 or other non-INT8 weights for existing Whisper, Piper, and other models. For every candidate, confirm sherpa-onnx/target backend compatibility, Windows CPU operation, language coverage, license, and source. Compare quality, latency, RTF, memory, and disk usage on a shared corpus and hardware; do not assume larger parameter count or unquantized precision is automatically better. Add models to the catalog and lifecycle in batches only after evaluation.

Also evaluate additional languages/models, mirror providers, a lightweight management UI, and macOS/Linux/Android adapters. Mobile may use an embedded SDK and does not require a permanently running Web service. See the “Streaming Speech Processing” section in this phase for its detailed plan.

## Current Repository Structure

```text
SmartVoice/
├─ README.md
├─ LICENSE
├─ pyproject.toml                 # Python is currently selected; keep dependencies lean
├─ doc/
│  ├─ requirements-spec.md
│  ├─ industry-research.md
│  └─ implementation-plan.md
├─ catalog/models.json             # Pinned HTTPS sources and model file inventory
├─ src/smartvoice/
│  ├─ __main__.py                  # Local API and model CLI
│  ├─ app.py
│  ├─ api/v1/routes.py
│  ├─ config/settings.py
│  ├─ domain/
│  ├─ ports/inference.py
│  ├─ adapters/inference/sherpa_onnx/
│  └─ services/model_catalog.py
├─ tests/
│  ├─ test_api.py
│  ├─ test_model_catalog.py
│  └─ test_real_inference.py       # Runs after models are installed; otherwise skipped
├─ benchmarks/
│  ├─ runner.py                    # Quality/performance/concurrency comparison
│  ├─ config/                      # Tracked suite and empty manifest templates
│  └─ tests/                       # Benchmark-specific tests
└─ examples/                       # Product usage examples
```

Model weights live in the user data directory and are not checked into Git. Further splits of API schemas, platform services, runtime diagnostics, and model task management should follow module growth.

## Acceptance Method

### STT Quality and Performance

Maintain fixed, manually transcribed Chinese/English corpora with clear licensing. Cover accents, noise, far-field speech, proper nouns, Chinese/English mixing, silence, corruption, and long recordings. Run the complete corpus against the same model version and save each reference, recognition result, and environment details.

- Report English WER and Chinese CER. Separately report errors for key entities such as numbers and proper nouns. Use fixed text normalization.
- Record cold start, inference time, RTF, peak RAM/VRAM, and actual device.
- JiWER may be used for WER/CER; define stable Chinese normalization and tokenization rules.

### TTS Quality and Performance

Use a fixed Chinese/English text set covering numbers, punctuation, abbreviations, polyphonic characters, proper nouns, and long text. Save generated WAV files and runtime data.

- Use blinded human MOS/pairwise preference as the primary quality judgment; separately record naturalness, intelligibility, pronunciation, and prosody.
- DNSMOS and UTMOS may be used for automated regression screening. TTSDS may be used for periodic multidimensional model comparisons. Automated predicted scores are not the sole measure of user-perceived quality.
- Recognition of TTS output by an independent STT model may help assess intelligibility, but is affected by that STT model's own errors.
- Record synthesis time, time to first audio, RTF, peak RAM/VRAM, sample rate, and audio duration. Do not use PESQ/STOI as the primary metric for unpaired TTS audio.

### API, Reliability, Offline Operation, and Privacy

- Check normal requests, invalid fields, error codes, cancellation/timeouts, queue limits, and resource cleanup against the OpenAPI contract.
- Cover disk exhaustion, interrupted downloads, hash mismatch, missing models, insufficient VRAM, and device fallback.
- Run installed-model inference offline. Check default loopback binding, log redaction, and temporary audio cleanup.
- Verify adapter consistency with two providers/contract substitutes and confirm that client contracts do not change with the backend.

Set all quality/performance thresholds in Phase 0 using reference hardware and benchmark corpora. Each report records model revision/hash, corpus version, normalization rules, hardware, driver, and runtime versions.

## Architecture Boundary Improvements (Phases 1–4 Complete)

- `domain` now owns neutral contracts for model metadata, installed models, transcription results, and language-identification results. Inference, model repository, optional language identification, and load status are declared through `ports`.
- STT/TTS model validation, language decisions, automatic routing, and fallback have moved into application services; HTTP routes map requests/responses. API and CLI share model-management use cases.
- The sherpa-onnx provider reads specifications and paths through the model repository; host/process diagnostics belong to the platform adapter. Source catalog metadata is copied into package resources during wheel builds, so installed operation does not require a repository checkout.
- Language-identification assets are optional: service startup does not download them. `/v1/capabilities` reports availability; users can explicitly install them with `models install-language-id`. If assets are missing, fall back to an STT model that supports automatic detection.

## Current Code-phase Acceptance

- The Windows x64 CPU source vertical slice provides health/task readiness, runtime/capability/model queries, Chinese/English file STT and WAV TTS, WAV/MP3/FLAC/M4A decoding, request IDs, field-level errors, redacted diagnostics, bounded inference queues, and STT/TTS input/output limits.
- Windows host and process metrics are available through runtime, response headers, and logs, including CPU/memory, process working set/peak working set, and per-request CPU time. Default inference concurrency is 1; queue capacity and timeouts are configurable.
- Real-model tests cover Chinese/English STT/TTS, four input audio containers, corrupt audio, and long-text chunking. Performance scripts record cold/warm latency, RTF, CPU time, normalized CPU utilization, process memory, and resident peaks after loading task/language paths in one process.
- Historical single-machine measurements are in [Windows CPU settings](../benchmarks/config/windows-cpu.json) and the machine-readable [JSON report](../benchmarks/result/windows-cpu-ryzen-ai-9-hx-370.json). These results come from a Ryzen AI 9 HX 370; they are not a minimum-specification commitment or acceptance SLA and should not be directly compared with reports using the new benchmark schema.
- Model lifecycle operations are available through CLI and REST: activation/deactivation by task, in-use removal protection, background download progress/cancellation, Range resume, offline ZIP import/export, and disk usage. Inference timeout returns 504. Native inference threads cannot be forcibly stopped, so the thread continues to occupy an inference slot until it finishes; this prevents additional inference from overcommitting resources after a timeout.
- Configuration supports JSON files, environment-variable overrides, and key CLI startup overrides. Startup reports the data directory/service address and can detect an existing service instance.
- Important acceptance gaps remain: there is no approved, manually transcribed representative Chinese/English STT corpus, so reliable CER/WER and formal quality thresholds are unavailable; TTS has no fixed listening set or blinded human evaluation; performance SLAs have not been set on the final reference hardware. TTS `language` is an expected-language hint. The service checks conflicts against the dominant character script, while actual pronunciation is determined by the text in the bilingual model.
- Earlier Windows CPU validation used the SenseVoice/Melo sherpa-onnx path; Melo is no longer supported by the current catalog. GPU, other operating systems, broader model/backend verification, remote authentication, streaming APIs, and a management UI remain for later phases and are outside the current Windows CPU target.
