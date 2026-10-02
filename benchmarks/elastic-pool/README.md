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
python benchmarks/elastic-pool/benchmark.py asr --rates 14 --count 3000 --label confirmation
python benchmarks/elastic-pool/benchmark.py asr --warm-trials 10
python benchmarks/elastic-pool/benchmark.py tts --warm-trials 10
python benchmarks/elastic-pool/benchmark.py supertonic --warm-trials 10
python benchmarks/elastic-pool/benchmark.py supertonic --idle-trial
```

Use `--abort-overload` to stop a confirmation early once failures exceed its full planned request budget (0.1%); the JSON records planned and actual counts.

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
default short runs are screening only; use at least 3,000 requests at a candidate
rate for formal 99.9% confirmation. The P90 ratio is a comparison threshold, not
a universal user-perceived latency SLA.

CPU and RSS are sampled every 100 ms from the server process and accessible child
processes. Sherpa inference uses threads in the server process, so its model
memory/compute is included; the client process is excluded. The report marks
whether the full process tree was accessible. Shared pages can be counted in
multiple processes' RSS; these scenarios do not start a Qwen child engine.
CPU is CPU-seconds / wall-seconds; 1 equivalent core = 100% process CPU. Memory is
MiB (2^20 bytes), not model-weight size or private/shared memory accounting.
Sampling may miss very brief memory peaks. Results depend on the host workload,
thermal conditions and fixture lengths. A zero-failure run does not establish
99.9% reliability for arbitrary future requests; roughly 3,000 zero-failure
independent requests are needed even for a one-sided 95% lower confidence bound
of 99.9% under a stationary binomial model.

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

Aggregate local confirmation and warm-trial data with `python benchmarks/elastic-pool/summarize.py`. The reviewed fixed-arrival results are embedded in the matching model reports: [SenseVoice](../result/macos-arm64-stt-sensevoice-small-int8-fleurs-standard-2026-09-29.json), [Matcha](../result/macos-arm64-tts-matcha-zh-baker-fleurs-standard-2026-09-29.json), and [Supertonic](../result/macos-arm64-tts-supertonic-v3-multilingual-int8-fleurs-standard-2026-09-29.json). Local aggregate data is ignored by Git. See [configuration/lifecycle details](../../doc/inference-concurrency.md).
