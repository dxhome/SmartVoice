# Streaming real-browser QA, revision 1

This checklist targets the shipped browser client at `/streaming`; the WebSocket benchmark runner does not substitute for browser DOM updates, Web Audio scheduling, or physical playback. Record browser/version, OS, mode, direction, fixture SHA-256, network profile, terminal status, and the final diagnostics JSON for every run. Keep screenshots or a short screen recording for failures.

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

1. **Slow network:** DevTools Network throttling to Slow 3G; run all three modes. Record time to session-ready, source/target first visible output, buffer growth, terminal status and playback gaps. Repeat once with offline toggled mid-session; expect a visible interruption and no false `complete` result.
2. **Disconnect/reconnect:** terminate the WebSocket during upload, during partial output, and while audio is queued. The UI must leave the running state, stop stale playback, preserve a useful error, and allow a clean retry with no events from the old run.
3. **Background scheduling:** start a 60–120 second speech fixture, background/minimize the tab during upload and during playback, then foreground it. Record dropped/late chunks, ordering, `played_chunks`, last-played time and terminal skip reasons. Do not count an `audio_started` callback alone as audible playback.
4. **Repeated playback:** repeat the same speech run 10 times without reloading the page. Check that audio queues, transcript rows, timers and audio contexts return to idle after each run and that no prior-run text/audio leaks into the next.
5. **DOM consistency:** use browser DevTools to inspect `.transcript-pair`, `.source-line`, and `.translation-line`. Capture each `unit_id`, revision, committed state and rendered text at first partial and final. A committed row must not be overwritten by a stale partial.

## Acceptance worksheet

For each route and stress case, record `pass`, `fail`, or `blocked`, with evidence links:

| Browser/version | Route/case | Network/background condition | DOM/order | Audio complete/order | Terminal/error behavior | Result/evidence |
|---|---|---|---|---|---|---|
| | | | | | | |

Current scope is manual because this environment has no installed Playwright/Selenium runtime and the in-app browser blocks the loopback service URL. Do not mark browser acceptance based on `benchmarks/streaming/run.py`; that runner measures WebSocket event delivery only.
