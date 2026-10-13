"""Full-only, one-hour real streaming through the public async SDK."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import time
import unittest
import uuid
import wave
from pathlib import Path

from smartvoice.config.settings import Settings
from smartvoice.services.model_registry import get_model_spec
from smartvoice.services.model_storage import model_directory
from smartvoice.services.streaming.planning import resolve
from smartvoice_sdk import SmartVoiceClient

from tests.streaming_long_audio import ROOT, MANIFEST, configuration, fixture, repeated_blocks
from ._real_service import RealService


class _Catalog:
    get_spec = staticmethod(get_model_spec)


def _required_models() -> tuple[str, ...]:
    plan, _, _ = resolve(configuration("transcription", "zh"), _Catalog())
    return plan.model_ids


_MODELS = _required_models()
_INSTALLED = all(
    (model_directory(Settings.from_env(), model_id) / "smartvoice-model.json").is_file()
    for model_id in _MODELS
)


def _current_rss_bytes() -> int | None:
    try:
        import psutil

        return int(psutil.Process().memory_info().rss)
    except ImportError:
        try:
            result = subprocess.run(
                ["ps", "-o", "rss=", "-p", str(os.getpid())],
                check=True, capture_output=True, text=True, timeout=5,
            )
            return int(result.stdout.strip()) * 1024
        except (OSError, ValueError, subprocess.SubprocessError):
            return None


@unittest.skipUnless(
    os.environ.get("SMARTVOICE_TEST_SUITE") == "full" and _INSTALLED,
    "The SDK one-hour real-model stream runs in full with the zh transcription chain installed",
)
class SDKFullStreamingTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.service = RealService(_MODELS).start()
        cls.report_dir = ROOT / ".smartvoice-dev" / "sdk-full"
        cls.report_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.service.close()
        finally:
            super().tearDownClass()

    async def test_one_hour_stream_recovers_twice_and_keeps_client_buffers_bounded(self):
        case, pcm = fixture("zh")
        self.assertTrue(pcm)
        started = time.monotonic()
        samples_sent = 0
        max_pending_frames = 0
        max_audio_history = 0
        events_seen = 0
        source_finals = 0
        last_source_end = 0
        disconnects: list[float] = []
        recovered = 0
        peak_sampled_rss_bytes = _current_rss_bytes()
        last_rss_sample = started
        terminal = None
        failure = None
        report_path = self.report_dir / f"sdk-hour-{uuid.uuid4().hex}.json"
        try:
            async with SmartVoiceClient(
                self.service.base_url, request_timeout=120, resume_attempts=5, resume_backoff=0.2,
            ) as client:
                async with await client.start_stream(
                    mode="transcription", source_language="zh", resumable=True,
                ) as session:
                    capabilities = await client.streaming_capabilities()
                    chain = capabilities.chain("transcription", "zh")
                    self.assertIsNotNone(chain)
                    self.assertTrue(chain["available"], chain)
                    recovery_limit = session._input_history_limit
                    current_socket = session._socket
                    outstanding_disconnect = False
                    targets = (10.0, 1800.0)

                    async def send_hour():
                        nonlocal samples_sent, max_pending_frames
                        audio_started = time.monotonic()
                        for block in repeated_blocks(pcm, 3600, frame_samples=16000):
                            await session.send_audio_frame(block)
                            samples_sent += len(block) // 2
                            max_pending_frames = max(max_pending_frames, len(session._pending_input))
                            deadline = audio_started + samples_sent / 16000
                            wait = deadline - time.monotonic()
                            if wait > 0:
                                await asyncio.sleep(wait)
                        await session.finish()

                    async def receive_hour():
                        nonlocal events_seen, source_finals, last_source_end
                        nonlocal recovered, current_socket, outstanding_disconnect, terminal
                        nonlocal max_audio_history
                        nonlocal peak_sampled_rss_bytes, last_rss_sample
                        async for event in session.events():
                            events_seen += 1
                            if event.type in {"source_final", "source_unit_final"}:
                                source_finals += 1
                            if event.type in {"source_final", "utterance_complete"}:
                                last_source_end = max(last_source_end, int(event.data.get("end_sample", 0)))
                            max_audio_history_local = len(session._audio_seen)
                            max_audio_history = max(max_audio_history, max_audio_history_local)
                            if outstanding_disconnect and session._socket is not current_socket:
                                recovered += 1
                                outstanding_disconnect = False
                                current_socket = session._socket
                            elapsed_audio = samples_sent / 16000
                            if (event.type.startswith("source_") and event.data.get("text", "").strip()
                                    and recovered < len(targets) and elapsed_audio >= targets[recovered]
                                    and not outstanding_disconnect):
                                current_socket = session._socket
                                outstanding_disconnect = True
                                disconnects.append(elapsed_audio)
                                await current_socket.close()
                            if event.terminal:
                                terminal = dict(event.data)
                            if time.monotonic() - last_rss_sample >= 30:
                                rss_bytes = _current_rss_bytes()
                                if rss_bytes is not None:
                                    peak_sampled_rss_bytes = max(peak_sampled_rss_bytes or 0, rss_bytes)
                                last_rss_sample = time.monotonic()

                    async with asyncio.timeout(4200):
                        sender = asyncio.create_task(send_hour())
                        receiver = asyncio.create_task(receive_hour())
                        try:
                            await asyncio.gather(sender, receiver)
                        finally:
                            for task in (sender, receiver):
                                if not task.done():
                                    task.cancel()
                            await asyncio.gather(sender, receiver, return_exceptions=True)

                    self.assertEqual(samples_sent, 3600 * 16000)
                    self.assertEqual(session._input_samples, samples_sent)
                    self.assertEqual((terminal or {}).get("type"), "session_complete", terminal)
                    self.assertEqual((terminal or {}).get("status"), "complete", terminal)
                    self.assertEqual(source_finals > 0, True)
                    self.assertEqual(last_source_end, samples_sent)
                    self.assertEqual(len(disconnects), 2, disconnects)
                    self.assertEqual(recovered, 2)
                    self.assertLessEqual(max_pending_frames, recovery_limit)
                    self.assertLessEqual(max_audio_history, 128)
        except BaseException as exc:
            failure = f"{type(exc).__name__}: {str(exc)[:1500]}"
            raise
        finally:
            final_rss = _current_rss_bytes()
            if final_rss is not None:
                peak_sampled_rss_bytes = max(peak_sampled_rss_bytes or 0, final_rss)
            result = {
                "schema": "smartvoice.sdk.full-hour.v1",
                "mode": "transcription",
                "source_language": "zh",
                "fixture_id": case["id"],
                "fixture_sha256": case["sha256"],
                "manifest_sha256": hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
                "models": list(_MODELS),
                "audio_seconds_sent": samples_sent / 16000,
                "wall_seconds": time.monotonic() - started,
                "events_seen": events_seen,
                "source_finals": source_finals,
                "last_source_end_sample": last_source_end,
                "disconnect_audio_seconds": disconnects,
                "successful_resumes": recovered,
                "max_pending_input_frames": max_pending_frames,
                "input_replay_window_frames": session._input_history_limit if "session" in locals() else None,
                "max_retained_audio_event_fingerprints": max_audio_history,
                "process_peak_sampled_rss_bytes": peak_sampled_rss_bytes,
                "terminal": terminal,
                "failure": failure,
                "scope": "One hour of real-time pinned PCM through the public SDK and live local service; includes two TCP/WebSocket interruptions; sampled process RSS includes service and model runtime.",
            }
            report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
            print(f"SDK full streaming report: {report_path}", flush=True)


if __name__ == "__main__":
    unittest.main()
