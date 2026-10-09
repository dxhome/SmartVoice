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
        category = item.get("category", {"transcription": "stt", "speech": "tts"}.get(item["task"]))
        if category not in ("stt", "tts", "streaming") or (category != "streaming" and category != {"transcription": "stt", "speech": "tts"}.get(item["task"])):
            raise RuntimeError(f"Invalid model category for {model_id}")
        expected_prefix = {"transcription": "stt", "speech": "tts", "translation": "mt", "punctuation": "punct"}.get(item["task"])
        if item.get("category") == "streaming":
            expected_prefix = "streaming-" + {"transcription": "stt", "speech": "tts", "translation": "mt", "punctuation": "ct"}[item["task"]]
            if not item.get("streaming"):
                raise RuntimeError(f"Streaming model lacks stage capabilities: {model_id}")
        if not MODEL_ID_PATTERN.fullmatch(model_id) or not expected_prefix or not model_id.startswith(f"{expected_prefix}-"):
            raise RuntimeError(f"Invalid canonical model ID {model_id!r}; expected the {expected_prefix or 'stt/tts'}-... format")
        source = item["source"]
        qwen_tts_source_prefix = "https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice/resolve/85e237c12c027371202489a0ec509ded67b5e4b5/"
        allowed_source_prefixes = (
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/",
            "https://huggingface.co/k2-fsa/sherpa-models/resolve/6eed21873e424aa3b01b52c767d9d3bd3cca94d8/",
            "https://huggingface.co/csukuangfj/sherpa-onnx-whisper-base/resolve/bb53ee204431c90d314c1cc08d28d23e5b7927cc/",
            qwen_tts_source_prefix,
            'https://huggingface.co/Helsinki-NLP/opus-mt-zh-en/resolve/cf109095479db38d6df799875e34039d4938aaa6/',
            'https://huggingface.co/csukuangfj/sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12/resolve/432aeba669265e7aeb06b9359753419683b38597/',
            'https://huggingface.co/csukuangfj/sherpa-onnx-streaming-paraformer-bilingual-zh-en/resolve/8e40c43232a1c5c66c82111efc5820d3accca11b/',
            'https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-en-2023-06-26/resolve/672fbf1b30579d6585301139bb363f42a0ad4a24/',
            'https://huggingface.co/facebook/m2m100_418M/resolve/55c2e61bbf05dfb8d7abccdc3fae6fc8512fd636/',

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
        characters = item.get("speech_segment_characters", 0)
        seconds = item.get("transcription_segment_seconds", 0)
        if (type(seconds) is not int or not (seconds == 0 or 2 <= seconds <= 29)
                or (seconds and item["task"] != "transcription")):
            raise RuntimeError(f"Invalid transcription segment policy for {item['id']}")
        if (type(characters) is not int or not (characters == 0 or 40 <= characters <= 400)
                or (characters and item["task"] != "speech")):
            raise RuntimeError(f"Invalid segment policy for {model_id}")
        installation_method = item.get("installation_method", "download")
        if installation_method not in ("download", "import", "convert"):
            raise RuntimeError(f"Invalid installation method for {model_id}")
        capability = item.get("streaming")
        if capability is not None:
            if (not isinstance(capability, dict) or capability.get("stage") not in ("asr", "formatting", "translation", "tts")
                    or not isinstance(capability.get("adapter"), str)
                    or type(capability.get("resident_mib")) is not int or capability["resident_mib"] <= 0):
                raise RuntimeError(f"Invalid streaming capability for {model_id}")
        if capability is not None:
            expected_stage={"transcription":"asr","speech":"tts","translation":"translation","punctuation":"formatting"}[item["task"]]
            if capability['stage']!=expected_stage:
                raise RuntimeError(f"Streaming stage/task mismatch for {model_id}")
            if expected_stage=='asr' and (type(capability.get('native_partial')) is not bool or capability.get('input_policy') not in ('none','gain_v4')):
                raise RuntimeError(f"Invalid ASR input/partial capability for {model_id}")
            if expected_stage=='translation':
                pairs=capability.get('pairs')
                if not isinstance(pairs,list) or not pairs or any(not isinstance(pair,list) or len(pair)!=2 or any(lang not in item['languages'] for lang in pair) for pair in pairs):
                    raise RuntimeError(f"Invalid translation directions for {model_id}")
                policy=capability.get('policy',{})
                if any(type(policy.get(key)) is not int or policy[key]<=0 for key in ('beam_size','max_source_chars','max_source_tokens','max_target_tokens')):
                    raise RuntimeError(f"Invalid bounded translation policy for {model_id}")
        if installation_method in ("import", "convert") and set(file_sha256 or {}) != set(item["required_files"]):
            raise RuntimeError(f"Imported model files must all have pinned hashes: {model_id}")
        if installation_method == "convert":
            preparation = item.get("preparation", {})
            sources = preparation.get("source_files", {})
            if (item["backend"] != "ctranslate2" or preparation.get("quantization") != "int8"
                    or set(preparation.get("versions", {})) != {"ctranslate2", "transformers", "torch", "sentencepiece"}
                    or not sources or any(
                        not re.fullmatch(r"[a-zA-Z0-9_.-]+", name)
                        or metadata.get("url") != source.rsplit('/', 1)[0] + '/' + name
                        or not re.fullmatch(r"[a-f0-9]{64}", metadata.get("sha256", ""))
                        for name, metadata in sources.items())):
                raise RuntimeError(f"Invalid conversion preparation for {model_id}")
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
            speech_segment_characters=item.get("speech_segment_characters", 0),
            transcription_segment_seconds=seconds,
            streaming=item.get("streaming"),
            installation_method=item.get("installation_method", "download"),
            category=item.get("category", {"transcription":"stt", "speech":"tts"}.get(item["task"], "streaming")),
            legacy_ids=tuple(item.get("legacy_ids", ())),
            preparation=item.get("preparation"),
        ))
    identifiers = [spec.id for spec in specs]
    if len(identifiers) != len(set(identifiers)):
        raise RuntimeError("Canonical model IDs must be unique")
    aliases = [alias for spec in specs for alias in spec.legacy_ids]
    if any(not isinstance(alias, str) or not MODEL_ID_PATTERN.fullmatch(alias) for alias in aliases):
        raise RuntimeError("Invalid legacy model ID")
    if len(aliases) != len(set(aliases)) or set(aliases) & set(identifiers):
        raise RuntimeError("Legacy model IDs must be unique and distinct from canonical IDs")
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
        if model_id == spec.id or model_id in spec.legacy_ids:
            return spec
    raise UnsupportedFeatureError(f"Model ID {model_id!r} is not supported by this SmartVoice release.")
