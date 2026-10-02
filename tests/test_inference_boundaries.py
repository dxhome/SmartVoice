from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from smartvoice.adapters.inference.composite_provider import CompositeInferenceProvider
from smartvoice.app import create_app
from smartvoice.config.settings import Settings
from smartvoice.domain.contracts import SynthesizedSpeech
from smartvoice.domain.errors import UnsupportedFeatureError
from smartvoice.__main__ import _models_with_availability


class StubInferenceProvider:
    def __init__(self, backend: str, *, status: str = "available", reason: str | None = None):
        self.backend = backend
        self.status = status
        self.reason = reason
        self.transcription_calls: list[tuple[bytes, str, str | None]] = []
        self.synthesis_calls: list[tuple[str, str, float, str | None, str]] = []

    def installed_models(self):
        return []

    def runtime(self):
        return {
            "backend": self.backend,
            "requested_device": "cpu",
            "actual_device": "cpu" if self.status == "available" else None,
            "provider_status": self.status,
            "reason": self.reason,
            "installed_model_count": 0,
        }

    def capabilities(self):
        return {
            "api_version": "v1",
            "capability_schema_version": "1.0",
            "tasks": [{"task": "transcription", "model": f"{self.backend}-model", "available": self.status == "available"}],
        }

    def transcribe(self, audio: bytes, language: str = "auto", model_id: str | None = None):
        self.transcription_calls.append((audio, language, model_id))
        return {"text": self.backend}

    def synthesize(self, text: str, voice: str = "default", speed: float = 1.0, model_id: str | None = None, language: str = "auto"):
        self.synthesis_calls.append((text, voice, speed, model_id, language))
        return SynthesizedSpeech(audio=self.backend.encode(), sample_rate=24000, duration=0.1)


class InferenceBoundaryTests(unittest.TestCase):
    def make_composite(self, providers: dict[str, object]) -> CompositeInferenceProvider:
        specs = {
            "stt-a": SimpleNamespace(backend="backend-a"),
            "tts-b": SimpleNamespace(backend="backend-b"),
            "missing": SimpleNamespace(backend="backend-missing"),
        }
        repository = SimpleNamespace(get_spec=lambda model_id: specs[model_id])
        return CompositeInferenceProvider(providers, repository)

    def test_composite_dispatches_each_task_by_model_backend(self):
        provider_a = StubInferenceProvider("backend-a")
        provider_b = StubInferenceProvider("backend-b")
        composite = self.make_composite({"backend-a": provider_a, "backend-b": provider_b})

        self.assertEqual(composite.transcribe(b"audio", "zh", "stt-a"), {"text": "backend-a"})
        speech = composite.synthesize("hello", "voice-b", 0.9, "tts-b", "en")

        self.assertEqual(provider_a.transcription_calls, [(b"audio", "zh", "stt-a")])
        self.assertEqual(provider_b.synthesis_calls, [("hello", "voice-b", 0.9, "tts-b", "en")])
        self.assertEqual(speech.audio, b"backend-b")
        with self.assertRaises(UnsupportedFeatureError):
            composite.transcribe(b"audio", model_id="missing")

    def test_composite_without_managed_adapters_retains_global_waiting_capacity(self):
        composite = self.make_composite({"backend-a": StubInferenceProvider("backend-a")})
        self.assertIsNone(composite.request_capacity())
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory))
            app = create_app(settings, provider=composite)
            self.assertEqual(app.state.inference_queue._capacity, 1 + settings.max_queued_inference)

    def test_runtime_and_capabilities_report_unavailable_backend_reason(self):
        unavailable = StubInferenceProvider(
            "backend-a", status="dependency_missing", reason="Sherpa runtime dependency is missing."
        )
        composite = self.make_composite({"backend-a": unavailable})

        runtime = composite.runtime()
        capabilities = composite.capabilities()

        self.assertEqual(runtime["provider_status"], "unavailable")
        self.assertEqual(runtime["backends"]["backend-a"]["reason"], "Sherpa runtime dependency is missing.")
        self.assertIn("Sherpa runtime dependency is missing.", runtime["reason"])
        self.assertEqual(capabilities["tasks"][0]["available"], False)
        self.assertEqual(capabilities["tasks"][0]["backend"], "backend-a")

    def test_cli_model_availability_uses_public_backend_runtime_status(self):
        models = [
            {"id": "m1", "backend": "backend-a", "status": "installed"},
            {"id": "m2", "backend": "backend-b", "status": "installed"},
            {"id": "m3", "backend": "backend-a", "status": "uninstalled"},
        ]

        result = _models_with_availability(models, {
            "backend-a": {"provider_status": "dependency_missing", "reason": "Runtime missing."},
            "backend-b": {"provider_status": "available", "reason": None},
        })

        self.assertEqual([model["status"] for model in result], ["unavailable", "available", "not_installed"])
        self.assertEqual(result[0]["availability_reason"], "Runtime missing.")

    def test_app_shutdown_closes_composite_and_optional_runtime(self):
        closed: list[str] = []

        class ManagedProvider(StubInferenceProvider):
            def close(self):
                closed.append(self.backend)

        managed = ManagedProvider("managed")
        unmanaged = StubInferenceProvider("unmanaged")
        composite = self.make_composite({"managed": managed, "unmanaged": unmanaged})

        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Settings(data_dir=Path(directory)), provider=composite)
            with TestClient(app):
                self.assertEqual(closed, [])

        self.assertEqual(closed, ["managed"])


if __name__ == "__main__":
    unittest.main()
