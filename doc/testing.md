# Test Suites

SmartVoice provides three standard test entry points. Benchmark and model quality evaluation workflows are documented separately and are not part of these suites.

Speech-model verification experiments use one command entry point: `python scripts/validate_speech.py <workflow> [options]`. Run `python scripts/validate_speech.py --help` to list workflows and `python scripts/validate_speech.py <workflow> --help` for each workflow's options. The focused runners remain separate internal modules so each can keep its own model/process lifecycle; build scripts are not validation commands.

## CI tests

Run the complete automated test suite without invoking real speech models:

```bash
python scripts/test.py ci
```

This suite covers settings, CLI behavior, REST API contracts and validation, routing, model catalog and file handling, download jobs, provider contracts, inference queue behavior, language detection, spoken-language asset management, and host metrics. External downloads and inference engines are mocked where needed. The CI entry point excludes `test_real_inference.py` and `test_stt_audio_regression.py` so it remains fast and does not depend on local models.

The CI entry point also excludes `test_streaming_real_long.py`. It retains shared
worker fault tests and the bounded long-audio generator/output-audit tests without
loading speech models.

The active GitHub Actions CI workflow installs the development dependencies and runs this entry point on pull requests and pushes to `main`. It also builds and checks the source distribution and the Linux wheel.

## Default regression tests

Run the same complete functional suite and exercise real CPU inference through the REST routes. Real inference tests run by default when their dependencies and required models are present; no environment flag is needed. The tests include the routed integration scenarios below plus one direct smoke test for every catalog model that is installed and runtime-available. Each model uses one representative language (Chinese when supported, otherwise English or the first concrete catalog language). Uninstalled models and models whose runtime is unavailable are reported as individual skips. Direct STT tests use `tests/fixtures/zh.wav`, a short Chinese sample generated locally with Kokoro TTS.

```bash
python scripts/test.py regression
```

The default regression includes all ordinary functional checks and the existing real-inference smoke tests. It also runs fixed short Chinese and English requests, model-specific window boundary cases, and 75-second segmented inputs for SenseVoice, Qwen3-ASR, and Whisper. Missing models are skipped individually so this suite can run on a normal development installation. These STT cases check successful responses, non-empty text, model/language/duration metadata, and segmentation behavior. They do not score transcription accuracy; see [`../tests/fixtures/stt/README.md`](../tests/fixtures/stt/README.md).

The long TTS case explicitly selects Supertonic. Qwen3-TTS remains covered by short direct/routed compatibility checks; its long-text optimization is deferred.

## Full test set

```bash
python scripts/test.py full
```

Full discovers and runs the same complete test tree as CI and default regression, then adds 300- and 590-second English and Chinese recordings for each of the three STT models (12 long model-language-duration inferences). The recordings are created deterministically from the committed reference utterances, separated by silence, and padded to the exact target duration. Full mode requires all three STT models to be installed and runtime-available, plus the validated `sherpa-onnx 1.13.8+smartvoice.whisper2` wheel. Missing prerequisites fail the run rather than skipping long-audio coverage. It stays out of routine CI and default regression because its long-audio inferences are substantially more expensive.

The `ci` mode remains the lightweight test set used by GitHub Actions. The `regression` mode contains the full existing functional suite and its inference checks with the quick and boundary STT cases; `full` contains all CI and regression cases plus long-audio expansion.

### Real streaming hour cases

Full includes `tests/test_streaming_real_long.py`: six separate cases, each with
3600 seconds of checksum-pinned cyclic FLEURS PCM. The real online ASR and enabled
formatting/translation/TTS adapters execute through the production session and
shared worker pool. Input is accelerated according to actual consumption, with
at most two input frames buffered. Queues, compute permits, semantic commitment
and native deadlines remain active; clocks and models are not mocked.

```sh
SMARTVOICE_HOME=/absolute/model/home python scripts/test.py full --streaming-only
```

