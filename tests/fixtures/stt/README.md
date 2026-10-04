# STT regression fixtures

The six mono 16 kHz PCM utterances and matching `transcripts.json` are fixed English and Mandarin speech fixtures synthesized locally with SmartVoice's `tts-kokoro-multilingual-v1-1-zh-en` model. They allow the suite to create exact-duration speech inputs without network access or personal Downloads folders. They exercise real inference and segmentation contracts; synthetic speech does not represent natural-speaker quality results.

## Audio provenance

- Model: Kokoro 1.1 Chinese/English, SmartVoice catalog ID `tts-kokoro-multilingual-v1-1-zh-en`; the Sherpa-ONNX model archive is [`kokoro-multi-lang-v1_1.tar.bz2`](https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/kokoro-multi-lang-v1_1.tar.bz2), pinned in the catalog by SHA-256 `a3f4c73d043860e3fd2e5b06f36795eb81de0fc8e8de6df703245edddd87dbad`.
- Upstream model: [`hexgrad/Kokoro-82M-v1.1-zh`](https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh), whose model card declares Apache-2.0. The Sherpa-ONNX archive also includes an Apache-2.0 `LICENSE`.
- Voice and generation settings: SmartVoice's default Kokoro voice resolves to speaker ID `0` for English and `3` for Chinese; speed `1.0`. Native output is mono 24 kHz PCM16 WAV, converted to mono 16 kHz PCM16 WAV for these fixtures. The audio files were generated locally on 2026-10-04; no human voice recordings are included.
- The six transcript strings are the authored text prompts used for these fixtures. They are included as labels and are not transcript claims from a human-speaker corpus.

The files are small test outputs, not model weights. Keep the model and conversion provenance above with the fixtures so their origin and upstream license references remain clear.

Long inputs are built deterministically at test time by cycling the labelled utterances with 240 ms of silence between utterances and padding the tail to the requested duration. The test transcript contains every complete utterance included in the audio.

The transcripts are reference labels for the fixed utterances only. CI, regression, and full test suites do not compare recognized text against them and do not score model accuracy. They verify that the service accepts the audio, returns non-empty text and correct request metadata, and reports expected segmentation behavior.
