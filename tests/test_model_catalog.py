from __future__ import annotations

import io
import hashlib
import shutil
import tarfile
import unittest
import uuid
from pathlib import Path

from unittest.mock import patch
from dataclasses import replace

from smartvoice.config.settings import Settings
from smartvoice.services.model_catalog import _safe_extract, get_model_spec, install_model, installed_models, load_catalog


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

        def fake_download(url, target, progress):
            shutil.copyfile(archive_path, target)

        settings = Settings(data_dir=test_dir / "data")
        spec = replace(
            get_model_spec("sensevoice-small-local"),
            archive_sha256=hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        )
        with patch("smartvoice.services.model_catalog._download", fake_download), patch(
            "smartvoice.services.model_catalog.get_model_spec", return_value=spec
        ):
            destination = install_model(settings, "sensevoice-small-local")

        self.assertTrue((destination / "smartvoice-model.json").is_file())
        self.assertEqual([entry["id"] for entry in installed_models(settings)], ["sensevoice-small-local"])


if __name__ == "__main__":
    unittest.main()
