from __future__ import annotations

import unittest
import uuid
import hashlib
import json
import time
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from smartvoice.app import create_app
from smartvoice.config.settings import Settings


class FakeProvider:
    synthesize_call = None
    transcribe_calls = None

    def __init__(self):
        self.transcribe_calls = []

    def installed_models(self):
        return [
            {"id": "stt-sensevoice-small-int8", "task": "transcription"},
            {"id": "tts-melo-zh-en", "task": "speech"},
            {"id": "tts-supertonic-v3-multilingual-int8", "task": "speech"},
            {"id": "stt-whisper-base-multilingual-int8", "task": "transcription"},
        ]

    def runtime(self):
        return {"backend": "test-double", "actual_device": "cpu"}

    def capabilities(self):
        return {"api_version": "v1", "tasks": ["transcription", "speech"]}

    def transcribe(self, audio, language="auto", model_id=None):
        self.transcribe_calls.append((language, model_id))
        assert audio == b"audio fixture"
        return {
            "text": "测试转写",
            "language": "zh",
            "duration": 1.25,
            "model": "stt-sensevoice-small-int8",
            "device": "cpu",
            "segments": [{"text": "测试", "start": 0.1}],
        }

    def synthesize(self, text, voice="default", speed=1.0, model_id=None, language="auto"):
        self.synthesize_call = (text, model_id, language)
        assert text in {"你好", "Hi", "Hello", "Bonjour", "The weather is sunny today and I will take a walk in the park."}
        return b"RIFF-test-wav", 24000, 0.8


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.data_dir = Path.cwd() / ".smartvoice-dev" / f"api-test-{uuid.uuid4().hex}"
        self.data_dir.mkdir(parents=True)

    def make_client(self, max_upload_bytes=100):
        settings = Settings(data_dir=self.data_dir, max_upload_bytes=max_upload_bytes)
        return TestClient(create_app(settings=settings, provider=FakeProvider()))

    def set_english_tts_router(self):
        (self.data_dir / "router.json").write_text(json.dumps({
            "schema_version": "1.0",
            "tasks": {"transcription": {}, "speech": {"en": ["tts-melo-zh-en"]}},
        }), encoding="utf-8")

    def test_health_and_readiness(self):
        with self.make_client() as client:
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.get("/health").json()["status"], "ok")
            self.assertEqual(client.get("/ready").json()["status"], "ready")

    def test_http_debug_logs_request_and_response_and_errors_include_reason(self):
        import logging

        settings = Settings(data_dir=self.data_dir)
        with TestClient(create_app(settings=settings, provider=FakeProvider(), debug_http=True)) as client:
            with self.assertLogs("smartvoice.api", level=logging.DEBUG) as captured:
                response = client.post(
                    "/v1/audio/speech",
                    headers={"X-Debug-Test": "present", "Authorization": "Bearer secret"},
                    json={"model": "tts-smartvoice-auto", "input": "Hello", "language": "en"},
                )
                missing = client.get("/v1/models/not-installed")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(missing.status_code, 404)
        logs = "\n".join(captured.output)
        self.assertIn("http_request", logs)
        self.assertIn('"input":"Hello"', logs)
        self.assertIn("x-debug-test", logs)
        self.assertIn("[REDACTED]", logs)
        self.assertIn("http_response", logs)
        self.assertIn("http_request_error", logs)
        self.assertIn("not installed", logs)

    def test_http_debug_summarizes_body_fields_over_128_bytes(self):
        from smartvoice.app import _format_http_body

        body = json.dumps({
            "small": "ok",
            "large": "x" * 129,
            "nested": {"payload": "é" * 65},
        }).encode("utf-8")
        formatted = _format_http_body(body)

        self.assertIn('"small":"ok"', formatted)
        self.assertIn('"large":"<content omitted: 129 bytes>"', formatted)
        self.assertIn('"payload":"<content omitted: 130 bytes>"', formatted)
        self.assertNotIn("x" * 129, formatted)

    def test_docs_show_project_branding_and_github_link(self):
        with self.make_client() as client:
            docs = client.get("/docs")
            schema = client.get("/openapi.json").json()
            logo = client.get("/assets/smartvoice-logo.png")

        self.assertEqual(docs.status_code, 200)
        self.assertIn('img[src*="smartvoice-logo.png"]', docs.text)
        self.assertIn("width: 200px !important", docs.text)
        self.assertIn("height: 200px !important", docs.text)
        self.assertIn("/openapi.json", docs.text)
        description = schema["info"]["description"]
        self.assertIn("Private, local speech recognition and synthesis", description)
        self.assertIn("https://github.com/dxhome/SmartVoice", description)
        self.assertNotIn("prototype", description.lower())
        self.assertEqual(logo.status_code, 200)
        self.assertEqual(logo.headers["content-type"], "image/png")
        self.assertTrue(logo.content.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_readiness_reports_missing_models(self):
        class EmptyProvider(FakeProvider):
            def installed_models(self):
                return []

        settings = Settings(data_dir=self.data_dir)
        with TestClient(create_app(settings=settings, provider=EmptyProvider())) as client:
            response = client.get("/ready")
            self.assertEqual(response.status_code, 503)
            self.assertIn("model_not_available_for_task:transcription", response.json()["reasons"])
            self.assertIn("model_not_available_for_task:speech", response.json()["reasons"])

    def test_readiness_is_task_based_not_tied_to_model_ids(self):
        class DifferentCatalogProvider(FakeProvider):
            def installed_models(self):
                return [
                    {"id": "another-asr", "task": "transcription"},
                    {"id": "another-tts", "task": "speech"},
                ]

        settings = Settings(data_dir=self.data_dir)
        with TestClient(create_app(settings=settings, provider=DifferentCatalogProvider())) as client:
            response = client.get("/ready")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["available_models"], ["another-asr", "another-tts"])

    def test_catalog_reports_installed_state_and_storage(self):
        with self.make_client() as client:
            response = client.get("/v1/catalog")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["data"])
            self.assertIn("available_disk_bytes", response.json()["storage"])

    def test_openai_model_list_shape_and_task_filters_on_smartvoice_extensions(self):
        with self.make_client() as client:
            installed = client.get("/v1/models")
            one_model = client.get("/v1/models/stt-sensevoice-small-int8")
            catalog = client.get("/v1/catalog", params={"task": "speech"})
            capabilities = client.get("/v1/capabilities", params={"task": "transcription"})
            schema = client.get("/openapi.json").json()

        self.assertEqual(installed.status_code, 200)
        self.assertEqual(installed.json()["object"], "list")
        self.assertTrue(installed.json()["data"])
        self.assertTrue(all(item["object"] == "model" for item in installed.json()["data"]))
        model = installed.json()["data"][0]
        self.assertIn("created", model)
        self.assertIn("owned_by", model)
        self.assertEqual(one_model.status_code, 200)
        self.assertEqual(one_model.json()["id"], "stt-sensevoice-small-int8")
        self.assertEqual(catalog.status_code, 200)
        self.assertTrue(catalog.json()["data"])
        self.assertTrue(all(item["task"] == "speech" for item in catalog.json()["data"]))
        self.assertIn("available_disk_bytes", catalog.json()["storage"])
        self.assertEqual(capabilities.json()["tasks"], ["transcription"])
        self.assertNotIn("parameters", schema["paths"]["/v1/models"]["get"])
        for path in ("/v1/catalog", "/v1/capabilities"):
            parameters = schema["paths"][path]["get"]["parameters"]
            task = next(parameter for parameter in parameters if parameter["name"] == "task")
            enum_schema = next(
                option for option in task["schema"]["anyOf"] if "enum" in option
            )
            self.assertEqual(enum_schema["enum"], ["transcription", "speech"])

    def test_runtime_includes_cpu_host_and_process_resource_metrics(self):
        from smartvoice.adapters.inference.sherpa_onnx.provider import SherpaOnnxProvider

        provider = SherpaOnnxProvider(Settings(data_dir=self.data_dir))
        with TestClient(create_app(settings=provider.settings, provider=provider)) as client:
            response = client.get("/v1/runtime")
        self.assertEqual(response.status_code, 200)
        runtime = response.json()
        self.assertEqual(runtime["actual_device"], "cpu")
        self.assertIn("processor", runtime["host"])
        self.assertGreater(runtime["process"]["pid"], 0)
        self.assertGreaterEqual(runtime["process"]["cpu_time_seconds"], 0)

    def test_uninstall_route_refuses_a_loaded_model(self):
        class LoadedProvider(FakeProvider):
            def is_model_loaded(self, model_id):
                return model_id == "stt-sensevoice-small-int8"

        with TestClient(create_app(
            settings=Settings(data_dir=self.data_dir), provider=LoadedProvider()
        )) as client:
            response = client.delete("/v1/models/stt-sensevoice-small-int8")
        self.assertEqual(response.status_code, 400)
        self.assertIn("loaded by the running service", response.json()["error"]["message"])

    def test_model_download_job_routes_delegate_to_manager(self):
        class Jobs:
            def start_download(self, model_id):
                return {"job_id": "job-123", "model_id": model_id, "status": "running"}

            def get(self, job_id):
                return {"job_id": job_id, "status": "completed"}

            def cancel(self, job_id):
                return {"job_id": job_id, "status": "canceling"}

            def cancel_all(self):
                return None

        with self.make_client() as client:
            client.app.state.model_jobs = Jobs()
            started = client.post("/v1/models/stt-sensevoice-small-int8/download")
            self.assertEqual(started.status_code, 202)
            self.assertEqual(started.json()["job_id"], "job-123")
            self.assertEqual(client.get("/v1/jobs/job-123").json()["status"], "completed")
            self.assertEqual(client.delete("/v1/jobs/job-123").json()["status"], "canceling")

    def test_model_export_uninstall_and_import_routes(self):
        from smartvoice.services.model_catalog import get_model_spec

        with self.make_client() as client:
            settings = client.app.state.settings
            spec = get_model_spec("stt-sensevoice-small-int8")
            model_dir = settings.models_dir / spec.id
            model_dir.mkdir(parents=True)
            files = {}
            file_hashes = {}
            for name in spec.required_files:
                content = f"fixture:{name}".encode()
                (model_dir / name).write_bytes(content)
                files[name] = name
                file_hashes[name] = hashlib.sha256(content).hexdigest()
            (model_dir / "smartvoice-model.json").write_text(json.dumps({
                "schema_version": "1.0",
                "id": spec.id,
                "task": spec.task,
                "source": spec.source,
                "archive_sha256": spec.archive_sha256,
                "files": files,
                "file_sha256": file_hashes,
            }), encoding="utf-8")

            self.assertEqual(client.put(f"/v1/models/{spec.id}/default").status_code, 404)
            self.assertEqual(client.delete(f"/v1/models/{spec.id}/default").status_code, 404)
            exported = client.get(f"/v1/models/{spec.id}/export")
            self.assertEqual(exported.status_code, 200)
            self.assertEqual(exported.headers["content-type"], "application/zip")
            removed = client.delete(f"/v1/models/{spec.id}")
            self.assertEqual(removed.status_code, 200, removed.text)
            imported = client.post(
                "/v1/models/import",
                files={"file": ("offline.zip", exported.content, "application/zip")},
            )
            self.assertEqual(imported.status_code, 201, imported.text)
            self.assertEqual(imported.json()["id"], spec.id)

    def test_transcription_response_and_request_id(self):
        with self.make_client() as client:
            response = client.post(
                "/v1/audio/transcriptions",
                headers={"X-Request-ID": "test-123"},
                files={"file": ("sample.wav", b"audio fixture", "audio/wav")},
                data={"model": "stt-smartvoice-auto", "language": "zh", "timestamps": "true", "response_format": "verbose_json"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["X-Request-ID"], "test-123")
            self.assertEqual(response.json()["text"], "测试转写")
            self.assertTrue(response.json()["segments"])

    def test_transcription_rejects_unknown_fields(self):
        with self.make_client() as client:
            response = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("sample.wav", b"audio fixture", "audio/wav")},
                data={"model": "stt-smartvoice-auto", "unexpected": "value"},
            )
            self.assertEqual(response.status_code, 501)
            self.assertIn("unexpected", response.json()["error"]["message"])

    def test_transcription_can_select_catalog_model(self):
        with self.make_client() as client:
            response = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("sample.wav", b"audio fixture", "audio/wav")},
                data={"model": "stt-sensevoice-small-int8", "language": "en"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["model"], "stt-sensevoice-small-int8")
            self.assertEqual(response.json()["model_mode"], "direct")

    def test_transcription_virtual_model_routes_by_explicit_language(self):
        with self.make_client() as client:
            response = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("sample.wav", b"audio fixture", "audio/wav")},
                data={"model": "stt-smartvoice-auto", "language": "zh"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["requested_model"], "stt-smartvoice-auto")
        self.assertEqual(response.json()["model"], "stt-sensevoice-small-int8")
        self.assertEqual(response.json()["model_mode"], "router")
        self.assertEqual(response.json()["language_source"], "request")
        self.assertEqual(response.json()["route_candidates"][0]["model"], "stt-sensevoice-small-int8")

    def test_transcription_auto_detects_then_routes_to_language_candidate(self):
        provider = FakeProvider()
        with TestClient(create_app(settings=Settings(data_dir=self.data_dir), provider=provider)) as client:
            with self.assertLogs("smartvoice.api", level="DEBUG") as captured:
                response = client.post(
                    "/v1/audio/transcriptions",
                    files={"file": ("sample.wav", b"audio fixture", "audio/wav")},
                    data={"model": "stt-smartvoice-auto", "language": "auto"},
                )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["language"], "zh")
        self.assertEqual(response.json()["language_source"], "model_detection")
        detection_log = next(line for line in captured.output if "language_detection_completed" in line)
        self.assertIn("detector_model=stt-whisper-base-multilingual-int8", detection_log)
        self.assertIn("language=zh", detection_log)
        self.assertNotIn("audio fixture", detection_log)
        self.assertEqual(provider.transcribe_calls, [
            ("auto", "stt-whisper-base-multilingual-int8"),
            ("zh", "stt-sensevoice-small-int8"),
        ])

    def test_transcription_retries_detection_until_language_has_a_route(self):
        class VariableLanguageProvider(FakeProvider):
            def __init__(self):
                super().__init__()
                self.detections = ["ru", "fr"]

            def transcribe(self, audio, language="auto", model_id=None):
                result = super().transcribe(audio, language, model_id)
                if language == "auto":
                    result["language"] = self.detections.pop(0)
                return result

        provider = VariableLanguageProvider()
        with TestClient(create_app(settings=Settings(data_dir=self.data_dir), provider=provider)) as client:
            response = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("sample.wav", b"audio fixture", "audio/wav")},
                data={"model": "stt-smartvoice-auto", "language": "auto"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["language"], "fr")
        self.assertEqual(provider.transcribe_calls, [
            ("auto", "stt-whisper-base-multilingual-int8"),
            ("auto", "stt-whisper-base-multilingual-int8"),
        ])

    def test_transcription_stops_after_three_unsupported_detection_results(self):
        class UnsupportedLanguageProvider(FakeProvider):
            def __init__(self):
                super().__init__()
                self.detections = ["ru", "es", "it", "fr"]

            def transcribe(self, audio, language="auto", model_id=None):
                result = super().transcribe(audio, language, model_id)
                if language == "auto":
                    result["language"] = self.detections.pop(0)
                return result

        provider = UnsupportedLanguageProvider()
        with TestClient(create_app(settings=Settings(data_dir=self.data_dir), provider=provider)) as client:
            response = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("sample.wav", b"audio fixture", "audio/wav")},
                data={"model": "stt-smartvoice-auto", "language": "auto"},
            )
        self.assertEqual(response.status_code, 501)
        self.assertIn("after 3 attempts", response.json()["error"]["message"])
        self.assertEqual(provider.transcribe_calls, [
            ("auto", "stt-whisper-base-multilingual-int8"),
            ("auto", "stt-whisper-base-multilingual-int8"),
            ("auto", "stt-whisper-base-multilingual-int8"),
        ])

    def test_transcription_auto_requires_installed_detection_model(self):
        class NoAutoModelProvider(FakeProvider):
            def installed_models(self):
                return []

        with TestClient(create_app(settings=Settings(data_dir=self.data_dir), provider=NoAutoModelProvider())) as client:
            response = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("sample.wav", b"audio fixture", "audio/wav")},
                data={"model": "stt-smartvoice-auto", "language": "auto"},
            )
        self.assertEqual(response.status_code, 503)
        self.assertIn("auto-detection support", response.json()["error"]["message"])

    def test_router_does_not_retry_another_candidate_after_inference_failure(self):
        from smartvoice.domain.errors import InferenceError

        class FailingProvider(FakeProvider):
            def transcribe(self, audio, language="auto", model_id=None):
                self.transcribe_calls.append((language, model_id))
                raise InferenceError("fixture failure")

        provider = FailingProvider()
        with TestClient(create_app(settings=Settings(data_dir=self.data_dir), provider=provider)) as client:
            response = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("sample.wav", b"audio fixture", "audio/wav")},
                data={"model": "stt-smartvoice-auto", "language": "zh"},
            )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(provider.transcribe_calls, [("zh", "stt-sensevoice-small-int8")])

    def test_router_reports_unavailable_when_no_language_candidate_is_installed(self):
        class EmptyProvider(FakeProvider):
            def installed_models(self):
                return []

        with TestClient(create_app(settings=Settings(data_dir=self.data_dir), provider=EmptyProvider())) as client:
            response = client.post("/v1/audio/speech", json={
                "model": "tts-smartvoice-auto", "input": "你好", "language": "zh",
            })
        self.assertEqual(response.status_code, 503)
        self.assertIn("tts-melo-zh-en", response.json()["error"]["message"])

    def test_transcription_rejects_unsupported_language_code(self):
        with self.make_client() as client:
            response = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("sample.wav", b"audio fixture", "audio/wav")},
                data={"model": "stt-smartvoice-auto", "language": "en-US"},
            )
        self.assertEqual(response.status_code, 501)
        self.assertEqual(response.json()["error"]["code"], "not_implemented")

    def test_router_returns_not_implemented_for_valid_language_without_route(self):
        for task, model_id, payload in (
            ("transcription", "stt-smartvoice-auto", None),
            ("speech", "tts-smartvoice-auto", {"input": "Bonjour tout le monde", "language": "ru"}),
        ):
            with self.subTest(task=task), self.make_client() as client:
                if task == "transcription":
                    response = client.post(
                        "/v1/audio/transcriptions",
                        files={"file": ("sample.wav", b"audio fixture", "audio/wav")},
                        data={"model": model_id, "language": "ru"},
                    )
                else:
                    response = client.post("/v1/audio/speech", json={"model": model_id, **payload})
            self.assertEqual(response.status_code, 501)
            self.assertEqual(response.json()["error"]["code"], "not_implemented")

    def test_unknown_or_wrong_task_model_returns_not_implemented(self):
        for model_id in ("stt-no-such-model", "tts-melo-zh-en"):
            with self.subTest(model_id=model_id), self.make_client() as client:
                response = client.post(
                    "/v1/audio/transcriptions",
                    files={"file": ("sample.wav", b"audio fixture", "audio/wav")},
                    data={"model": model_id, "language": "zh"},
                )
            self.assertEqual(response.status_code, 501)

    def test_unsupported_response_formats_return_not_implemented(self):
        with self.make_client() as client:
            stt = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("sample.wav", b"audio fixture", "audio/wav")},
                data={"model": "stt-smartvoice-auto", "language": "zh", "response_format": "srt"},
            )
            tts = client.post("/v1/audio/speech", json={
                "model": "tts-smartvoice-auto", "input": "Hello", "language": "en", "response_format": "mp3",
            })
        self.assertEqual(stt.status_code, 501)
        self.assertEqual(tts.status_code, 501)

    def test_unsupported_task_filter_and_tts_option_return_not_implemented(self):
        with self.make_client() as client:
            task = client.get("/v1/catalog", params={"task": "streaming"})
            option = client.post("/v1/audio/speech", json={
                "model": "tts-smartvoice-auto", "input": "Hello", "language": "en", "stream": True,
            })
        self.assertEqual(task.status_code, 501)
        self.assertEqual(option.status_code, 501)

    def test_model_listing_exposes_virtual_router_models(self):
        with self.make_client() as client:
            models = client.get("/v1/models").json()["data"]
            virtual = client.get("/v1/models/stt-smartvoice-auto")
        ids = {item["id"] for item in models}
        self.assertTrue({"stt-smartvoice-auto", "tts-smartvoice-auto"}.issubset(ids))
        self.assertTrue(virtual.json()["virtual"])

    def test_router_reload_keeps_previous_config_when_json_invalid(self):
        with self.make_client() as client:
            router = client.app.state.model_router
            original = router.snapshot()
            router.path.write_text("{ invalid", encoding="utf-8")
            response = client.post("/v1/router/reload")
            self.assertEqual(router.snapshot(), original)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "router_config_invalid")

    def test_tts_returns_wav_and_metadata_headers(self):
        with self.make_client() as client:
            response = client.post("/v1/audio/speech", json={"model": "tts-smartvoice-auto", "input": "你好"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "audio/wav")
            self.assertEqual(response.headers["x-audio-sample-rate"], "24000")
            self.assertIn("x-inference-time-seconds", response.headers)
            self.assertEqual(response.headers["x-requested-language"], "auto")
            self.assertEqual(response.headers["x-resolved-language"], "zh")
            self.assertTrue(
                "x-process-working-set-bytes" in response.headers
                or "x-process-peak-working-set-bytes" in response.headers
            )
            self.assertTrue(response.content.startswith(b"RIFF"))

    def test_tts_language_hint_is_returned_in_metadata(self):
        with self.make_client() as client:
            response = client.post("/v1/audio/speech", json={"model": "tts-smartvoice-auto", "input": "Hello", "language": "en"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["x-requested-language"], "en")
            self.assertEqual(response.headers["x-resolved-language"], "en")

    def test_tts_auto_routes_with_offline_text_language_detection(self):
        provider = FakeProvider()
        text = "Bonjour, je voudrais réserver une table pour deux personnes ce soir."
        provider.synthesize = lambda text, voice="default", speed=1.0, model_id=None, language="auto": (
            setattr(provider, "synthesize_call", (text, model_id, language)) or (b"RIFF-test-wav", 24000, 0.8)
        )
        with TestClient(create_app(settings=Settings(data_dir=self.data_dir), provider=provider)) as client:
            with self.assertLogs("smartvoice.api", level="DEBUG") as captured:
                response = client.post("/v1/audio/speech", json={"input": text, "model": "tts-smartvoice-auto"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["x-resolved-language"], "fr")
        self.assertEqual(response.headers["x-model-id"], "tts-supertonic-v3-multilingual-int8")
        self.assertEqual(response.headers["x-model-mode"], "router")
        self.assertEqual(provider.synthesize_call, (text, "tts-supertonic-v3-multilingual-int8", "fr"))
        detection_log = next(line for line in captured.output if "language_detection_completed" in line)
        self.assertIn("detector=langid", detection_log)
        self.assertIn("language=fr", detection_log)
        self.assertIn("confidence=", detection_log)
        self.assertNotIn(text, detection_log)

    def test_tts_short_text_uses_best_low_confidence_language_after_retries(self):
        self.set_english_tts_router()
        with self.make_client() as client:
            response = client.post("/v1/audio/speech", json={
                "input": "Hi", "model": "tts-smartvoice-auto",
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.headers["x-resolved-language"] in {"en", "fr", "de"})
        self.assertLess(float(response.headers["x-language-confidence"]), 0.70)

    def test_tts_retries_until_confidence_reaches_threshold(self):
        self.set_english_tts_router()
        with self.make_client() as client, patch(
            "smartvoice.api.v1.routes.detect_text_language",
            side_effect=[("en", 0.42), ("en", 0.70)],
        ) as detect:
            response = client.post("/v1/audio/speech", json={
                "input": "Hello", "model": "tts-smartvoice-auto",
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["x-resolved-language"], "en")
        self.assertEqual(detect.call_count, 2)

    def test_tts_uses_best_language_after_three_low_confidence_attempts(self):
        self.set_english_tts_router()
        with self.make_client() as client, patch(
            "smartvoice.api.v1.routes.detect_text_language",
            side_effect=[("fr", 0.62), ("en", 0.69), ("de", 0.65)],
        ) as detect:
            response = client.post("/v1/audio/speech", json={
                "input": "Hello", "model": "tts-smartvoice-auto",
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["x-resolved-language"], "en")
        self.assertEqual(response.headers["x-language-confidence"], "0.690")
        self.assertEqual(detect.call_count, 3)

    def test_tts_accepts_explicit_french_with_supertonic(self):
        provider = FakeProvider()
        with TestClient(create_app(settings=Settings(data_dir=self.data_dir), provider=provider)) as client:
            response = client.post("/v1/audio/speech", json={
                "model": "tts-supertonic-v3-multilingual-int8", "input": "Bonjour", "language": "fr",
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(provider.synthesize_call, ("Bonjour", "tts-supertonic-v3-multilingual-int8", "fr"))

    def test_tts_detects_japanese_and_korean_scripts(self):
        from smartvoice.api.v1.routes import _tts_script_language

        self.assertEqual(_tts_script_language("こんにちは"), "ja")
        self.assertEqual(_tts_script_language("안녕하세요"), "ko")

    def test_tts_explicit_language_hint_wins_over_text_script(self):
        with self.make_client() as client:
            response = client.post("/v1/audio/speech", json={"model": "tts-smartvoice-auto", "input": "你好", "language": "en"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["x-resolved-language"], "en")
            self.assertEqual(response.headers["x-model-mode"], "router")

    def test_tts_rejects_whitespace_only_input(self):
        with self.make_client() as client:
            response = client.post("/v1/audio/speech", json={"model": "tts-smartvoice-auto", "input": "   "})
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["error"]["code"], "validation_error")
            self.assertEqual(response.json()["error"]["details"][0]["field"], "body.input")
            self.assertIn("non-whitespace", response.json()["error"]["details"][0]["message"])

    def test_inference_execution_timeout_maps_to_gateway_timeout(self):
        class SlowProvider(FakeProvider):
            def transcribe(self, audio, language="auto", model_id=None):
                time.sleep(0.08)
                return super().transcribe(audio, language, model_id)

        settings = Settings(data_dir=self.data_dir, inference_execution_timeout_seconds=0.01)
        with TestClient(create_app(settings=settings, provider=SlowProvider())) as client:
            response = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("sample.wav", b"audio fixture", "audio/wav")},
                data={"model": "stt-smartvoice-auto"},
            )
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["error"]["code"], "inference_timeout")

    def test_request_logs_include_request_id_status_and_duration(self):
        with self.make_client() as client, self.assertLogs("smartvoice.api", level="INFO") as captured:
            response = client.get("/health", headers={"X-Request-ID": "log-test-123"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(any(
            "request_completed" in line
            and "request_id=log-test-123" in line
            and "status=200" in line
            and "duration_ms=" in line
            for line in captured.output
        ))

    def test_upload_size_is_bounded(self):
        with self.make_client(max_upload_bytes=4) as client:
            response = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("sample.wav", b"too large", "audio/wav")},
                data={"model": "stt-smartvoice-auto"},
            )
            self.assertEqual(response.status_code, 413)
            self.assertEqual(response.json()["error"]["code"], "file_too_large")
            self.assertIn("request_id", response.json()["error"])

    def test_remote_bind_addresses_are_not_allowed(self):
        from smartvoice.__main__ import _is_loopback

        self.assertTrue(_is_loopback("127.0.0.1"))
        self.assertTrue(_is_loopback("::1"))
        self.assertFalse(_is_loopback("0.0.0.0"))


if __name__ == "__main__":
    unittest.main()
