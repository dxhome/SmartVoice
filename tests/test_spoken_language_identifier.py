from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from smartvoice.config.settings import Settings
from smartvoice.services import spoken_language_identifier as language_id


class SpokenLanguageIdentifierAssetsTests(unittest.TestCase):
    def test_first_start_downloads_required_int8_assets_and_reuses_verified_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(data_dir=Path(temp_dir))
            downloads = []

            def fake_download(url, path, _progress):
                downloads.append(url)
                path.write_bytes(path.name.encode())

            def expected_hash(path):
                filename = path.name.removeprefix(f"{language_id.MODEL_ID}-").removesuffix(".part")
                return {
                    "tiny-encoder.int8.onnx": language_id._LFS_SHA256["tiny-encoder.int8.onnx"],
                    "tiny-decoder.int8.onnx": language_id._LFS_SHA256["tiny-decoder.int8.onnx"],
                    "tiny-tokens.txt": language_id._TOKEN_SHA256,
                }[filename]

            with patch.object(language_id, "_download", side_effect=fake_download), patch.object(
                language_id, "_sha256", side_effect=expected_hash
            ):
                installed = language_id.ensure_language_id_model(settings)
                second = language_id.ensure_language_id_model(settings)

            self.assertEqual(installed, second)
            self.assertEqual(len(downloads), 3)
            self.assertTrue((installed / "tiny-encoder.int8.onnx").is_file())
            self.assertTrue((installed / "tiny-decoder.int8.onnx").is_file())
            self.assertTrue((installed / "tiny-tokens.txt").is_file())

    def test_download_fails_closed_if_sha256_does_not_match(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(data_dir=Path(temp_dir))

            def fake_download(_url, path, _progress):
                path.write_bytes(b"invalid model data")

            with patch.object(language_id, "_download", side_effect=fake_download), patch.object(
                language_id, "_sha256", return_value="bad-hash"
            ), self.assertRaisesRegex(ValueError, "SHA-256 verification failed"):
                language_id.ensure_language_id_model(settings)

            self.assertFalse(language_id.language_id_model_dir(settings).exists())


if __name__ == "__main__":
    unittest.main()
