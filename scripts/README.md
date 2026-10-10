# Scripts

This directory contains maintained developer entry points. Model-backed checks and corpus preparation have one public command:

```bash
python scripts/validate_speech.py --help
python scripts/validate_speech.py <workflow> --help
```

The command starts the selected focused runner in a fresh Python process. This keeps native model lifecycle and long-running experiments isolated without exposing a separate top-level command for every check.

## Top-level scripts

| Script | Purpose |
| --- | --- |
| `test.py` | Run the standard `ci`, `regression`, and `full` test suites. |
| `validate_speech.py` | Unified entry point for speech validation and corpus-preparation workflows. |
| `build_sherpa_whisper_fix.py` | Build the pinned sherpa-onnx wheel containing the local Whisper decoding repair. |
| `build_qwen3_tts_linux.py` | Build/package the native Qwen3-TTS runtime for Linux. |
| `build_qwen3_tts_windows.py` | Build/package the native Qwen3-TTS runtime for Windows. |

### Full streaming regression

`python scripts/test.py full` now also requires all default streaming chain models
(including the declared Chinese mixed-script TTS fallback) and the checksum-pinned
FLEURS fixtures. Missing dependencies, assets, or fixtures fail preflight; the
six real-model hour cases are not silently skipped. Existing REST/STT requirements
still apply. To run only the streaming portion:

```sh
SMARTVOICE_HOME=/absolute/model/home python scripts/test.py full --streaming-only
```

Each of the three scenarios in both directions consumes **3600 seconds of audio**
through the real product session, native online ASR, punctuation, translation and
TTS stages enabled by that scenario. Input is cyclic pinned speech, generated in
bounded frames and sent as fast as the bounded service queues can consume it.
No model or clock is mocked and no queue/worker limit is disabled. This test uses
the session service directly; WebSocket and browser regression remain separate.

The suite checks complete input consumption and output drain, ordered event/audio
chunks, committed target text coverage and source references, bounded histories
and private context, model leases, compute permits and owned native PID shutdown.
Per-route reports (including failures) are saved under
`.smartvoice-dev/full-streaming/<run-id>/`. A one-hour per-route wall deadline
prevents hangs. Acceleration depends on actual hardware/model throughput.

This is not a one-hour wall-clock soak or a real-time latency benchmark; it does
not establish natural meeting translation quality, concurrency capacity, browser
playback or physical audibility. CI keeps fast generator/audit and fault tests;
the real hour cases run in `full` only.

### Export a portable hour summary

Use the fixed exporter instead of assembling checked-in summaries with absolute
paths in an ad hoc script:

```bash
python -m benchmarks.streaming.summarize_full_hour \
  --reports-dir .smartvoice-dev/full-streaming/<run-id> \
  --supplement .smartvoice-dev/full-streaming/<extra-run-id>/translated_subtitles-en.json \
  --output benchmarks/streaming/reports/full-hour-<revision>.json
```

`--supplement` is optional and repeatable. All six route reports must exist and
have the expected identity; failures and incomplete input remain in the output.
The exporter requires a fresh output filename and records evidence SHA-256 values.
Repository-local evidence uses repository-relative paths. External evidence uses
only a filename, artifact ID and checksum, without its local directory. Raw local
evidence is not automatically distributed with the summary.

`benchmarks.streaming.report_paths` defines the shared export rules. The streaming
benchmark also uses these rules for its audio root. Standard CI recursively checks
path fields and code-checksum map keys in `benchmarks/streaming/reports/*.json`,
rejecting POSIX/Windows absolute paths, drive-relative paths and parent traversal.
This check concerns filesystem references; it does not anonymize spoken text.

## Validation workflows

All workflows accept their existing runner options after the workflow name. Use `--help` on the selected workflow for details.

