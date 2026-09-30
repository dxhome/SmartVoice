<div align="center">
  <img src="assets/smartvoice-logo.png" alt="SmartVoice logo" width="220">
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
| Platforms | Windows x64 and macOS Apple Silicon, run from source |
| Inference | sherpa-onnx and native C INT8 Qwen3-TTS adapter on macOS Apple Silicon |
| STT | Whisper Base multilingual, SenseVoice Small, Qwen3-ASR 0.6B |
| TTS | Kokoro 1.1, Matcha Baker, Supertonic 3, Qwen3-TTS 0.6B |
| Smart routing | Selects an installed model by task and language using an editable priority list; `smartvoice-auto` works for both STT and TTS |
| API | OpenAPI docs, transcription, speech synthesis, model catalog and runtime status |
| Model management | Install, uninstall, offline import/export, and resumable downloads |
| Not yet supported | GPU inference, streaming, Linux/Android, remote access, packaged installers, MCP |

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
| `tts-kokoro-multilingual-v1-1-zh-en` | TTS | Chinese, English; 103 speakers | ~0.41 GB | Chinese / English fallback |
| `tts-matcha-zh-baker` | TTS | Chinese; one voice | ~0.13 GB | Chinese fallback |
| `tts-supertonic-v3-multilingual-int8` | TTS | 31 languages; no Chinese | ~0.15 GB | Recommended for its supported languages |
| `tts-qwen3-0-6b-customvoice` | TTS | 10 languages; 9 preset voices | ~2.50 GB | Recommended for Chinese and measured multilingual routes |

Model sizes are estimates of the unpacked model files, rounded to two decimal places in decimal GB; actual disk use can vary slightly. Installation may need additional temporary space. Qwen3-TTS 0.6B needs at least 6 GiB free disk space during installation. See [`catalog/models.json`](catalog/models.json) for the byte estimates, exact language codes, sources, and model details.

Melo TTS has been removed from the supported catalog, and Supertonic 3 does not support Chinese. Existing model files are left on disk. If an older `<data_dir>/router.json` references the removed Melo model or routes Supertonic for Chinese, SmartVoice rejects that saved routing table, uses the built-in router, and reports a warning. Remove those stale entries and run `python -m smartvoice router reload` to clear the warning.

For routed STT requests with `language=auto`, SmartVoice can use an optional dedicated spoken-language detector. Install it explicitly with `python -m smartvoice models install-language-id`; without it, SmartVoice falls back to an installed STT model that supports automatic language detection. The detector is not downloaded during service startup. Its availability is reported by `/v1/capabilities`.

## Quick start

SmartVoice requires Python 3.11 or newer and runs inference on CPU. The service binds to `127.0.0.1` by default.

### 1. Install SmartVoice

From a source checkout, create a virtual environment and install the inference dependencies.

macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[inference]"
```

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[inference]"
```

On macOS Apple Silicon, the source install builds the native INT8 Qwen3-TTS runtime using Xcode Command Line Tools. Qwen3-TTS is not available in native Windows builds. When the PyPI package is available, install it with `python -m pip install "smartvoice[inference]"` instead.

### 2. Download models

Install a basic STT and TTS model. Model files are downloaded once and stored outside the source checkout, under `~/Library/Application Support/SmartVoice` on macOS or `%LOCALAPPDATA%\SmartVoice` on Windows.

macOS, with the virtual environment activated:

```bash
python -m smartvoice models install stt-sensevoice-small-int8
python -m smartvoice models install tts-kokoro-multilingual-v1-1-zh-en
```

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m smartvoice models install stt-sensevoice-small-int8
.\.venv\Scripts\python.exe -m smartvoice models install tts-kokoro-multilingual-v1-1-zh-en
```

On macOS Apple Silicon, install Qwen3-TTS to use the configured Chinese and multilingual routes:

```bash
python -m smartvoice models install tts-qwen3-0-6b-customvoice
```

On Windows, use the `.venv` Python for model installation commands. Qwen3-TTS requires the macOS Apple Silicon native runtime and is unavailable on Windows.

The first model installation requires internet access. After installation, inference runs locally. See [Model catalog](#model-catalog) for supported languages and estimated sizes.

### 3. Start SmartVoice

macOS, with the virtual environment activated:

```bash
python -m smartvoice --host 127.0.0.1 --port 8000
```

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m smartvoice --host 127.0.0.1 --port 8000
```

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

The CLI supports `models list`, `install`, `uninstall`, `export`, and `import`. Models can also be installed through API download jobs. Exported model packages can be transferred to offline machines and imported there. An installed model cannot be uninstalled while the service is using it.

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

Callers use versioned API and capability endpoints; inference details stay behind provider and repository interfaces. The source deployment supports CPU inference on Windows x64 and macOS Apple Silicon. Qwen3-TTS uses the same native C INT8 adapter where its platform binary is available; packaged support targets macOS Apple Silicon. GPU providers, streaming, Linux and Android runtimes are not available in SmartVoice.

## Development

```bash
python -m pip install -e ".[inference,dev]"
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
