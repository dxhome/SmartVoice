# Historical human review imports

`pre-s4-human-reviews.json` preserves the 11 submitted streaming evaluations (including repeated reviews of the same case). The original `quality_accepted` values are all false, so the imports are evidence of submitted ratings, not an approved golden set. No ratings or timestamps were rewritten.

`tts-human-reviews.json` preserves eight blind A/B TTS reviews. The source record stores A/B scores and preferences but does not include the A/B-to-model stimulus mapping; do not infer that mapping from the scores.

The speech-onset review checklist is separate. Fill it only after listening to the bundled clips without seeing the VAD estimate. Human onset values are required before onset-adjusted TTFO can be used for formal acceptance.

`current-product-quality-review-packet-v1.json` contains the current product source/target outputs for 22 representative clean high-risk cases from the 90-case quality corpus. Ratings are blank by design; English-to-Chinese items without a supplied target reference require an independent reviewer reference or a meaning-based assessment from the source audio.
