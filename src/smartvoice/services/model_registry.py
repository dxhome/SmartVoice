"""Built-in model metadata and language capability registry."""

from __future__ import annotations

import json
import re

from smartvoice.domain.errors import UnsupportedFeatureError
from smartvoice.domain.models import ModelSpec
from smartvoice.services.builtin_resources import read_builtin_json
from smartvoice.services.model_catalog_constants import MODEL_ID_PATTERN


def load_catalog() -> list[ModelSpec]:
    raw = json.loads(read_builtin_json("models.json"))
    if raw.get("schema_version") != "1.0":
        raise RuntimeError("Unsupported model catalog schema")
    specs = []
    for item in raw["models"]:
        model_id = item["id"]
        expected_prefix = "stt" if item["task"] == "transcription" else "tts" if item["task"] == "speech" else None
        if not MODEL_ID_PATTERN.fullmatch(model_id) or not expected_prefix or not model_id.startswith(f"{expected_prefix}-"):
            raise RuntimeError(f"Invalid canonical model ID {model_id!r}; expected the {expected_prefix or 'stt/tts'}-... format")
        source = item["source"]
        qwen_tts_source_prefix = "https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice/resolve/85e237c12c027371202489a0ec509ded67b5e4b5/"
        allowed_source_prefixes = (
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/",
            "https://huggingface.co/k2-fsa/sherpa-models/resolve/6eed21873e424aa3b01b52c767d9d3bd3cca94d8/",
            "https://huggingface.co/csukuangfj/sherpa-onnx-whisper-base/resolve/bb53ee204431c90d314c1cc08d28d23e5b7927cc/",
            qwen_tts_source_prefix,
        )
        qwen_source_prefix = "https://huggingface.co/csukuangfj2/sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25/resolve/68818b2313fe77bd06f6a7c5068ff3ef59d02b8a/"
        allowed_file_prefixes = (*allowed_source_prefixes, qwen_source_prefix)
        if not source.startswith(allowed_source_prefixes):
            raise RuntimeError(f"Catalog source is not allowlisted for {item['id']}")
        file_sources = item.get("file_sources")
        file_sha256 = item.get("file_sha256")
        if file_sources is not None:
            if set(file_sources) != set(item["required_files"]) or set(file_sha256 or {}) != set(item["required_files"]):
                raise RuntimeError(f"Catalog file sources and hashes must match required files for {item['id']}")
            if any(not isinstance(url, str) or not url.startswith(allowed_file_prefixes[2:]) for url in file_sources.values()):
                raise RuntimeError(f"Catalog file source is not allowlisted for {item['id']}")
        extra_files = item.get("extra_files")
        if extra_files is not None:
            if not isinstance(extra_files, dict) or not set(extra_files).issubset(set(item["required_files"])):
                raise RuntimeError(f"Catalog extra files must be required files for {item['id']}")
            for name, metadata in extra_files.items():
                if (not isinstance(metadata, dict) or set(metadata) != {"source", "sha256"}
                        or not isinstance(metadata["sha256"], str)
                        or not re.fullmatch(r"[0-9a-f]{64}", metadata["sha256"])
                        or not isinstance(metadata["source"], str)
                        or not metadata["source"].startswith(allowed_source_prefixes)):
                    raise RuntimeError(f"Invalid extra file metadata for {item['id']}: {name}")
        specs.append(ModelSpec(
            id=item["id"], task=item["task"], name=item["name"],
            languages=tuple(item["languages"]), backend=item["backend"],
            source=source, archive_name=item["archive_name"], archive_sha256=item["archive_sha256"],
            required_files=tuple(item["required_files"]), model_file=item["model_file"],
            required_dirs=tuple(item.get("required_dirs", ())),
            tokens_file=item["tokens_file"], license_note=item["license_note"],
            lexicon_file=item.get("lexicon_file"),
            model_type=item.get("model_type", "vits"),
            tts_files=tuple(item.get("tts_files", ())),
            data_dir=item.get("data_dir"), decoder_file=item.get("decoder_file"),
            file_sources=file_sources, file_sha256=file_sha256,
            extra_files=extra_files, voices_file=item.get("voices_file"),
            vocoder_file=item.get("vocoder_file"), voice_count=item.get("voice_count"),
            estimated_size_bytes=item.get("estimated_size_bytes"),
            minimum_free_bytes=item.get("minimum_free_bytes"),
            rule_fsts=item.get("rule_fsts"),
        ))
    identifiers = [spec.id for spec in specs]
    if len(identifiers) != len(set(identifiers)):
        raise RuntimeError("Canonical model IDs must be unique")
    return specs


def supported_language_codes(task: str | None = None) -> frozenset[str]:
    """Return language codes advertised by installed catalog model definitions."""
    return frozenset(
        language
        for spec in load_catalog()
        if task is None or spec.task == task
        for language in spec.languages
        if language != "auto"
    )


def get_model_spec(model_id: str) -> ModelSpec:
    for spec in load_catalog():
        if model_id == spec.id:
            return spec
    raise UnsupportedFeatureError(f"Model ID {model_id!r} is not supported by this SmartVoice release.")