This scoped command omits REST/STT suites. Unfiltered `full` requires both its
existing STT prerequisites and every default streaming chain model, including
Chinese mixed-script fallback, the streaming extra and pinned fixtures. Missing
prerequisites fail; an enabled full hour case cannot be silently skipped.

The tests clone immutable asset files into isolated temporary model roots using
hardlinks (copy fallback where needed), leaving user configuration and derived
runtime state separate. They generate only bounded PCM frames, incrementally
audit output and verify input coverage, complete drain, audio/text/reference
alignment, history/context bounds, model-use leases, compute permits and owned
process shutdown. Per-route success/failure reports remain in
`.smartvoice-dev/full-streaming/<run-id>/`. The per-route wall deadline is one hour.

This covers one hour of **audio content**, not one hour of wall-clock stability.
It does not assert real-time TTFO/P90, natural meeting quality, WebSocket/browser
playback or concurrency capacity. Those remain separate measured regressions.

## Real TCP HTTP completion checks

After the suites above, run the explicit local HTTP matrix:

```bash
python scripts/validate_speech.py stt-http --report sandbox/tts-output/runs/stt-http-current.json
```

This starts an owned loopback server on an ephemeral port, accesses verified model assets through a repository with an immutable installed snapshot, and shuts down the server and removes isolated temporary state on exit. Only LID assets are linked into that state; integrity caches and the routing snapshot are copied locally. The real-inference test suites use the same isolation helper. The HTTP matrix requires all three STT models, installed LID assets, Supertonic, and the validated Whisper repair wheel. It neither downloads models nor changes the user's routing configuration.

The matrix covers 75-second WAV/MP3 inputs with explicit Chinese and model-auto language, 300-second English WAV, 600-second Chinese MP3, and bilingual smart routing. Successful pool-operation counts must equal returned chunk counts; direct requests must perform zero LID calls and routed auto requests exactly one. It also checks 601-second rejection, invalid audio, Supertonic MP3/WAV compatibility, real HTTP deadlines and client disconnects, and final queue/pool drain. Metadata-only JSON reports contain no transcription text. These synthetic fixtures establish functional completion, not natural-speech quality or concurrency capacity. Process peak RSS is cumulative for this harness and is not a per-model memory measurement.

For an explicitly selected local MP3, add `--sample /absolute/path/sample.mp3`. Add `--sample-only` to run that recording through the three STT models with explicit Chinese and auto, plus smart routing, while retaining error/resource checks and omitting the synthetic success matrix. The report includes the filename, SHA256 and decoded duration, never the transcript or original audio. Selecting a file does not establish its identity against a historical upload or prove transcription accuracy.

Real queue admission supplements use the installed Qwen3-ASR runtime:

```bash
python scripts/validate_speech.py stt-admission --report sandbox/tts-output/runs/stt-admission.json
```

This creates four isolated TCP applications: transport capacity exhausted, transport queue wait expired, model capacity exhausted and model queue wait expired. One prewarmed native instance processes 70 seconds of Chinese audio while a short request exercises rejection. The probe checks HTTP 503 / `inference_overloaded`, the rejection source, exact background chunk/native-call counts, final queue/pool drain and a successful recovery request. It uses actual native inference, without blocking model mocks or changing production defaults.

## Local natural-speech policy experiments

```bash
python scripts/validate_speech.py stt-policy --report sandbox/tts-output/runs/stt-policy-confirmed.json
python scripts/validate_speech.py stt-policy --legacy-only --report sandbox/tts-output/runs/stt-policy-legacy-language.json
python scripts/validate_speech.py stt-policy --diagnostic --report sandbox/tts-output/runs/stt-natural-diagnostic-final.json
python scripts/validate_speech.py stt-policy --quiet-boundaries --report sandbox/tts-output/runs/stt-quiet-boundaries.json
python scripts/validate_speech.py stt-policy --relative-boundaries --report sandbox/tts-output/runs/stt-relative-boundaries.json
python scripts/validate_speech.py stt-policy --relative-boundaries --minimum-quiet-seconds 8 --report sandbox/tts-output/runs/stt-relative-boundaries-8.json
```

