# Product streaming benchmark

Run from the repository root with the product environment and a prepared server (Streaming is available by default). No asset downloads or model conversions occur in these runners.

```sh
.venv/bin/python scripts/validate_speech.py streaming \
  --manifest benchmarks/streaming/data/fleurs/clean.manifest.json \
  --audio-root benchmarks/streaming \
  --annotations benchmarks/streaming/data/fleurs/onset-annotations-v1.json \
  --baseline benchmarks/streaming/baselines/product-p0-2026-10-09-r1.json \
  --url ws://127.0.0.1:8766/v1/audio/stream \
  --rounds 1 --output .smartvoice-dev/streaming-product/evaluation.json
```

The default selection is ten clean fixed cases per direction, across all three modes. `--cases zh_00_clean,en_00_clean` runs a smoke subset; it cannot establish P90 acceptance. Outputs are create-only. `--rounds 3` records 30 sessions per direction/mode; retain every failure. Optional `--server-pid PID --cpu-cores N --concurrency N` records process-tree CPU core equivalents, CPU distribution and summed RSS, including native children. Summed RSS can double-count shared pages. Requested sessions/core is not validated capacity/core. The product default admission limit is two sessions; capacity experiments should use isolated services and record the configured limit separately from successful overlap.

To exercise the initial speech-completion floor locally, use only `spoken_interpretation`, all ten clean fixtures and 30 rounds (300 sessions per language direction). The report evaluates the 99%/300-session numerical rule as `production_audio_gate_replay_assessment` and explicitly marks `production_telemetry_status: not_available_from_local_benchmark`. This repeatable FLEURS replay checks the denominator and product behavior; it is not production telemetry or independent production traffic.

```sh
python scripts/validate_speech.py streaming \
  --manifest benchmarks/streaming/data/fleurs/clean.manifest.json \
  --audio-root benchmarks/streaming \
  --baseline benchmarks/streaming/baselines/product-p0-2026-10-09-r1.json \
  --url ws://127.0.0.1:8766/v1/audio/stream \
  --modes spoken_interpretation --rounds 30 --concurrency 2 \
  --output .smartvoice-dev/streaming-product/speech-300-replay.json
```

Fixed-arrival load is a separate open-loop probe. It schedules session starts independently of completion, paces each PCM stream at 1x, and reports the advertised session limit, offered rate, overlapping client attempts and successful session overlap, failure/audio success, session duration, process-tree CPU/RSS, successful-session overlap per physical core, and sessions per observed CPU core equivalent:

```sh
python scripts/validate_speech.py streaming-capacity \
  --mode spoken_interpretation --case zh_02_clean \
  --arrival-rate 0.1 --duration-seconds 300 \
  --server-pid PID --cpu-cores 8 \
  --output .smartvoice-dev/streaming-product/capacity-zh-en-r1.json
```

Run each route and repeat at increasing offered rates; stop on the first rate that violates the established latency, success-rate, or resource budget. The utility measures one route per run. A requested arrival rate is not a passing capacity result by itself.

On macOS systems where system-wide process enumeration is sandbox-blocked, resource sampling falls back to the API root PID and marks `process_tree_complete: false`. Such a report is useful for a local smoke check but is not a full process-tree capacity result.

Repeated-session and long-input stability probes are separate. Repeated content is intentionally identified as a soak probe rather than natural long-form quality coverage. The optional `--server-command` starts and stops only the subprocess supplied to the runner; use an isolated port/config and never point it at a shared server:

```sh
python scripts/validate_speech.py streaming-stability \
  --url ws://127.0.0.1:8876/v1/audio/stream \
  --server-command .venv/bin/python -m smartvoice --port 8876 \
  --mode transcription --case zh_02_clean --sessions 10 --long-audio-seconds 120 \
  --cpu-cores 8 --output .smartvoice-dev/streaming-product/stability-zh-r1.json
```

For simultaneous real-time zh→en and en→zh speech over a wall-clock hour, run
the duplex soak against an isolated service configured with `max_sessions=2`.
The report records per-frame input pacing lag, sampled current/peak input queues,
terminal input-queue peaks, sampled API RSS/CPU, and native worker self-reported CPU/RSS. When the host
blocks descendant enumeration, worker measurements remain available but the
report must not claim a complete process tree. Example:

