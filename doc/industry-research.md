# Local STT/TTS Inference: Industry Research

> **Research snapshot dated 2026-09-27.** Model availability, dependencies, licensing, and performance can change; treat this as decision history rather than current installation guidance. See [README.md](../README.md) and `catalog/models.json` for the maintained model catalog.

**Research date:** 2026-09-27

**Scope:** Edge devices including PCs and smartphones; long-term support for Windows, macOS, Linux, and Android, with Windows first. Focus on lightweight offline inference, extensible multilingual support with Chinese and English first, STT/TTS web APIs, agent interoperability, model distribution, and licensing.

## 1. Executive Summary

It is not quite accurate to say that the industry has no all-in-one STT/TTS inference software. `sherpa-onnx` provides offline ASR, TTS, and VAD for Windows, Linux, and macOS, with CPU and NVIDIA CUDA support and Python, C/C++, and C# interfaces. FunASR is a mature ASR toolkit; faster-whisper is an optimized local implementation of Whisper; and Piper, Kokoro, and CosyVoice provide TTS implementations with different strengths.

The shared gap is product integration: an installable cross-platform application, a stable local HTTP API, model discovery/download/verification/removal, automatic CPU/GPU selection, transparent capabilities and licensing, and complete operation after network disconnection. The recommended direction is a local service with a unified control plane and multiple inference adapters, rather than relying on one framework to support every model.

The first release should focus on a lightweight Windows service and API control plane, using sherpa-onnx for verified lightweight ONNX models while keeping adapters independent. Decouple the API from inference engines so native runtimes can be used on macOS, Linux, and Android later. Deliver a CPU-capable path, optional GPU acceleration, file transcription, and non-streaming TTS first. Real-time streaming recognition can be a target capability only for explicitly supported models. Do not claim that arbitrary STT/TTS models work, or that models below 7B parameters will run smoothly on every PC.

## 2. Problem and Terminology

- **STT/ASR:** Accepts audio and returns text, language, timestamps, segments, or related data.
- **TTS:** Accepts text and optional speaker/style settings and returns audio.
- **Offline operation:** Inference requires no network. Initial model downloads and updates require connectivity unless models are imported from offline packages.
- **Inference framework versus product:** A framework runs models and operators. A product also needs installation and upgrades, model management, APIs, configuration, logs, permissions, error handling, and documentation.
- **All-in-one:** For this project, one process, installer, or service exposes both STT and TTS. It does not require every model to use the same inference engine.

## 3. Market and Technology Landscape

### 3.1 Frameworks and runtimes

