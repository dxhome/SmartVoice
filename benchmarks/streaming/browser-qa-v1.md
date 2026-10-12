# Streaming real-browser QA, revision 1

This checklist targets the shipped browser client at `/console/streaming`; the WebSocket benchmark runner does not substitute for browser DOM updates, Web Audio scheduling, or physical playback. Record browser/version, OS, mode, direction, fixture SHA-256, network profile, terminal status, and the final diagnostics JSON for every run. Keep screenshots or a short screen recording for failures.

## Baseline route coverage

Run with a supported desktop browser and the pinned short clean files under `benchmarks/streaming/data/fleurs/clean/`.

| Route | Fixture | Assertions |
|---|---|---|
| Source subtitles, zh | `zh_02_clean.wav` | Nonempty source partial appears before final; revisions replace the draft for the same unit; final remains visible after completion. |
| Source subtitles, en | `en_02_clean.wav` | Same DOM assertions; text and source-unit ordering match the final event stream. |
| Translated subtitles, zh→en | `zh_04_clean.wav` | Source and target partials appear; a revised target replaces its previous draft without duplicate committed rows; final order is stable. |
| Translated subtitles, en→zh | `en_01_clean.wav` | Same assertions; inspect Chinese text rendering and final-unit order. |
| Spoken interpretation, zh→en | `zh_02_clean.wav` | Every received `audio_sequence` is scheduled in order; `played_chunks` equals terminal audio count; playback reaches drain; no skipped reason. |
| Spoken interpretation, en→zh | `en_01_clean.wav` | Same audio assertions and correct target language rendering. |

## Browser/network stress cases

1. **Slow network:** DevTools Network throttling to Slow 3G; run all three modes. Record time to session-ready, source/target first visible output, buffer growth, terminal status and playback gaps. Repeat once with offline toggled mid-session; expect an interruption and automatic resumption within the configured grace period. Beyond that grace, expect a visible recovery failure and no false `complete` result.
2. **Disconnect/reconnect:** terminate the WebSocket during upload, during partial output, and while audio is queued. Within recovery grace, the UI must reconnect to the same logical session, deduplicate input and completed audio, and finish with identical final text. Unfinished audio chunks may restart; completed chunks must never complete twice. Recovery expiry must report an error and allow a clean retry.
3. **Background scheduling:** start a 60–120 second speech fixture, background/minimize the tab during upload and during playback, then foreground it. Record dropped/late chunks, ordering, `played_chunks`, last-played time and terminal skip reasons. Do not count an `audio_started` callback alone as audible playback.
4. **Repeated playback:** repeat the same speech run 10 times without reloading the page. Check that audio queues, transcript rows, timers and audio contexts return to idle after each run and that no prior-run text/audio leaks into the next.
5. **DOM consistency:** use browser DevTools to inspect `.transcript-pair`, `.source-line`, and `.translation-line`. Capture each `unit_id`, revision, committed state and rendered text at first partial and final. A committed row must not be overwritten by a stale partial.

## Acceptance worksheet

For each route and stress case, record `pass`, `fail`, or `blocked`, with evidence links:

| Browser/version | Route/case | Network/background condition | DOM/order | Audio complete/order | Terminal/error behavior | Result/evidence |
|---|---|---|---|---|---|---|
| | | | | | | |

For repeatable loopback browser verification, use `python -m benchmarks.streaming.browser_proxy --upstream http://127.0.0.1:9192 --port 9292`. Open `/console/streaming` on port 9292 in a real browser, load the pinned files, and use its QA controls. The proxy limits upload to 48,000 B/s and download to 64,000 B/s with 80 ms one-way latency. It pipelines latency rather than adding an RTT per audio frame, and reports actual delivery bytes/delays and bounded-buffer peaks. The disconnect controls close the real WebSocket during upload or after the first audio payload. The injected observer records DOM revisions and Web Audio completion, saving evidence locally. It does not change the shipped page or inference logic.

Run `python -m benchmarks.streaming.analyze_browser <evidence-directory> --output <matrix.json>` to check final DOM equality, exact audio/final-text spans, playback order, and strict final-text equality to the first successful same-fixture/mode/language case. Confirm `visibility_states` contains `hidden` before claiming background coverage: opening a second IAB tab alone does not establish this. Speaker-level audibility remains an independent check. Do not mark browser acceptance based on `benchmarks/streaming/run.py`; that runner measures WebSocket event delivery only.

### Expected negative controls

Keep negative-control evidence in the matrix and preserve its original `passed: false` and error list. Declare only the exact evidence file and expected server error, for example:

```sh
python -m benchmarks.streaming.analyze_browser <evidence-directory> \
  --output <matrix.json> \
  --expect-negative browser-1791710555047592000.json:input_timeout \
  --product-default-idle-seconds 60
```

The analyzer records `classification=expected_negative`, `expectation_met`, and whether the row counts as an unexpected product failure. A control matches only when the declared server error and an incomplete terminal are both present. Do not classify by browser name or fixture alone. The 10-second Safari idle-timeout run is a legacy-configuration negative control; the matching background playback case with the current 60-second default completed successfully.
