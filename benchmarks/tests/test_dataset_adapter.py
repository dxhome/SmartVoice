from __future__ import annotations

import sys
import tempfile
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np

from benchmarks.datasets import load_fleurs_samples


class DatasetAdapterTests(unittest.TestCase):
    def test_fleurs_adapter_pins_source_and_caches_converted_audio_outside_repo(self):
        calls = []
        fake_rows = [
            {
                "id": "shared_transcript_id" if index < 2 else f"en_us_{index}",
                "path": f"en_us/test/recording-{index}.wav",
                "transcription": f"sample {index}",
                "audio": {"array": np.array([0.0, 0.2, -0.2], dtype=np.float32), "sampling_rate": 16000},
            }
            for index in range(5)
        ]

        def load_dataset(builder, **kwargs):
            calls.append((builder, kwargs))
            return fake_rows

        class FakeHfApi:
            def list_repo_files(self, repo, *, repo_type, revision):
                self.request = (repo, repo_type, revision)
                return [
                    "en_us/test-00000-of-00001.parquet",
                    "en_us/train-00000-of-00001.parquet",
                ]

        def hf_hub_download(**kwargs):
            self.assertEqual(kwargs["repo_id"], "google/fleurs")
            self.assertEqual(kwargs["revision"], "pinned-revision")
            return "local-cache.parquet"

        fake_hub = types.SimpleNamespace(HfApi=FakeHfApi, hf_hub_download=hf_hub_download)

        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(sys.modules, {
                "datasets": types.SimpleNamespace(load_dataset=load_dataset),
                "huggingface_hub": fake_hub,
            }):
                samples, metadata = load_fleurs_samples(
                    repo_id="google/fleurs", revision="pinned-revision", split="test",
                    dataset_config="en_us", language="en", cache_dir=Path(temporary), limit=3,
                )
            self.assertEqual(len(samples), 3)
            self.assertEqual([sample.text for sample in samples], ["sample 0", "sample 1", "sample 2"])
            self.assertEqual(len({sample.sample_id for sample in samples}), 3)
            self.assertEqual(len({sample.audio_path for sample in samples}), 3)
            self.assertEqual(metadata["revision"], "pinned-revision")
            self.assertEqual(metadata["sample_count"], 3)
            self.assertTrue(all(sample.audio_path.is_relative_to(Path(temporary)) for sample in samples))
            with wave.open(str(samples[0].audio_path), "rb") as audio:
                self.assertEqual(audio.getframerate(), 16000)
                self.assertEqual(audio.getnframes(), 3)

        self.assertEqual(calls[0][0], "parquet")
        self.assertEqual(calls[0][1]["data_files"], {"test": ["local-cache.parquet"]})
        self.assertEqual(calls[0][1]["split"], "test")


if __name__ == "__main__":
    unittest.main()
