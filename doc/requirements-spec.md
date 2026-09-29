# SmartVoice Local STT/TTS Service: Requirements Specification

- **Version:** 0.1 (post-research requirements baseline)
- **Date:** 2026-09-27
- **Status:** Revised after the user's goal update on 2026-09-27
- **Related document:** [Industry Research](industry-research.md)

## 0. Current Product Goals

This section supersedes conflicting goals in earlier versions of this document.

1. **Lightweight and edge-first:** Target resource-constrained devices, including PCs and smartphones. Evaluate lightweight operation using application and model size, resident RAM/VRAM, time to first output, and energy use. Parameter count below 7B alone does not establish that a model is lightweight.
2. **Multi-platform evolution:** Target Windows, macOS, Linux, and Android; the first release targets Windows only. Future platforms should share API and capability definitions while adapting inference backends, model formats, and hardware acceleration as needed.
3. **Multilingual support:** Support Chinese, English, and extensible additional languages. Chinese and English are the first-release priorities. Declare language coverage per STT/TTS model; no individual model is required to support every language.
4. **Agent-friendly Web API:** Provide a stable HTTP API described with OpenAPI. Prefer common OpenAI Audio API conventions for STT/TTS paths and fields. Provide an MCP server as an Agent tool adapter if needed.
5. **Frontend/backend decoupling:** Frontends and API clients depend on stable, versioned public contracts. The server can replace inference backends, model formats, or runtimes without requiring changes to frontend interactions or API usage.
6. **Platform and hardware decoupling:** Core business logic is independent of operating systems, CPU/GPU vendors, and hardware architectures. Platform/device adapters handle those differences. Windows is the first implementation target; macOS, Linux, and Android are future targets.
7. **Framework positioning:** sherpa-onnx is one candidate inference engine. Evaluate its gaps against this product's API, model management, and installation requirements; do not assume it alone provides the complete product.

These goals take precedence over conflicting older goals such as launching on Windows and Linux together, treating 7B model selection as the primary target, or making OpenAI-style APIs the only interface strategy.

## 1. Background and Problem Statement

Users need offline speech recognition and synthesis on ordinary personal computers. Today they often install separate STT and TTS projects and manage different Python/CUDA environments, model repositories, downloads, and APIs. A single, simple local service should provide both capabilities.

Mature components already provide many of the underlying capabilities, and sherpa-onnx itself supports both kinds of inference. SmartVoice should focus on organizing these components into an installable, manageable, unified, and auditable desktop/local service.

## 2. Product Goals

1. Provide a single-machine local STT and TTS service that can perform inference fully offline.
2. Support CPU inference and, when compatible hardware and software are available, GPU acceleration; report the device actually used.
3. Support mainstream small models verified by the project. The `<7B` catalog limit is not a hardware-compatibility promise. Each model must include minimum recommended RAM/VRAM and performance benchmarks.
4. Provide a browsable candidate model catalog with downloads from public sources, verification, installation, switching, removal, and offline import.
5. Provide other applications and Agents with a stable local Web API described by OpenAPI and compatible with common audio API conventions.
6. Respect licenses for models, code, and voice data, with local privacy as the default.

## 3. Non-goals

The first release does not include model training/fine-tuning, hosted cloud APIs, accounts or billing, multi-machine scheduling, a general-purpose arbitrary model runner, real-time interpretation/translation, or a full browser UI. Do not promise real-time speed for every model on ordinary CPUs or out-of-the-box support for arbitrary GPUs and drivers.

## 4. Users and Main Scenarios

- **Individual users:** Download the application and perform local Chinese/English transcription and text-to-speech on a CPU.
- **GPU users:** Install/select a compatible CUDA runtime, choose higher-quality models, and verify that tasks actually execute on the GPU.
- **Developers:** Integrate speech input, read-aloud, and transcription batch processing through a local REST API.
- **Users on restricted networks:** Download from an accessible public model source or import an offline model package, then continue inference without a network connection.

Typical flow: install the application → choose CPU/GPU at first launch → review model cards, licenses, storage, and devices in the model catalog → download models → call the STT/TTS API → check status, switch models, or remove models.

## 5. Architecture Constraints and Extension Boundaries

### 5.1 Frontend and Inference Backend Contracts

Frontends include the Web management UI, CLI, desktop shell, and external Agents/clients. They communicate with the service only through versioned public HTTP APIs and, if introduced, an explicitly versioned real-time audio WebSocket protocol. They must not link directly to inference frameworks, access model files, or depend on runtime-specific parameters.

