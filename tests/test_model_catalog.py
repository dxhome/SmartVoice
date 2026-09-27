from __future__ import annotations

import io
import hashlib
import json
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
from smartvoice.services.model_catalog import (
    ModelDownloadCancelled, _download, _safe_extract, activate_model, export_model, get_model_spec, import_model,
    install_model, installed_models, load_catalog, uninstall_model,
)


class ModelCatalogTests(unittest.TestCase):
    def test_catalog_sources_are_fixed_allowlisted_https_urls(self):
        specs = load_catalog()
        self.assertEqual({spec.task for spec in specs}, {"transcription", "speech"})
        self.assertTrue(all(spec.source.startswith("https://github.com/k2-fsa/sherpa-onnx/releases/download/") for spec in specs))
        self.assertEqual(get_model_spec("sensevoice-small-local").languages[:2], ("zh", "en"))

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

    def test_activation_rejects_tampered_model_file(self):
        test_dir = Path.cwd() / ".smartvoice-dev" / f"tampered-model-{uuid.uuid4().hex}"
        model_dir = test_dir / "data" / "models" / "sensevoice-small-local"
        model_dir.mkdir(parents=True)
        spec = get_model_spec("sensevoice-small-local")
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

        with self.assertRaisesRegex(InvalidRequestError, "failed verification"):
            activate_model(Settings(data_dir=test_dir / "data"), spec.id)

    def test_uninstall_refuses_a_model_loaded_by_the_provider(self):
        test_dir = Path.cwd() / ".smartvoice-dev" / f"loaded-uninstall-{uuid.uuid4().hex}"
        model_dir = test_dir / "data" / "models" / "sensevoice-small-local"
        model_dir.mkdir(parents=True)
        with self.assertRaisesRegex(InvalidRequestError, "loaded by the running service"):
            uninstall_model(Settings(data_dir=test_dir / "data"), "sensevoice-small-local", loaded=True)
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
        with patch("smartvoice.services.model_catalog.urllib.request.build_opener", return_value=opener):
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

        with patch("smartvoice.services.model_catalog.urllib.request.build_opener", return_value=Opener()):
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
            get_model_spec("sensevoice-small-local"),
            archive_sha256=hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        )
        with patch("smartvoice.services.model_catalog._download", fake_download), patch(
            "smartvoice.services.model_catalog.get_model_spec", return_value=spec
        ), patch("smartvoice.services.model_catalog.load_catalog", return_value=[spec]):
            destination = install_model(settings, "sensevoice-small-local")
            self.assertTrue((destination / "smartvoice-model.json").is_file())
            self.assertEqual([entry["id"] for entry in installed_models(settings)], ["sensevoice-small-local"])
            self.assertTrue(installed_models(settings)[0]["active"])

            package = test_dir / "offline-model.zip"
            export_model(settings, "sensevoice-small-local", package)
            released = uninstall_model(settings, "sensevoice-small-local")
            self.assertGreater(released, 0)
            self.assertEqual(installed_models(settings), [])

            restored = import_model(settings, package)
            self.assertTrue((restored / "smartvoice-model.json").is_file())
            self.assertEqual(activate_model(settings, "sensevoice-small-local")["transcription"], "sensevoice-small-local")
            self.assertEqual(uninstall_model(settings, "sensevoice-small-local"), released)


if __name__ == "__main__":
    unittest.main()
