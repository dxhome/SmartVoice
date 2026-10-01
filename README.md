<div align="center">
  <img src="https://raw.githubusercontent.com/dxhome/SmartVoice/main/assets/smartvoice-logo.png" alt="SmartVoice logo" width="220">
  <p><strong>Local speech recognition and synthesis, with language-aware model routing.</strong></p>
  <p>Multilingual STT and TTS · CPU inference · OpenAPI · No per-request API fee</p>
</div>

# SmartVoice

SmartVoice is a local speech-to-text (STT) and text-to-speech (TTS) service. It offers both through one OpenAI-style audio API, with models installed and run on your machine.

## Why SmartVoice?

- **One local service for speech:** Transcribe audio and synthesize speech through the same API, without sending media to a cloud provider.
- **Choose models by language automatically:** Use SmartVoice's virtual model IDs to route each request to an installed model configured for that language.
- **Works offline after setup:** Install model files once, then run inference locally on CPU with no per-request API fee.

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
| Not yet supported | GPU inference, streaming, Linux/Windows ARM64, Intel macOS, Android, HTTPS, packaged installers, MCP |

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

Models use the inference adapter identified in the catalog. Language availability describes catalog capability, not comparative quality; model and voice licenses may differ from SmartVoice's license. The recommended STT models are SenseVoice and Qwen3-ASR. TTS recommendations are reflected in the [Smart routing](#smart-routing) priorities. Supertonic 3 does not support Chinese. Benchmark coverage varies by language, and TTS listening quality has not been rated with MOS.

| Model ID | Task | Languages / voices | Estimated model files | Recommendation |
|---|---|---|---|---|
| `stt-whisper-base-multilingual-int8` | STT | Auto detection; English, Chinese, Japanese, Korean, French, German | ~0.16 GB | — |
| `stt-sensevoice-small-int8` | STT | Chinese, English, Cantonese, Japanese, Korean | ~0.24 GB | Recommended |
| `stt-qwen3-asr-600m-int8` | STT | 30 language codes including Cantonese; automatic detection | ~0.99 GB | Recommended |
| `tts-kokoro-multilingual-v1-1-zh-en` | TTS | Chinese, English; 103 speakers | ~0.43 GB | Chinese / English fallback |
| `tts-matcha-zh-baker` | TTS | Chinese; one voice | ~0.15 GB | Chinese fallback |
| `tts-supertonic-v3-multilingual-int8` | TTS | 31 languages; no Chinese | ~0.15 GB | Recommended for its supported languages |
| `tts-qwen3-0-6b-customvoice` | TTS | 10 languages; 9 preset voices | ~2.50 GB | Recommended for Chinese and measured multilingual routes |

Catalog model sizes are estimates of the unpacked model files, rounded to two decimal places in decimal GB; actual disk use can vary slightly. The web test page shows the recorded on-disk size when available and otherwise labels the catalog estimate. Installation may need additional temporary space. Qwen3-TTS 0.6B needs at least 6 GiB free disk space during installation. See [`catalog/models.json`](catalog/models.json) for the byte estimates, exact language codes, sources, and model details.