| Option | STT | TTS | CPU/GPU and platforms | Fit and limitations |
|---|---|---|---|---|
| [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) | Streaming and non-streaming | Yes | CPU, CUDA GPU; Windows, Linux, macOS, and more | The closest mature foundation for unified local inference. Models must use supported architectures and export formats; arbitrary Hugging Face checkpoints do not run directly. A strong lightweight default backend candidate. |
| [FunASR](https://github.com/modelscope/FunASR) | Strong, including Paraformer and SenseVoice | Not a primary focus | CPU/GPU, PyTorch ecosystem | Strong Chinese ASR ecosystem with model, VAD, and punctuation combinations. Suitable as an ASR adapter, but does not provide unified TTS. Manage its dependencies and runtime separately from the lightweight ONNX path. |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | Whisper file/segment transcription | No | CPU int8, NVIDIA GPU fp16/int8; Python | CTranslate2 can reduce memory use and supports quantization. CUDA version matching is an installation risk. Useful as an optional multilingual transcription backend, not inherently a low-latency streaming solution. |
| [whisper.cpp](https://github.com/ggml-org/whisper.cpp) | Whisper transcription | No | CPU and some GPU acceleration; multiple platforms | C/C++ and GGML quantization suit desktop distribution. A unified API layer must provide TTS separately. |
| PyTorch / Transformers | Model dependent | Model dependent | CPU is possible but often slower; CUDA can accelerate | Broad model coverage and fast iteration, with heavier dependencies, CUDA/weight-format complexity, and model-specific behavior. Better as an optional high-quality model route than the only installation path. |
| ONNX Runtime | Exported models | Exported models | CPU Execution Provider, CUDA and other providers | Useful for lightweight deployment. Execution provider and model graph compatibility must be validated; ONNX alone does not guarantee acceleration or compatibility. |

**Conclusion:** A unified API is practical; a unified model runtime is neither realistic nor necessary. Each backend should publish a capability manifest covering tasks, languages, streaming, devices, precision, input/output formats, and dependencies.

### 3.2 STT model candidates

| Model/family | Size and positioning | Language/capability summary | Suggested role | Notes |
|---|---|---|---|---|
| [SenseVoiceSmall](https://huggingface.co/FunAudioLLM/SenseVoiceSmall) / FunASR | About 234M according to the FunASR catalog | Chinese, English, Cantonese, Japanese, and Korean; emotion/audio-event tags | Chinese-first default candidate | Native and ONNX/sherpa-adapted versions may differ in capability and accuracy. Benchmark the exact checkpoint. |
| [Paraformer](https://www.funasr.com/en/models.html) | Multiple sizes; common Chinese models | Chinese and some English models; can be combined with VAD/punctuation | High-throughput offline Chinese candidate | Declare the full model combination and language range. Do not misrepresent VAD or punctuation as core recognition capabilities. |
| [Whisper](https://github.com/openai/whisper), including tiny/base/small | Small is about 244M; smaller versions also exist | Multilingual transcription and translation | Candidate for multilingual and English coverage | Standard Whisper is oriented toward file/segment tasks. Word-by-word streaming requires chunking and is not native model streaming. Check license and quality for each implementation and quantized format. |
| [Moonshine](https://github.com/usefulsensors/moonshine) | Lightweight tiny/base families | Primarily an English low-latency route | Alternative for real-time English | Language coverage is narrower; position it as a language-specific profile, not a universal default. |
| sherpa-onnx pretrained ASR models | Zipformer, Paraformer, SenseVoice, Whisper, and others | Varies by model; includes Chinese/English, multilingual, and streaming models | First-release CPU/small-memory catalog | Register each repository, accuracy, dictionary/tokenizer, sample rate, and license separately. |

Official FunASR documentation describes SenseVoiceSmall as a five-language 234M model and lists Paraformer and SenseVoice options. The sherpa-onnx catalog includes streaming and offline multilingual choices. “Popular models” should mean selectable catalog entries, not models all preinstalled at once. Language coverage is specific to each task, model, and voice. Prioritize Chinese and English for acceptance while allowing the catalog to grow to Japanese, Korean, Cantonese, and other validated languages.

### 3.3 TTS model candidates

| Model/family | Size and positioning | Language/capability summary | Suggested role | Notes |
|---|---|---|---|---|
| [Piper](https://github.com/OHF-Voice/piper1-gpl) | Many lightweight ONNX voices | Languages vary by voice; CPU friendly | Lowest-resource, fast local synthesis | Each voice has its own model card and license. Track the Piper software license separately from voice-weight licenses. |
| [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) / community ONNX conversions | 82M | Lightweight, high-quality candidate; language coverage differs between official and converted versions | Efficient natural-sounding English and other supported voices | Language/voice mapping depends on the exact checkpoint and frontend. A conversion is not automatically equivalent to the original. Confirm license, dictionary/G2P dependencies, and commercial terms. |
| [CosyVoice 2 0.5B](https://huggingface.co/FunAudioLLM/CosyVoice2-0.5B) | 0.5B | Chinese/English and other languages; zero-shot, cross-lingual, and streaming features depend on implementation | Candidate for natural Chinese and voice cloning | PyTorch, memory, and dependencies are heavier. Check license and consent requirements for voice cloning. Do not promise real-time performance on ordinary CPUs. |
| [Qwen3-TTS 0.6B family](https://github.com/QwenLM/Qwen3-TTS) | 0.6B | Multilingual generation and speaker/control features vary by submodel | Optional high-quality model | Below the 7B ceiling, but still heavier in memory, latency, and dependencies than lightweight TTS. Treat as an optional profile and confirm distribution terms. |
| [XTTS-v2](https://huggingface.co/coqui/XTTS-v2) | Approximately 0.5B-class multilingual cloning | Multilingual, reference-audio voice cloning | Comparison candidate | The model card specifies the Coqui Public Model License. Open source code does not imply unrestricted commercial use of the weights. Exclude from the default downloadable catalog initially. |
| sherpa-onnx TTS models | Supported Piper, VITS, Matcha, Kokoro/ZipVoice options change over time | Varies by model | CPU-first catalog and unified lightweight runtime | Present the model card, license, languages, resource needs, and backend compatibility before download. |

### 3.4 Model acquisition and licensing

- A public repository does not imply a license grant. Code, weights, acoustic frontends, dictionaries/phonemizers, and bundled voices may have different terms.
- Record source URL, pinned revision/commit, file list, SHA-256, model-card URL, SPDX identifier and license text, commercial-use conditions, size, task, languages, runtime, and device requirements in the model registry.
- Support providers such as Hugging Face, ModelScope, and trusted direct URLs. Allow a user-configured mirror or offline import when a source is unreachable. Do not hard-code a regional mirror.
- Support resumable downloads, temporary files, atomic installation after verification, cleanup/resume on failure, disk-space preflight, and removal. Disclose network access during installation; inference should default to no network access.
- Do not promise that every model discoverable on a hub will run. List only versions validated by this project.
- Keep audio input, reference audio, and generated output in the local data directory by default. Do not upload or use them for training. Require explicit opt-in before exposing a LAN API.

## 4. Product Opportunity

Existing tools specialize in inference, language-specific ASR, individual TTS models, or model-serving deployment. Users still need to install Python/CUDA, reconcile ports and API differences, screen model licenses, manage caches and paths, resolve device conflicts, diagnose errors, migrate offline, and configure startup. Product value should focus on:

1. One service exposing both STT and TTS APIs.
2. A CPU installation path with no GPU prerequisite and clear compatibility checks for GPU use.
3. A catalog distinguishing lightweight default models from optional higher-quality models.
4. Model download, verification, activation, uninstall, and offline import in one manager.
5. Clear reporting of the actual device, quantization, license, expected download space, and known limitations.

## 5. Recommended Architecture

```text
Client / local script
        | HTTP JSON + multipart; optional WebSocket
Unified API service (OpenAPI, binding/auth, validation, request queue)
        +-- Model registry and local model manager (download, verification, cache, license/capability metadata)
        +-- STT adapters: sherpa-onnx / FunASR / faster-whisper (optional)
        +-- TTS adapters: sherpa-onnx / Piper / PyTorch TTS (optional)
                 |
        CPU by default; select GPU when compatible; report device state and fallback reason
```

Describe the API with OpenAPI 3.x. Use explicit language/locale codes and query model capabilities. Support a commonly used subset of OpenAI-style `/v1/audio/transcriptions` and `/v1/audio/speech`, plus `/v1/models` and project-specific capability, download, and status endpoints. OpenAI-style APIs are a de facto convention that can reduce integration effort; they are not a neutral standard or a guarantee of full cloud API compatibility.

### Key architecture principles

- Keep an independent adapter interface such as `load/unload/health/capabilities/transcribe/synthesize`. Do not reload a model for every request.
- Let the control plane own model lifecycle, API validation, and service state. Isolate conflicting adapter dependencies, using separate processes where dependency complexity warrants it.
- Make `device=auto|cpu|cuda` explicit. Auto mode detects available backends and follows a compatibility matrix. Report load failures and allow configured CPU fallback; never report CPU fallback as GPU success.
- Manage VRAM/RAM concurrency. Limit the number of large models resident on one GPU by default and return actionable queue or conflict states.
- Keep inference data local. Offline startup must not trigger telemetry, remote model checks, or automatic updates.
- Compatibility depends on installer, Python/runtime, driver, and exact model versions. Use pinned dependencies and auditable build manifests.

## 6. Evidence Sources

The sources below are official project documentation, official model repositories, or papers. Online content changes; implementation work should record access dates, model revisions, and file checksums.

1. [sherpa-onnx GitHub](https://github.com/k2-fsa/sherpa-onnx): offline ASR/TTS/VAD, platforms, and model examples.
2. [sherpa-onnx documentation overview](https://k2-fsa.github.io/sherpa/intro.html): CPU/GPU, platforms, API bindings, and project differences.
3. [sherpa-onnx Python installation and CUDA](https://k2-fsa.github.io/sherpa/onnx/python/install.html): CPU and NVIDIA GPU installation.
4. [FunASR documentation](https://funasr.com/en/docs/command-line.html) and [model catalog](https://www.funasr.com/en/models.html): SenseVoice, Paraformer, and language coverage.
5. [FunASR paper](https://arxiv.org/abs/2305.11013): Paraformer/VAD/punctuation toolkit background.
6. [faster-whisper GitHub](https://github.com/SYSTRAN/faster-whisper): CPU int8, GPU precision, dependencies, and benchmarks.
7. [Piper repository](https://github.com/OHF-Voice/piper1-gpl): local TTS and voice model license considerations.
8. [Kokoro model card](https://huggingface.co/hexgrad/Kokoro-82M): model and voice details; use the exact selected conversion repository as the source of truth.
9. [CosyVoice 2 model card](https://huggingface.co/FunAudioLLM/CosyVoice2-0.5B): 0.5B model and acquisition details.
10. [Qwen3-TTS repository](https://github.com/QwenLM/Qwen3-TTS): code, inference, and released model information.
11. [XTTS-v2 model card](https://huggingface.co/coqui/XTTS-v2): model license information.
12. [Whisper repository](https://github.com/openai/whisper): models, tasks, and implementation notes.

## 7. Web API and Agent Interoperability

### 7.1 Is there a universal STT/TTS Web API standard?

There is no single cross-vendor industry standard with broad implementation that defines complete STT/TTS REST paths, fields, audio streaming, and error semantics. Distinguish these three specifications/conventions:

- **OpenAPI Specification (OAS):** A machine-readable description format for REST APIs that supports documentation, client generation, and validation. It does not prescribe field names or paths for transcription or synthesis.
- **OpenAI Audio API compatibility convention:** Common routes include `POST /v1/audio/transcriptions` for multipart file transcription and `POST /v1/audio/speech` for text-to-speech. LocalAI and other local inference services support these routes for practical SDK and agent interoperability. This is a de facto compatibility convention, not a neutral standard; fields and responses vary among providers.
- **MCP (Model Context Protocol):** A protocol for agent clients to discover and call tools. It can expose `transcribe_audio` and `synthesize_speech`; audio can be returned in MCP content blocks. MCP does not replace lower-level HTTP audio endpoints and is best treated as an optional adapter.

### 7.2 Recommended API strategy

1. Publish the local service contract with OpenAPI 3.x, generate Swagger UI/clients, and version the specification.
2. Prefer common OpenAI Audio API routes and fields for file transcription and speech synthesis so applications and agents that support a configurable base URL can connect. Authentication may use no key on loopback or a local token; a cloud API key must not be required.
3. Define a separate WebSocket message schema for streaming audio: connection setup, audio chunks, partial/final results, errors, cancellation, and close. Link the schema from OpenAPI. Do not describe a cloud Realtime protocol as a universal standard.
4. Offer an optional MCP server over stdio or local HTTP, exposing two clearly defined tools. Avoid arbitrary filesystem paths in tool parameters; use constrained file handles or data blocks with size limits.
5. Publish a compatibility matrix for paths, fields, response formats, errors, and streaming behavior, marking each compatible, partially compatible, or unsupported.

### 7.3 Official references

- [OpenAPI Specification](https://spec.openapis.org/oas/latest.html): REST API description format.
- [OpenAI Audio API Reference](https://platform.openai.com/docs/api-reference/audio): widely used transcription and speech generation API examples.
- [LocalAI TTS API](https://localai.io/features/text-to-audio/): example of a local inference service supporting OpenAI TTS APIs.
- [MCP Specification](https://modelcontextprotocol.io/specification/2025-11-25): agent tool interoperability protocol. Pin the specification version used by each implementation.

## 8. Updated Research Conclusion

The target has expanded from “all-in-one for ordinary PCs” to “edge devices, multi-platform evolution, and agent-friendly APIs.” Lightweight design therefore needs a resource budget and platform-specific inference and packaging strategies. A Windows-first release should not lock future Android work into a desktop service design: API and capability schemas can be reused while runtimes and model formats vary by platform. The following section assesses sherpa-onnx against this updated target.

## 9. sherpa-onnx Gap Assessment for SmartVoice

**Assessment date:** 2026-09-27

**Target:** Lightweight edge devices (PCs and phones), extensible multilingual support with Chinese and English first, Windows first, and standard Web API integration for agents.

### 9.1 Conclusion

`sherpa-onnx` is a strong edge inference engine/SDK and closely fits the core inference needs for lightweight, cross-platform, offline STT and TTS. It is not itself the installable product and unified agent API service required by SmartVoice. The largest gaps are product-level: Windows installation and lifecycle, a complete standard REST API, an explicit OpenAI Audio API compatibility promise, consistent STT/TTS APIs, model catalog management, API security, and documentation.

Use sherpa-onnx as the default Windows inference backend/SDK candidate in the first release, after validating target models, performance, and packaging. SmartVoice should own the API facade, model management, configuration, and product UX. Add other runtimes through adapters where sherpa coverage or quality is insufficient; avoid forking the inference engine to implement product features.

### 9.2 Goal-by-goal comparison

| Goal | sherpa-onnx today | Gap | Assessment |
|---|---|---|---|
| Lightweight, offline, edge inference | ONNX Runtime based, with C/C++ and C APIs, lightweight ASR/TTS models, and offline inference | Lightweight performance is not guaranteed for every model; measure RAM, latency, and power on target devices. SmartVoice needs model recommendations and resource profiles. | **Core capability is close** |
| Multilingual expansion | Catalog includes STT/TTS candidates for various languages; some models cover Chinese, English, Cantonese, and Japanese | Language coverage varies by model and voice. SmartVoice needs task/model-specific language capabilities. | **Many backend options; product catalog needed** |
| STT and TTS on one runtime | Supports online/offline ASR, TTS, and VAD | Architectures and capabilities differ; arbitrary checkpoints and a unified model installation/activation UX are not implied. | **Inference coverage is close** |
| Windows first | Windows support and C/C++/Python/JavaScript APIs/examples | Validate exact binaries, CPU instruction sets, codecs, installer, signing, updates, antivirus false positives, and dependency packaging. | **Foundation exists; product gap is moderate** |
| Later macOS/Linux/Android | Multiple desktop/mobile platforms and Android examples/bindings | Execution provider and performance may differ per platform. Android lifecycle, JNI/NDK, model assets, and permissions need separate integration. | **Good engine coverage; moderate product gap** |
| Standard Web REST API | Non-streaming WebSocket and streaming WebSocket examples; Python non-streaming examples include HTTP handling | Examples are not a unified production REST API. No OpenAPI description or OpenAI `/v1/audio/*` compatibility commitment was identified; TTS lacks an equivalent standard REST product surface. | **Major gap** |
| Real-time STT | Streaming WebSocket server/client | SmartVoice still needs message schema, authentication, errors, cancellation, versioning, connection limits, and API documentation. | **Inference exists; protocol productization is missing** |
| TTS API | Model inference libraries and examples | Needs unified REST inputs/outputs, voice discovery, audio formats, errors, and optional chunked streaming. | **Inference exists; HTTP API is missing** |
| Agent integration | Can be connected through SDKs and examples | No explicit OpenAPI contract or compatibility matrix; no evidence of a built-in MCP tool server. | **Needs a SmartVoice facade/adapter** |
| Model discovery/download | Documentation lists pretrained models and download links | No application registry, pinned revisions, checksum verification, resumable downloads, license screening, removal, or import workflow. | **Management layer needed** |
| Privacy, security, deployment | Local execution is possible; WebSocket examples can use TLS | Examples are often for development. SmartVoice needs loopback by default, upload limits, tokens, redacted request logs, and signed updates. | **Productization needed** |

### 9.3 API standard gaps

#### Reusable today

- sherpa-onnx includes WebSocket server/client examples for online streaming and offline recognition. Offline examples use WebSocket, while streaming examples send audio chunks in a session.
- Non-streaming WebSocket service examples and Python HTTP/WebSocket server examples can validate a model-serving approach.
- sherpa-onnx offers multilingual libraries and bindings that SmartVoice can embed or call through a local service.

#### SmartVoice still needs to define

1. **OpenAPI 3.x REST contract:** transcription, speech, models, health, and error schemas.
2. **OpenAI compatibility boundary:** document multipart fields, supported formats, verbose JSON, error bodies, model ID semantics, and TTS media types individually. Claim only what is implemented.
3. **Streaming protocol:** WebSocket message envelope, PCM encoding/sample rate, sequence numbers, partial/final results, backpressure, cancellation, and reconnection.
4. **Agent adapter:** OpenAPI enables HTTP clients; add a lightweight MCP server wrapper if native MCP discovery is required. MCP does not replace a REST API standard.
5. **Lifecycle APIs:** capabilities/model metadata, download jobs, device status, loading/unloading, and readiness.

At the API layer, sherpa-onnx is not an almost-finished product awaiting configuration. It provides reusable server components; SmartVoice must build and own its interface surface.

### 9.4 Approximate gap scores

Scores measure relative fit to this target (5 = very close, 1 = little coverage), not overall quality.

| Dimension | Score | Explanation |
|---|---:|---|
| Lightweight offline inference engine | 4.5/5 | CPU, ONNX, mobile, and embedded directions fit well; validate each model and device. |
| STT/TTS inference | 4/5 | Both are covered, but model selection is needed for language, quality, and architecture. |
| Multi-platform foundation | 4/5 | Target desktop and mobile platforms are supported; packaging and consistent behavior remain project work. |
| Deliverable Windows product | 2.5/5 | Windows support is not an installer, updater, configuration, service, and diagnostics experience. |
| Unified standardized Web API | 1.5/5 | Demos exist, but there is no SmartVoice-level OpenAPI and stable REST contract. |
| Model store, licensing, lifecycle | 2/5 | Model lists and links exist; registry, verified downloads, install/removal, and license UX must be built. |
| Agent plug-and-play experience | 2/5 | SDK/API wrapping is possible; OpenAI Audio compatibility and MCP need additional work. |

### 9.5 Recommended boundary between SmartVoice and sherpa-onnx

```text
Agent / desktop client
      |
      +-- REST: OpenAPI description + common OpenAI Audio API compatibility
      +-- WebSocket: SmartVoice streaming STT schema
      +-- Optional MCP server: transcribe_audio / synthesize_speech
                  |
       SmartVoice Windows service (API, model catalog, device/jobs, logs, configuration)
                  | Adapter interface
                  +-- sherpa-onnx (preferred lightweight CPU/ONNX/mobile model backend)
                  +-- Other engine adapters (only for needed quality/coverage gaps)
```

The API contract belongs to SmartVoice. Do not expose sherpa internal classes, command-line options, or example WebSocket messages directly. Android may reuse API/capability semantics while using the sherpa C API/JNI or another native backend. Assess mobile background-service rules, localhost exposure, and power management before deciding whether to run a local HTTP service on phones.

### 9.6 Work needed for the first Windows release

#### P0: Technical validation and scope freeze

- Confirm the sherpa-onnx CPU wheel/native dependencies, minimum Windows version, CPU instruction set, and audio decoding support on target Windows x64 systems.
- Select Chinese- and English-first STT/TTS candidates, with at least one additional target language where candidates support it. Pin source revisions and hashes; measure model size, cold start, peak RAM, real-time factor, TTS first-chunk latency, and quality.
- Verify offline Chinese/English coverage and record streaming capabilities. Do not mark a model as compatible merely because it appears in the upstream catalog.
- Review distribution and license obligations separately for the engine, ONNX Runtime, models, voices, and phonemizer dependencies.

#### P1: SmartVoice product layer

- Windows service/CLI launcher, user data directory, configuration, log rotation, and clean uninstall.
- OpenAPI REST facade for STT/TTS and model/health queries.
- Model catalog UI or CLI for verified downloads, status, activation/uninstall, and offline import.
- Restricted loopback listener and local token. Do not expose arbitrary local file paths or arbitrary model download URLs through the API.
- Installer, pinned dependencies, updates, and signing. Users should not need to install Python or build tools after installation.

#### P2: Agent and platform expansion

- OpenAI API compatibility test client/examples and agent setup documentation.
- Optional MCP stdio server for local agent launch; assess HTTP transport for remote deployment separately.
- Once compatibility abstractions are stable, expand to macOS/Linux and then Android JNI/NDK components and mobile lifecycle design.

### 9.7 Final recommendation

Do not treat “using sherpa-onnx” as the completion goal. It can reduce work on STT/TTS runtimes and cross-platform native inference, but it does not replace SmartVoice Windows distribution, standard Web APIs, model governance, or agent integration.

Use a SmartVoice product/API layer with sherpa-onnx as the default backend and optional model adapters. Before adding FunASR, faster-whisper, or a large TTS runtime, validate sherpa-onnx candidate quality, resource use, and licensing on a Windows reference machine. If lightweight operation is the priority, introduce heavier PyTorch/CUDA dependencies cautiously.

### 9.8 References

- [sherpa-onnx project](https://github.com/k2-fsa/sherpa-onnx): capabilities, platforms, and language bindings.
- [sherpa-onnx Python installation](https://k2-fsa.github.io/sherpa/onnx/python/install.html): CPU and CUDA options.
- [Streaming WebSocket server documentation](https://k2-fsa.github.io/sherpa/onnx/python/streaming-websocket-server.html): streaming ASR example.
- [Non-streaming WebSocket server documentation](https://k2-fsa.github.io/sherpa/onnx/websocket/offline-websocket.html): offline recognition server example.
- [sherpa-onnx example HTTP handler](https://github.com/k2-fsa/sherpa-onnx/blob/master/python-api-examples/non_streaming_server.py): non-streaming example service code; this is not a production REST compatibility commitment.
- [OpenAPI Specification](https://spec.openapis.org/oas/latest.html): REST API description format.
- [OpenAI Audio API Reference](https://platform.openai.com/docs/api-reference/audio): common transcription and synthesis paths/fields.
- [LocalAI TTS API](https://localai.io/features/text-to-audio/): example local service compatible with OpenAI TTS APIs.
- [MCP Specification](https://modelcontextprotocol.io/specification/2025-11-25): agent tool-calling protocol.