These explicit experiments require the existing pinned local FLEURS manifest/audio and validated Whisper wheel; missing inputs fail without downloads. They compare three window/overlap policies on four Chinese and four English utterances and their mixed concatenation, retaining digests, source attribution, CER/WER and actual recognizer initialization events. The legacy probe reconstructs the previous native-call language sequence for mixed input only, not old response metadata. It does not change product defaults. See [language/cache/policy results](archive/stt/stt-language-cache-policy-validation.md) for the limited sample scope and remaining alignment work.

Diagnostic mode uses ten clean recordings per language, verifies unsegmented original-clip results under explicit language and auto, and compares current long-window results with the original recording boundaries and raw native-window concatenation. Timestamp requests are enabled; reports retain only scores and metadata. Known recording boundaries are diagnostic references, not word alignment or a production capability.

Quiet-boundary mode uses the same twenty recordings and compares declared maximum windows with candidates that search for a 200 ms quiet interval from the second second onward, with all-cut or forced-only overlap. Window start/overlap, native duration, model-reported language and output length are recorded. It verifies finite coverage through the actual end. The optional adapter minimum is experimental; production assembly retains the last-third search and all-cut overlap. An acoustic quiet interval is not a verified sentence or word boundary.

Relative-boundary mode probes an experimental RMS threshold: the lower of 0.006 and 10% of the search interval's 95th-percentile RMS, keeping the original threshold for exact silence. It compares declared-window/all-overlap and early-cut/forced-overlap candidates. `--minimum-quiet-seconds` varies the minimum for early candidates. Reports annotate known recording intersections and energy above the original absolute threshold; neither is asserted word alignment or VAD. All policies preserve original samples sent to inference. Production defaults do not enable relative thresholds. See [admission and boundary results](archive/stt/stt-admission-boundary-validation.md) for completed runs and policy decisions.

## Continuous speech acceptance workflow

The current implementation task prioritizes SenseVoice. Scope commands explicitly; a scoped full run is not an all-model full run:

```bash
python scripts/validate_speech.py stt-quality --models stt-sensevoice-small-int8 --resume --report sandbox/tts-output/runs/stt-sensevoice-candidates.json
python scripts/validate_speech.py stt-http --models stt-sensevoice-small-int8 --report sandbox/tts-output/runs/stt-sensevoice-http.json
python scripts/validate_speech.py stt-admission --model stt-sensevoice-small-int8 --report sandbox/tts-output/runs/stt-sensevoice-admission.json
python scripts/validate_speech.py stt-load --models stt-sensevoice-small-int8 --interval .2 --report sandbox/tts-output/runs/stt-sensevoice-load.json
python scripts/test.py full --stt-models stt-sensevoice-small-int8
```

Unfiltered `full` still requires all three STT models and the Whisper repair wheel. Explicit selection requires the selected models; the repair wheel is required when Whisper is selected. Test selection is process-local and does not alter production capabilities or settings. Direct-model HTTP cases honor `--models`; virtual automatic routing retains actual route configuration and records the selected model separately. Cross-model SenseVoice load uses Qwen3-ASR, and lifecycle checks use the selected background model. Whisper window/decoder optimization is deferred.

Run these explicit commands separately from other native model workloads:

```bash
python scripts/validate_speech.py prepare-corpus --help
python scripts/validate_speech.py prepare-corpus --output sandbox/stt-continuous
python scripts/validate_speech.py stt-quality --report sandbox/tts-output/runs/stt-candidates-final.json
python scripts/validate_speech.py stt-load --report sandbox/tts-output/runs/stt-current-load.json
python scripts/test.py full
```

