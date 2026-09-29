"""Resumable, integrity-checked downloads and atomic model installation."""

from __future__ import annotations

import json
import logging
import os
import shutil
import ssl
import tarfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlparse
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable

from smartvoice.config.settings import Settings
from smartvoice.domain.errors import InvalidRequestError
from smartvoice.services.model_catalog_constants import MAX_ARCHIVE_BYTES, MAX_ARCHIVE_MEMBERS, MAX_EXTRACTED_BYTES
from smartvoice.services.model_registry import get_model_spec
from smartvoice.services.file_integrity import sha256 as _sha256


class ModelDownloadCancelled(Exception):
    pass


def _download(
    url: str,
    path: Path,
    progress: Callable[[int, int | None], None] | None,
    cancel_event: threading.Event | None = None,
) -> None:
    """Download with bounded retries for transient TLS/network disconnects."""
    retry_delays = (1, 3, 7)
    for attempt in range(len(retry_delays) + 1):
        try:
            _download_once(url, path, progress, cancel_event)
            return
        except (urllib.error.URLError, TimeoutError, ConnectionError, ssl.SSLError) as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            retryable = isinstance(reason, (TimeoutError, ConnectionError, ssl.SSLEOFError, ssl.SSLZeroReturnError))
            if not retryable or attempt == len(retry_delays):
                if retryable:
                    raise urllib.error.URLError(
                        f"HTTPS download failed after {attempt + 1} attempts; check network access to the model host or retry later ({reason})"
                    ) from exc
                raise
            delay = retry_delays[attempt]
            logging.getLogger(__name__).warning(
                "Transient model download connection error; retrying attempt %d/%d in %d seconds: %s",
                attempt + 2, len(retry_delays) + 1, delay, reason,
            )
            time.sleep(delay)


def _download_once(
    url: str,
    path: Path,
    progress: Callable[[int, int | None], None] | None,
    cancel_event: threading.Event | None = None,
) -> None:
    class SafeHttpsRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            parsed = urlparse(newurl)
            allowed_hosts = {"github.com", "release-assets.githubusercontent.com", "huggingface.co", "cdn-lfs.huggingface.co", "cas-bridge.xethub.hf.co", "us.aws.cdn.hf.co"}
            if parsed.scheme != "https" or parsed.hostname not in allowed_hosts or parsed.username or parsed.password:
                raise ValueError("Model download redirected outside the approved HTTPS hosts")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    offset = path.stat().st_size if path.exists() else 0
    headers = {"User-Agent": "SmartVoice local model manager"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(SafeHttpsRedirect())
    try:
        response = opener.open(request, timeout=60)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and offset:
            exc.close()
            path.unlink(missing_ok=True)
            _download(url, path, progress, cancel_event)
            return
        raise
    with response:
        append = offset > 0 and response.status == 206
        if offset and not append:
            offset = 0
        with path.open("ab" if append else "wb") as output:
            content_length = response.headers.get("Content-Length")
            expected_part = int(content_length) if content_length and content_length.isdigit() else None
            expected = offset + expected_part if expected_part is not None else None
            if expected and expected > MAX_ARCHIVE_BYTES:
                raise ValueError("Model archive exceeds the 2 GiB download limit")
            downloaded = offset
            if progress:
                progress(downloaded, expected)
            while True:
                if cancel_event and cancel_event.is_set():
                    raise ModelDownloadCancelled("Model download was canceled; run install again to resume it.")
                block = response.read(1024 * 1024)
                if not block:
                    break
                downloaded += len(block)
                if downloaded > MAX_ARCHIVE_BYTES:
                    raise ValueError("Model archive exceeds the 2 GiB download limit")
                output.write(block)
                if progress:
                    progress(downloaded, expected)
            if expected is not None and downloaded != expected:
                raise ValueError(f"Incomplete download: expected {expected} bytes, received {downloaded}")


def _safe_extract(archive_path: Path, destination: Path) -> None:
    total_bytes = 0
    members_seen = 0
    with tarfile.open(archive_path, mode="r:bz2") as archive:
        for member in archive:
            members_seen += 1
            if members_seen > MAX_ARCHIVE_MEMBERS:
                raise ValueError("Model archive contains too many entries")
            rel = PurePosixPath(member.name)
            if "\\" in member.name or ":" in member.name or rel.is_absolute() or any(part in {"..", ""} for part in rel.parts):
                raise ValueError("Model archive contains an unsafe path")
            if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                raise ValueError("Model archive contains an unsupported entry type")
            target = destination.joinpath(*rel.parts)
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError("Model archive path escapes installation directory")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            total_bytes += member.size
            if member.size < 0 or total_bytes > MAX_EXTRACTED_BYTES:
                raise ValueError("Model archive exceeds the 3 GiB expanded size limit")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"Cannot read archive entry {member.name}")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)


