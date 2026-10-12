<div align="center">
  <img src="https://raw.githubusercontent.com/dxhome/SmartVoice/main/assets/smartvoice-logo.png" alt="SmartVoice logo" width="220">
  <p><strong>Run speech recognition and speech synthesis on your own machine.</strong></p>
  <p>OpenAI-style REST audio API · Streaming WebSocket · Language-aware routing · Local CPU inference</p>
  <p><a href="README.md">English</a> | <a href="README.zh-CN.md">简体中文</a></p>
</div>

# SmartVoice

SmartVoice is a local speech recognition, speech synthesis, and streaming service for developers building voice-enabled apps and agents. Install the models you want, run inference on your machine, and connect through an OpenAI-style REST audio API or the versioned streaming WebSocket.

**Audio stays on your machine during inference.** Once the models are installed, supported workflows can run offline, with no cloud account or per-request inference fee. SmartVoice supports CPU inference on Windows x64, macOS Apple Silicon, and Linux x86_64.

[Get started](#quick-start) · [Explore models](#model-management) · [Try the API](#api) · [Architecture](#architecture) · [Report an issue](https://github.com/dxhome/SmartVoice/issues)

## What can you build?

- **Private voice input:** Transcribe recordings locally instead of sending audio to a hosted speech API.
- **Offline voice assistants:** Add speech recognition and speech output to a local agent or application.
- **Multilingual speech features:** Use the `smartvoice-auto` model ID to route STT and TTS requests by task and language, or stream Chinese and English audio through live workflows.

## Why SmartVoice?

- **Private by design:** Inference runs on your machine; you choose where models, audio, and service configuration live.
- **One API for speech:** Connect compatible agents and apps through a single local API for transcription, speech synthesis, and streaming workflows.
- **Streaming built in:** Transcribe live audio, show translated subtitles, or hear spoken interpretation as audio arrives.
- **Choose and move your models:** Install only what you need, select models directly, or export model packages for offline transfer.

SmartVoice supports CPU inference on Windows x64, macOS Apple Silicon, and Linux x86_64. GPU inference, Linux/Windows ARM64, Intel macOS, Android, HTTPS, packaged installers, and MCP are not supported. Model downloads require internet access; speech inference with installed models runs locally. The service binds to `127.0.0.1` by default and has no API authentication, so use a trusted network and firewall when binding to an external interface.

## Capabilities

| Area | Supported capabilities |
|---|---|
| Platforms | CPU inference on Windows x64, macOS Apple Silicon, and Linux x86_64; Linux model-backed inference is validated on Ubuntu 26.04 x86_64 |
| Speech recognition | Whisper Base multilingual, SenseVoice Small, and Qwen3-ASR 0.6B |
| Speech synthesis | Kokoro 1.1, Matcha Baker, Supertonic 3, and Qwen3-TTS 0.6B; MP3 output by default |
| Streaming | Real-time transcription, Chinese↔English translated subtitles, and spoken interpretation through a versioned WebSocket API and browser workspace |
| Smart routing | Editable, task- and language-aware model priorities; `smartvoice-auto` routes both transcription and speech synthesis |
| Console | Browser-based model management, speech testing, routing configuration, and streaming workspace |
| Model management | Install, refresh, and uninstall models; download jobs; offline model import/export |
| API | Versioned REST API, streaming WebSocket, OpenAPI documentation, and runtime/capability status |

For architecture principles, platform compatibility, and model availability semantics, see [Architecture Summary and Guidelines](doc/architecture-guidelines.md). SmartVoice implements a supported subset of OpenAI Audio API conventions, not full API compatibility; see the [API specification](doc/api-spec.md).

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

For each request, SmartVoice applies the route table's task and language rules, then selects the highest-priority matching model that is installed and available in the current runtime. Selection follows the editable routing policy, not model installation order. If inference fails after selection, SmartVoice returns the error instead of silently switching models. See the [API specification](doc/api-spec.md) for request details.

## Streaming

Streaming is a supported SmartVoice feature for processing audio as it arrives. It offers three workflows: live transcription with original subtitles, Chinese↔English translated subtitles, and spoken interpretation with translated speech. Use the browser workspace at `/console/streaming` to process an audio file, or connect a client that sends real-time PCM audio through `WS /v1/audio/stream`. The Console reports which model chains are ready for the selected workflow.

For API clients, check `GET /v1/audio/stream/capabilities` before starting a session. The WebSocket contract is versioned as `smartvoice.stream.v1`; clients send 16 kHz, mono PCM16 frames and receive incremental transcript/translation events and, for spoken interpretation, ordered audio chunks. The browser workspace includes subtitle revision handling, ordered playback, and reconnect support while the service process remains active. Reconnection does not survive a service restart.

Install the required streaming model components for the language direction and mode. All workflows need online speech recognition and punctuation; translated subtitles add the appropriate translation model, and spoken interpretation also needs a compatible TTS model. Translation model assets are prepared during installation, which requires the `streaming` and `model-preparation` extras. See [Model management](#model-management) and the [streaming setup and API contract](doc/streaming.md).

## Model management

SmartVoice keeps application code and model files separate. Browse and install models in the Console, with the CLI, or through model download jobs in the API. Availability checks validate both model files and the active runtime. Model downloads need internet access; inference with installed models can run offline. Model and voice licenses may differ from SmartVoice's license.

### Speech models

| Model ID | Task | Languages / voices | Estimated model files | Hot-request RTF | req/s per CPU core |
|---|---|---|---:|---:|---:|
| `stt-whisper-base-multilingual-int8` | STT | Auto detection; English, Chinese, Japanese, Korean, French, German | ~0.16 GB | 0.052 | 1.237 |
| `stt-sensevoice-small-int8` | STT | Chinese, English, Cantonese, Japanese, Korean | ~0.24 GB | 0.017 | 4.169 |
| `stt-qwen3-asr-600m-int8` | STT | 30 language codes including Cantonese; automatic detection | ~0.99 GB | 0.274 | 0.643 |
| `tts-kokoro-multilingual-v1-1-zh-en` | TTS | Chinese, English; 103 speakers | ~0.43 GB | 0.324 | 0.329 |
| `tts-matcha-zh-baker` | TTS | Chinese; one voice | ~0.15 GB | 0.030 | 2.379 |
| `tts-supertonic-v3-multilingual-int8` | TTS | 31 languages; no Chinese | ~0.15 GB | 0.236 | 0.692 |
| `tts-qwen3-0-6b-customvoice` | TTS | 10 languages; 9 preset voices | ~2.50 GB | 0.557 | Not measured |

### Streaming model components

Streaming combines an online recognizer and punctuation stage with optional translation and a speech synthesizer for spoken interpretation. Install only the components needed for the language direction and mode:

| Model ID | Role | Languages |
|---|---|---|
| `streaming-stt-paraformer-zh-en-int8` | Online speech recognition | Chinese, English |
| `streaming-stt-zipformer-en-int8` | Online speech recognition | English |
| `streaming-ct-transformer-zh-en` | Punctuation | Chinese, English |
| `streaming-mt-opus-zh-en-int8` | Translation | Chinese → English |
| `streaming-mt-m2m100-zh-en-int8` | Translation | English → Chinese |

Streaming speech synthesis reuses the regular TTS models above. See [Streaming](#streaming) for setup and supported workflows.

For the speech models above, RTF is the median for warm requests (Chinese where supported; English for Supertonic); lower is faster relative to audio duration. Throughput per CPU core is measured at the highest passing parallel request rate. Both are representative measurements from an Apple Silicon Mac running macOS 27.0, with 10 logical CPUs, 16 GiB RAM, Python 3.11.9, and sherpa-onnx 1.13.8. Sherpa tests used two instances, two threads per instance, and a four-request waiting limit. Qwen3-TTS uses one serialized native runtime; its per-core rate is unavailable because its engine process was not fully included in CPU sampling. See the [benchmark reports](benchmarks/README.md#concurrency-and-request-experience).

Sizes are approximate unpacked model-file sizes; see [`catalog/models.json`](catalog/models.json) for exact assets. Qwen3-TTS 0.6B needs at least 6 GiB free during installation. Its Windows runtime requires AVX2 and FMA. The published Linux x86_64 wheel requires glibc 2.38 or newer; see [platform compatibility](doc/architecture-guidelines.md#build-and-hardware-qualifications).

### Install and manage

Install a model by ID, or use `all` to install the full catalog and language detector (this may require several gigabytes of disk space):

```bash
python -m smartvoice models list
python -m smartvoice models install stt-sensevoice-small-int8
python -m smartvoice models install tts-kokoro-multilingual-v1-1-zh-en
python -m smartvoice models refresh
python -m smartvoice models export stt-sensevoice-small-int8 ./sensevoice.smartvoice.zip
python -m smartvoice models import ./sensevoice.smartvoice.zip
python -m smartvoice models uninstall stt-sensevoice-small-int8
```

For a Hugging Face mirror, pass `--source` when all files for that model are hosted there; downloaded files are still checked against catalog-pinned hashes. Models can also be installed from the Console or through API download jobs. A model currently in use cannot be uninstalled.

To install streaming translation models from their catalog sources, install the conversion dependencies first:

```bash
python -m pip install 'smartvoice[streaming,model-preparation]'
```

Translation assets are verified and prepared during installation; inference uses the prepared assets locally. For the full CLI and API details, see [model management](doc/streaming.md#最小安装与启动流程) and the [API specification](doc/api-spec.md).

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

Open the [SmartVoice Console](http://127.0.0.1:8000/console). Use **Models** to browse the catalog and install or manage models. Downloads require internet access and are stored outside the source checkout, under `~/Library/Application Support/SmartVoice` on macOS, `~/.smartvoice` on Linux, or `%LOCALAPPDATA%\SmartVoice` on Windows. After installation, inference runs locally. See [Model management](#model-management) for models, languages, and approximate sizes.

In the Console workspace, try speech recognition and synthesis with installed models. Use the generated API examples for your selected models and settings, and edit Smart Router priorities in the Console; saved routing changes take effect immediately. The interactive [API docs](http://127.0.0.1:8000/docs) are also available.

To try streaming, install the model components for a workflow and open [Streaming](http://127.0.0.1:8000/console/streaming). The page can stream a local audio file through transcription, translated subtitles, or spoken interpretation. For translation models, first install `smartvoice[streaming,model-preparation]` as described in [Model management](#model-management).

Service settings are stored in `<data_dir>/smartvoice.json`; router priorities are stored separately in `<data_dir>/router.json`.

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

TTS defaults to mono MP3 (96 kbps); set `"response_format":"wav"` to request WAV. See `/docs` or the [API specification](doc/api-spec.md) for request parameters, supported languages, response formats, limits, and errors.

### Stream audio

Check the available model chains before opening a streaming session:

```bash
curl http://127.0.0.1:8000/v1/audio/stream/capabilities
```

Connect to `ws://127.0.0.1:8000/v1/audio/stream` using protocol `smartvoice.stream.v1`. Configure a `transcription`, `translated_subtitles`, or `spoken_interpretation` session, then stream 16 kHz mono PCM16 audio frames. Clients receive incremental transcript and translation events; spoken interpretation also returns ordered audio chunks. The [streaming setup and API contract](doc/streaming.md) includes the client protocol and examples.

## Architecture

```text
Client / Agent / Browser Console ──► Versioned REST API + Streaming WebSocket
                                               │
Local CLI ─────────────────────────────────────┤
                                               ▼
                                      Application services
                         ┌─────────────────────┼──────────────────────┐
                         ▼                     ▼                      ▼
                  Transcription &         Smart routing          Streaming speech
                    synthesis             & model choice       recognition / translation
                         │                     │                  / speech output
                         └─────────────────────┼──────────────────────┘
                                               ▼
                                         Domain contracts
                               ┌───────────────┴───────────────┐
                               ▼                               ▼
                       Inference interfaces             Model management
                               │                               │
                               ▼                               ▼
                  sherpa-onnx and native Qwen       Catalog, downloads, and
                       runtime adapters               local model storage
```

The supported source and wheel deployments provide CPU inference on Windows x64, macOS Apple Silicon, and Linux x86_64. See [Architecture Summary and Guidelines](doc/architecture-guidelines.md) for backend and platform details.

## Development

```bash
python -m pip install -e ".[dev]"
python scripts/test.py ci
```

`python scripts/test.py regression` runs functional tests and real inference for models installed in the current environment. `python scripts/test.py full` adds long-input STT and real-model streaming regression; it requires the selected model assets and fixtures. See [`doc/testing.md`](doc/testing.md) for test modes and prerequisites.

See [`doc/releasing.md`](doc/releasing.md) for versioning, GitHub Releases, and optional PyPI publishing.

## Repository layout

```text
.github/       GitHub Actions workflows for CI and releases
assets/        Project logo and browser icons
benchmarks/    Speech quality, performance, and streaming evaluation tools and reports
catalog/       Model catalog, default routing table, and streaming workflows
config/        Example service configuration
doc/           API, architecture, testing, release, and product documentation
native/        Native Qwen3-TTS runtime source and build support
samples/       Multilingual speech samples and transcripts
scripts/       Build, test, model preparation, and validation commands
src/           Application API, Console, CLI, services, and inference adapters
tests/         Unit, API, streaming, and real-inference regression tests
pyproject.toml Package metadata, dependencies, and build configuration
```

## License

SmartVoice source code is licensed under the [Apache License 2.0](LICENSE). Model weights, voices, and other third-party assets may have separate license terms; review them before use or redistribution.
