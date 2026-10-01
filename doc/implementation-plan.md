# SmartVoice Implementation Plan

**Last reviewed:** 2026-10-01

This plan reflects the current code and product scope. It separates the implemented local-service baseline from the next P1 capability and the P2 verification and expansion work. For architecture principles, implementation summary, and the centralized backend/platform compatibility matrix, see [architecture-guidelines.md](architecture-guidelines.md); for HTTP behavior, see [api-spec.md](api-spec.md); for test commands, see [testing.md](testing.md).

## Priority Definitions

- **P0 — Current baseline:** The basic local speech service and its architecture are implemented. There are no known uncompleted P0 implementation items in this plan.
- **P1 — Next product capability:** Streaming STT, translated subtitles, and spoken interpretation.
- **P2 — Acceptance and expansion:** Formal quality/performance acceptance, verification of the Windows/Linux model-platform matrix, and remaining platform, runtime, security, packaging, and product extensions.

P2 work is planned but is not a promise that every item will ship in the next release. Confirm platform targets, measurable thresholds, and release scope before starting an item.

## Current Implemented Baseline

The current product is a local CPU speech service. It exposes REST endpoints for health/readiness, runtime/capability and model discovery, model catalog/lifecycle, file transcription, and WAV synthesis. STT and TTS use the `smartvoice-auto` virtual model for language-aware routing or a concrete installed model ID for direct calls. Routing configuration is validated and can be reloaded while the service runs.

The code is divided into API, application services, domain contracts, ports, and platform/inference/storage adapters. Model lifecycle includes catalog-backed installation jobs, cancellation and download resume, model file verification, removal, and verified offline package import/export. The service defaults to loopback, bounds inference requests, and reports runtime and request diagnostics.

The regression suite runs functional/API tests and real CPU inference via REST routes. Its routed integration cases exercise service/model discovery, routed and direct STT, Chinese/English speech-language detection, supported input audio formats, invalid audio, routed/direct TTS, automatic TTS language detection, and long-text synthesis when their required models/assets are installed. It also generates a direct smoke test for every catalog model and runs it for each installed model whose runtime is available; absent models are reported as skips. Run `python scripts/test.py ci` for CI and `python scripts/test.py regression` for local regression with SmartVoice installed and any desired models present.

Current platform and backend compatibility is summarized in [architecture-guidelines.md](architecture-guidelines.md); installation state and current inference availability are separate. Source-install paths and release wheel builds cover Windows x64, macOS Apple Silicon, and Linux x86_64. Linux x86_64 has passed clean installation and model-backed inference validation on Ubuntu 26.04. Other Linux distributions and Linux arm64 are not yet verified. Android is not supported. GPU inference, streaming sessions, remote/LAN access, and a management UI are not part of the current baseline.

## P1 — Streaming STT, Translation, and Interpretation

### Goal and modes

Add a session-oriented capability that continuously receives audio and returns incremental results in three modes:

1. **Streaming transcription:** Return partial and final text in the source language.
2. **Translated subtitles:** Return translated text, with optional source transcript and source/translation alignment.
3. **Spoken interpretation:** Return translated text and ordered, chunked synthesized audio. Speak only committed translations that are stable enough to avoid retracting already-played speech.

Keep the existing file-based `POST /v1/audio/transcriptions` behavior. Streaming is a separate session API and must not change the semantics of existing one-shot endpoints.

### Current code boundary and required extension

The current implementation already has useful seams: HTTP routes call application services; `TranscriptionService` owns one-shot model selection and recognition orchestration; `InferenceProvider` and `ModelRepository` are provider-neutral ports; catalog entries define installed model assets and task/language metadata; provider capabilities report runtime support. The current `InferenceProvider.transcribe()` accepts a complete `bytes` payload and returns a completed `TranscriptionResult`. It does not expose a live stream lifecycle, incremental events, translation, or streaming TTS. The current `TaskName`/`TaskCapability` model represents only transcription and speech synthesis, while `TranscriptionService.execute()` and the ordinary `InferenceQueue` are one-shot flows.

