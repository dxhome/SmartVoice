from __future__ import annotations

import unittest

from smartvoice.services.language_detection import detect_text_language, prepare_text_for_language_detection
from smartvoice.services.model_registry import load_catalog, supported_language_codes


SHORT_TEXT_SAMPLES = {
    "ar": "لا", "bg": "не", "cs": "že", "da": "jo", "de": "du", "el": "να",
    "en": "I", "es": "no", "et": "on", "fi": "ei", "fr": "je", "hi": "है",
    "hr": "da", "hu": "én", "id": "ya", "it": "io", "ja": "あ", "ko": "나",
    "lt": "ta", "lv": "jā", "nl": "ik", "pl": "ja", "pt": "eu", "ro": "nu",
    "ru": "да", "sk": "ja", "sl": "da", "sv": "ja", "tr": "ev",
    "uk": "да", "vi": "ở", "zh": "好",
}

SENTENCE_SAMPLES = {
    "ar": "صباح الخير!", "bg": "Добро утро!", "cs": "Dobré ráno!", "da": "Godmorgen!",
    "de": "Guten Morgen!", "el": "Καλημέρα σας!", "en": "Good morning!", "es": "¡Buenos días!",
    "et": "Tere hommikust!", "fi": "Hyvää huomenta!", "fr": "Bonjour à tous!", "hi": "सुप्रभात मित्र!",
    "hr": "Dobro jutro!", "hu": "Jó reggelt!", "id": "Selamat pagi!", "it": "Buongiorno a te!",
    "ja": "おはようございます！", "ko": "오늘 아침 참 좋아요!", "lt": "Labas rytas!", "lv": "Labrīt visiem!",
    "nl": "Goedemorgen!", "pl": "Dzień dobry!", "pt": "Bom dia a todos!", "ro": "Bună dimineața!",
    "ru": "Доброе утро!", "sk": "Dobré ráno!", "sl": "Dobro jutro!", "sv": "God morgon!",
    "tr": "Günaydın dostum!", "uk": "Доброго ранку!", "vi": "Chào buổi sáng!", "zh": "早上好，今天天气不错。",
}

# Golden outputs from the current detector, using each short sample after the
# production padding step. Changes require reviewing this language/confidence
# baseline rather than silently replacing it.
SHORT_TEXT_BASELINE = {
    "ar": ("ar", 1.000), "bg": ("bg", 1.000), "cs": ("hr", 0.993), "da": ("sl", 0.696),
    "de": ("fr", 1.000), "el": ("el", 1.000), "en": ("en", 0.187), "es": ("pt", 0.981),
    "et": ("en", 0.998), "fi": ("de", 0.992), "fr": ("hr", 0.726), "hi": ("hi", 1.000),
    "hr": ("pt", 0.794), "hu": ("hu", 1.000), "id": ("id", 0.996), "it": ("it", 0.917),
    "ja": ("ja", 1.000), "ko": ("ko", 1.000), "lt": ("en", 0.187), "lv": ("lv", 1.000),
    "nl": ("da", 1.000), "pl": ("et", 0.785), "pt": ("pt", 0.894), "ro": ("en", 0.187),
    "ru": ("bg", 1.000), "sk": ("et", 0.785), "sl": ("pt", 0.794), "sv": ("et", 0.785),
    "tr": ("sl", 0.999), "uk": ("bg", 1.000), "vi": ("vi", 1.000), "zh": ("zh", 1.000),
}

SENTENCE_BASELINE = {
    "ar": ("ar", 1.000), "bg": ("bg", 1.000), "cs": ("sk", 0.678), "da": ("da", 1.000),
    "de": ("de", 0.579), "el": ("el", 1.000), "en": ("en", 0.187), "es": ("es", 1.000),
    "et": ("et", 1.000), "fi": ("fi", 1.000), "fr": ("fr", 1.000), "hi": ("hi", 1.000),
    "hr": ("pt", 0.822), "hu": ("hu", 1.000), "id": ("id", 1.000), "it": ("it", 0.982),
    "ja": ("ja", 1.000), "ko": ("ko", 1.000), "lt": ("lt", 0.708), "lv": ("lv", 1.000),
    "nl": ("nl", 1.000), "pl": ("pl", 1.000), "pt": ("pt", 0.731), "ro": ("ro", 1.000),
    "ru": ("ru", 1.000), "sk": ("sk", 0.678), "sl": ("pt", 0.822), "sv": ("sv", 1.000),
    "tr": ("tr", 1.000), "uk": ("uk", 0.744), "vi": ("vi", 1.000), "zh": ("zh", 1.000),
}

LEXICAL_CUE_HOLDOUT = {
    "bg": "Обичам те!",
    "da": "Jeg er her.",
    "et": "Kuidas sul läheb?",
    "id": "Saya suka ini.",
    "nl": "Het is mooi.",
    "sv": "Jag mår bra.",
}


class LanguageDetectionTests(unittest.TestCase):
    def test_short_input_is_repeated_until_it_has_ten_characters(self):
        self.assertEqual(prepare_text_for_language_detection("I"), " ".join(["I"] * 10))
        self.assertEqual(prepare_text_for_language_detection("你好"), " ".join(["你好"] * 5))

    def test_long_input_is_not_changed(self):
        value = "0123456789abc"
        self.assertEqual(prepare_text_for_language_detection(value), value)

    def test_empty_input_is_safe(self):
        self.assertEqual(prepare_text_for_language_detection(""), "")

    def test_short_samples_match_frozen_results_for_all_current_tts_languages(self):
        languages = {
            language
            for model in load_catalog() if model.task == "speech"
            for language in model.languages if language != "auto"
        }
        self.assertEqual(languages, set(SHORT_TEXT_SAMPLES))
        self.assertEqual(languages, set(SHORT_TEXT_BASELINE))
        for expected_language, sample in SHORT_TEXT_SAMPLES.items():
            with self.subTest(language=expected_language):
                self.assertIn(len(sample), {1, 2})
                padded = prepare_text_for_language_detection(sample)
                detected, confidence = detect_text_language(padded)
                self.assertGreaterEqual(len(padded), 10)
                self.assertIn(detected, supported_language_codes())
                expected_detected, expected_confidence = SHORT_TEXT_BASELINE[expected_language]
                self.assertEqual(detected, expected_detected)
                self.assertAlmostEqual(confidence, expected_confidence, places=3)

    def test_sentence_samples_match_frozen_results_for_all_current_tts_languages(self):
        languages = {
            language
            for model in load_catalog() if model.task == "speech"
            for language in model.languages if language != "auto"
        }
        self.assertEqual(languages, set(SENTENCE_SAMPLES))
        self.assertEqual(languages, set(SENTENCE_BASELINE))
        for expected_language, sentence in SENTENCE_SAMPLES.items():
            with self.subTest(language=expected_language):
                self.assertGreaterEqual(len(sentence), 10)
                detection_text = prepare_text_for_language_detection(sentence)
                self.assertEqual(detection_text, sentence)
                detected, confidence = detect_text_language(detection_text)
                expected_detected, expected_confidence = SENTENCE_BASELINE[expected_language]
                self.assertEqual(detected, expected_detected)
                self.assertAlmostEqual(confidence, expected_confidence, places=3)

    def test_lightweight_lexical_cues_work_on_alternate_sentences(self):
        for expected_language, sentence in LEXICAL_CUE_HOLDOUT.items():
            with self.subTest(language=expected_language):
                detected, confidence = detect_text_language(sentence)
                self.assertEqual(detected, expected_language)
                self.assertEqual(confidence, 1.0)


if __name__ == "__main__":
    unittest.main()
