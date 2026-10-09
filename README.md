<div align="center">
  <img src="https://raw.githubusercontent.com/dxhome/SmartVoice/main/assets/smartvoice-logo.png" alt="SmartVoice logo" width="220">
  <p><strong>Run speech recognition and speech synthesis on your own machine.</strong></p>
  <p>One OpenAI-style audio API · Language-aware model routing · CPU inference · No per-request inference fee</p>
  <p><a href="README.md">English</a> | <a href="README.zh-CN.md">简体中文</a></p>
</div>

# SmartVoice

SmartVoice is a local speech-to-text (STT) and text-to-speech (TTS) service for developers building voice-enabled apps and agents. Install the models you want, run inference on your machine, and connect through one OpenAI-style audio API.

**Audio stays on your machine during inference.** Once the models are installed, supported workflows can run offline, with no cloud account or per-request inference fee. SmartVoice supports CPU inference on Windows x64, macOS Apple Silicon, and Linux x86_64.

[Get started](#quick-start) · [Browse models](#model-catalog) · [Try the API](#api) · [Architecture](#architecture) · [Report an issue](https://github.com/dxhome/SmartVoice/issues)

## What can you build?

- **Private voice input:** Transcribe recordings locally instead of sending audio to a hosted speech API.
- **Offline voice assistants:** Add speech recognition and speech output to a local agent or application.
- **Multilingual speech features:** Use the `smartvoice-auto` model ID to route STT and TTS requests by task and language.

## Why SmartVoice?

- **Private by design:** Inference runs on your machine; you choose where models, audio, and service configuration live.
- **One API for STT and TTS:** Connect compatible agents and apps through a single audio API.
- **Choose and move your models:** Install only what you need, select models directly, or export model packages for offline transfer.

**Streaming preview:** Original subtitles (zh/en), translated subtitles and translated speech (zh↔en) are available through a versioned WebSocket and browser page. Enable with `SMARTVOICE_STREAMING_ENABLED=true`, then use `/console/streaming`, `WS /v1/audio/stream` and `GET /v1/audio/stream/capabilities`; see [streaming setup and contract](doc/streaming.md) and [migration evidence and remaining acceptance](doc/streaming-migration.md).

**Current scope:** CPU inference is supported; GPU inference, packaged installers, and Android are not yet supported. Installing or updating models requires downloading files from their configured sources. The service binds to `127.0.0.1` by default; network exposure has no API authentication, so keep it on a trusted network and restrict access with your firewall.

## Capabilities

| Area | Capability |
|---|---|
| Platforms | Windows x64, macOS Apple Silicon, and Linux x86_64 source builds and platform wheels; Linux is validated on Ubuntu 26.04 x86_64 |
| Inference | sherpa-onnx CPU inference and native C INT8 Qwen3-TTS runtimes are supported on all three targets, including model-backed inference on Linux x86_64 |
| STT | Whisper Base multilingual, SenseVoice Small, Qwen3-ASR 0.6B |
| TTS | Kokoro 1.1, Matcha Baker, Supertonic 3, Qwen3-TTS 0.6B |
| Smart routing | Selects an installed model by task and language using an editable priority list; `smartvoice-auto` works for both STT and TTS |
| API | OpenAPI docs, transcription, speech synthesis, model catalog and runtime status |
| Model management | Install, uninstall, offline import/export, and resumable downloads |
| Not yet supported | GPU inference, Linux/Windows ARM64, Intel macOS, Android, HTTPS, packaged installers, MCP |

For the architecture principles, implementation overview, per-backend OS/architecture/device/build matrix, and the difference between installed models and inference availability, see [Architecture Summary and Guidelines](doc/architecture-guidelines.md).

SmartVoice supports a subset of OpenAI Audio API conventions; this is not a claim of full API compatibility. See the [API specification](doc/api-spec.md) for request and response details.

## Smart routing

Smart routing selects an installed model using the request task, language, and an ordered, user-editable JSON table. Use `smartvoice-auto` for language-aware routing on either audio endpoint, or pass a concrete model ID to call that model directly.

TTS model priority order:

1. Chinese: Qwen3-TTS → Matcha → Kokoro
2. German, French, Spanish, Japanese, Korean: Supertonic → Qwen3-TTS
3. English: Supertonic → Qwen3-TTS → Kokoro
4. Portuguese, Russian, Italian: Supertonic → Qwen3-TTS
5. Other supported languages: Supertonic

For STT, the built-in route table prioritizes SenseVoice for Chinese, English, Cantonese, Japanese, and Korean; Qwen3-ASR covers the remaining configured languages, with Whisper as a fallback for selected languages.

SmartVoice routes by the API endpoint and language:

| Request | Virtual model ID | Language source |
|---|---|---|
| Transcription | `smartvoice-auto` | Explicit `language`, or automatic detection when omitted or `auto` |
| Speech synthesis | `smartvoice-auto` | Explicit `language`, or text detection when omitted or `auto` |

The built-in route table is copied to `<data_dir>/router.json` on first startup. Edit it to customize model priorities, then apply changes without restarting:

```bash
python -m smartvoice router reload
```

The first installed and verified candidate is selected. An inference failure does not trigger a retry with the next candidate. See the [API specification](doc/api-spec.md) for request details.

## Model catalog

Language availability describes catalog support, not comparative quality. Model and voice licenses may differ from SmartVoice's license.

| Model ID | Task | Languages / voices | Estimated model files | Hot-request RTF | req/s per CPU core |
|---|---|---|---:|---:|---:|
| `stt-whisper-base-multilingual-int8` | STT | Auto detection; English, Chinese, Japanese, Korean, French, German | ~0.16 GB | 0.052 | 1.237 |
| `stt-sensevoice-small-int8` | STT | Chinese, English, Cantonese, Japanese, Korean | ~0.24 GB | 0.017 | 4.169 |
| `stt-qwen3-asr-600m-int8` | STT | 30 language codes including Cantonese; automatic detection | ~0.99 GB | 0.274 | 0.643 |
| `tts-kokoro-multilingual-v1-1-zh-en` | TTS | Chinese, English; 103 speakers | ~0.43 GB | 0.324 | 0.329 |
| `tts-matcha-zh-baker` | TTS | Chinese; one voice | ~0.15 GB | 0.030 | 2.379 |
| `tts-supertonic-v3-multilingual-int8` | TTS | 31 languages; no Chinese | ~0.15 GB | 0.236 | 0.692 |
| `tts-qwen3-0-6b-customvoice` | TTS | 10 languages; 9 preset voices | ~2.50 GB | 0.557 | Not measured* |

RTF is the median for warm requests (Chinese where supported; English for Supertonic); lower is faster relative to audio duration. Throughput per CPU core is measured at the highest passing parallel request rate. Both use representative samples and are specific to the test platform: an Apple Silicon Mac running macOS 27.0, with 10 logical CPUs, 16 GiB RAM, Python 3.11.9, and sherpa-onnx 1.13.8. Sherpa tests used two instances, two threads per instance, and a four-request waiting limit; Qwen3-TTS uses one serialized native runtime. Qwen3-TTS's per-core rate is unavailable because its native engine process was not fully included in CPU sampling. See the [benchmark reports](benchmarks/README.md#concurrency-and-request-experience) for details.

Model sizes are approximate unpacked sizes; see [`catalog/models.json`](catalog/models.json) for exact assets and sizes. Qwen3-TTS 0.6B needs at least 6 GiB free during installation. Its Windows runtime requires AVX2 and FMA; Linux builds use the builder's available instruction set. See the [platform compatibility notes](doc/architecture-guidelines.md#build-and-hardware-qualifications).

For STT requests with `language=auto`, SmartVoice uses the optional Whisper Tiny language detector, then routes by the detected language. `python -m smartvoice models install all` installs it; otherwise SmartVoice uses an installed model that supports automatic detection. Its availability appears in `/v1/capabilities`.

## Quick start

SmartVoice requires Python 3.11 or newer and runs inference on CPU. The service binds to `127.0.0.1` by default.

### 1. Install SmartVoice

Choose an installation method below. The PyPI option is recommended for normal use; the source option is for development or when you want an editable checkout.

<details>
<summary>From PyPI</summary>

Install the package for your platform. The macOS Apple Silicon, Windows x64, and Linux x86_64 wheels include the native Qwen3-TTS runtime. The Linux wheel is tagged `manylinux_2_38_x86_64`, bundles OpenBLAS and its required runtime libraries, and requires glibc 2.38 or newer. Linux x86_64 clean installation and model inference are validated on Ubuntu 26.04. CPUs without the native runtime's required instructions and Linux ARM64 are not supported by this wheel.

macOS Apple Silicon:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install smartvoice
```

Linux x86_64 (Ubuntu 24.04 or newer):

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install smartvoice
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install smartvoice
```

</details>

<details>
<summary>From source</summary>

Run these commands from the repository root. Windows x64 source builds include the Qwen native runtime, so install the MSYS2 build tools described below before installing SmartVoice. Linux x86_64 source builds require GCC, Make, and OpenBLAS development files.

macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

Ubuntu 26.04, x86_64 (the validated Linux baseline):

```bash
sudo apt-get update
sudo apt-get install -y python3-venv build-essential libopenblas-dev
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

On macOS Apple Silicon, the source install builds the native INT8 Qwen3-TTS runtime using Xcode Command Line Tools. Windows x64 and Linux x86_64 source deployments need the explicit native build step described below.

#### Build the Qwen3-TTS runtime on Linux

The Linux x86_64 source build uses GCC, Make, and OpenBLAS. The builder detects the CPU instructions exposed by the current machine and runs the native kernel self-test before staging the runtime. On Ubuntu, install the prerequisites with:

```bash
sudo apt-get install -y build-essential libopenblas-dev
```

For an editable source checkout, run the builder explicitly to stage the runtime in the source package:

```bash
SMARTVOICE_QWEN_OUTPUT="$PWD/src/smartvoice/resources/bin" \
  python scripts/build_qwen3_tts_linux.py
```

The builder stages `qwen_tts` and the runtime dependency license notices in the package resources. Source builds link to system OpenBLAS, so keep the OpenBLAS runtime package installed. The published Linux wheel bundles OpenBLAS and its required runtime libraries.

#### Build the Qwen3-TTS runtime on Windows

The Windows source deployment builds Qwen3-TTS with MSYS2 UCRT64 GCC and OpenBLAS. Install MSYS2 and its UCRT64 toolchain packages in an elevated MSYS2 UCRT64 terminal before installing SmartVoice:

```bash
pacman -S --needed make gcc diffutils mingw-w64-ucrt-x86_64-openblas
```

Then, from the SmartVoice repository root in PowerShell, build the runtime into the source package and check that it starts:

```powershell
# Omit this if MSYS2 is installed at C:\msys64 or %USERPROFILE%\msys64.
$env:SMARTVOICE_MSYS2_ROOT = "C:\msys64"
$env:SMARTVOICE_QWEN_OUTPUT = "$PWD\src\smartvoice\resources\bin"
.\.venv\Scripts\python.exe .\scripts\build_qwen3_tts_windows.py
```

The build script runs the native runtime self-test and stages `qwen_tts.exe`, its required runtime DLLs, and license notices in `src/smartvoice/resources/bin` for the editable source checkout. The Windows package build also bundles the runtime in its build output. These generated binaries are local build artifacts and are not committed. If MSYS2 is installed elsewhere, set `SMARTVOICE_MSYS2_ROOT` to that directory. Python source installation remains editable; a SmartVoice wheel is not required.

</details>

Activate `.venv` before running the commands below. On Windows PowerShell, you can keep the environment inactive and replace `python` with ``.\.venv\Scripts\python.exe``.

### 2. Start SmartVoice

Start the local service:

```bash
python -m smartvoice --host 127.0.0.1 --port 8000
```

The service listens only on `127.0.0.1` by default. To allow other machines to connect, explicitly bind to an external interface, for example `--host 0.0.0.0`. External binding is plain HTTP without API authentication; use it only on a trusted network and configure the machine firewall as needed. HTTPS is not currently supported.

### 3. Manage models and try speech in Console

Open the [SmartVoice Console](http://127.0.0.1:8000/console). Use **Models** to browse the model catalog and install or manage models. Model downloads require internet access and are stored outside the source checkout, under `~/Library/Application Support/SmartVoice` on macOS, `~/.smartvoice` on Linux, or `%LOCALAPPDATA%\SmartVoice` on Windows. After installation, inference runs locally. See [Model catalog](#model-catalog) for supported languages and estimated sizes.

In the Console workspace, try speech recognition and synthesis with installed models. Use the generated API examples for your selected models and settings, and edit Smart Router priorities in the Console; saved routing changes take effect immediately. The interactive [API docs](http://127.0.0.1:8000/docs) are also available.

Service settings are stored in `<data_dir>/smartvoice.json`; router priorities are stored separately in `<data_dir>/router.json`. Sherpa models use a lazy instance pool for parallel inference. See [inference concurrency](doc/architecture-guidelines.md#inference-concurrency-lifecycle-and-limits) for details.

You can manage models from the CLI as well. For example, install a basic STT and TTS pair and the language detector:

```bash
python -m smartvoice models install stt-sensevoice-small-int8
python -m smartvoice models install tts-kokoro-multilingual-v1-1-zh-en
python -m smartvoice models install-language-id
```

On macOS Apple Silicon, Windows x64, and Linux x86_64 PyPI installations, the Qwen3-TTS runtime is bundled. From a source checkout, build the runtime on Windows and Linux as described above; the macOS source install compiles it automatically. Then install Qwen3-TTS:

```bash
python -m smartvoice models install tts-qwen3-0-6b-customvoice
```

### 4. Connect an agent or application

In your agent or application's settings, choose **OpenAI-compatible API** and set the SmartVoice API base URL to:

```text
http://127.0.0.1:8000/v1
```

The API supports a subset of the OpenAI Audio API conventions. Copy an STT or TTS example from the Console, or use the examples in [API](#api). The API base URL ends in `/v1`; the Console and interactive API docs are served from the same local SmartVoice instance.

## API

The audio endpoints follow a supported subset of the OpenAI Audio API conventions. For request fields, response formats, and errors, see the [API specification](doc/api-spec.md).

### Transcribe audio with routing

```powershell
curl.exe -F "file=@sample.wav" -F "model=smartvoice-auto" -F "language=auto" `
  http://127.0.0.1:8000/v1/audio/transcriptions
```

Supported upload formats include WAV, MP3, M4A, and FLAC. Audio is limited to 25 MiB and 10 minutes by default.

### Synthesize speech with routing

```powershell
curl.exe -X POST http://127.0.0.1:8000/v1/audio/speech `
  -H "Content-Type: application/json" `
  -d '{"model":"smartvoice-auto","input":"Hello from SmartVoice.","language":"auto"}' `
  --output speech.mp3
```

TTS defaults to mono MP3 (96 kbps); send `"response_format":"wav"` for WAV. Requests support up to 4,000 characters; generated audio is limited to 180 seconds, with separate 32 MiB canonical WAV and final-response budgets. Supertonic uses bounded text chunks; long STT inputs use silence-aware model-declared windows (25 seconds for Whisper Base; 15 seconds by default) with 1-second overlap and a single language decision across chunks, while retaining the 10-minute input limit. See `/docs` or the [API specification](doc/api-spec.md) for available parameters, language behavior, and error codes. The local Whisper Chinese decoding repair requires a patched native wheel; see [build and validation instructions](doc/archive/stt/whisper-chinese-decoding-fix.md).

## Manage models

The CLI supports `models list`, `refresh`, `install`, `uninstall`, `export`, and `import`. `models list` groups models by installed state, then by STT, TTS, Streaming (STT/MT/CT), and SmartVoice native; each model shows its availability, and unavailable models include a reason. Availability checks both model-file integrity and the active inference runtime/device. The running service keeps one process-wide model availability snapshot, initializes it at startup, and updates it after model management operations. If model files are changed outside the running service, refresh that snapshot with `python -m smartvoice models refresh` or `POST /v1/models/refresh`. As a fallback, the service checks the snapshot every 10 minutes and refreshes it in the background while continuing to serve the last successful snapshot. A failed TTL refresh is retried after 5, 10, and 15 seconds; after three retries it waits for a manual or model-management refresh to resume scanning. Configure the interval with `model_availability_ttl_seconds` or `SMARTVOICE_MODEL_AVAILABILITY_TTL_SECONDS`. Use `python -m smartvoice models install all` to install every catalog model that is not already installed and the Whisper Tiny language detector. Model downloads run sequentially from their catalog sources; invalid existing model directories are skipped with a repair hint. Installing all models can require several gigabytes of disk space. On Windows x64, Qwen3-TTS is available only when its native runtime has been built; see [Build the Qwen3-TTS runtime on Windows](#build-the-qwen3-tts-runtime-on-windows). Models can also be installed through API download jobs. Exported model packages can be transferred to offline machines and imported there. An installed model cannot be uninstalled while the service is using it.

Streaming models use `streaming-stt-*`, `streaming-mt-*` and `streaming-ct-*` IDs. Shared TTS IDs remain unchanged. Filter CLI output with `models list --category streaming`; query installed assets with `GET /v1/models?category=streaming` or all entries with `GET /v1/catalog?category=streaming`. `install all` includes translation models: install `python -m pip install -e '.[streaming,model-preparation]'` prerequisites before running it. Translation installation verifies upstream files, converts offline and checks pinned output hashes; inference never converts. Old streaming IDs/directories remain compatible. See [setup and readiness](doc/streaming.md).

Install a model with `python -m smartvoice models install <model-id>`. By default, SmartVoice uses the model's catalog source. For Hugging Face models, pass `--source` with a compatible mirror base URL to use another source, such as:

```bash
python -m smartvoice models install stt-qwen3-asr-600m-int8 --source https://hf-mirror.com
```

The base URL is combined with the catalog-pinned repository, revision, and file paths. This option is available only when all required model files are hosted on Hugging Face. Downloaded files are still checked against their catalog SHA-256 values.

```bash
python -m smartvoice models list
python -m smartvoice models refresh
python -m smartvoice models export stt-sensevoice-small-int8 ./sensevoice.smartvoice.zip
python -m smartvoice models import ./sensevoice.smartvoice.zip
python -m smartvoice models uninstall stt-sensevoice-small-int8
```

## Architecture

```text
Client / Agent ──► Versioned HTTP API ──► Application services
                         ▲                       │
                         │                       ▼
                    Local CLI              Domain contracts
                                                 │
                               ┌─────────────────┴─────────────────┐
                               ▼                                   ▼
                       Inference port                      Model repository port
                               │                                   │
                               ▼                                   ▼
                    Composite inference provider       Filesystem catalog adapter
                       ┌───────┴────────┐
                       ▼                ▼
                sherpa-onnx       native C INT8 Qwen-TTS
                   adapter            adapter
                       │
                       ▼
              Platform diagnostics adapter
```

Callers use versioned API and capability endpoints; inference details stay behind provider and repository interfaces. The source deployment supports CPU inference on Windows x64, macOS Apple Silicon, and Linux x86_64. Qwen3-TTS uses the native C INT8 adapter on all three platforms; Linux source deployments build against the system OpenBLAS library as described above. GPU providers and Android runtimes are not available in SmartVoice. Streaming is an opt-in preview, currently verified on macOS arm64. See [Architecture Summary and Guidelines](doc/architecture-guidelines.md) for the detailed matrix and model availability semantics.

## Development

```bash
python -m pip install -e ".[dev]"
python scripts/test.py ci
```

`python scripts/test.py regression` is the default inference regression suite. It includes the existing functional and real-inference tests, short bilingual functional cases for SenseVoice, Qwen3-ASR, and Whisper, audio-window boundary cases, and 75-second segmented inputs. Models not installed on the current runtime are skipped individually. Run `python scripts/test.py full` for every CI and regression test plus 300- and 590-second bilingual inputs across all three STT models. Full mode requires all three models and the validated patched Whisper runtime; it fails its prerequisite check rather than silently skipping them. Long-input expansion is intentionally excluded from the default regression run. The STT tests check functional success and response metadata, not transcript accuracy. See [`doc/testing.md`](doc/testing.md) and [`tests/fixtures/stt/README.md`](tests/fixtures/stt/README.md) for suite details.
See [`doc/releasing.md`](doc/releasing.md) for versioning, GitHub Releases, and optional PyPI publishing.

## Repository layout

```text
catalog/       Model metadata and default router table
src/           API/CLI entry points, application services, domain contracts, ports, and adapters
tests/         Unit, API, and optional real-inference tests
benchmarks/    Benchmark code, configurations, and results
config/        Example service settings
doc/           API specification, testing, release, requirements, and design notes
assets/        Project logo
```

## License

SmartVoice source code is licensed under the [Apache License 2.0](LICENSE). Model weights, voices, and other third-party assets may have separate license terms; review them before use or redistribution.
