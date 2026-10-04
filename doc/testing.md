# Test Suites

SmartVoice provides three standard test entry points. Benchmark and model quality evaluation workflows are documented separately and are not part of these suites.

## CI tests

Run the complete automated test suite without invoking real speech models:

```bash
python scripts/test.py ci
```

This suite covers settings, CLI behavior, REST API contracts and validation, routing, model catalog and file handling, download jobs, provider contracts, inference queue behavior, language detection, spoken-language asset management, and host metrics. External downloads and inference engines are mocked where needed. The CI entry point excludes `test_real_inference.py` and `test_stt_audio_regression.py` so it remains fast and does not depend on local models.

The active GitHub Actions CI workflow installs the development dependencies and runs this entry point on pull requests and pushes to `main`. It also builds and checks the source distribution and the Linux wheel.

## Default regression tests

Run the same complete functional suite and exercise real CPU inference through the REST routes. Real inference tests run by default when their dependencies and required models are present; no environment flag is needed. The tests include the routed integration scenarios below plus one direct smoke test for every catalog model that is installed and runtime-available. Each model uses one representative language (Chinese when supported, otherwise English or the first concrete catalog language). Uninstalled models and models whose runtime is unavailable are reported as individual skips. Direct STT tests use `tests/fixtures/zh.wav`, a short Chinese sample generated locally with Kokoro TTS.

```bash
python scripts/test.py regression
```

The default regression includes all ordinary functional checks and the existing real-inference smoke tests. It also runs fixed short Chinese and English requests, model-specific window boundary cases, and 75-second segmented inputs for SenseVoice, Qwen3-ASR, and Whisper. Missing models are skipped individually so this suite can run on a normal development installation. These STT cases check successful responses, non-empty text, model/language/duration metadata, and segmentation behavior. They do not score transcription accuracy; see [`../tests/fixtures/stt/README.md`](../tests/fixtures/stt/README.md).

## Full test set

```bash
python scripts/test.py full
```

Full discovers and runs the same complete test tree as CI and default regression, then adds 300- and 590-second English and Chinese recordings for each of the three STT models (12 long model-language-duration inferences). The recordings are created deterministically from the committed reference utterances, separated by silence, and padded to the exact target duration. Full mode requires all three STT models to be installed and runtime-available, plus the validated `sherpa-onnx 1.13.8+smartvoice.whisper2` wheel. Missing prerequisites fail the run rather than skipping long-audio coverage. It stays out of routine CI and default regression because its long-audio inferences are substantially more expensive.

The `ci` mode remains the lightweight test set used by GitHub Actions. The `regression` mode contains the full existing functional suite and its inference checks with the quick and boundary STT cases; `full` contains all CI and regression cases plus long-audio expansion.

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
