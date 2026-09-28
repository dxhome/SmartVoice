"""Offline text-language detection for TTS routing."""

from __future__ import annotations

from functools import lru_cache

from smartvoice.services.model_router import LanguageDetectionError

TEXT_DETECTION_LANGUAGES = (
    "en", "fr", "de", "es", "ru", "ko", "ja", "zh", "ar", "bg", "cs", "da",
    "el", "et", "fa", "fi", "hr", "hu", "id", "it", "lt", "lv", "mk", "nl", "pl",
    "pt", "ro", "sk", "sl", "sv", "th", "tr", "uk", "vi", "tl",
)


def script_language(text: str) -> str | None:
    if any("\u3040" <= char <= "\u30ff" for char in text):
        return "ja"
    if any("\uac00" <= char <= "\ud7af" for char in text):
        return "ko"
    if any("\u3400" <= char <= "\u9fff" for char in text):
        return "zh"
    return None


def detect_text_language(text: str) -> tuple[str, float]:
    scripted = script_language(text)
    if scripted:
        return scripted, 1.0
    try:
        identifier = _text_identifier()
    except ImportError as exc:
        raise LanguageDetectionError("Offline text language detection is unavailable; install SmartVoice's runtime dependencies.") from exc
    language, confidence = identifier.classify(text)
    return ("fil" if language == "tl" else language), float(confidence)


@lru_cache(maxsize=1)
def _text_identifier():
    from langid.langid import LanguageIdentifier, model

    identifier = LanguageIdentifier.from_modelstring(model, norm_probs=True)
    identifier.set_languages(list(TEXT_DETECTION_LANGUAGES))
    return identifier