The preparation command is an explicit network operation. It downloads pinned AISHELL-4 test recordings and AMI manual v1.6.2 annotations plus headset mixes; ordinary startup and test discovery do not download this corpus. The manifest contains source URLs, revisions, licenses, full-file SHA256 values, fixed channels, timed annotation extraction rules and derived clip hashes. It selects two recordings per language in recording-ID order and retains continuous 75/300/590-second prefixes. Reference text belongs in the local corpus manifest; inference reports do not retain transcripts. Preserve license and attribution when sharing derived audio. AISHELL-4 uses CC BY-SA 4.0; AMI uses CC BY 4.0.

Candidate trials use the twelve continuous clips and existing FLEURS Chinese, English and mixed stitched controls. Three fresh subprocesses per model and policy alternate execution order. Current model window maxima remain unchanged; candidates use relative quiet thresholds, forced-cut-only overlap and minimum windows of eight seconds for SenseVoice/Whisper or two seconds for Qwen3-ASR. `--resume` reuses only completed trials with matching production source fingerprints, dependencies, model manifests, settings and sample hashes; other attempts remain separate files. CER/WER retain case folding and punctuation removal without numeral or script equivalence. Overlap duration and annotations crossing clip endpoints are recorded. Neither known quiet regions nor model timestamps replace a human boundary audit.

The mixed-load tool screens a fixed arrival rate, then runs 500 short requests per policy and workload. A twenty-request screen does not establish stability: confirmation must retain full success, stable waits and arrival scheduling within the configured interval. Failed confirmations remain failed evidence; lower-rate diagnostic runs with fewer than 500 requests cannot pass the recommendation gate. It tests a 120-second synthetic background STT with same-model STT, different-model STT or Supertonic TTS. By default it tests Qwen3-ASR and models whose completed quality trial passes every language/group gate. `--models` selects explicit backgrounds; `--scenarios` selects workloads and leaves the others uncovered. `--instances 1` or `--instances 2` changes only isolated experimental state; omission uses actual production configuration. Cold loading, serial controls, warm arrival runs, stage totals and queue drain are recorded separately. Five additional lifecycle cycles exercise load, long audio, timeout, disconnect, recovery and shutdown, checking weak references, leases, spool closure and RSS trend. RSS includes the owned service and probe client in one process; it is unsuitable for a server-only memory claim.

Internal diagnostics are disabled by default. Validation middleware binds an opt-in collector through a framework-independent port; nested scopes report exclusive wall time for transport/model queues, decoding, initialization, native inference, LID and merge. Uninstrumented orchestration and response work remain outside those totals. Existing HTTP responses and public capability defaults are unchanged. Reports record source differences by digest and changed paths, dependency/model/sample fingerprints and effective settings.

Recommended candidate gates require every language and continuous/stitched group to avoid quality regression, a completed human boundary audit, and paired short P95, long completion and peak RSS ratios no greater than 1.1, with stable queues, full success and final drain. A completed trial means execution finished; it does not imply promotion. The original problem MP3 identity is confirmed; its historical reference text must be pinned again for reproducible quality scoring. See [continuous acceptance results](archive/stt/stt-continuous-acceptance-validation.md) and the [original MP3 replay](archive/stt/stt-admission-boundary-validation.md).

### SenseVoice boundary diagnosis

```bash
python scripts/validate_speech.py sensevoice-boundaries --report sandbox/tts-output/runs/stt-sensevoice-boundary-diagnosis.json
python scripts/validate_speech.py sensevoice-boundaries --report sandbox/tts-output/runs/stt-sensevoice-boundary-diagnosis.json --review-dir sandbox/stt-boundary-review
```

This optional edit-alignment diagnostic requires `rapidfuzz` in the verification environment; ordinary regression scoring retains its fallback. Eight fresh subprocesses isolate relative quiet threshold, an eight-second minimum and forced-cut-only overlap. The default scope contains four 75-second continuous prefixes plus the three stitched controls; `--full` includes 300/590-second inputs. This is a diagnostic round, not three-round quality acceptance or a production default change.