```sh
python scripts/validate_speech.py streaming-duplex-soak \
  --url ws://127.0.0.1:8876/v1/audio/stream --seconds 3600 \
  --output .smartvoice-dev/streaming-product/duplex-hour-r1.json \
  --progress .smartvoice-dev/streaming-product/duplex-hour-r1.progress.json
```

For browser-specific DOM/audio/network/background validation, follow [browser QA revision 1](browser-qa-v1.md). This is intentionally separate from WebSocket replay because loopback delivery does not measure browser rendering or playback.

To compare the configured admission limits 2/4/8/16 against concurrent ASR quality, launch one isolated local service at a time. The matrix keeps a fixed 2800 MiB streaming-memory budget so a high `max_sessions` value cannot bypass memory admission on a constrained host:

```sh
python scripts/validate_speech.py streaming-capacity-matrix \
  --data-dir "$HOME/Library/Application Support/SmartVoice" \
  --levels 2,4,8,16 --mode transcription --cases zh_02_clean,en_02_clean \
  --memory-mib 2800 --compute-slots 2 --arrival-window-seconds 10 \
  --output .smartvoice-dev/streaming-product/max-session-matrix.json
```

The matrix now supports three independent rounds and offered-load multipliers. For default-limit saturation, configure only `max_sessions=2` and offer 1x, 2x, 4x and 8x the admission limit over a short fixed window. This distinguishes a true two-session steady load from burst/overload behavior; then run the 2/4/8/16 configuration sweep at 1x. Repeat for each of the six mode/direction routes, using a clean fixture per source language. Example: add `--arrival-multipliers 1,2,4,8 --rounds 3 --arrival-window-seconds 2` for the default-limit overload sweep. A route whose memory reservation admits fewer sessions must be reported as memory-limited; do not inflate the budget beyond safe host headroom. Compare final outputs against the frozen baseline for successful sessions only, and report rejected/failed attempts in the offered-load denominator.

The same final output must match the frozen product snapshot for every successful session. At 2800 MiB, source ASR+formatting is conservatively budgeted at 700 MiB/session, so memory admission can cap effective concurrency at four even when `max_sessions` is 8 or 16. Run translated subtitles and speech separately with a memory budget appropriate to the route; do not increase a budget beyond host capacity to force the configured maximum.

Replay the packaged 90-case robustness/quality corpus once through source and translated subtitles with `--manifest benchmarks/streaming/data/fleurs/quality-corpus-v1.json --audio-root benchmarks/streaming --all-cases --modes transcription,translated_subtitles`. This includes clean, quiet, 10 dB noise, room-proxy, silence and noise-only controls. Expected silence is counted as functional success only when the product terminates `no_speech` with no final text. Target references and actual finals are retained for review; translated-text quality remains human-reviewed, not scored by literal WER/CER.

Reports pin corpus/config/catalog/code/dependency revisions. A client monotonic clock measures from first PCM send to first nonempty source unit, target draft and complete first WAV receipt. Subtract validated human onset or clearly marked provisional VAD onset; negative onset-adjusted intervals are invalid, not clamped. This runner measures loopback delivery, not browser painting, scheduling or physical audibility. Browser playback and long recordings require separate regression.