Do not retrofit live sessions by repeatedly invoking file transcription on partial buffers, or by holding a normal `InferenceQueue` permit for the lifetime of a WebSocket. Add a focused streaming-session application service and a streaming inference port only after confirming the selected runtime/model behavior. The port should express business operations such as opening a configured stream, accepting normalized audio frames, polling incremental recognition/translation/audio results, finishing/canceling, and releasing resources; keep sherpa-onnx objects and model-specific frame/decode calls in its adapter. Translation and streaming synthesis should likewise be exposed through replaceable capability boundaries when the selected implementation requires separate models. Reuse the existing model repository and validated catalog lifecycle where possible, extending their metadata/schema rather than creating a parallel unverified installation path. Keep HTTP/WebSocket parsing and event serialization in the API layer; session policy, routing, partial/final commitment, and pipeline coordination belong in application/domain contracts.

Extend typed domain contracts and capability discovery to represent session modes, input/output modalities, source/target language or language-pair support, streaming/interim/final behavior, and why a mode is unavailable. Do not infer these features from `model_type`, backend name, or catalog ID. Preserve one-shot `TranscriptionService` behavior and existing REST compatibility.

### Work sequence

1. **Freeze the session contract.** Define session creation fields, modes, source/target languages, model selection, partial/final semantics, event envelopes, timestamps, audio format, errors, finish/cancel, disconnect behavior, and versioning. Use a WebSocket API unless implementation discovery establishes a concrete reason to choose another transport.
2. **Validate the model chain and runtime integration.** Evaluate streaming ASR, translation, and TTS options for each initial language pair, including model files, licensing, runtime API/thread-safety, Windows/macOS support, model size and actual target-device performance. A mode may use a cascade or a single end-to-end model, but model/runtime details must remain behind adapters. Define capability metadata and validate that a complete chain is available before starting a session.
3. **Define streaming-specific application and adapter boundaries.** Add the session orchestrator and focused live-stream inference capability separately from the existing one-shot provider contract. Decide whether a runtime instance can safely serve multiple live streams, how a session maps to recognizer state, and whether model-level locks, per-stream execution or bounded micro-batching are required. Do not change ordinary inference concurrency defaults based on streaming assumptions.
4. **Build bounded session infrastructure.** Define per-session recognition state, fair scheduling, session quotas, audio buffering, backpressure, idle/maximum lifetime, output limits, cancellation, disconnect cleanup, service shutdown, overload, and slow-client handling. Long-lived sessions must not occupy the ordinary one-shot inference queue indefinitely.
5. **Implement the modes incrementally.** Start with source-language partial/final transcription. Add translated subtitles after model and language-pair validation. Add interpreted audio only after translation commitment, chunk ordering, cancellation, and backlog behavior are defined.
6. **Accept each mode independently.** Measure first-partial and final latency, translation revisions, first-audio latency, sustained real-time factor, WER/CER and translation quality, CPU/memory, session capacity, and behavior under cancellation, disconnect, overload, and shutdown.

Do not add VAD by default. First evaluate streaming ASR endpoint detection; add VAD only if measured recognition or user-experience results justify the added runtime and configuration.

### Session and event contract details

- Session creation declares the mode, source language (explicit or auto), target language when translation is enabled, whether source text should be returned, and model/routing selection. Pin the selected processing chain and policy for the lifetime of the session and report it in `session-ready`.
- Define server events for session readiness, source-language partial/final text, target-language partial/final translation, target audio chunks, utterance completion, session completion, and errors. Events carry session ID, utterance/segment ID, language, finality, and available time ranges.
- Link translated segments to their source segments and generated audio chunks to the translation segments they speak. Specify encoding, sample rate, channels, sequence/order, and how the client detects missing or completed chunks.
- Define session finish/cancel, heartbeat, idle and maximum lifetime, disconnect behavior, error envelope, recoverability, and protocol versioning. Set explicit limits for audio chunk size, duration, session count, and input/output buffering.
- Keep model and runtime specifics out of the public event schema. The session API expresses capabilities and output semantics; adapters own streaming recognizer, translator, and synthesizer objects.
- Keep a clean compatibility boundary: the existing synchronous one-shot provider methods and `/v1/audio/transcriptions`/`/v1/audio/speech` endpoints continue to serve finite requests. Streaming code must not place HTTP request/socket objects in application services or domain contracts, nor expose sherpa-onnx frame sizes, recognizer handles, or decoder state to clients.
- Treat model install/verification/uninstall as shared repository responsibilities. Sessions hold a model-use lease or equivalent protection so a model cannot be removed while a session is using it; routing/configuration changes affect new sessions only unless a separately defined behavior says otherwise.