def install_model(
    settings: Settings,
    model_id: str,
    progress: Callable[[int, int | None], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> Path:
    spec = get_model_spec(model_id)
    settings.models_dir.mkdir(parents=True, exist_ok=True)
    destination = settings.models_dir / spec.id
    if destination.exists():
        raise InvalidRequestError(f"Model directory already exists: {destination}")
    free = shutil.disk_usage(settings.models_dir).free
    minimum_free = max(1024 * 1024 * 1024, spec.minimum_free_bytes or 0)
    if free < minimum_free:
        raise OSError(f"At least {minimum_free / 1024**3:.2f} GiB free disk space is required to install this model")

    download_dir = settings.models_dir / ".downloads"
    download_dir.mkdir(exist_ok=True)
    archive_path = download_dir / f"{spec.id}.part"
    temporary = settings.models_dir / f".{spec.id}-{uuid.uuid4().hex}.tmp"
    temporary.mkdir()
    try:
        extracted = temporary / "extracted"
        extracted.mkdir()
        component_parts: list[Path] = []
        if spec.file_sources:
            # A previous catalog entry used this path for a non-ONNX archive.
            archive_path.unlink(missing_ok=True)
            for filename, url in spec.file_sources.items():
                part_path = download_dir / f"{spec.id}-{filename}.part"
                part_path.parent.mkdir(parents=True, exist_ok=True)
                component_parts.append(part_path)
                if not part_path.is_file() or _sha256(part_path) != spec.file_sha256[filename]:
                    _download(url, part_path, progress, cancel_event)
                if _sha256(part_path) != spec.file_sha256[filename]:
                    part_path.unlink(missing_ok=True)
                    raise ValueError(f"Model file SHA-256 mismatch for {spec.id}: {filename}")
                (extracted / filename).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(part_path, extracted / filename)
            archive_digest = None
        else:
            if not archive_path.is_file() or _sha256(archive_path) != spec.archive_sha256:
                _download(spec.source, archive_path, progress, cancel_event)
            archive_digest = _sha256(archive_path)
            if archive_digest != spec.archive_sha256:
                archive_path.unlink(missing_ok=True)
                raise ValueError(f"Model archive SHA-256 mismatch for {spec.id}")
            _safe_extract(archive_path, extracted)

        for filename, metadata in (spec.extra_files or {}).items():
            part_path = download_dir / f"{spec.id}-{Path(filename).name}.part"
            component_parts.append(part_path)
            if not part_path.is_file() or _sha256(part_path) != metadata["sha256"]:
                _download(metadata["source"], part_path, progress, cancel_event)
            if _sha256(part_path) != metadata["sha256"]:
                part_path.unlink(missing_ok=True)
                raise ValueError(f"Model file SHA-256 mismatch for {spec.id}: {filename}")
            target = extracted / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(part_path, target)

        found: dict[str, Path] = {}
        for required in spec.required_files:
            matches = list(extracted.rglob(required))
            if len(matches) != 1:
                raise ValueError(f"Expected one {required!r} in model archive, found {len(matches)}")
            found[required] = matches[0]
        for required in spec.required_dirs:
            matches = list(extracted.rglob(required))
            if len(matches) != 1 or not matches[0].is_dir():
                raise ValueError(f"Expected one model directory {required!r} in archive, found {len(matches)}")
            found[required] = matches[0]
        file_map: dict[str, str] = {}
        for relative_name, source_path in found.items():
            rel = source_path.relative_to(extracted)
            file_map[relative_name] = rel.as_posix()
            if source_path.is_dir():
                for child in source_path.rglob("*"):
                    if child.is_file() and not child.is_symlink():
                        child_rel = child.relative_to(extracted).as_posix()
                        file_map[child_rel] = child_rel
        manifest = {
            "schema_version": "1.0", "id": spec.id, "task": spec.task,
            "source": spec.source, "archive_sha256": archive_digest,
            "hash_scope": (
                "each required model file verified against its SHA-256 pinned in the SmartVoice source catalog"
                if spec.file_sources else
                "model archive and auxiliary model files verified against separately pinned SHA-256 values"
                if spec.extra_files else
                "verified against the archive SHA-256 pinned in the SmartVoice source catalog"
            ),
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "files": file_map,
            "file_sha256": {
                key: _sha256(extracted / relative)
                for key, relative in file_map.items()
                if (extracted / relative).is_file()
            },
            "license_review_required": True,
        }
        (extracted / "smartvoice-model.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(extracted, destination)
        if spec.file_sources or spec.extra_files:
            for part_path in component_parts:
                part_path.unlink(missing_ok=True)
        else:
            archive_path.unlink(missing_ok=True)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    return destination