The server defines stable domain contracts and backend adapter interfaces. Adapters map unified requests to specific model/runtime calls and map results, errors, cancellation, and capability descriptions back to unified structures. Replacing sherpa-onnx, ONNX Runtime, another native engine, or adding a model format should not change frontend APIs or interactions. Update clients only when public capabilities intentionally change through versioned contracts.

Adapters should abstract at least model loading/unloading, STT, TTS, streaming capability, language/voice, input/output formats, device selection, cancellation, health checks, and error classification. Frontends use capability queries and must not infer functionality from backend names. Optional capabilities such as language identification must report availability and reasons for unavailability. Service startup must not connect to the network implicitly to prepare optional models.

### 5.2 Platform and Hardware Interfaces

The core domain must not call Windows-specific APIs, POSIX APIs, CUDA APIs, audio device APIs, or CPU-specific instructions directly. Interfaces should provide access to the application data directory, configuration/logging, process lifecycle, audio codecs, device enumeration, compute-device detection, resource status, model files, and platform service startup.

Represent runtime devices uniformly as `auto`, `cpu`, and extensible device descriptors, such as CUDA GPU, DirectML, Core ML, or Android NNAPI. Runtime/provider plugins report device capabilities; business logic must not hard-code NVIDIA/CUDA branches. If a platform does not support a provider, capability queries report unavailability and the reason. CPU fallback must be explicit and observable.

The first phase needs only Windows platform adapters and verified CPU/GPU providers. However, core code, model metadata, API schemas, task scheduling, and model management must not encode Windows- or chip-specific assumptions in the domain. Each future platform should add adapters and compatibility validation rather than require rewriting the API or business core.

Mobile platforms such as Android may use an embedded SDK/API rather than a permanently running Web service. They should reuse domain contracts, model capability descriptions, and task semantics; all platforms do not need the same process shape.

## 6. Scope and Priorities

### P0: First Usable Release

- Windows x64 is the only first-phase platform. CPU support is required; GPU acceleration is optional and limited to the verified compatibility matrix. Core logic must use platform adapter interfaces. macOS, Linux, and Android are future targets.
- Single-user local REST API, bound to loopback by default. Frontends depend only on the public API and are decoupled from inference runtimes.
- File-based STT for common WAV/MP3/M4A/FLAC formats, as verified by the decoder. Acceptance prioritizes Chinese and English while allowing future languages in the model catalog.
- Non-streaming TTS prioritizing Chinese and English, returning WAV. Sample rate, channels, and model voice must be explicit. Other languages may be added through the catalog.
- `GET /health`, `GET /v1/models`, `POST /v1/audio/transcriptions`, and `POST /v1/audio/speech`.
- The default model catalog includes at least one lightweight STT model and one lightweight TTS model. Prefer sherpa-onnx CPU candidates and add alternatives such as FunASR or CosyVoice based on measured Chinese quality.
- Model download/cancellation/resume or failure recovery, verification, installation, activation, removal, disk-space guidance, and offline import.
- Automatic and explicit CPU/GPU selection; the actual device is queryable.
- README/API documentation, model-license details, and network requirements.

### P1: Extensions

- Optional GPU acceleration on Windows, released against a verified GPU/driver/runtime matrix.
- Real-time streaming STT only for models that declare streaming support, with explicit interim/final results over WebSocket or chunked HTTP.
- Multilingual models, timestamps, VAD, speaker segmentation/labels, punctuation, and hotwords according to backend support.
- Higher-quality TTS, multiple speakers, and style parameters. Reference-audio cloning remains unavailable until licensing, authorization, and privacy requirements are addressed.
- Mirror providers such as ModelScope, model package import/export, a system tray application, or a lightweight Web management UI.

### P2: Future Exploration

- On-device macOS, Linux, and Android support; Apple Silicon, AMD GPU, DirectML/Vulkan backends; concurrent services, multi-user authorization, plugin SDKs, and automatic performance tuning. For Android, first evaluate embedded SDK/JNI and in-app use; account for mobile background restrictions before providing localhost Web services.

## 7. Functional Requirements

### 7.1 Installation, Startup, and Local Service