### Incremental commitment, revision, and endpointing

Streaming recognition may revise partial text as more audio arrives, and a translation may change when its source transcript changes. Define when partial text can be edited, how a stable source segment is committed, how partial and final translations are distinguished, and when a translation is final. Spoken interpretation must only synthesize committed translations because already-played audio cannot be retracted.

Balance translation trigger frequency, revision rate, utterance endpointing, context, quality, latency, and compute cost. Do not run translation or TTS for every ASR partial. VAD and endpointing are separate: VAD detects speech/silence for segmentation or suppression, while ASR endpointing decides when an utterance is ready to commit. Evaluate built-in endpointing first. If optional VAD is tested, measure missed quiet speech, truncation, noise false positives, and CPU overhead.

### Streaming model capability and routing details

Current catalog entries for Whisper, SenseVoice, and Qwen3-ASR are offline models; catalog presence does not imply streaming support. Add explicit metadata for online/offline operation, input/output modality, streaming support, source/target language or language pairs, required assets, runtime/platform/device requirements, and license. Evaluate streaming ASR, text translation, speech translation, and TTS as a complete feasible chain for each mode and language pair. If auto language detection cannot finish within the session's latency budget, define a wait state or require an explicit source language; never silently change language or model after user-visible output begins.

### Streaming concurrency, resources, and failure semantics

The existing inference queue serves finite one-shot requests. A long-lived session must not occupy an ordinary request slot indefinitely, and incoming audio must not accumulate in an unbounded queue. Design per-session recognizer state, fair scheduling, concurrent-session quotas, model sharing/isolation, bounded audio buffers and output queues, input backpressure, slow-client behavior, and TTS lag/backlog handling. Define cancellation/disconnect cleanup, idle and maximum lifetime, overload responses, shutdown behavior, and what happens to committed versus uncommitted output when a failure occurs.

Measure first-partial and final latency, end-to-end sustained real-time factor, queue latency, CPU/memory, session capacity, and behavior under a fixed-rate load. For translation modes, also measure revision rate, alignment, translation quality, and language-pair latency. For speech output, measure time to first audio, playback order, cancellation, and backlog recovery.

### P1 completion criteria

- Each enabled mode has a versioned API/session contract and explicit capability reporting.
- Session limits, audio buffers, queues, and resource use are bounded and observable.
- Partial/final results and source/target/audio segments have stable IDs and alignment semantics.
- Disconnect, cancellation, timeout, overload, and shutdown clean up session work and resources.
- Each supported language pair and mode passes real-model integration tests and documented latency/quality acceptance on named reference hardware.

## P2 — Inference Concurrency and Runtime Safety

The application `InferenceQueue` controls request admission/queueing. Adapters control safe use of runtime instances; `num_threads` is compute parallelism for an inference call and does not define API request concurrency. Keep sherpa-onnx recognizers, streams, and TTS objects inside the adapter boundary.

The implementation currently separates cache initialization protection from per-runtime-instance inference protection. Calls sharing one recognizer or TTS instance remain serialized; independent instances may proceed concurrently when the admission limit permits. Continue validating same-instance safety for `OfflineRecognizer` and every TTS runtime type on target CPU platforms. If testing requires instance pools, compare their memory cost and model-load latency.

