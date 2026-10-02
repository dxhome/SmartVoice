# Test Suites

SmartVoice provides two standard test entry points. Benchmark and model quality evaluation workflows are documented separately and are not part of these suites.

## CI tests

Run the complete automated test suite without invoking real speech models:

```bash
python scripts/test.py ci
```

This suite covers settings, CLI behavior, REST API contracts and validation, routing, model catalog and file handling, download jobs, provider contracts, inference queue behavior, language detection, spoken-language asset management, and host metrics. External downloads and inference engines are mocked where needed. The CI entry point excludes `test_real_inference.py` so it remains fast and does not depend on local models.

The active GitHub Actions CI workflow installs the development dependencies and runs this entry point on pull requests and pushes to `main`. It also builds and checks the source distribution and the Linux wheel.

## Full regression tests

Run the same complete functional suite and exercise real CPU inference through the REST routes. Real inference tests run by default when their dependencies and required models are present; no environment flag is needed. The tests include the routed integration scenarios below plus one direct smoke test for every catalog model that is installed and runtime-available. Each model uses one representative language (Chinese when supported, otherwise English or the first concrete catalog language). Uninstalled models and models whose runtime is unavailable are reported as individual skips. Direct STT tests use `tests/fixtures/zh.wav`, a short Chinese sample generated locally with Kokoro TTS.

```bash
python scripts/test.py regression
```

SmartVoice's required dependencies include its supported inference runtimes. Existing multi-route integration cases run when their SenseVoice, Kokoro, and Whisper Tiny language-ID assets are available; otherwise those cases are skipped. The per-catalog direct tests independently run for every installed model, while uninstalled models are skipped by model ID. This lets a local regression cover whichever supported models are present without requiring the full catalog.

Install dependencies and models first:

```bash
python -m pip install -e ".[dev]"
# Install any catalog models you want to include in real inference coverage.
python -m smartvoice models install stt-sensevoice-small-int8
python -m smartvoice models install tts-kokoro-multilingual-v1-1-zh-en
# Optional: enables the existing routed language-identification scenarios.
python -m smartvoice models install-language-id
```

The models are stored under the SmartVoice data directory. Set `SMARTVOICE_HOME` if they are installed in a non-default location. Regression tests use CPU inference and may take several minutes depending on the machine.

Elastic runtime concurrency and lifecycle tests are included in the fast suite. The [production fixed-arrival HTTP benchmark](../benchmarks/elastic-pool/README.md) tests the real application path, validates responses, checks for queue buildup and drain, and records latency, CPU, RSS, and instance warm-up costs. Formal reports use one model per platform JSON and keep this same-model capacity category alongside that model's quality and serial performance results. Current macOS results are available for [SenseVoice](../benchmarks/result/macos-arm64-stt-sensevoice-small-int8-fleurs-standard-2026-09-29.json), [Matcha](../benchmarks/result/macos-arm64-tts-matcha-zh-baker-fleurs-standard-2026-09-29.json), and [Supertonic](../benchmarks/result/macos-arm64-tts-supertonic-v3-multilingual-int8-fleurs-standard-2026-09-29.json). SenseVoice passed at 14 req/s and Matcha at 8 req/s under the tested configuration; Supertonic's 1.9 req/s run met observed latency/success checks but has only 1,000 requests, so its report remains pending formal confirmation at the 3,000-request minimum. These are measured rates on one machine and workload, not general service guarantees. See the [benchmark criteria](../benchmarks/README.md#concurrency-and-request-experience) before comparing models.
