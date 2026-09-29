# Model Comparison Benchmark

The first benchmark layer compares models on one fixed platform. Results are reported in three independent categories: `quality`, `performance`, and `concurrency`. Do not combine them into one score or compare values from different datasets/languages as if they were equivalent.

## Scope

- Configured languages: English (`en_us`), Simplified Chinese (`cmn_hans_cn`), Hindi (`hi_in`), Spanish (`es_419`), Arabic (`ar_eg`), French (`fr_fr`), Brazilian Portuguese (`pt_br`), Russian (`ru_ru`), German (`de_de`), and Cantonese (`yue_hant_hk`). Language profiles and model-language combinations are configuration-driven.
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
- Warm-up count, measurement count, and performance sample count are configured by profile.

### Concurrency

- Closed-loop concurrent clients run at configured worker levels and report requests/second, p50/p95/p99 request latency, queue wait, inference and CPU time, successes/failures, and observed RSS peak.
- A sustained phase runs a fixed number of workers for a configured duration and reports latency/queue/CPU/RTF summaries, RSS range, and estimated RSS growth per minute.
- These are observations, not SLA thresholds. Compare only runs with matching model, language, platform, profile, service settings, and dataset/config fingerprints.

## Git and local data policy

Git tracks the runner, config, benchmark instructions, and one JSON result report per model under `result/`. The public dataset is downloaded only when requested. Hugging Face cache, converted audio, server logs, generated TTS audio, blind mappings, and raw rating sheets live under the SmartVoice user data directory (`<data_dir>/benchmark-cache` and `<data_dir>/benchmark-runs`) and are ignored if copied below this directory. Model reports contain no utterance text, audio, user paths, process IDs, or individual listening ratings.

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

Quick pipeline check using a small sample set, two workers, and a short soak:

```bash
python -m benchmarks.runner --profile smoke --model stt-sensevoice-small-int8 --model tts-melo-zh-en
```

Standard comparison (100 quality samples/language, 20 warm performance iterations, up to 4 concurrent workers, 60-second soak):

```bash
python -m benchmarks.runner --profile standard \
  --model stt-qwen3-asr-600m-int8 \
  --output benchmarks/result/macos-arm64-stt-qwen3-asr-600m-int8.json
```

Run the command once per model when saving tracked results; do not place multi-model output in `result/`.

Full comparison uses all available FLEURS test samples, 50 warm iterations, concurrency up to 8 workers, and a 5-minute soak. Use `--model <id>` one or more times to select a subset. Edit the tracked config to add languages, data sources, or model-language combinations; keep dataset audio outside Git.

Run selected categories only with `--category <name>`; the supported values are `quality`, `performance`, and `concurrency`. Repeat the flag to run more than one category. For example, `--category quality` skips performance and load tests while measuring all quality samples in the selected model-language combinations.

The runner starts and stops an isolated local SmartVoice server per model/language case. It requires selected models and the configured TTS judge model to be installed. It honors the current SmartVoice settings for inference threads and queue limits, which are recorded in the report.

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