Use fixed-arrival-rate tests to compare throughput, tail latency, rejection rate, CPU, and peak RSS. Closed-loop tests alone can hide overload by reducing request generation as latency rises. Consider ASR `decode_streams()` micro-batching only as an opt-in research path; measure added batching latency and transcript equivalence before any production use. Keep defaults at `max_concurrent_inference=1` and `max_queued_inference=2` until target-device evidence supports a change.

## P2 — Formal Acceptance Baseline

Set a measurable acceptance baseline before making broad quality or performance claims:

- Name the reference Windows and Linux machines, OS versions, CPU architectures, available memory, audio devices where relevant, and runtime/compiler versions.
- Define supported input duration, cold-start and warm-request targets, STT/TTS latency, memory ceilings, throughput, and expected behavior under resource pressure.
- Define offline-operation, privacy, logging, data cleanup, and failure-recovery checks.
- Record the supported API subset and release boundaries, including whether packaging beyond source installation is required.

Do not turn exploratory results from one machine into minimum system requirements or SLAs. Record reproducible test conditions and distinguish hard acceptance thresholds from informational measurements.

## P2 — Voice Quality Acceptance

### STT

- Create an approved, manually transcribed Chinese/English test corpus with documented licensing. Cover accents, noise, far-field speech, names and numbers, code-switching, silence, corrupt files, and long recordings.
- Define stable text normalization and tokenization. Report English WER and Chinese CER, plus error rates for important entities such as names and numbers.
- Pin model revisions, corpus version, and environment for every evaluation.

### TTS

- Create a fixed Chinese/English prompt set covering numbers, punctuation, abbreviations, polyphonic characters, names, and long text.
- Use blinded human MOS or pairwise preference as the primary quality signal. Record naturalness, intelligibility, pronunciation, and prosody separately.
- Track synthesis time, first-audio latency, RTF, memory, sample rate, and duration. Automated metrics may screen regressions but must not replace human evaluation.

Publish quality thresholds only after the corpus, reference hardware, and measurement procedure are approved.

## P2 — Model and Platform Verification Matrix

Validate every model that the product advertises as available. For each model/runtime/platform combination, record source and pinned revision, hashes, license obligations, task and language coverage, required assets, install behavior, cold start, latency/RTF, peak memory, disk size, and real inference results. Model presence in the catalog alone is not evidence of platform support.

The verification matrix covers Windows x64, Linux x86_64 on Ubuntu 26.04, and the current macOS Apple Silicon path. Expand Linux distro or architecture claims only after validating them. Test actual API capability reporting so incompatible models are unavailable with a clear reason.

For Qwen3-TTS, preserve the native C INT8 CPU runtime across Windows, macOS, and validated Linux x86_64. Recent evaluation found no PyTorch CUDA configuration, including PyTorch INT8 quantization, that outperformed the native INT8 runtime in the tested conditions; CUDA support is therefore low priority for now. Revisit it only if new hardware, workload, or benchmark evidence shows a meaningful end-to-end benefit. Preserve platform-neutral API/model contracts and keep platform execution details behind adapters.

### Platform starting point

| Target | Current documented state | Remaining verification |
|---|---|---|
| Windows x64 | Source CPU service and native C INT8 Qwen3-TTS runtime are supported. | Clean-environment install, supported OS/CPU requirements, codec/runtime dependencies, model-by-model smoke tests, and release acceptance. |
| macOS Apple Silicon | Source CPU service and native C INT8 Qwen3-TTS runtime are supported. | Maintain build reproducibility and keep model, runtime, and quality results current as dependencies change. |
| Linux x86_64 | Source and wheel paths for sherpa-onnx and native Qwen3-TTS; clean installation and model-backed inference validated on Ubuntu 26.04 x86_64. | Keep Ubuntu 26.04 acceptance current as dependencies change. Other distributions and Linux arm64 remain unverified. |
| Android | Not supported or evaluated. | Remains a separate P2 product/platform decision; requires NDK/ABI and embedded lifecycle design. |

### Platform implementation and acceptance work

