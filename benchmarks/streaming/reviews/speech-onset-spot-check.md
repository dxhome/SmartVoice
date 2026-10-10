# Human speech-onset spot check

Purpose: replace provisional VAD onset estimates for six samples that cover the measured latency tail and low-level English speech. The annotator should listen before viewing VAD values, and record the first clearly audible speech (ignore leading room noise and breaths).

For each row, record onset time relative to the WAV start in seconds, calculate `human_onset_sample = round(seconds * 16000)`, reviewer alias, and a short note if the start is ambiguous. Then update the matching case in `data/fleurs/onset-annotations-v1.json`. Do not overwrite `vad_onset_sample`.

| Language | Case | Audio | Human onset seconds | Reviewer | Note |
|---|---|---|---:|---|---|
| zh | `zh_01_clean` | [play clip](../data/fleurs/clean/zh_01_clean.wav) |  |  |  |
| zh | `zh_03_clean` | [play clip](../data/fleurs/clean/zh_03_clean.wav) |  |  |  |
| zh | `zh_08_clean` | [play clip](../data/fleurs/clean/zh_08_clean.wav) |  |  |  |
| en | `en_03_clean` | [play clip](../data/fleurs/clean/en_03_clean.wav) |  |  |  |
| en | `en_04_clean` | [play clip](../data/fleurs/clean/en_04_clean.wav) |  |  |  |
| en | `en_08_clean` | [play clip](../data/fleurs/clean/en_08_clean.wav) |  |  |  |

These six annotations are a spot check, not a full human onset ground truth set. Until they are filled and validated, onset-adjusted P90 remains diagnostic only.
