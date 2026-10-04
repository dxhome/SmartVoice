from __future__ import annotations

import io
import hashlib
import json
import os
import shutil
import tarfile
import threading
import unittest
import uuid
import zipfile
from pathlib import Path

from unittest.mock import patch
from dataclasses import replace

from smartvoice.config.settings import Settings
from smartvoice.domain.errors import InvalidRequestError
from smartvoice.services.model_download import ModelDownloadCancelled, _download, _safe_extract, install_model
from smartvoice.services.model_registry import get_model_spec, load_catalog
from smartvoice.services.model_storage import export_model, import_model, installed_models, uninstall_model


class ModelCatalogTests(unittest.TestCase):
    def test_transcription_window_policy_is_explicit_and_optional(self):
        self.assertEqual(get_model_spec('stt-whisper-base-multilingual-int8').transcription_segment_seconds, 25)
        self.assertEqual(get_model_spec('stt-sensevoice-small-int8').transcription_segment_seconds, 0)

    def test_invalid_transcription_window_policy_is_rejected(self):
        raw=json.loads(Path('src/smartvoice/resources/models.json').read_text())
        for seconds in [1, 30, -1, True, 2.5]:
            with self.subTest(seconds=seconds):
                raw['models'][0]['transcription_segment_seconds']=seconds
                with patch('smartvoice.services.model_registry.read_builtin_json',return_value=json.dumps(raw)):
                    with self.assertRaisesRegex(RuntimeError, 'transcription segment policy'):
                        load_catalog()

    def test_model_ids_follow_task_prefix_and_reject_retired_names(self):
        specs = load_catalog()
        self.assertTrue(all(spec.id.startswith(("stt-", "tts-")) for spec in specs))
        for retired_id in (
            "whisper-base-multilingual-local", "sensevoice-small-local", "melo-tts-zh-en-local",
            "tts-melo-zh-en",
            "supertonic-3-multilingual-local", "piper-fr-fr-siwis-medium-local",
            "piper-de-de-thorsten-medium-local", "tts-piper-fr-fr-siwis-medium-int8",
            "tts-piper-de-de-thorsten-medium-int8",
        ):
            with self.assertRaises(InvalidRequestError):
                get_model_spec(retired_id)

    def test_catalog_sources_are_fixed_allowlisted_https_urls(self):
        specs = load_catalog()
        self.assertEqual({spec.task for spec in specs}, {"transcription", "speech"})
        self.assertTrue(all(spec.source.startswith((
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/",
            "https://huggingface.co/k2-fsa/sherpa-models/resolve/",
            "https://huggingface.co/csukuangfj/sherpa-onnx-whisper-base/resolve/",
            "https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice/resolve/",
        )) for spec in specs))
        self.assertIn("ja", get_model_spec("tts-supertonic-v3-multilingual-int8").languages)
        self.assertEqual(get_model_spec("stt-whisper-base-multilingual-int8").model_type, "whisper")
        self.assertEqual(get_model_spec("stt-sensevoice-small-int8").languages[:2], ("zh", "en"))

    def _archive(self, name: str) -> tuple[Path, Path]:
        test_dir = Path.cwd() / ".smartvoice-dev" / f"archive-test-{uuid.uuid4().hex}"
        test_dir.mkdir(parents=True)
        archive_path = test_dir / "unsafe.tar.bz2"
        with tarfile.open(archive_path, "w:bz2") as archive:
            payload = b"escape"
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        return archive_path, test_dir / "target"

    def test_archive_extractor_rejects_posix_path_traversal(self):
        archive_path, destination = self._archive("../outside.txt")
        with self.assertRaisesRegex(ValueError, "unsafe path"):
            _safe_extract(archive_path, destination)

    def test_archive_extractor_rejects_windows_path_traversal(self):
        archive_path, destination = self._archive(r"..\outside.txt")
        with self.assertRaisesRegex(ValueError, "unsafe path"):
            _safe_extract(archive_path, destination)

    def test_offline_import_rejects_unsafe_zip_paths_and_cleans_temporary_files(self):
        test_dir = Path.cwd() / ".smartvoice-dev" / f"unsafe-import-{uuid.uuid4().hex}"
        test_dir.mkdir(parents=True)
        package = test_dir / "unsafe.zip"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("../escape.txt", "must not be extracted")

        settings = Settings(data_dir=test_dir / "data")
        with self.assertRaisesRegex(ValueError, "unsafe path"):
            import_model(settings, package)
        self.assertEqual(list(settings.models_dir.glob(".import-*.tmp")), [])
        self.assertFalse((settings.models_dir.parent / "escape.txt").exists())

    def test_installed_models_reject_tampered_model_manifest(self):
        test_dir = Path.cwd() / ".smartvoice-dev" / f"tampered-model-{uuid.uuid4().hex}"
        model_dir = test_dir / "data" / "models" / "stt-sensevoice-small-int8"
        model_dir.mkdir(parents=True)
        spec = get_model_spec("stt-sensevoice-small-int8")
        files = {}
        hashes = {}
        for name in spec.required_files:
            content = f"fixture:{name}".encode()
            (model_dir / name).write_bytes(content)
            files[name] = name
            hashes[name] = hashlib.sha256(content).hexdigest()
        (model_dir / "smartvoice-model.json").write_text(json.dumps({
            "schema_version": "1.0", "id": spec.id, "task": spec.task,
            "source": spec.source, "archive_sha256": spec.archive_sha256,
            "files": files, "file_sha256": hashes,
        }), encoding="utf-8")
        (model_dir / spec.required_files[0]).write_bytes(b"tampered")

        (model_dir / "smartvoice-model.json").write_text("not json", encoding="utf-8")
        self.assertEqual(installed_models(Settings(data_dir=test_dir / "data")), [])

    def test_installed_model_scan_reuses_persisted_hashes_and_force_refresh_rehashes(self):
        from smartvoice.services import model_storage
        from smartvoice.services.file_integrity import cache_verified_files, load_hash_cache

        test_dir = Path.cwd() / ".smartvoice-dev" / f"integrity-cache-test-{uuid.uuid4().hex}"
        settings = Settings(data_dir=test_dir / "data")
        spec = replace(get_model_spec("stt-sensevoice-small-int8"), file_sha256={})
        model_dir = settings.models_dir / spec.id
        model_dir.mkdir(parents=True)
        files = {}
        hashes = {}
        for name in spec.required_files:
            content = f"fixture:{name}".encode()
            (model_dir / name).write_bytes(content)
            files[name] = name
            hashes[name] = hashlib.sha256(content).hexdigest()
        (model_dir / "smartvoice-model.json").write_text(json.dumps({
            "schema_version": "1.0", "id": spec.id, "task": spec.task,
            "source": spec.source, "archive_sha256": spec.archive_sha256,
            "files": files, "file_sha256": hashes,
        }), encoding="utf-8")
        cache_verified_files(
            settings.models_dir / ".integrity-cache.json",
            {model_dir / name: digest for name, digest in hashes.items()},
        )

        with patch.object(model_storage, "get_model_spec", return_value=spec), patch.object(
            model_storage, "load_catalog", return_value=[spec]
        ), patch.object(model_storage, "_sha256", wraps=model_storage._sha256) as digest:
            self.assertEqual([item["id"] for item in installed_models(settings)], [spec.id])
            digest.assert_not_called()

            changed_name = spec.required_files[0]
            changed_path = model_dir / changed_name
            before = changed_path.stat()
            changed_path.write_bytes(b"x" * before.st_size)
            self.assertEqual(changed_path.stat().st_size, before.st_size)
            os.utime(changed_path, ns=(before.st_atime_ns, before.st_mtime_ns))

            # The default path trusts the persisted signature and remains fast.
            self.assertEqual([item["id"] for item in installed_models(settings)], [spec.id])
            digest.assert_not_called()

            # An explicit refresh ignores the cache, detects the change, and
            # removes the stale persisted digest.
            self.assertEqual(installed_models(settings, force_integrity_check=True), [])
            digest.assert_called()

        cache = load_hash_cache(settings.models_dir / ".integrity-cache.json")
        self.assertNotIn(str(changed_path.resolve()), cache)

    def test_uninstall_refuses_a_model_loaded_by_the_provider(self):
        test_dir = Path.cwd() / ".smartvoice-dev" / f"loaded-uninstall-{uuid.uuid4().hex}"
        model_dir = test_dir / "data" / "models" / "stt-sensevoice-small-int8"
        model_dir.mkdir(parents=True)
        with self.assertRaisesRegex(InvalidRequestError, "loaded by the running service"):
            uninstall_model(Settings(data_dir=test_dir / "data"), "stt-sensevoice-small-int8", loaded=True)
        self.assertTrue(model_dir.is_dir())

    def test_download_resumes_a_partial_archive_with_http_range(self):
        test_dir = Path.cwd() / ".smartvoice-dev" / f"resume-test-{uuid.uuid4().hex}"
        test_dir.mkdir(parents=True)
        partial = test_dir / "model.part"
        partial.write_bytes(b"abc")

        class Response:
            status = 206
            headers = {"Content-Length": "3"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, _size=-1):
                nonlocal payload
                value, payload = payload, b""
                return value

        class Opener:
            def open(self, request, timeout):
                self.request = request
                return Response()

        payload = b"def"
        opener = Opener()
        with patch("smartvoice.services.model_download.urllib.request.build_opener", return_value=opener):
            _download("https://models.example/model", partial, None)
        self.assertEqual(partial.read_bytes(), b"abcdef")
        self.assertEqual(opener.request.headers["Range"], "bytes=3-")

    def test_canceled_download_keeps_partial_data_for_resume(self):
        test_dir = Path.cwd() / ".smartvoice-dev" / f"cancel-test-{uuid.uuid4().hex}"
        test_dir.mkdir(parents=True)
        partial = test_dir / "model.part"
        partial.write_bytes(b"partial")
        canceled = threading.Event()
        canceled.set()

        class Response:
            status = 206
            headers = {"Content-Length": "10"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, _size=-1):
                return b""

        class Opener:
            def open(self, request, timeout):
                return Response()

        with patch("smartvoice.services.model_download.urllib.request.build_opener", return_value=Opener()):
            with self.assertRaisesRegex(ModelDownloadCancelled, "canceled"):
                _download("https://models.example/model", partial, None, canceled)
        self.assertEqual(partial.read_bytes(), b"partial")

    def test_model_install_is_atomic_and_records_file_hashes(self):
        test_dir = Path.cwd() / ".smartvoice-dev" / f"install-test-{uuid.uuid4().hex}"
        archive_path = test_dir / "fixture.tar.bz2"
        archive_root = "sensevoice-fixture"
        test_dir.mkdir(parents=True)
        with tarfile.open(archive_path, "w:bz2") as archive:
            for name, content in (
                ("model.int8.onnx", b"fake model bytes"),
                ("tokens.txt", b"<blank> 0\n"),
                ("LICENSE", b"test license"),
            ):
                payload = tarfile.TarInfo(f"{archive_root}/{name}")
                payload.size = len(content)
                archive.addfile(payload, io.BytesIO(content))

        def fake_download(url, target, progress, cancel_event=None):
            shutil.copyfile(archive_path, target)

        settings = Settings(data_dir=test_dir / "data")
        spec = replace(
            get_model_spec("stt-sensevoice-small-int8"),
            archive_sha256=hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        )
        with patch("smartvoice.services.model_download._download", fake_download), patch(
            "smartvoice.services.model_download.get_model_spec", return_value=spec
        ), patch("smartvoice.services.model_storage.get_model_spec", return_value=spec), patch(
            "smartvoice.services.model_storage.load_catalog", return_value=[spec]
        ), patch("smartvoice.services.model_download.shutil.disk_usage",
                 return_value=type("DiskUsage", (), {"free": 8 * 1024**3})()):
            destination = install_model(settings, "stt-sensevoice-small-int8")
            self.assertTrue((destination / "smartvoice-model.json").is_file())
            self.assertEqual([entry["id"] for entry in installed_models(settings)], ["stt-sensevoice-small-int8"])
            self.assertNotIn("default", installed_models(settings)[0])

            package = test_dir / "offline-model.zip"
            export_model(settings, "stt-sensevoice-small-int8", package)
            released = uninstall_model(settings, "stt-sensevoice-small-int8")
            self.assertGreater(released, 0)
            self.assertEqual(installed_models(settings), [])

            restored = import_model(settings, package)
            self.assertTrue((restored / "smartvoice-model.json").is_file())
            self.assertEqual(uninstall_model(settings, "stt-sensevoice-small-int8"), released)

    def test_model_install_resolves_same_named_files_by_catalog_path(self):
        test_dir = Path.cwd() / ".smartvoice-dev" / f"duplicate-config-test-{uuid.uuid4().hex}"
        settings = Settings(data_dir=test_dir / "data")
        spec = get_model_spec("tts-qwen3-0-6b-customvoice")

        def fixture_content(relative_name: str) -> bytes:
            return f"fixture:{relative_name}".encode()

        fixture_hashes = {
            name: hashlib.sha256(fixture_content(name)).hexdigest()
            for name in spec.required_files
        }
        fixture_spec = replace(spec, file_sha256=fixture_hashes)

        def fake_download(url, target, _progress, _cancel_event=None):
            relative_name = url.split("/resolve/", 1)[1].split("/", 1)[1]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(fixture_content(relative_name))

        test_dir.mkdir(parents=True)
        disk_usage = type("DiskUsage", (), {"free": 8 * 1024**3})()
        with patch("smartvoice.services.model_download._download", fake_download), patch(
            "smartvoice.services.model_download.get_model_spec", return_value=fixture_spec
        ), patch("smartvoice.services.model_download.shutil.disk_usage", return_value=disk_usage), patch(
            "smartvoice.services.model_storage.get_model_spec", return_value=fixture_spec
        ), patch("smartvoice.services.model_storage.load_catalog", return_value=[fixture_spec]):
            destination = install_model(settings, fixture_spec.id)
            self.assertEqual(installed_models(settings)[0]["id"], fixture_spec.id)

        manifest = json.loads((destination / "smartvoice-model.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["files"]["config.json"], "config.json")
        self.assertEqual(manifest["files"]["speech_tokenizer/config.json"], "speech_tokenizer/config.json")
        self.assertEqual((destination / "config.json").read_bytes(), fixture_content("config.json"))
        self.assertEqual(
            (destination / "speech_tokenizer/config.json").read_bytes(),
            fixture_content("speech_tokenizer/config.json"),
        )


if __name__ == "__main__":
    unittest.main()
