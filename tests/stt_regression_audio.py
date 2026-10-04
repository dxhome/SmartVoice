"""Deterministic bilingual speech fixtures for real STT regression tests."""
import io
import json
from pathlib import Path
import wave

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "stt"
RATE = 16000
SILENCE_SECONDS = 0.24
TRANSCRIPTS = json.loads((FIXTURE_DIR / "transcripts.json").read_text(encoding="utf-8"))

def _load_pcm(language: str):
    clips = []
    for item in TRANSCRIPTS[language]:
        with wave.open(str(FIXTURE_DIR / item["file"]), "rb") as source:
            if source.getnchannels() != 1 or source.getsampwidth() != 2 or source.getframerate() != RATE:
                raise ValueError(f"Invalid STT regression fixture: {item['file']}")
            clips.append(source.readframes(source.getnframes()))
    return clips


def build_audio(duration_seconds: float, language: str) -> bytes:
    """Cycle the fixed utterances and pad with digital silence to exact duration."""
    target = round(duration_seconds * RATE)
    silence = b"\0\0" * round(SILENCE_SECONDS * RATE)
    clips = _load_pcm(language)
    out = io.BytesIO()
    frames = 0
    index = 0
    with wave.open(out, "wb") as destination:
        destination.setnchannels(1)
        destination.setsampwidth(2)
        destination.setframerate(RATE)
        while True:
            pcm = clips[index % len(clips)]
            count = len(pcm)//2
            if frames + count > target:
                break
            destination.writeframesraw(pcm)
            frames += count
            index += 1
            if frames + len(silence)//2 <= target:
                destination.writeframesraw(silence)
                frames += len(silence)//2
        if frames == 0:
            raise ValueError("Duration is too short to fit one complete speech fixture")
        remaining = target - frames
        destination.writeframesraw(b"\0\0" * remaining)
    return out.getvalue()


def short_samples(language: str):
    return [(item["file"], (FIXTURE_DIR / item["file"]).read_bytes())
            for item in TRANSCRIPTS[language]]
