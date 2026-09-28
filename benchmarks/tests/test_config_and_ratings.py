from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.aggregate_tts_ratings import aggregate
from benchmarks.runner import _read_config


class BenchmarkConfigTests(unittest.TestCase):
    def test_tracked_comparison_config_defines_language_and_category_profiles(self):
        path = Path(__file__).parents[1] / "config" / "model-comparison.json"
        config = _read_config(path)
        self.assertEqual(set(config["languages"]), {"en", "zh"})
        self.assertEqual(set(config["profiles"]), {"smoke", "standard", "full"})
        self.assertTrue(all("normalization" in language for language in config["languages"].values()))
        self.assertTrue(config["tts_quality_judge_model_id"])

    def test_tts_ratings_are_aggregated_by_model_and_language_without_raw_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ratings_path = root / "ratings.csv"
            mapping_path = root / "mapping.json"
            with ratings_path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=[
                    "sample_key", "naturalness_1_to_5", "intelligibility_1_to_5", "pronunciation_prosody_1_to_5",
                ])
                writer.writeheader()
                writer.writerow({"sample_key": "opaque-a", "naturalness_1_to_5": "4", "intelligibility_1_to_5": "5", "pronunciation_prosody_1_to_5": "3"})
                writer.writerow({"sample_key": "opaque-a", "naturalness_1_to_5": "5", "intelligibility_1_to_5": "5", "pronunciation_prosody_1_to_5": "4"})
            mapping_path.write_text(json.dumps({
                "opaque-a": {"model_id": "tts-example", "language": "en"},
            }), encoding="utf-8")

            result = aggregate(ratings_path, mapping_path)

        item = result["results"][0]
        self.assertEqual((item["model_id"], item["language"]), ("tts-example", "en"))
        self.assertEqual(item["rating_count"], 2)
        self.assertEqual(item["metrics"]["naturalness_mos"]["mean"], 4.5)
        self.assertNotIn("opaque-a", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
