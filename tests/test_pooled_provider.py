"""Provider facade integration and routing metadata freshness."""
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi.testclient import TestClient

from smartvoice.adapters.inference.runtime.pooled_provider import PooledInferenceProvider, PooledSherpaProvider
from smartvoice.adapters.inference.factory import create_inference_provider
from smartvoice.app import create_app
from smartvoice.config.settings import Settings
from smartvoice.domain.contracts import SynthesizedSpeech


class FakeRuntime:
    def __init__(self):
        self.calls = []
        self.models = [{"id": "a", "backend": "sherpa-onnx"}]
        self.scans = 0
        self.model_repository = SimpleNamespace(catalog_models=lambda: self.models)

    def installed_models(self):
        self.scans += 1
        return self.models

    def synthesize(self, text, voice, speed, model_id, language):
        self.calls.append((text, voice, speed, model_id, language))
        return SynthesizedSpeech(b"RIFF", 24000, 1.0)

    def close(self):
        pass


class SpeechMetadata:
    """Minimal metadata adapter for exercising the real HTTP/provider path."""

    def __init__(self):
        self.model_repository = SimpleNamespace(
            catalog_models=lambda: [{"id": "tts-matcha-zh-baker", "backend": "sherpa-onnx"}]
        )

    def installed_models(self):
        return [{"id": "tts-matcha-zh-baker", "task": "speech", "languages": ["zh"]}]

    def runtime(self):
        return {"backend": "sherpa-onnx", "provider_status": "available", "actual_device": "cpu"}

    def capabilities(self):
        return {"api_version": "v1", "tasks": [{"task": "speech", "available": True}]}

    def language_identification_available(self):
        return False

    def close(self):
        pass


class ConcurrentSpeechRuntime:
    def __init__(self, entered, release, *, barrier=None):
        self.entered = entered
        self.release = release
        self.barrier = barrier
        self.lock = threading.Lock()
        self.active = 0
        self.peak_active = 0

    def close(self):
        pass

    def synthesize(self, text, voice, speed, model_id, language):
        with self.lock:
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
        self.entered.set()
        try:
            if self.barrier is not None:
                self.barrier.wait(timeout=3)
            else:
                self.release.wait(timeout=3)
            return SynthesizedSpeech(b"RIFF-test-wav", 24000, 0.1)
        finally:
            with self.lock:
                self.active -= 1


class PooledProviderTests(unittest.TestCase):
    def test_tts_language_and_voice_do_not_create_duplicate_model_pools(self):
        metadata = FakeRuntime()
        settings = Settings(data_dir=Path("/tmp/smartvoice"))
        provider = PooledSherpaProvider(metadata, lambda seed: FakeRuntime(), settings)
        self.addCleanup(provider.close)
        provider.synthesize("a", model_id="a", language="zh")
        provider.synthesize("b", voice="3", model_id="a", language="en")
        self.assertEqual(list(provider.pool.snapshot()), ["a"])
        self.assertEqual(provider.pool.snapshot()["a"]["instances"], 1)

    def test_public_model_list_is_fresh_while_routing_snapshot_is_cached(self):
        metadata = FakeRuntime()
        provider = PooledInferenceProvider(metadata, lambda seed: FakeRuntime(), Settings(data_dir=Path("/tmp")))
        self.addCleanup(provider.close)
        provider.inference_models()
        provider.inference_models()
        self.assertEqual(metadata.scans, 1)
        metadata.models = []
        self.assertEqual(provider.installed_models(), [])
        self.assertTrue(provider.inference_models())
        with patch("smartvoice.adapters.inference.runtime.pooled_provider.time.monotonic", return_value=float("inf")):
            self.assertEqual(provider.inference_models(), [])

    def test_parallel_flag_uses_adapter_capacity_and_zero_forces_serial_limit(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory))
            provider = create_inference_provider(settings)
            self.addCleanup(provider.close)
            app = create_app(settings, provider=provider)
            self.assertGreater(app.state.inference_queue._capacity, settings.max_instances)
            limited = create_app(Settings(data_dir=Path(directory), max_concurrent_inference=0), provider=provider)
            self.assertEqual(limited.state.inference_queue._max_concurrent, 1)

    def test_two_same_model_http_requests_run_on_separate_pooled_instances(self):
        import tempfile

        entered = threading.Event()
        barrier = None
        runtimes = []
        runtimes_lock = threading.Lock()

        def make_runtime(seed):
            runtime = ConcurrentSpeechRuntime(entered, threading.Event(), barrier=barrier)
            with runtimes_lock:
                runtimes.append(runtime)
            return runtime

        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                data_dir=Path(directory), min_instances=1, max_instances=2,
                max_queued_inference=0, num_threads=2,
            )
            provider = PooledSherpaProvider(SpeechMetadata(), make_runtime, settings)
            # Expand only after the first runtime has finished its initial
            # load-and-inference lease; the test then measures true warm-pool
            # parallel execution rather than cold-start queuing.
            provider.synthesize("预热", model_id="tts-matcha-zh-baker", language="zh")
            entered.clear()
            barrier = threading.Barrier(2)
            runtimes[0].barrier = barrier
            app = create_app(settings, provider=provider)
            with TestClient(app) as client:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    responses = list(executor.map(
                        lambda text: client.post("/v1/audio/speech", json={
                            "model": "tts-matcha-zh-baker", "input": text, "language": "zh",
                        }),
                        ("你好", "欢迎"),
                    ))
                self.assertTrue(all(response.status_code == 200 for response in responses))
                self.assertTrue(all(response.content.startswith(b"RIFF") for response in responses))
                snapshot = provider.pool.snapshot()["tts-matcha-zh-baker"]
                self.assertEqual(snapshot["peak_active"], 2)
                self.assertEqual(snapshot["active"], 0)
                self.assertEqual(snapshot["waiting"], 0)
                # Includes the explicit warm-up request above.
                self.assertEqual(snapshot["completed"], 3)
            self.assertEqual(len(runtimes), 2)
            self.assertTrue(all(runtime.peak_active == 1 for runtime in runtimes))

    def test_full_model_pool_returns_http_503_and_running_request_completes(self):
        import tempfile

        entered, release = threading.Event(), threading.Event()
        runtimes = []

        def make_runtime(seed):
            runtime = ConcurrentSpeechRuntime(entered, release)
            runtimes.append(runtime)
            return runtime

        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                data_dir=Path(directory), min_instances=1, max_instances=1,
                max_queued_inference=0, num_threads=2,
            )
            provider = PooledSherpaProvider(SpeechMetadata(), make_runtime, settings)
            with TestClient(create_app(settings, provider=provider)) as client:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    first = executor.submit(client.post, "/v1/audio/speech", json={
                        "model": "tts-matcha-zh-baker", "input": "你好", "language": "zh",
                    })
                    self.assertTrue(entered.wait(timeout=2))
                    try:
                        rejected = client.post("/v1/audio/speech", json={
                            "model": "tts-matcha-zh-baker", "input": "欢迎", "language": "zh",
                        })
                        self.assertEqual(rejected.status_code, 503)
                        self.assertEqual(rejected.json()["error"]["code"], "inference_overloaded")
                    finally:
                        release.set()
                    self.assertEqual(first.result(timeout=3).status_code, 200)
                snapshot = provider.pool.snapshot()["tts-matcha-zh-baker"]
                self.assertEqual(snapshot["completed"], 1)
                self.assertEqual(snapshot["instances"], 1)
                self.assertEqual(snapshot["active"], 0)
                self.assertEqual(snapshot["waiting"], 0)
            self.assertEqual(len(runtimes), 1)
