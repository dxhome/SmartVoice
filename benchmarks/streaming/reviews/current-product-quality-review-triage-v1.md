# Current product S5 review triage

This is a machine-assisted listening checklist for `current-product-quality-review-packet-v1.json`, not a human review, score, or acceptance result. Do not copy these observations into ratings without listening to the source audio. The packet remains `pending_human_review` and `quality_accepted: false` until a reviewer records scores.

## Suggested order

Start with the cases where the current text already signals a likely meaning-changing defect. For each case, listen to the source first, then compare source final, target final, and reference. The target reference is a guide, not an exact-string requirement. Rate all required dimensions using the review UI/schema; add concise notes for material defects.

| Priority | Case | Direction | Listening focus from output comparison |
|---|---|---|---|
| 1 | `zh_07_clean` (new confirmation) | zh→en | ASR ends with `队游客扫射`; target omits rifle/action detail (`swept his M16 tourists`). Check the spoken name, weapon, and action. |
| 2 | `zh_04_clean` (existing regression) | zh→en | Source final is heavily corrupted; target has an incoherent comparison. Check the full sentence and whether any meaning survives. |
| 3 | `zh_08_clean` (existing regression) | zh→en | Target turns a beach/swimming/shade sentence into “beach-swaming safe”; check segmentation and proposition preservation. |
| 4 | `zh_06_clean` (existing regression) | zh→en | “Sagittarius galaxy” is rendered as “galaxies in human horses”; check the proper noun and sentence meaning. |
| 5 | `zh_05_clean` (new confirmation) | zh→en | Compare near/far crust thickness; target repeats “outer side” and loses the contrast. |
| 6 | `zh_05_clean` (existing regression) | zh→en | Check METI, Apple, 34 incidents, overheating, and “not serious”; output has `expansion of heat` and a potentially fragmented ending. |
| 7 | `zh_06_clean` (new confirmation) | zh→en | Check Erdoğan's name and whether the full “statement” was captured; source final ends at `声`. |
| 8 | `zh_03_clean` (new confirmation) | zh→en | Check Layton's name, duplicated “在”, Conservative bill, and whether “programme” changes the bill meaning. |
| 9 | `zh_09_clean` (existing regression) | zh→en | Check “Rossby number”; output says “Rose multipliers.” Confirm the negative relationship is preserved. |
| 10 | `zh_07_clean` (existing regression) | zh→en | Check temporal meaning “had become” vs “still”; the source output uses `依然`. |
| 11 | `zh_00_clean` (new confirmation) | zh→en | Source and target finals appear cut off at “预 / pre”; check whether this is an actual truncated final or just a display artifact. Verify the negation and certainty. |
| 12 | `zh_00_clean` (existing regression) | zh→en | Check the proper name Martelly and the committee name; target says “New Zero” and may lose the intended entity. |
| 13 | `zh_01_clean` (existing regression) | zh→en | Check the 35 mm photography/cinema explanation and whether the two output segments form a coherent complete sentence. |
| 14 | `en_00_clean` (new confirmation) | en→zh | No target reference is supplied. Independently judge the source meaning and whether the Chinese output preserves the ancestor/protein/savanna comparison; inspect the `<unk>` token. |
| 15 | `en_03_clean` (new confirmation) | en→zh | No target reference is supplied. Identify the spoken “Shing zone” phrase from audio and judge whether `雪区` is correct; source transcription itself may be corrupted. |
| 16 | `en_02_clean` (new confirmation) | en→zh | No target reference is supplied. Check the disease transmission route, pig subject, and mosquito vector from audio. |
| 17 | `en_01_clean` (new confirmation) | en→zh | No target reference is supplied. Check whether the rebuilt buildings and visitor-understanding purpose are both preserved. |
| 18 | `zh_01_clean` (new confirmation) | zh→en | Check the instruction to combine dry powders and form a ball; ensure “squeeze”/“rub” has not altered the action. |
| 19 | `zh_02_clean` (new confirmation) | zh→en | Check the conditional about knowing a Romance language and learning Portuguese. |
| 20 | `zh_02_clean` (existing regression) | zh→en | Negation anchor: verify “this is not goodbye” and the chapter/new beginning contrast. |
| 21 | `zh_03_clean` (existing regression) | zh→en | Check the requirement that men wear long pants covering the knees. |
| 22 | `zh_04_clean` (new confirmation) | zh→en | Check 15 m, August 2011, March 2017, and especially the “not until” timing. |

## Current completion state

- 22 cases are present; 18 have supplied target references and 4 en→zh cases require listening-based semantic judgment.
- All six rating fields remain null in the packet. No case is marked reviewed or accepted by this triage.
- Triage is based on packet text/reference comparison only. It cannot verify what was actually spoken or whether the final text matches the audio.
- A recorded S5 review should include source accuracy, names/numbers/negation, segmentation/punctuation, target meaning, target names/numbers/negation, and target fluency as applicable.
