# FLEURS benchmark audio notice

This fixture contains the 20 clean Chinese and English evaluation clips used by the SmartVoice streaming product benchmark, derived from the FLEURS dataset.

- Dataset: FLEURS, Google; [dataset card](https://huggingface.co/datasets/google/fleurs)
- Pinned revision: `d16ac437ac42fb543bf2893b934dac1af970e361`
- License stated by the source manifest: Creative Commons Attribution 4.0 International (CC BY 4.0)
- Attribution: FLEURS, Google, Conneau et al. (2022)
- Local preparation: converted to 16 kHz mono PCM16 WAV for streaming evaluation. Only clean variants are included; no synthetic noise or gain variants are included.

The accompanying manifest records each sample's SHA-256 and reference transcript. The onset annotations are VAD estimates only; human onset labels remain pending and must not be used as ground truth.