| Workflow | Purpose | Internal runner |
| --- | --- | --- |
| `streaming` | Benchmark source/translated subtitles and spoken interpretation over WebSocket. | `validation/verify_streaming.py` |
| `streaming-capacity` | Measure fixed-arrival streaming load, route latency, failures, CPU/RSS and per-core concurrency. | `validation/verify_streaming_capacity.py` |
| `streaming-capacity-matrix` | Start isolated services at multiple `max_sessions` settings and compare concurrent quality against the frozen snapshot. | `validation/verify_streaming_capacity_matrix.py` |
| `streaming-stability` | Run repeated/long-input sessions and optionally verify exit cleanup for an owned server process. | `validation/verify_streaming_stability.py` |
| `stt-http` | Real TCP HTTP checks for STT/TTS formats, long audio, limits, timeout and disconnect. | `validation/verify_stt_http.py` |
| `stt-admission` | Inference admission, overload, queue timeout and recovery checks. | `validation/verify_stt_admission.py` |
| `stt-policy` | Compare language/cache behavior, segmentation windows and overlap policies. | `validation/compare_stt_policies.py` |
| `stt-quality` | Run repeated current/candidate STT quality comparisons. | `validation/verify_stt_candidates.py` |
| `stt-load` | Measure fixed-arrival model load and paired workload behavior. | `validation/verify_stt_load.py` |
| `stt-isolated-load` | Run load/lifecycle cases in an owned server process with isolated state. | `validation/verify_stt_isolated_load.py` |
| `sensevoice-release` | Orchestrate SenseVoice quality, load, lifecycle, and scoped regression acceptance. | `validation/verify_sensevoice_release.py` |
| `speech-interaction` | Probe STT/TTS coexistence, latency, and resource use. | `validation/verify_speech_interaction.py` |
| `sensevoice-boundaries` | Diagnose SenseVoice cut boundaries and optionally export a human review packet. | `validation/diagnose_sensevoice_boundaries.py` |
| `prepare-corpus` | Download and prepare the explicitly requested, pinned continuous-speech corpus. | `validation/prepare_stt_continuous.py` |

## Internal validation package

`validation/` holds the focused runners and shared implementation helpers. It is not a collection of separate public commands.

- `stt_validation_common.py` provides provenance, audio-window tracing, coverage checks, stage tracing, and temporary-storage tracking shared by validation workflows.
- `stt_window_policies.py` defines validated experimental window-policy values shared by candidate and load runs.
- `stt_isolated_http.py` owns the private loopback HTTP server used by isolated load/lifecycle workflows.
- The remaining modules implement the workflows listed above and may be invoked internally by an orchestrator to preserve subprocess isolation.

## Adding a script or workflow

- Prefer adding a workflow to `validate_speech.py` and extending an existing focused runner when the purpose and lifecycle are already covered.
- Keep reusable validation helpers in `validation/`; do not add another top-level `verify_*` or `stt_*` entry point for a small variation.
- Add a new runner only for a distinct responsibility, runtime lifecycle, or process-isolation need that cannot remain clear as an option to an existing workflow.
- Keep product behavior out of verification scripts. Runners may use installed assets and isolated temporary state, but must not silently change personal configuration or production defaults.
- Do not commit one-off logs, generated audio, temporary reports, or scratch scripts. Put transient outputs under the ignored `sandbox/` tree; promote only reviewed, reproducible evidence to `doc/archive/` or benchmark records.
- Update this README and `doc/testing.md` whenever a supported workflow or standard test command changes.


## Product streaming preview

`tests/test_streaming.py` covers plan resolution, protocol validation, lifecycle/cancellation, admission, model leases, shared inference scheduling, native worker termination, ordered audio and metric accounting. Run the standard CI suite with `scripts/test.py ci`. Real six-route replay is available through `scripts/validate_speech.py streaming`; see [benchmark procedure](../benchmarks/streaming/README.md). Explicit pinned local bundles can be prepared with `scripts/prepare_streaming_bundle.py`; it never downloads or converts assets. Real model, playback, platform and resource acceptance are separate from fake-stage CI.


Streaming deterministic tests: `python -m unittest tests.test_streaming tests.test_streaming_resilience tests.test_streaming_documentation`; see [coverage matrix](../doc/streaming-test-coverage.md). They require no model downloads and do not establish streaming quality/performance acceptance.
