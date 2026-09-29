# Test Suites

SmartVoice provides two standard test entry points. Benchmark and model quality evaluation workflows are documented separately and are not part of these suites.

## CI tests

Run the complete automated test suite without invoking real speech models:

```bash
python scripts/test.py ci
```

This suite covers settings, CLI behavior, REST API contracts and validation, routing, model catalog and file handling, download jobs, provider contracts, inference queue behavior, language detection, spoken-language asset management, and host metrics. External downloads and inference engines are mocked where needed. The real inference test is explicitly skipped, even if model files happen to be present locally.

CI installs the development dependencies and runs this entry point on every pull request and push to `main`.

## Full regression tests

Run the same complete functional suite and require real CPU inference through the REST routes. The integration tests exercise service discovery, model and capability reporting, routed and direct STT, Chinese/English speech-language detection, supported audio formats, malformed audio errors, routed/direct TTS, automatic text-language detection, and long-text synthesis.

```bash
python scripts/test.py regression
```

The regression entry point checks for the `[inference]` dependencies, the `stt-sensevoice-small-int8` model and its `zh.wav`/`en.wav` samples, the `tts-melo-zh-en` model, and verified Whisper Tiny language-ID assets before starting. Missing prerequisites cause a clear failure instead of silently skipping real inference.

Install dependencies and models first:

```bash
python -m pip install -e ".[inference,dev]"
python -m smartvoice models install stt-sensevoice-small-int8
python -m smartvoice models install tts-melo-zh-en
python -m smartvoice models install-language-id
```

The models are stored under the SmartVoice data directory. Set `SMARTVOICE_HOME` if they are installed in a non-default location. Regression tests use CPU inference and may take several minutes depending on the machine.