- **FR-001** Installation must not require users to preconfigure Python, a compiler, or a GPU environment. If the first release uses Python distribution, bundle/isolate a supported runtime and record its version.
- **FR-002** First launch reports CPU/GPU detection, service address, and model catalog path. The service can continue in CPU mode when a GPU is incompatible.
- **FR-003** Bind only to `127.0.0.1`/`::1` by default; do not expose the LAN by default. Explicit remote listening must show a risk notice and support API tokens.
- **FR-004** On shutdown, release models and ports. On duplicate startup, report the existing instance rather than silently overwriting configuration.
- **FR-005** All settings can be changed through configuration or CLI. Logs must not contain full recognition audio or synthesis text; redact by default.
- **FR-006** Frontends may depend only on versioned public API/capability contracts, not backend SDKs, internal types, model catalog layout, or private runtime parameters. Switching backends must not require frontend changes.
- **FR-007** Core business logic accesses filesystem paths, configuration, logging, devices, lifecycle, and audio I/O through platform service interfaces. Windows APIs and hardware-vendor APIs are restricted to their respective adapters.
- **FR-008** Platform adapters/providers report supported capabilities, versions, devices, and failure reasons. The service must not hide runtime fallback as successful execution on the requested device.

### 7.2 Model Catalog and Lifecycle

- **FR-010** Each catalog entry displays its name, task, architecture/parameter count when known, supported languages, streaming capability, precision/quantization, backend, download size, installed size, recommended RAM/VRAM, source and pinned revision, license, and known limitations.
- **FR-011** Sources are restricted to HTTPS and local files. Catalog definitions may be updated, but runtime behavior must not execute repository scripts or arbitrary code embedded in models. Prefer supported safer formats such as safetensors/ONNX. Models requiring `trust_remote_code` need separate review and are disabled by default.
- **FR-012** Download to a temporary directory and atomically install only after file size and hash checks succeed. Support cancellation, resume, retries, optional rate limits, readable errors, and cleanup of invalid partial downloads.
- **FR-013** Models can be listed, installed, activated, deactivated, and removed by task. Before removal, show the path and space to be reclaimed. Do not remove files currently in use.
- **FR-014** Provide offline model import and catalog export manifests. Imports still validate format, paths, hashes, and license metadata.
- **FR-015** Model loading failures report the model ID, backend, device, stage, and actionable user guidance without exposing secrets or sensitive fragments of private paths.
- **FR-016** Loading multiple models simultaneously is optional. The minimum first-release requirement is one configurable active model per task. Model switching reports loading state and preserves the previous active model if loading fails.

### 7.3 STT

- **FR-020** Support audio file uploads and an optional local file path mode (disabled by default to prevent path traversal). Document accepted formats, maximum size, duration, and sample rate in the API.
- **FR-021** Requests may specify model, language (`auto` or a language code), response format, timestamps, and optional domain parameters. Return 4xx for unknown parameters; do not silently ignore important settings.
- **FR-022** Responses include text, model ID, detected/requested language, and audio duration. Include segment timestamps, confidence, and speaker labels only when supported. Do not fabricate missing fields.
- **FR-023** Long-file chunking/VAD strategies must be bounded and documented. Release temporary files after timeout or cancellation.
- **FR-024** Streaming is available only for models that declare streaming capability. Distinguish partial/final output and include sequence numbers. Clean up sessions after disconnection.

### 7.4 TTS

- **FR-030** Accept text, model, voice, language, format, sample rate, and model-supported speed/style parameters. Return readable errors for unsupported parameters.
- **FR-031** Return audio that can be saved or played directly; WAV is the first-release format. Validate additional formats separately. Provide content type, duration, sample rate, and model metadata.
- **FR-032** Bound long text by character count, sentence splitting policy, output duration, and file size. Explain how to split requests that exceed limits.
- **FR-033** Provide queries for model-supported voices. Record each preset voice's source/license with its model.
- **FR-034** Reference-audio/cloning capabilities remain disabled until model licensing, speaker authorization, storage cleanup, and abuse controls are approved.

### 7.5 Status, Configuration, and Observability

- **FR-040** Provide endpoints for liveness/readiness, versions, runtime versions, device detection, installed/active models, and resource status.
- **FR-041** Request logs include request ID, duration, status code, model, and actual device. Do not log text, audio, reference audio, or tokens by default.
- **FR-042** Configure model catalog, cache path, log level, CPU thread count, device, inference queue, and API bind address.
- **FR-043** GPU initialization errors are diagnosable (GPU not found, driver mismatch, missing runtime, insufficient VRAM, unsupported model). Allow users to explicitly retry on CPU.

## 8. API Draft

### `POST /v1/audio/transcriptions`

`multipart/form-data` fields: `file` (required), `model`, `language`, `response_format=json|text|verbose_json`, and `timestamps`.

