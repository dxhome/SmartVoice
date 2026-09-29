"""Offline text-language detection for TTS routing."""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

from smartvoice.services.model_router import LanguageDetectionError

TEXT_DETECTION_LANGUAGES = (
    "en", "fr", "de", "es", "ru", "ko", "ja", "zh", "ar", "bg", "cs", "da", "hi",
    "el", "et", "fi", "hr", "hu", "id", "it", "lt", "lv", "nl", "pl", "pt",
    "ro", "sk", "sl", "sv", "tr", "uk", "vi",
)
MIN_LANGUAGE_DETECTION_CHARACTERS = 10
MAX_SHORT_TEXT_CUE_CHARACTERS = 40

# High precision spelling and common-word cues for short text. Ambiguous
# inputs fall through to langid instead of being forced by a weak cue.
SHORT_TEXT_LANGUAGE_CUES = {
    "bg": ("добро утро", "обичам", "ще"),
    "da": ("godmorgen", "jeg", "morgenmad"),
    "et": ("tere hommikust", "hommikust", "kuidas", "õ"),
    "id": ("selamat", "terima kasih", "saya", "tidak", "kamu"),
    "nl": ("goedemorgen", "alsjeblieft", "dank je", "het"),
    "sv": ("god morgon", "jag", "mår", "och", "inte"),
}


def prepare_text_for_language_detection(text: str) -> str:
    """Pad short input for classification without changing synthesis input."""
    if not text:
        return text
    if len(text) >= MIN_LANGUAGE_DETECTION_CHARACTERS:
        return text
    repetitions = (MIN_LANGUAGE_DETECTION_CHARACTERS + len(text) - 1) // len(text)
    # Keep copies token-separated so the classifier does not treat the joins
    # as invented character sequences (for example, "jejeje" from "je").
    return " ".join([text] * repetitions)


def script_language(text: str) -> str | None:
    if any("\u3040" <= char <= "\u30ff" for char in text):
        return "ja"
    if any("\uac00" <= char <= "\ud7af" for char in text):
        return "ko"
    if any("\u3400" <= char <= "\u9fff" for char in text):
        return "zh"
    if any("\u0370" <= char <= "\u03ff" for char in text):
        return "el"
    if any("\u0900" <= char <= "\u097f" for char in text):
        return "hi"
    if any("\u0600" <= char <= "\u06ff" or "\u0750" <= char <= "\u077f" for char in text):
        return "ar"
    return None


def detect_text_language(text: str) -> tuple[str, float]:
    scripted = script_language(text)
    if scripted:
        return scripted, 1.0
    lexical = _short_text_lexical_language(text)
    if lexical:
        return lexical, 1.0
    try:
        identifier = _text_identifier()
    except ImportError as exc:
        raise LanguageDetectionError("Offline text language detection is unavailable; install SmartVoice's runtime dependencies.") from exc
    language, confidence = identifier.classify(text)
    return ("fil" if language == "tl" else language), float(confidence)


def _short_text_lexical_language(text: str) -> str | None:
    if len(text) > MAX_SHORT_TEXT_CUE_CHARACTERS:
        return None
    normalized = unicodedata.normalize("NFC", text).casefold()
    matches = []
    for language, cues in SHORT_TEXT_LANGUAGE_CUES.items():
        if any(
            cue in normalized if len(cue) == 1 else re.search(
                rf"(?<!\w){re.escape(cue)}(?!\w)", normalized,
            )
            for cue in cues
        ):
            matches.append(language)
    return matches[0] if len(matches) == 1 else None


@lru_cache(maxsize=1)
def _text_identifier():
    from langid.langid import LanguageIdentifier, model

    identifier = LanguageIdentifier.from_modelstring(model, norm_probs=True)
    identifier.set_languages(list(TEXT_DETECTION_LANGUAGES))
    return identifier
