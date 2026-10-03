# Same-model concurrency and request-experience benchmark

This benchmark launches the real SmartVoice FastAPI application in a separate
local process. It uses the default elastic configuration explicitly (min 1, max
2, 2 threads, 4 waiting per model, adapter admission). It does not rewrite your
personal `smartvoice.json`, router settings or model files. It does not modify
Sherpa, retry failed requests or batch them. Workloads are the same SenseVoice,
Matcha and English Supertonic fixtures as the sandbox research.

Install the models first. From the repository root, install SmartVoice and the
benchmark dependencies with `python -m pip install -r benchmarks/requirements.txt`.
The scripts require Python 3.11+. Add a scenario for each new model to
`scenarios.json`, including its task, language and representative input fixtures;
keep the same acceptance criteria and measurement settings for models being
compared. Run one workload at a time to avoid CPU contention between cases and
other model-backed regression tests.

```sh
python benchmarks/elastic-pool/benchmark.py asr --rates 10 12 14 16
python benchmarks/elastic-pool/benchmark.py tts --rates 5 6 7 8 9
python benchmarks/elastic-pool/benchmark.py supertonic --rates 1.2 1.4 1.6 1.8 2
python benchmarks/elastic-pool/benchmark.py asr --rates 14 --confirmation --label confirmation
python benchmarks/elastic-pool/benchmark.py asr --warm-trials 10
python benchmarks/elastic-pool/benchmark.py tts --warm-trials 10
python benchmarks/elastic-pool/benchmark.py supertonic --warm-trials 10
python benchmarks/elastic-pool/benchmark.py supertonic --idle-trial
```

Screen capacity from a high offered rate downward and stop at the first stable
passing rate. Choose a starting rate above the expected limit and a step that
fits the desired precision; the first passing rate is the highest passing point
on that schedule, with one-step resolution. The immediately higher screened
rate is the failure boundary. Confirm the candidate with a rate-sized run, then
run the fresh-process warm trials:

```sh
python benchmarks/elastic-pool/benchmark.py asr --start-rate 24 --decrement 2 --minimum-rate 2 --label descending-screen
python benchmarks/elastic-pool/benchmark.py asr --rates 14 --confirmation --label confirmation --abort-overload
python benchmarks/elastic-pool/benchmark.py asr --warm-trials 10
```

`--start-rate` requires `--decrement` and `--minimum-rate`, generates the
descending schedule automatically, and stops at the first passing screen. If
the candidate confirmation fails, confirm the next lower passing
screened rate. Use a smaller decrement around the boundary when more precision
is useful. Explicit `--rates` remains available; combine it with
`--stop-on-pass` to stop at the first pass in a manually ordered schedule.

Use `--confirmation` for the formal candidate run. It selects between 500 and
2,000 requests from the offered rate, reserving four minutes of a 30-minute
round for startup, baseline and drain. Rates below 0.321 req/s cannot fit the
500-request minimum in that budget and are rejected for formal confirmation.
The selected count and actual elapsed time are recorded in the result. A
zero-failure run at this sample size has weaker statistical confidence than
3,000 observations, so compare the observed success rate and count directly.

Each run also reports `delivered_rps_per_cpu_core`, calculated as successful
responses per second divided by the mean server CPU cores sampled during that
run. It reflects observed throughput per measured CPU capacity; it is not the
offered rate divided by CPU, and it inherits the resource sampler's process-tree
coverage limits.

Use `--abort-overload` to stop a confirmation early when failures exceed the
planned success-rate budget or when the rolling 30-second P90 stays above the
baseline limit. The JSON records planned and actual counts and the console
prints the early-stop reason.

For isolated configuration experiments, `--num-steps`, `--threads-per-instance`
and `--instances` override values only in the benchmark server process. The
Supertonic step override is applied at the Sherpa call boundary; the product
adapter default is unchanged. These overrides are recorded in run metadata, and
`--warm-trials N --label <name>` writes distinct audio and trial files for A/B
quality checks.

Output is written to the ignored local directory
`.smartvoice-dev/benchmark-concurrency/`. Reusing a scenario and label
overwrites its result, so use a fresh label to preserve prior runs. Configuration
and workload definitions are in `scenarios.json`; instance/thread limits there
record the tested settings, and the actual server settings are in `server.py`.

