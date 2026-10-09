# Model Comparison Benchmark

The model comparison reports `quality` and serial `performance`; same-model
capacity and request experience use the fixed-arrival concurrency protocol below.
Keep these results as independent dimensions. Do not combine them into one score
or compare values from different datasets/languages as if they were equivalent.

## Scope

- Configured languages: English (`en_us`), Simplified Chinese (`cmn_hans_cn`), Hindi (`hi_in`), Spanish (`es_419`), Arabic (`ar_eg`), French (`fr_fr`), Brazilian Portuguese (`pt_br`), Russian (`ru_ru`), German (`de_de`), Cantonese (`yue_hant_hk`), Japanese (`ja_jp`), Korean (`ko_kr`), and Italian (`it_it`). Language profiles and model-language combinations are configuration-driven.
- STT primary corpus: the pinned `google/fleurs` test split. Each model report records the Hugging Face revision, split, dataset config, license, and sample-ID digest by language.
- TTS prompts: the pinned FLEURS test transcripts for each configured model-language pair. Generated speech is prepared for local blinded listening; MOS ratings are not invented or inferred from an automatic metric.
- Models and language support are declared in `config/model-comparison.json`. Requests pass the model ID explicitly.

FLEURS is a public multilingual speech corpus; its Hugging Face dataset card declares CC BY 4.0 and documents 102 languages with a held-out test split. Check the upstream card and applicable attribution terms before redistributing derived material: [dataset card](https://huggingface.co/datasets/google/fleurs), [dataset description](https://huggingface.co/datasets/google/fleurs/blob/refs/pr/29/README.md).

## Categories

### Quality

- STT reports corpus WER or CER according to each language's configured normalization. Reports include a bootstrap 95% interval, sample count, and failed sample count.
- TTS quality is measured through local human blind listening: naturalness, intelligibility, and pronunciation/prosody on a 1–5 scale. A fixed SenseVoice model supplies round-trip ASR WER/CER as a supplemental intelligibility signal, not a substitute for listening.
- TTS output files, prompt text, blind mapping, and raw ratings are local artifacts. Commit only the aggregated score JSON if desired.

### Performance

- A fresh server process per model/language case gives a process-cold first request. OS file cache is not cleared, so this is not a cold-disk measurement.
- Serial warm measurements report request wall time, API inference time, queue time, RTF, process CPU time, all-logical-CPU-normalized utilization, RSS, and peak RSS. TTS also reports characters per inference second.
- For adapters that run inference in a child process, request latency and inference time cover the operation, while the existing CPU/RSS response headers measure only the SmartVoice API process; reports identify this resource-measurement limit explicitly.
- Warm-up count, measurement count, and performance sample count are configured by profile.

### Concurrency and request experience

This category measures **one model instance pool under load**. It answers how much
same-model traffic the configured service can sustain while returning valid
responses promptly. Do not use the old closed-loop worker count as the concurrency
capacity: a closed-loop client slows its request generation as the service slows,
which can hide overload and queue growth.

- Generate requests at a fixed, open-loop arrival rate. Record the offered rate,
  delivered rate, request count, successful valid responses, failures by HTTP
  status, and success rate. Use identical model, language, input fixtures and
  request mix at each rate.
- Establish a low-load, fully-warm P90 end-to-end latency baseline, then increase
  the offered rate in steps. A rate passes when valid-response success is at least
  99.9%, full-response P90 is no more than 1.2 times the baseline, and per-window
  latency/queue wait show no accumulating backlog. Report the highest tested
  passing rate; also report the first failing rate. A success-only rate or an
  unbounded queue is not evidence of sustainable capacity.
- Validate response content as well as HTTP status. For STT, compare concurrent
  output with a serial result for the same fixture and report mismatches. For TTS,
  require a decodable, non-empty, non-silent audio response with the expected
  format. Keep model quality metrics in the `quality` category; concurrency
  validation detects output failures or changes caused by concurrent execution.
- Report user-facing full-response P50/P90/P95, plus HTTP admission wait and
  adapter-instance wait separately. Include inference time and client scheduling
  lag so a slow load generator cannot make a run appear healthy.
- Measure resource use for the complete server process tree: average and P90 CPU
  cores, settled and peak RSS. Report single-instance warm operation, expansion
  from one to two instances, and fully-warm two-instance operation separately.
  Across repeated fresh-process trials, report P90 first-instance warm request,
  expansion request, second-instance initialization plus first inference, and
  fully-warm response latency. State whether OS file cache was cold; do not call a
  process-cold run a cold-disk run.
- Record all settings that affect capacity, including parallel-inference switch,
  min/max instances, threads per instance, waiting-queue limit, queue/execution
  timeouts, and server process count. Include OS/CPU/RAM, runtime versions, model
  and input fingerprints, load-generator concurrency, offered-rate schedule,
  duration and window size. Model capacity is per model and per service process;
  do not add independent model results together.

The acceptance threshold above is the current comparison rule, not a universal
latency SLA. Compare only matching inputs, language, platform, service settings,
and model/runtime fingerprints. Resource results are reported alongside capacity
and latency so a higher rate is not presented without its memory/CPU cost.

## Git and local data policy

Git tracks the runner, config, benchmark instructions, and reviewed formal result reports under `result/`. Smoke reports are quick-validation artifacts only: they default to the local SmartVoice user data directory, are marked `quick_validation_only`, must not be used as formal model evaluations, and the runner rejects attempts to write them under `benchmarks/result/`. Do not copy smoke reports into Git. The public dataset is downloaded only when requested. Hugging Face cache, converted audio, server logs, generated TTS audio, blind mappings, raw rating sheets, and default reports live under the SmartVoice user data directory (`<data_dir>/benchmark-cache` and `<data_dir>/benchmark-runs`) and are ignored if copied below this directory. Model reports contain no utterance text, audio, user paths, process IDs, or individual listening ratings.

The default report is written outside the repository. When saving a result under `result/`, select one model with `--model` and use a model-specific filename so each report contains one model only. Review the report before committing it.

## Setup

Install the app and benchmark dependencies, and make sure the models selected in the config are installed:

```bash
python -m pip install -e ".[inference,dev]"
python -m pip install -r benchmarks/requirements.txt
python -m smartvoice models list
```

The adapter downloads the pinned FLEURS test Parquet shards as data, without executing the dataset repository's Python loader. The source shards and selected utterances' converted WAV files are cached under the configured SmartVoice user data directory; no audio is stored in this repository. Dataset loading requires network access the first time for each configured language/revision.

## Run

The `smoke` profile is for quick validation only, not formal model evaluation. It
runs quality and serial performance only: 8 quality samples per language, then
one cold request, one warm-up request, and two measured requests per model/language
case. It does not run the concurrency-capacity protocol. Its report stays in the
local user data directory and cannot be written to `benchmarks/result/`:

```bash
python -m benchmarks.runner --profile smoke --model stt-sensevoice-small-int8 --model tts-kokoro-multilingual-v1-1-zh-en
```

Standard quality/performance comparison (100 quality samples/language and 20 warm performance iterations):

```bash
python -m benchmarks.runner --profile standard \
  --model stt-qwen3-asr-600m-int8 \
  --output benchmarks/result/macos-arm64-stt-qwen3-asr-600m-int8.json
```

For formal tracked comparisons, use `standard` or `full`, review the report, and save one model per JSON file under `result/`. Never use `smoke` results as formal evaluation evidence.

Full quality/performance comparison uses all available FLEURS test samples and 50 warm iterations. The `cpu_bounded` profile uses five quality samples per language and two warm performance iterations; its quality scores are exploratory and should not be treated as directly comparable to standard-profile reports. Use `--model <id>` one or more times to select a subset. Edit the tracked config to add languages, data sources, model-language combinations, or profiles; keep dataset audio outside Git.

Run selected model-comparison categories with `--category <name>`; these are
`quality` and `performance`. Same-model concurrency uses the dedicated fixed-rate
HTTP harness in [`elastic-pool/`](elastic-pool/README.md). Add a scenario there
for each model/language/input fixture so results share the same open-loop load,
correctness checks, latency thresholds, and resource measurements. This separates
same-model capacity from the retired closed-loop worker-count test.

The model-comparison runner starts and stops an isolated local SmartVoice server
per model/language case. It requires selected models and the configured TTS judge
model to be installed and records serial performance and service settings.
Tracked model reports keep one evaluated model per platform and JSON file. Legacy closed-loop worker-count data has been removed from the formal concurrency category; use only the fixed-arrival results embedded in each report.

For a controlled code comparison, `--server-source <checkout>` selects the source tree used by server subprocesses while the benchmark runner and report remain in the current checkout. Use the same model files, settings, and workloads for each source tree.

## TTS listening

Each run that includes TTS writes `tts-ratings-template.csv`, `tts-audio/`, and `blind-mapping.json` in the local run directory. Listen to each relative `audio_file`, fill the three rating columns from 1 to 5, and keep the mapping file private. Multiple listener rows may use the same `sample_key`.

Use the [TTS listening rubric](tts-listening-rubric.md) for rating anchors and a consistent listening setup.

Aggregate ratings without copying raw listener data or audio into the report:

```bash
python -m benchmarks.aggregate_tts_ratings \
  --ratings /path/to/local-run/tts-ratings-template.csv \
  --mapping /path/to/local-run/blind-mapping.json \
  --output benchmarks/result/tts-quality-<machine>-<date>.json
```

## Custom STT corpus

`config/stt-reference-manifest.example.csv` is a header-only template. Keep actual recordings and completed manifests local. Evaluate an installed model against a user-provided corpus:

```bash
python -m benchmarks.evaluate_manifest /path/to/private-manifest.csv \
  --model stt-sensevoice-small-int8 \
  --output /path/outside/repository/custom-stt-result.json
```

The custom-corpus report is aggregated and does not include audio paths, references, or hypotheses.

## Extending to another language

1. Add a language entry with the dataset config and a supported normalization profile (`latin_wer`, `han_cer`, `char_cer`, or `whitespace_wer`).
2. Add that language to model entries that support it.
3. Confirm dataset revision, split, license, language normalization, and model training-data overlap.
4. Run `smoke`, inspect quality and artifact output, then run `standard` before publishing the model's result report.

The dataset adapter currently targets FLEURS. Supporting a different dataset family requires adding an adapter under `benchmarks/`, not changing the inference API or application source.


## Streaming preview evaluation

The independent streaming entry is `python scripts/validate_speech.py streaming --help`. See [streaming benchmark procedure and known limitations](streaming/README.md), [product session contract](../doc/streaming.md), and [deterministic coverage matrix](../doc/streaming-test-coverage.md). This entry reports six streaming routes separately from one-shot model comparison. It currently requires caller-provided pinned audio/onset/baseline assets; the documented fixtures are local sandbox assets. Full asset migration, runner guard fixes and formal streaming acceptance remain deferred.
