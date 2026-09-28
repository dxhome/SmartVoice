from __future__ import annotations

import unittest

from benchmarks.metrics import bootstrap_interval, error_rate, normalize_text, numeric_summary, quality_summary


class QualityMetricTests(unittest.TestCase):
    def test_english_wer_uses_fixed_case_and_punctuation_normalization(self):
        self.assertEqual(normalize_text("Hello, WORLD!", "latin_wer"), "hello world")
        self.assertEqual(error_rate(["Hello, world!"], ["hello world"], "latin_wer"), 0.0)
        self.assertEqual(error_rate(["hello world"], ["hello there"], "latin_wer"), 0.5)

    def test_simplified_chinese_cer_ignores_whitespace_and_punctuation(self):
        self.assertEqual(normalize_text("你好， 世界！", "han_cer"), "你好世界")
        self.assertEqual(error_rate(["你好，世界！"], ["你好世界"], "han_cer"), 0.0)
        self.assertEqual(error_rate(["你好世界"], ["你好天气"], "han_cer"), 0.5)

    def test_quality_summary_and_bootstrap_are_reproducible(self):
        records = [
            {"reference": "one two", "hypothesis": "one two"},
            {"reference": "three four", "hypothesis": "three five"},
        ]
        summary = quality_summary(records, "latin_wer", bootstrap_samples=50)
        self.assertEqual(summary["metric"], "wer")
        self.assertEqual(summary["value"], 0.25)
        self.assertEqual(
            bootstrap_interval(["a b"], ["a b"], "latin_wer"),
            {"lower_95": None, "upper_95": None},
        )

    def test_numeric_summary_handles_empty_and_small_lists(self):
        self.assertEqual(numeric_summary([])["median"], None)
        self.assertEqual(numeric_summary([1.0, 2.0])["median"], 1.5)


if __name__ == "__main__":
    unittest.main()