1. **Define the build matrix.** For every intended target, record OS version, CPU architecture/ABI, compiler/toolchain, BLAS/runtime libraries, minimum OS, and exact executable or wheel artifact. Distinguish native Windows from any proposed WSL2 arrangement; do not describe WSL2 as native Windows support.
2. **Maintain Linux acceptance.** Linux x86_64 has passed clean-install and model-backed inference validation on Ubuntu 26.04. Re-run these checks as dependencies or runtime builds change before expanding the supported distribution matrix.
3. **Maintain Qwen runtime delivery by platform.** The C INT8 source builder and release wheel path cover Linux x86_64. Keep validating system-library and ISA assumptions on the supported Ubuntu baseline. Keep actionable unavailable status where no compatible artifact exists. Do not add an implicit PyTorch fallback to cover a platform gap.
4. **Harden native executable lifecycle.** Validate executable format and architecture before advertising a model. Test paths containing spaces, port allocation, loopback-only binding, startup diagnostics, timeouts/cancellation, child-process cleanup, application shutdown, and errors when the binary is missing or incompatible.
5. **Keep builds reproducible.** Pin upstream source revisions, retain notices, verify platform-specific wheel/artifact tags and minimum OS targets, and assert that release packages contain the correct executable. Do not place native binaries in an artifact whose compatibility tag claims universal or pure Python support.
6. **Preserve model reuse and integrity.** Confirm that pinned model files and catalog hashes work unchanged on each runtime target. Changing an inference runtime must not trigger a second weight download. If pre-quantized or converted model assets are introduced, version and hash them separately and make conversion reproducible.
7. **Evaluate runtime formats separately.** For Qwen and other large candidates, compare source weight disk size with runtime memory, cold/warm startup, speed, and output quality. C INT8 runtime quantization may reduce resident memory without reducing the downloaded model size. Do not treat third-party MLX/GGUF/4-bit files as interchangeable without validating tensor layout, loader, quality, and license.
8. **Run a clean-platform acceptance suite.** On each target, install from a clean environment; verify runtime/capability reporting, direct and routed Chinese/English tasks where supported, audio output format, install and model reuse, queue/concurrency, timeout/shutdown, CPU/thread reporting, peak memory, and quality/performance against the same corpus. Record OS, CPU, compiler, runtime revision, model revision, and build flags.

### Platform completion criteria

- Every claimed target has a reproducible clean build/install path and a tested artifact.
- Unsupported model/platform combinations are unavailable with a useful reason; there is no silent backend or precision switch.
- API/model contracts remain platform-neutral, and runtime/capabilities identify the actual backend/device accurately.
- Chinese/English quality, cold/warm latency, RTF, peak memory, and disk footprint are recorded for each supported target before support is marked verified.

## P2 — Platform and Product Extensions

### Windows and Linux platform acceptance

- Maintain the implemented source-install/build and release-wheel paths for Windows x64 and Linux x86_64, and document their target-specific prerequisites.
- Validate audio codecs, filesystem paths/permissions, CLI process signals, child-process lifecycle, loopback binding, resource reporting, and shutdown on each supported OS.
- Maintain the passing Linux x86_64 clean-install and model-backed regression baseline on Ubuntu 26.04; complete any remaining clean-install and model-backed acceptance for other target platforms before advertising them as fully verified.
- Run the same regression and model acceptance scenarios on clean environments for each supported target.

### Other P2 extensions