Rates are open loop: requests arrive at fixed scheduled intervals independent of
completion. Up to 64 client workers can submit; scheduling lag is reported.
Latency starts at the scheduled arrival and ends after the complete JSON/WAV has
been read. P90 uses the nearest-rank estimator. Responses are checked for HTTP 200,
STT text matching its serial baseline or valid nonempty, nonsilent PCM WAV; TTS semantic quality requires separate
comparison against serial output and listening. Run `python benchmarks/elastic-pool/quality.py` after the rate tests for a small serial-versus-concurrent STT back-transcription check; this does not establish MOS or semantic correctness for every response. Successful-only and all-request
P90, statuses, per-10-second windows, instance peak activity, pool drain state and
runtime waiting are recorded. A rate passes when valid-response success is at
least 99.9%, successful-response P90 is at most 1.2 times the low-rate, fully
warm baseline, the final 10-second latency/wait window does not grow materially
over the first window, and the pool drains after offered traffic stops. The
default short runs are screening only; use `--confirmation` at a candidate rate
for formal 99.9% evaluation within the 30-minute round budget. The P90 ratio is a comparison threshold, not
a universal user-perceived latency SLA.

CPU and RSS are sampled every 100 ms from the server process and accessible child
processes. Sherpa inference uses threads in the server process, so its model
memory/compute is included; the client process is excluded. Qwen3-TTS runs in a
separate native child process, and its measurements are valid only when the
report says the complete process tree was accessible. Shared pages can be counted
in multiple processes' RSS.
CPU is CPU-seconds / wall-seconds; 1 equivalent core = 100% process CPU. Memory is
MiB (2^20 bytes), not model-weight size or private/shared memory accounting.
Sampling may miss very brief memory peaks. Results depend on the host workload,
thermal conditions and fixture lengths. A zero-failure run does not establish
99.9% reliability for arbitrary future requests; the shorter 500–2,000 request
confirmation rounds are a capacity comparison within a time budget, not a
one-sided 95% confidence demonstration of 99.9% reliability.

The optional `--idle-trial` uses the same implementation with its idle timer shortened to 1 second, verifies real native instance reclamation 2 -> 1, records RSS before/after and checks the retained instance still serves without another initialization. It is a lifecycle check, separate from default-configuration performance results.

Warm trials start a fresh service process each time. The first request includes
catalog checks, model construction, the first native inference and response
encoding. After all fixtures have been exercised serially on the first instance, two simultaneous requests expand 1 -> 2 while the resident
instance continues serving. The expansion request's P90 and pool initialization
(first inference included) are recorded separately. Both instances are exercised
with all fixtures before warm measurements. OS file caches are not flushed, so
these are fresh-process/model cold starts, not cold-disk measurements.

Use at least ten fresh-process trials when comparing P90 first-instance warm,
expansion request, second-instance initialization-plus-first-inference and
fully-warm latency. Each summary should include the model/language/input
fingerprint, offered and delivered rates, valid success rate, end-to-end P90,
queue/instance wait P90, CPU and RSS by lifecycle phase, and the active/waiting
pool snapshot. Preserve raw local JSON for auditability; publish only reviewed
aggregate results.

Aggregate local confirmation and warm-trial data with `python benchmarks/elastic-pool/summarize.py`. Reviewed fixed-arrival results and the derived throughput-per-core metric are embedded in each model report: [SenseVoice](../result/macos-arm64-stt-sensevoice-small-int8-fleurs-standard-2026-09-29.json), [Whisper](../result/macos-arm64-stt-whisper-base-multilingual-int8-fleurs-standard-2026-09-29.json), [Qwen3 ASR](../result/macos-arm64-stt-qwen3-asr-600m-int8-fleurs-standard-2026-09-29.json), [Matcha](../result/macos-arm64-tts-matcha-zh-baker-fleurs-standard-2026-09-29.json), [Supertonic](../result/macos-arm64-tts-supertonic-v3-multilingual-int8-fleurs-standard-2026-09-29.json), and [Kokoro](../result/macos-arm64-tts-kokoro-multilingual-v1-1-zh-en-fleurs-standard-2026-09-29.json). Local aggregate data is ignored by Git. See [inference concurrency, lifecycle, and limits](../../doc/architecture-guidelines.md#inference-concurrency-lifecycle-and-limits).
