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

## Validation workflows

All workflows accept their existing runner options after the workflow name. Use `--help` on the selected workflow for details.

| Workflow | Purpose | Internal runner |
| --- | --- | --- |
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