Without `--review-dir`, reports contain scores, edit positions and timings only. The explicit flag exports local audio excerpts and bounded reference/native-output snippets. `review.md` prioritizes candidate error recordings and compares current/candidate cuts. Continuous listening material covers the four 75-second prefixes, including when `--full` is selected. Reference intervals are context, not invented native word timestamps. Human verdicts remain pending until reviewed. Preserve corpus license/attribution when sharing exported excerpts. See [SenseVoice boundary findings](archive/stt/stt-sensevoice-boundary-diagnosis.md).

## Exploratory mixed speech interaction probes

### SenseVoice named candidate and server-only memory acceptance

The newer SenseVoice candidate preserves all-cut overlap: `--candidate-policy relative-minimum8` uses a relative threshold and an eight-second minimum. It is distinct from the legacy forced-only candidate. Run three quality rounds, then the sequential acceptance workflow:

```bash
python scripts/validate_speech.py stt-quality --models stt-sensevoice-small-int8 --candidate-policy relative-minimum8 --resume --report sandbox/tts-output/runs/stt-sensevoice-v2-quality.json
python scripts/validate_speech.py sensevoice-release --quality-report sandbox/tts-output/runs/stt-sensevoice-v2-quality.json --report sandbox/tts-output/runs/stt-sensevoice-v2-release.json --resume
```

The release workflow owns a separate HTTP server process, reports server and client RSS separately, pairs 500 requests at fixed workload-specific rates, compares one instance only for the same-model workload, and runs five lifecycle rounds for each policy before final scoped regression. Lifecycle Python allocation tracing is separate from unprofiled performance runs. Timeout/disconnect coverage must reach native inference and drain before reuse. A failed early-timeout pilot remains failed evidence. Human approval is still pending even when experiment sequencing assumes it passes. See [new candidate evidence and limitations](archive/stt/stt-sensevoice-v2-validation.md). Production settings and public HTTP contracts are unchanged.

```bash
python scripts/validate_speech.py speech-interaction --report sandbox/tts-output/runs/speech-interaction-all.json
python scripts/validate_speech.py speech-interaction --only parallel-two-warm memory-supertonic --report sandbox/tts-output/runs/speech-interaction-supplement.json
```

Requires the project runtime, benchmark dependencies (including psutil), installed Qwen3-ASR, SenseVoice, Whisper and Supertonic assets. Each configuration runs in a new process with an owned ephemeral TCP server and isolated state. It compares serial admission, per-model parallel admission with one instance, lazy second instances, and a prewarmed second ASR instance. Short STT/TTS requests arrive during a 120-second STT request. Separate single-model processes record memory before loading, after short/long requests and after shutdown. It checks response validity, final queue/pool drain and owned server shutdown. Run it separately from other model-backed workloads. Three short observations per task per round are exploratory measurements, not a tail-latency or capacity guarantee. See [results and limitations](archive/stt/stt-interaction-alignment-validation.md).

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


## Product streaming preview

`tests/test_streaming.py` covers plan resolution, protocol validation, lifecycle/cancellation, admission, model leases, shared inference scheduling, native worker termination, ordered audio and metric accounting. Run the standard CI suite with `scripts/test.py ci`. Real six-route replay is available through `scripts/validate_speech.py streaming`; see [benchmark procedure](../benchmarks/streaming/README.md). Explicit pinned local bundles can be prepared with `scripts/prepare_streaming_bundle.py`; it never downloads or converts assets. Real model, playback, platform and resource acceptance are separate from fake-stage CI.


### Streaming fault and sustained-session coverage

Run `python -m unittest tests.test_streaming tests.test_streaming_resilience tests.test_streaming_documentation` for the focused suite. The new resilience module covers six routes with multiple confirmed segments, revision/commit guards, initialization/inference cancellation, fault propagation, ACK/playback bounds, ASGI disconnect/reopen, shutdown, missing-model/memory admission, silence/skips/invalid PCM and 140-second logical input. See [coverage matrix and deferred acceptance](streaming-test-coverage.md). Simulated stages and logical timestamps do not establish model quality or real-time performance; native-process ownership is separately exercised with controlled subprocess work.