- **GPU inference (low priority):** Recent evaluation found no PyTorch CUDA path, including PyTorch INT8 quantization, faster end to end than the native INT8 Qwen3-TTS runtime in the tested conditions. Do not prioritize CUDA integration without new evidence of a meaningful benefit. If reconsidered, add it as an optional provider with a model/provider/device compatibility matrix, actual-device reporting, explicit fallback behavior, and hardware validation; retain the native INT8 CPU path as the shared baseline.
- **Concurrency tuning:** Verify same-instance safety for each runtime/model type. Use fixed-arrival-rate tests for throughput, tail latency, rejection rate, CPU, and memory. Keep current concurrency defaults until target-device evidence supports a change.
- **Android:** Evaluate embedded runtime, NDK/ABI support, application lifecycle, permissions, and resource constraints after the Windows/Linux platform contracts are stable.
- **Security and remote access:** Keep loopback-only behavior by default. Design authentication, authorization, upload limits, and privacy controls before enabling LAN or remote binding.
- **Management UX:** Evaluate a model-management UI only after CLI/API workflows and their user requirements are clear.
- **Catalog expansion:** Add languages, model families, mirrors, or precision variants in verified batches. Compare quality, latency, memory, disk use, dependencies, and licensing before publishing support.
- **Standalone installer:** The current plan is source deployment. Treat a desktop installer or packaged application as a P2 scope decision; define update, signing, rollback, and uninstall requirements before implementation.

### Catalog expansion workflow

Evaluate additions in small batches rather than turning every upstream model into a supported SmartVoice model. Candidates may include additional Whisper sizes, other quantization/precision variants, and further multilingual TTS models. For each candidate:

1. Pin the exact repository revision, files, hashes, model card, and license terms for weights and supporting assets.
2. Verify adapter/runtime compatibility and the target platform build before publishing it in the catalog.
3. Measure download and installed size, cold start, warm inference latency/RTF, peak RAM/VRAM, and actual device.
4. Evaluate language coverage and output quality using the shared STT corpus or TTS listening set.
5. Add the model to catalog/lifecycle tests only after compatibility, licensing, and quality review. Keep mirrors user-configurable or provider-based; do not hard-code regional mirrors.

## P2 — Reliability and Release Acceptance

Extend regression and platform validation to cover:

- Normal requests, malformed/unsupported fields, stable error codes, upload and generated-output limits, queue overload, timeout/cancellation, and resource cleanup against the OpenAPI contract.
- Disk exhaustion, interrupted/corrupt downloads, HTTP Range resume, hash mismatch, unsafe archives, offline model import/export, missing files, and unavailable/incompatible models.
- Installed-model inference with network disconnected; default loopback binding; temporary-file cleanup; shutdown and child-process cleanup.
- Request text/audio redaction in logs and diagnostics. Keep remote/LAN access disabled until authentication and authorization controls are designed and accepted.
- Provider/adapter contract consistency across substitutes, confirming that API and domain contracts do not change when the backend changes.

For STT evaluation, maintain a fixed licensed corpus with manual references and report English WER and Chinese CER using stable normalization/tokenization. Include errors for numbers and proper nouns. For TTS, use blinded MOS or pairwise human evaluation as the primary quality signal, with naturalness, intelligibility, pronunciation, and prosody recorded separately. DNSMOS/UTMOS/TTSDS may be used for screening or comparison, but predicted metrics are not a substitute for listening. Independent ASR may help assess intelligibility but includes that recognizer's own error. Do not use PESQ/STOI as primary metrics for unpaired TTS output.

Record model/source revisions, hashes, corpus version, normalization rules, hardware, OS, driver/compiler, runtime versions, build flags, and audio configuration with every quality/performance report. Set thresholds from the P2 reference hardware and corpus; label exploratory measurements clearly and do not present them as SLAs.

For every release candidate, retain the CI suite, full regression results, distribution build checks, supported platform/model matrix, quality report, and any known limitations. A timeout must not release runtime capacity while native inference is still running.

## Completed Work and Historical Notes

- The core domain/ports/adapters boundaries, application-level STT/TTS services, REST API, CLI, model repository/lifecycle, and static language-aware router are implemented.
- The prior router design questions and prototype JSON are superseded by the current implementation and are not open tasks. Routing uses the versioned JSON configuration, `smartvoice-auto`, direct model IDs, explicit/automatic language handling, ordered installed candidates, and hot reload.
- Architecture principles and development constraints are documented in [architecture-guidelines.md](architecture-guidelines.md). The current repository structure and usage instructions are maintained in [README.md](../README.md), not duplicated here.