Report filesystem references are repository-relative. When `--audio-root` is
outside the repository, `evaluation_inputs.audio_root` is an artifact reference
object with `artifact_id`, `name` and `availability`, rather than a local path
string. The corpus identity comes from the manifest SHA-256; it is not a hash of
the external directory. Replay still uses the original local root while running.
CI checks curated reports for absolute or escaping path references. Native hour
summaries use `python -m benchmarks.streaming.summarize_full_hour`; see
[the export instructions](../../scripts/README.md#export-a-portable-hour-summary).

Nearest-rank P50/P90/P95 retain sample counts. Missing output stays in success-rate denominators; successful audio TTFO is conditional on audio output. Report skipped chunks, terminal status, protocol failures and audio sequence/WAV integrity. Strict quality guards compare source finals, target finals, spoken text/spans and skip reasons against the versioned mechanical product snapshot; this is only selected-case coverage. It does not replace meaning/pronunciation review. Partial drafts may differ.

WER/CER uses product `benchmarks.metrics` normalization: NFKC, casefold and punctuation deletion. Historical sandbox punctuation-to-space values are not directly comparable. Do not relabel previously reviewed corpus scores without recomputation. Profiling records initialization, admission, native calls and stage timing; stage percentiles cannot be added to obtain end-to-end percentiles.

Release gates: fixed-final equality; high-risk years/names/negation/boundaries; known en_09_clean missing audio; audio order/completeness; three independent repeated rounds; long recording/browser playback; capacity at quality/latency gates with CPU/RSS/core; supported-platform packaging. The subtitle first-output targets are source P90<1s and target P90<2s. Speech TTFO is reported at P50/P90/P95 as an observational diagnostic, with no latency pass/fail threshold. The pinned clean-fixture full-audio gate is 100%. The initial production floor is 99% full-audio success per language direction over at least 300 eligible speech sessions; no-audio, invalid, incomplete, or skipped output counts as failure. Revisit this floor against production telemetry while retaining the strict clean-fixture gate.


## Remaining evaluation limitations

- The bundled FLEURS fixture, onset annotations and compact baseline make the 20 clean performance cases reproducible from a fresh checkout. Historical 11 streaming and 8 TTS review records are preserved in `reviews/`.
- The 90-case FLEURS-derived corpus and historical sandbox ASR/MT replay are bundled in `data/fleurs/quality-corpus-v1.json` and `reports/`; the current product summary is `reports/product-quality-90-2026-10-09-summary.json`. The 22-case S5 packet is `reviews/current-product-quality-review-packet-v1.json`; all ratings remain pending human listening. `reviews/current-product-quality-review-triage-v1.md` provides an unscored, text-based risk checklist to focus that review.
- Transcript-derived entity/number anchors and six agent-inspected translation-risk examples are preserved in `quality-anchors/`; they are diagnostics, not independent bilingual review or an automatically scored acceptance set.
- The compact baseline is explicitly `unreviewed_reference_snapshot`; equality protects against regressions but does not establish acceptable translation or speech quality.
- Human onset ground truth is incomplete. VAD-adjusted latency must remain diagnostic until manually annotated; first-PCM latency remains separately reported.
- The original `streaming` runner's `--concurrency` remains bounded batch concurrency; use `streaming-capacity` for fixed-arrival capacity. Browser QA remains a separately recorded manual campaign. TTFSA, physical audibility, sustained segment lag, revision frequency and natural long-form playback need additional evaluation.
- The runner reports measurements and guard findings; it does not automatically enforce all quality/performance thresholds or return a failing exit code for every failed guard.

## Audio and terminal acceptance semantics

- A session is functionally successful only after a `session_complete` terminal with status `complete` or `partial` and the required final text/audio.
- `output_audio_success` means at least one WAV chunk was received and every received chunk has a matching descriptor, contiguous sequence, nonempty PCM16 mono WAV, matching sample rate and duration, and no protocol failure. This may still be partial when the session reports skipped chunks.
- `full_audio_success` additionally requires terminal status `complete` and zero skipped chunks. Both rates use every speech session as the denominator; no-audio and invalid-audio sessions count as failures.
- The strict guard compares final text, spoken text/spans, skip reasons, terminal outcome, and audio integrity. It is a selected-case regression guard, not a human quality judgment.
- Reports pin the onset annotation, baseline, manifest, code, catalog, config, dependencies and effective advertised chain capabilities by hash/content. Error terminal events include available profiling and emitted counts.
- `acceptance.json` contains subtitle latency targets, the observational-only speech TTFO definition, a 100% full-audio clean-fixture gate, and the initial production floor of 99% per direction over at least 300 eligible speech sessions. Subtitle latency and clean-fixture outcomes are marked `pass`, `fail` or `insufficient_samples`; TTFO has no pass/fail threshold.

The runner now applies the audio and terminal rules above and includes the relevant hashes and effective capabilities in reports. The pinned fixtures are attributed in `data/fleurs/NOTICE.md` and `data/fleurs/quality/NOTICE.md`. A local real-browser matrix is now recorded: 14 product-behavior assertions pass, one legacy 10-second idle-timeout negative control matches its expected `input_timeout`, and there are zero unexpected failures; the matching background case passes with the current 60-second default. This is partial browser coverage, not full browser/release acceptance; see [follow-up evidence](../../doc/streaming.md#持续运行与浏览器补充验证). Human onset annotation and S5 semantic review remain pending. Repeatable capacity evidence and natural long-form playback remain subject to their separate acceptance scopes. The initial production audio-success floor is fixed at 99% per direction with at least 300 sessions; this is a P0 acceptance policy and should be checked against real production telemetry.

## Queue, wall-clock soak, and resource evidence

Run two continuous speech directions with fixed PCM checksums at 1×. The client
reanchors after a scheduling stall instead of burst-catching-up; it reports all
frames sent and every reanchor. A 3,600-second run is an actual wall-clock test,
not the accelerated logical-hour regression:

```bash
python -m benchmarks.streaming.duplex_soak \
  --url ws://127.0.0.1:9191/v1/audio/stream --seconds 3600 \
  --output .smartvoice-dev/streaming-product/hour.json \
  --progress .smartvoice-dev/streaming-product/hour.progress.json
python -m benchmarks.streaming.analyze_soak \
  .smartvoice-dev/streaming-product/hour.json \
  --baseline .smartvoice-dev/streaming-product/frozen-hour.json \
  --steady-after 120 \
  --output .smartvoice-dev/streaming-product/hour.analysis.json
```

The analysis checks complete input, WAV validation, consecutive audio sequence,
exact coverage of each target final by its audio spans, and source/target final
text equality to the same-input baseline. Repetition tests lifecycle/throughput;
it is not a natural hour of meeting speech or a semantic-quality evaluation.
Reported profiling percentiles use the bounded tail window; count/total/max
include all calls. Keep failure runs alongside successful runs.

Capabilities sample API RSS/CPU and scheduler-owned worker PIDs directly. Each
worker also self-reports its CPU/current RSS. `owned_workers_complete` means all
known model workers were sampled; `process_tree_complete` additionally requires
permitted descendant traversal. Do not claim full tree measurements when that
flag is false. Summed RSS can include shared pages and OS reclaim noise. CPU is
reported as observed core equivalents, with audio seconds per consumed CPU
second; this does not establish a concurrent-session capacity limit.

If other benchmark services run on the same host, record their time interval and
use `--steady-after` to restrict the trend/CPU analysis to a subsequent stable
window. RSS peaks still include startup and the entire run. The compute-slot
setting is a simultaneous-native-job allowance, not a count of physical cores.
An hour with eight slots is not evidence for the default two-slot configuration.

To distinguish marginal context cost from amortized shared-model RSS, run
`python -m benchmarks.streaming.session_cost --url <WS-url> --output <report>`
on an idle service. It initializes both directions, then compares warm idle,
one empty session and two empty sessions only when model-worker counts match.
Signed RSS deltas include allocator/OS noise; a negative delta must not be
reported as a zero-cost session or used to change residency budgets.

## Real browser impairment and recovery

Use the loopback QA proxy documented in [browser-qa-v1.md](browser-qa-v1.md).
It serves the shipped streaming page with an observation panel and forwards the
real WebSocket to a separate product API. Its network shaping limits bytes in
both directions and pipelines an 80ms one-way delay. Disconnect controls close
the actual transport during upload or playback. Captured evidence includes DOM
revisions, final rows, completed audio callbacks, network-buffer peaks and the
service's effective limits. Keep failed configurations and retries in the matrix.
A `hidden` sample is mandatory for background coverage. Audio completion here
means Web Audio callbacks, not independently measured speaker audibility.


## Latest product acceptance result (2026-10-11)

The default two-session/two-compute-permit duplex wall-clock hour passed with complete 1x input, exact final text, ordered complete audio and no overload. At the time of that run, the bilingual model admission budget was explicitly raised from 4096 MiB to 8192 MiB; the product default has since been updated to 8192 MiB based on the measured two-direction model residency. The sampled API plus scheduler-known worker RSS peaked at 1.95 GiB and showed no positive steady-state growth; full descendant-process enumeration is unavailable on this host.

The six-route fixed clean-corpus matrix contains 30 sessions per route (10 clips × 3 rounds) at both serial and concurrency 2. Functional failures were zero, strict source/target final guards passed, and speech full-audio success was 30/30 in both directions. Source partial P90 remained above 1s and target partial P90 remained above 2s in both matrices. TTFO is observational only and has no latency target. The serial/concurrency comparison indicates the caption delay is not primarily caused by two-session contention. See the [portable final acceptance report](reports/streaming-final-acceptance-2026-10-11.json) and [coverage evidence](../../doc/streaming.md#测试覆盖与评测结果).


The VAD-onset-adjusted diagnostics for the same fixed clips are below the subtitle limits (English/Chinese source P90 0.924/0.878s; English→Chinese/Chinese→English target P90 1.688/1.524s). These VAD onsets are not human-verified, so official first-PCM gates remain failed. Before another model tuning round, annotate audible speech onset on the fixed cases and report both clocks; do not reclassify the formal gate using VAD estimates.