Example JSON response:

```json
{
  "text": "The meeting has started today.",
  "language": "en",
  "duration": 2.84,
  "model": "stt-sensevoice-small-int8",
  "device": "cpu",
  "segments": []
}
```

### `POST /v1/audio/speech`

JSON fields: `input` (required), `model`, `voice`, `language`, `response_format`, and optional `speed`.

Return binary audio. Errors use a stable JSON error structure and request ID.

### Management Endpoints (Project Extensions)

- `GET /health`: service liveness.
- `GET /ready`: API readiness and model status.
- `GET /v1/models`: locally installed models and capabilities.
- `GET /v1/catalog`: verified installable catalog, including revision, license, resources, and source.
- `POST /v1/models/{id}/download`, `DELETE /v1/models/{id}`, `PUT /v1/models/{id}/activate`.
- `GET /v1/runtime`: CPU/GPU/runtime details and actual selection.
- Long-running tasks such as downloads may return a `job_id`; query their status with `GET /v1/jobs/{id}`.

The API must provide OpenAPI documentation, size/time limits, error codes, file cleanup behavior, and version compatibility rules. Download APIs must not accept arbitrary URLs, to prevent SSRF; they may select only catalog-registered sources or local files for import.

## 9. Non-functional Requirements and Acceptance Metrics

### 9.1 Performance and Resources

- **NFR-001 CPU availability:** On the published minimum reference machine, STT and TTS complete end-to-end tasks without a GPU. Publish real-time factor (processing seconds/audio seconds), TTFA (time to first audio), and memory peak per model.
- **NFR-002 GPU verification:** Within the declared compatibility matrix, status/logs prove that execution used a GPU. Outside the matrix, reject the request or explicitly report CPU fallback.
- **NFR-003** For every catalog model, record startup time, throughput, peak RAM/VRAM, model size, and quality results on at least one reference CPU and one reference NVIDIA GPU. Publish hardware, driver, and software versions with the report.
- **NFR-004** Avoid vague "real-time on ordinary PCs" claims. Product must define minimum specifications and acceptable RTF/latency thresholds before release.
- **NFR-005** Bound individual requests, queues, and concurrent resource use. Model unload should release GPU/CPU memory or provide restart guidance.

### 9.2 Reliability, Security, and Privacy

- **NFR-010** Inference requests do not require internet access. Installed models can complete STT/TTS while offline.
- **NFR-011** Bind to loopback by default with no audio/text telemetry. LAN listening requires explicit configuration and authentication.
- **NFR-012** Limit upload size, duration, concurrency, and temporary directory access. Prevent arbitrary path reads, arbitrary URL downloads, directory traversal, and resource exhaustion.
- **NFR-013** Verify TLS, pin revisions, and check file hashes for model downloads. Installation must not execute untrusted code from model repositories.
- **NFR-014** Temporary audio cleanup is configurable and defaults to deletion after a request. Request content is not retained in logs by default.
- **NFR-015** Model licenses, sources, and versions can be exported from the UI/API. Include third-party notices in distributions.

### 9.3 Maintainability and Extensibility

- **NFR-020** Version API schemas. Version adapter capability schemas and define whether unknown fields are rejected or ignored.
- **NFR-021** Decouple catalog data from service versions, while validating schema, hashes, and backend compatibility on every load.
- **NFR-022** Adding an adapter must not require public API changes. Adapters use consistent exception types, log fields, and cancellation semantics.
- **NFR-023** Support configuration directory migration and backup. Upgrades must not automatically delete models or overwrite user configuration.
- **NFR-024** Build and run core domain modules without loading Windows- or GPU-specific adapters. Adding platforms/providers should primarily require interface implementations and compatibility declarations, not changes to API/domain contracts.

## 10. Initial Model Catalog Recommendations

Initial catalog entries are candidates, not promised support. Mark a model "verified" only after testing a pinned version and completing license review.

| ID | Task | Candidate | Backend direction | Initial grouping |
|---|---|---|---|---|
| `stt-sensevoice-small-int8` | STT | SenseVoiceSmall | Native FunASR or corresponding sherpa-onnx ONNX package | Lightweight Chinese-first candidate |
| `paraformer-zh-local` | STT | Chinese Paraformer | FunASR or a compatible sherpa model | Chinese alternative |
| `whisper-small-local` | STT | Whisper small/base | faster-whisper or whisper.cpp | Optional multilingual model |
| `piper-voice-*` | TTS | Piper voice with reviewed license | sherpa-onnx/Piper | Lightweight CPU candidate |
| `kokoro-local` | TTS | Kokoro with a pinned ONNX conversion | ONNX Runtime/sherpa adapter (support must be confirmed) | Lightweight naturalness candidate |
| `cosyvoice2-0.5b-local` | TTS | CosyVoice 2 0.5B | PyTorch adapter | Optional higher-quality model |
| `qwen3-tts-0.6b-local` | TTS | Qwen3-TTS 0.6B submodel | PyTorch/official inference stack | Optional higher-quality model |

