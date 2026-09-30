# Test Suites

SmartVoice provides two standard test entry points. Benchmark and model quality evaluation workflows are documented separately and are not part of these suites.

## CI tests

Run the complete automated test suite without invoking real speech models:

```bash
python scripts/test.py ci
```

This suite covers settings, CLI behavior, REST API contracts and validation, routing, model catalog and file handling, download jobs, provider contracts, inference queue behavior, language detection, spoken-language asset management, and host metrics. External downloads and inference engines are mocked where needed. The real inference test is explicitly skipped, even if model files happen to be present locally.

The repository contains a GitHub Actions CI workflow that installs the development dependencies and runs this entry point on pull requests and pushes to `main`. CI is currently paused in the GitHub repository settings; until it is re-enabled, run this command locally before merging and do not expect a remote check to appear.

## Full regression tests

Run the same complete functional suite and exercise real CPU inference through the REST routes. The tests include the routed integration scenarios below plus one direct smoke test for every catalog model that is installed and runtime-available. Each model uses one representative language (Chinese when supported, otherwise English or the first concrete catalog language). Uninstalled models and models whose runtime is unavailable are reported as individual skips. Direct STT tests use `tests/fixtures/zh.wav`, a short Chinese sample generated locally with Kokoro TTS.

```bash
python scripts/test.py regression
```

The regression entry point requires the `[inference]` dependencies. Existing multi-route integration cases run when their SenseVoice, Kokoro, and Whisper Tiny language-ID assets are available; otherwise those cases are skipped. The per-catalog direct tests independently run for every installed model, while uninstalled models are skipped by model ID. This lets a local regression cover whichever supported models are present without requiring the full catalog.

Install dependencies and models first:

```bash
python -m pip install -e ".[inference,dev]"
# Install any catalog models you want to include in real inference coverage.
python -m smartvoice models install stt-sensevoice-small-int8
python -m smartvoice models install tts-kokoro-multilingual-v1-1-zh-en
# Optional: enables the existing routed language-identification scenarios.
python -m smartvoice models install-language-id
```

The models are stored under the SmartVoice data directory. Set `SMARTVOICE_HOME` if they are installed in a non-default location. Regression tests use CPU inference and may take several minutes depending on the machine.
