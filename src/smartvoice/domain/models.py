"""Provider-neutral metadata for an installable model."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelSpec:
    id: str
    task: str
    name: str
    languages: tuple[str, ...]
    backend: str
    source: str
    archive_name: str
    archive_sha256: str | None
    required_files: tuple[str, ...]
    required_dirs: tuple[str, ...]
    model_file: str
    tokens_file: str
    license_note: str
    lexicon_file: str | None = None
    model_type: str = "vits"
    tts_files: tuple[str, ...] = ()
    data_dir: str | None = None
    decoder_file: str | None = None
    file_sources: dict[str, str] | None = None
    file_sha256: dict[str, str] | None = None
    extra_files: dict[str, dict[str, str]] | None = None
    voices_file: str | None = None
    vocoder_file: str | None = None
    voice_count: int | None = None
    estimated_size_bytes: int | None = None
    minimum_free_bytes: int | None = None
    rule_fsts: str | None = None
    speech_segment_characters: int = 0
    transcription_segment_seconds: int = 0
