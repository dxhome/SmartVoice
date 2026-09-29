"""Managed sherpa-onnx Whisper Tiny assets for automatic STT language ID."""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from functools import lru_cache
from pathlib import Path

from smartvoice.config.settings import Settings
from smartvoice.services.file_integrity import sha256 as _sha256
from smartvoice.services.model_download import _download

logger = logging.getLogger("smartvoice.language_id")

MODEL_REVISION = "65176e2deb88badc814a94058666cadccc29b61c"
MODEL_ID = "whisper-tiny-int8"
_HF_RESOLVE = f"https://huggingface.co/csukuangfj/sherpa-onnx-whisper-tiny/resolve/{MODEL_REVISION}/"
_LFS_SHA256 = {
    "tiny-encoder.int8.onnx": "d24fb083ae3b1041fc24e97971d60e280c9342201fbb67b0ab428a8b4a51a434",
    "tiny-decoder.int8.onnx": "d2fece8dd42771f1df975c6c0445770d0c292bf7547c2cae04a6c0cc57540925",
}
_TOKEN_SHA256 = "b34b360dbb493e781e479794586d661700670d65564001f23024971d1f2fa126"


def language_id_model_dir(settings: Settings) -> Path:
    return settings.models_dir / ".smartvoice" / MODEL_ID


def installed_language_id_model_dir(settings: Settings) -> Path | None:
    """Return the bundled detector assets only when all pinned files are present."""
    destination = language_id_model_dir(settings)
    expected = {**_LFS_SHA256, "tiny-tokens.txt": _TOKEN_SHA256}
    if not all((destination / name).is_file() for name in expected):
        return None
    signature = tuple((destination / name).stat().st_size for name in expected) + tuple(
        (destination / name).stat().st_mtime_ns for name in expected
    )
    if not _verified_language_assets(destination, signature):
        return None
    return destination


@lru_cache(maxsize=8)
def _verified_language_assets(destination: Path, _signature: tuple[int, ...]) -> bool:
    expected = {**_LFS_SHA256, "tiny-tokens.txt": _TOKEN_SHA256}
    return all(_sha256(destination / name) == digest for name, digest in expected.items())


def ensure_language_id_model(settings: Settings) -> Path:
    """Install the pinned, int8-only LID assets atomically on first startup."""
    destination = language_id_model_dir(settings)
    required = ("tiny-encoder.int8.onnx", "tiny-decoder.int8.onnx", "tiny-tokens.txt")
    if all((destination / name).is_file() for name in required):
        expected = {**_LFS_SHA256, "tiny-tokens.txt": _TOKEN_SHA256}
        if all(expected[name] and _sha256(destination / name) == expected[name] for name in required):
            return destination
        shutil.rmtree(destination, ignore_errors=True)

    settings.models_dir.mkdir(parents=True, exist_ok=True)
    staging = settings.models_dir / f".language-id-{uuid.uuid4().hex}.tmp"
    staging.mkdir()
    partials = settings.models_dir / ".downloads"
    partials.mkdir(exist_ok=True)
    try:
        expected = {**_LFS_SHA256, "tiny-tokens.txt": _TOKEN_SHA256}
        for filename in required:
            part = partials / f"{MODEL_ID}-{filename}.part"
            digest = expected[filename]
            if not part.is_file() or not digest or _sha256(part) != digest:
                part.unlink(missing_ok=True)
                _download(_HF_RESOLVE + filename, part, None)
            if not digest or _sha256(part) != digest:
                part.unlink(missing_ok=True)
                raise ValueError(f"SHA-256 verification failed for built-in language model file {filename}")
            shutil.copyfile(part, staging / filename)
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, destination)
        for filename in required:
            (partials / f"{MODEL_ID}-{filename}.part").unlink(missing_ok=True)
        logger.info("language_id_model_ready model=%s path=%s", MODEL_ID, destination)
        return destination
    except Exception:
        logger.exception("language_id_model_initialization_failed model=%s", MODEL_ID)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
