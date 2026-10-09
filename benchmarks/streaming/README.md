# Product streaming benchmark

Run from the repository root with the product environment and an explicitly enabled, prepared server. No asset downloads or model conversions occur in this runner.

```sh
.venv/bin/python scripts/validate_speech.py streaming \
  --manifest sandbox/streaming/data/real.manifest.json \
  --audio-root sandbox/streaming \
  --annotations sandbox/streaming/reports/source-onset-annotations-v1.json \
  --baseline sandbox/streaming/reports/full-streaming-eval-2026-10-08-r1.json \
  --url ws://127.0.0.1:8766/v1/audio/stream \
  --rounds 1 --output .smartvoice-dev/streaming-product/evaluation.json
```

The default selection is ten clean fixed cases per direction, across all three modes. `--cases zh_00_clean,en_00_clean` runs a smoke subset; it cannot establish P90 acceptance. Outputs are create-only. `--rounds 3` records 30 sessions per direction/mode; retain every failure. Optional `--server-pid PID --cpu-cores N --concurrency N` records process-tree CPU core equivalents, CPU distribution and summed RSS, including native children. Summed RSS can double-count shared pages. Requested sessions/core is not validated capacity/core. Default admission accepts one session; raise server limits explicitly for capacity experiments. No automatic capacity acceptance is inferred.

Reports pin corpus/config/catalog/code/dependency revisions. A client monotonic clock measures from first PCM send to first nonempty source unit, target draft and complete first WAV receipt. Subtract validated human onset or clearly marked provisional VAD onset; negative onset-adjusted intervals are invalid, not clamped. This runner measures loopback delivery, not browser painting, scheduling or physical audibility. Browser playback and long recordings require separate regression.

Nearest-rank P50/P90/P95 retain sample counts. Missing output stays in success-rate denominators; successful audio TTFO is conditional on audio output. Report skipped chunks, terminal status, protocol failures and audio sequence/WAV integrity. Strict quality guards compare source finals, target finals, spoken text/spans and skip reasons against frozen sandbox records; this is only selected-case coverage. It does not replace meaning/pronunciation review. Partial drafts may differ.

WER/CER uses product `benchmarks.metrics` normalization: NFKC, casefold and punctuation deletion. Historical sandbox punctuation-to-space values are not directly comparable. Do not relabel previously reviewed corpus scores without recomputation. Profiling records initialization, admission, native calls and stage timing; stage percentiles cannot be added to obtain end-to-end percentiles.

Release gates: fixed-final equality; high-risk years/names/negation/boundaries; known en_09_clean missing audio; audio order/completeness; three independent repeated rounds; long recording/browser playback; capacity at quality/latency gates with CPU/RSS/core; supported-platform packaging. First-output targets are source P90<1s, target P90<2s and speech onset TTFO P90<3s. No accepted minimum audio-success gate exists yet, so latency alone cannot accept speech quality.


## Current evaluation limitations (documented, fixes deferred)

- The example corpus, onset annotations and frozen final baseline still live in ignored `sandbox/`; a fresh checkout cannot reproduce this command without supplying those assets. The runner is independent product code; its evaluation assets are not yet independently distributed.
- The strict comparison currently does not consume `audio_integrity_errors`; an audio sequence/size/format failure can coexist with `strict_quality_guard.passed=true`. Inspect audio-integrity errors and output success separately. Do not use that flag alone as acceptance.
- Complete audio success currently does not validate terminal status; canceled sessions with partial audio require explicit failure classification when reviewing reports.
- Error terminal events currently omit stage profiling. Baseline/annotation hashes and the full effective server configuration are not all frozen in the report.
- `--concurrency` is batch concurrency, not an open-loop offered-rate capacity test. TTFSA, physical audibility, sustained segment lag, revision frequency and long real playback need additional evaluation.
- The runner reports measurements and guard findings; it does not automatically enforce all quality/performance thresholds or return a failing exit code for every failed guard.

These are known pending items, not accepted benchmark behavior. Documentation and deterministic fault coverage are being completed first; runner fixes, corpus migration and performance/capacity campaigns are deferred.