The native Qwen3-TTS runtime has CPU-specific requirements: its Windows x64 build requires AVX2 and FMA, while Linux builds use the instruction set exposed to the builder. See the [platform compatibility notes](doc/architecture-guidelines.md#build-and-hardware-qualifications) before choosing a platform wheel for older CPUs or Linux distributions.

Melo TTS has been removed from the supported catalog, and Supertonic 3 does not support Chinese. Existing model files are left on disk. If an older `<data_dir>/router.json` references the removed Melo model or routes Supertonic for Chinese, SmartVoice rejects that saved routing table, uses the built-in router, and reports a warning. Remove those stale entries and run `python -m smartvoice router reload` to clear the warning.

For routed STT requests with `language=auto`, SmartVoice uses the Whisper Tiny spoken-language detector when it is installed, then routes to an STT model for the detected language. `python -m smartvoice models install all` installs this detector by default. Without it, SmartVoice falls back to an installed STT model that supports automatic language detection. The detector is not downloaded during service startup. Its availability is reported by `/v1/capabilities`.

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

Activate `.venv` before running the model and server commands below. On Windows PowerShell, you can instead keep the environment inactive and replace `python` with `.\.venv\Scripts\python.exe` in those commands.

### 2. Download models

Install a basic STT and TTS model. Model files are downloaded once and stored outside the source checkout, under `~/Library/Application Support/SmartVoice` on macOS, `~/.smartvoice` on Linux, or `%LOCALAPPDATA%\SmartVoice` on Windows.

```bash
python -m smartvoice models install stt-sensevoice-small-int8
python -m smartvoice models install tts-kokoro-multilingual-v1-1-zh-en
python -m smartvoice models install-language-id
```

On the macOS Apple Silicon, Windows x64, and Linux x86_64 PyPI wheels, the Qwen3-TTS runtime is bundled. From a source checkout, build the runtime on Windows and Linux as described above; the macOS source build compiles it during installation. Then install Qwen3-TTS to use the configured Chinese and multilingual routes:

```bash
python -m smartvoice models install tts-qwen3-0-6b-customvoice
```

Without the native runtime in a source deployment, Qwen3-TTS is listed as unavailable and other installed TTS models remain usable.

The first model installation requires internet access. After installation, inference runs locally. See [Model catalog](#model-catalog) for supported languages and estimated sizes.

### 3. Start SmartVoice

By default the HTTP service listens only on `127.0.0.1`. To allow other machines to connect, explicitly bind to an external interface, for example:

```bash
python -m smartvoice --host 127.0.0.1 --port 8000
# To listen on all IPv4 interfaces:
python -m smartvoice --host 0.0.0.0 --port 8000
```

External binding is plain HTTP without API authentication. Use it only on a trusted network and configure the machine firewall as needed. HTTPS is not currently supported.

Open the [test page](http://127.0.0.1:8000/test) to verify basic STT and TTS functionality with your installed models. Open [API docs](http://127.0.0.1:8000/docs) for endpoint details. Service settings are stored in `<data_dir>/smartvoice.json`; routing priorities are stored separately in `<data_dir>/router.json` and can be reloaded with `python -m smartvoice router reload`.

### 4. Connect an agent or application

In your agent or application's settings, choose **OpenAI-compatible API** and set the SmartVoice API base URL to:

```text
http://127.0.0.1:8000/v1
```

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
  --output speech.wav
```

TTS returns mono WAV audio. Requests support up to 4,000 characters; generated audio is limited to 180 seconds or 32 MiB by default. See `/docs` or the [API specification](doc/api-spec.md) for available parameters, language behavior, and error codes.

## Manage models

The CLI supports `models list`, `install`, `uninstall`, `export`, and `import`. `models list` groups models by installed state, then by STT, TTS, and SmartVoice native; each model shows its availability, and unavailable models include a reason. Availability checks both model-file integrity and the active inference runtime/device. Use `python -m smartvoice models install all` to install every catalog model that is not already installed and the Whisper Tiny language detector. Model downloads run sequentially from their catalog sources; invalid existing model directories are skipped with a repair hint. Installing all models can require several gigabytes of disk space. On Windows x64, Qwen3-TTS is available only when its native runtime has been built; see [Build the Qwen3-TTS runtime on Windows](#build-the-qwen3-tts-runtime-on-windows). Models can also be installed through API download jobs. Exported model packages can be transferred to offline machines and imported there. An installed model cannot be uninstalled while the service is using it.

Install a model with `python -m smartvoice models install <model-id>`. By default, SmartVoice uses the model's catalog source. For Hugging Face models, pass `--source` with a compatible mirror base URL to use another source, such as:

```bash
python -m smartvoice models install stt-qwen3-asr-600m-int8 --source https://hf-mirror.com
```

The base URL is combined with the catalog-pinned repository, revision, and file paths. This option is available only when all required model files are hosted on Hugging Face. Downloaded files are still checked against their catalog SHA-256 values.

```bash
python -m smartvoice models list
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

Callers use versioned API and capability endpoints; inference details stay behind provider and repository interfaces. The source deployment supports CPU inference on Windows x64, macOS Apple Silicon, and Linux x86_64. Qwen3-TTS uses the native C INT8 adapter on all three platforms; Linux source deployments build against the system OpenBLAS library as described above. GPU providers, streaming, and Android runtimes are not available in SmartVoice. See [Architecture Summary and Guidelines](doc/architecture-guidelines.md) for the detailed matrix and model availability semantics.

## Development

```bash
python -m pip install -e ".[dev]"
python scripts/test.py ci
```

Run `python scripts/test.py regression` to execute the full functional suite including real STT/TTS inference. It directly smoke-tests each catalog model installed and available on the current runtime, using one representative language per model; unavailable models are skipped individually. The existing route integration scenarios also run when their optional models and language-ID assets are installed. See [`doc/testing.md`](doc/testing.md) for details and [`benchmarks/README.md`](benchmarks/README.md) for model quality, latency, and concurrency comparisons.
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