Catalog review must record the source and license for each individual model file. Record parameter count as metadata, not as the sole lightweight acceptance threshold. Also record model/dependency size, cold-start time, peak memory, latency, energy use when measurable, and platform compatibility.

## 11. Acceptance Criteria

Before the first release, meet at least the following:

1. Install and start in a clean Windows reference environment; complete STT and TTS without a GPU. Future platforms have separate acceptance matrices and do not block the first phase.
2. Use contract tests or substitutes for at least two different backends/providers to verify that switching inference backends does not require frontend/API client changes.
3. Build/run the core domain without direct dependencies on Windows or GPU-vendor APIs; keep platform-specific dependencies in adapters.
4. Show source, license, size, and progress when downloading catalog models. A hash mismatch must never produce an available model. Inference must still work offline after download.
5. Prove that requests execute on a GPU in a supported GPU environment; CPU mode must still work when the GPU is disabled or removed.
6. Call REST schemas, error codes, file limits, and examples as documented. Cancellation, timeouts, and inference queues reclaim resources as specified.
7. Cover Chinese/English and initial extension languages, silent/corrupt/oversized audio, empty/oversized text, missing models, insufficient disk space, network interruption, insufficient VRAM, and device fallback.
8. Confirm that bind addresses, logs, and temporary audio follow the default privacy policy.
9. Every model marked "verified" has quality examples, performance benchmarks, source revision, hashes, license, and hardware requirements.

## 12. Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| One lightweight model cannot cover Chinese quality, multilingual use, and streaming goals | Users may assume coverage is complete | Drive selection with capabilities; distinguish lightweight, high-quality, and streaming capabilities; group the catalog clearly. |
| PyTorch/CUDA dependency conflicts and distribution size | Installation failure and high maintenance cost | Keep the CPU core independent; offer CUDA/high-quality backends as optional components and publish a verified matrix. |
| Hub unavailable or downloads interrupted | Failed first-use experience | Support multiple providers, mirrors, resume, and offline import; do not bundle large weights. |
| Unclear model licenses or voice rights | Legal and distribution risk | Review each model; show licenses for catalog listings and automatic downloads; exclude models with unclear terms by default. |
| Parameter count does not predict resource needs | OOM or slow performance | Use measured memory, RTF, latency, and minimum hardware; treat parameter count only as a screening attribute. |
| Unauthorized voice cloning misuse | Personal-rights and abuse risk | Keep cloning disabled in the first release; review explicit consent, retention, and abuse governance separately before enabling it. |
| Model format versions change | Upgrades may break user environments | Pin revisions/hashes, regression-test verified combinations, and version catalogs/adapters. |

## 13. Product Decisions Required

The following questions remain open before implementation; the product owner should confirm them:

1. What are the minimum supported Windows version and architecture? Is x64 the only target?
2. Beyond Chinese and English, which languages must pass first-release acceptance?
3. What are the minimum reference CPU, RAM, disk, and GPU/VRAM specifications?
4. What are the performance targets: maximum file STT duration and acceptable RTF; TTS time to first audio and long-text throughput?
5. Which OpenAI Audio API fields/response formats must be compatible? Should an MCP server ship in the first release?
6. Does first-phase GPU support require only NVIDIA CUDA or also AMD/Intel?
7. What model license scope is allowed? Must every catalog model permit commercial use/redistribution?
8. Which model sources are mandatory at launch: Hugging Face, ModelScope, configurable mirrors, or local import?
9. What UI is required: CLI/REST only, or a desktop tray/model management Web UI in the first release?
10. Is a Python CPU core plus optional larger GPU runtime acceptable as a multi-component installation strategy?

## 14. Recommended Next Steps

Have the product owner resolve the constraints in Section 13, then pin the first Windows STT/TTS checkpoints/revisions and target hardware. Measure resource use, quality, and latency on a reference PC; complete license review and OpenAPI/MCP scope decisions; then produce an implementation design and future-platform compatibility matrix before prototyping.
