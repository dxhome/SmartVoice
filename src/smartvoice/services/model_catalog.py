"""Pinned model catalog and safe local installation."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import ssl
import threading
import tarfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from urllib.parse import urlparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable

from smartvoice.config.settings import Settings
from smartvoice.domain.errors import InvalidRequestError, UnsupportedFeatureError

MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_EXTRACTED_BYTES = 3 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 50_000
MODEL_ID_PATTERN = re.compile(r"^(stt|tts)-[a-z0-9]+(?:-[a-z0-9]+)*$")


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
    minimum_free_bytes: int | None = None
    rule_fsts: str | None = None


def load_catalog() -> list[ModelSpec]:
    catalog_path = Path(__file__).resolve().parents[3] / "catalog" / "models.json"
    raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    if raw.get("schema_version") != "1.0":
        raise RuntimeError("Unsupported model catalog schema")
    specs = []
    for item in raw["models"]:
        model_id = item["id"]
        expected_prefix = "stt" if item["task"] == "transcription" else "tts" if item["task"] == "speech" else None
        if not MODEL_ID_PATTERN.fullmatch(model_id) or not expected_prefix or not model_id.startswith(f"{expected_prefix}-"):
            raise RuntimeError(f"Invalid canonical model ID {model_id!r}; expected the {expected_prefix or 'stt/tts'}-... format")
        source = item["source"]
        allowed_source_prefixes = (
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/",
            "https://huggingface.co/k2-fsa/sherpa-models/resolve/6eed21873e424aa3b01b52c767d9d3bd3cca94d8/",
            "https://huggingface.co/csukuangfj/sherpa-onnx-whisper-base/resolve/bb53ee204431c90d314c1cc08d28d23e5b7927cc/",
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
            minimum_free_bytes=item.get("minimum_free_bytes"),
            rule_fsts=item.get("rule_fsts"),
        ))
    identifiers = [spec.id for spec in specs]
    if len(identifiers) != len(set(identifiers)):
        raise RuntimeError("Canonical model IDs must be unique")
    return specs


def get_model_spec(model_id: str) -> ModelSpec:
    for spec in load_catalog():
        if model_id == spec.id:
            return spec
    raise UnsupportedFeatureError(f"Model ID {model_id!r} is not supported by this SmartVoice release.")


def model_directory(settings: Settings, model_id: str) -> Path:
    """Return the install directory for a canonical catalog model ID."""
    spec = get_model_spec(model_id)
    return settings.models_dir / spec.id


def installed_models(settings: Settings) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for spec in load_catalog():
        root = model_directory(settings, spec.id)
        manifest_path = root / "smartvoice-model.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            valid = (
                manifest.get("id") == spec.id
                and manifest.get("task") == spec.task
                and manifest.get("archive_sha256") == spec.archive_sha256
                and root.resolve().is_relative_to(settings.models_dir.resolve())
                and not root.is_symlink()
            )
            file_map = manifest.get("files", {})
            for required in spec.required_files:
                relative = file_map.get(required)
                if not isinstance(relative, str):
                    valid = False
                    break
                resolved = (root / relative).resolve()
                if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
                    valid = False
                    break
            for required in spec.required_dirs:
                relative = file_map.get(required)
                resolved = (root / relative).resolve() if isinstance(relative, str) else root
                if not resolved.is_relative_to(root.resolve()) or not resolved.is_dir():
                    valid = False
                    break
            if valid and spec.file_sha256:
                for required, expected in spec.file_sha256.items():
                    relative = file_map.get(required)
                    resolved = (root / relative).resolve() if isinstance(relative, str) else root
                    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file() or _sha256(resolved) != expected:
                        valid = False
                        break
        except (OSError, KeyError, AttributeError, TypeError, json.JSONDecodeError):
            valid = False
        if valid:
            results.append({
                "id": spec.id,
                "name": spec.name,
                "task": spec.task,
                "languages": list(spec.languages),
                "backend": spec.backend,
                "archive_sha256": manifest.get("archive_sha256"),
                "installed": True,
                "installed_size_bytes": sum(path.stat().st_size for path in root.rglob("*") if path.is_file()),
                "license_note": spec.license_note,
            })
    return results


def catalog_models(settings: Settings) -> list[dict[str, object]]:
    installed_by_id = {str(model["id"]): model for model in installed_models(settings)}
    output = [{
        "id": spec.id, "name": spec.name, "task": spec.task,
        "languages": list(spec.languages), "backend": spec.backend,
        "source": spec.source, "installed": spec.id in installed_by_id,
        "status": "installed" if spec.id in installed_by_id else (
            "invalid" if model_directory(settings, spec.id).exists() else "uninstalled"
        ),
        "archive_name": spec.archive_name,
        "archive_sha256": spec.archive_sha256,
        "installed_size_bytes": installed_by_id.get(spec.id, {}).get("installed_size_bytes"),
        "required_files": list(spec.required_files),
        "license_note": spec.license_note,
        "extra_files": spec.extra_files,
        "voices_file": spec.voices_file,
        "vocoder_file": spec.vocoder_file,
        "voice_count": spec.voice_count,
        "minimum_free_bytes": spec.minimum_free_bytes,
        "rule_fsts": spec.rule_fsts,
    } for spec in load_catalog()]
    return output


def model_storage(settings: Settings) -> dict[str, int]:
    probe = settings.models_dir
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    disk = shutil.disk_usage(probe)
    used = sum(int(model.get("installed_size_bytes", 0)) for model in installed_models(settings))
    return {
        "installed_model_bytes": used,
        "available_disk_bytes": disk.free,
        "total_disk_bytes": disk.total,
    }


def uninstall_model(settings: Settings, model_id: str, *, loaded: bool = False) -> int:
    spec = get_model_spec(model_id)
    if loaded:
        raise InvalidRequestError(f"Model {model_id!r} is loaded by the running service. Restart the service before uninstalling it.")
    candidate = model_directory(settings, spec.id)
    if candidate.is_symlink():
        raise InvalidRequestError("Refusing to uninstall a model directory that is a symbolic link.")
    directory = candidate.resolve()
    if not directory.is_relative_to(settings.models_dir.resolve()) or directory == settings.models_dir.resolve():
        raise InvalidRequestError("The model path is outside the configured model directory.")
    if not directory.exists():
        raise InvalidRequestError(f"Model {model_id!r} is not installed.")
    size = sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())
    shutil.rmtree(directory)
    return size


def export_model(settings: Settings, model_id: str, destination: Path) -> Path:
    spec = get_model_spec(model_id)
    source = settings.models_dir / spec.id
    if source.is_symlink() or not source.resolve().is_relative_to(settings.models_dir.resolve()):
        raise InvalidRequestError("The installed model path is not a regular directory inside the model directory.")
    if spec.id not in {str(item["id"]) for item in installed_models(settings)}:
        raise InvalidRequestError(f"Model {model_id!r} is not installed and verified.")
    if destination.resolve().is_relative_to(source.resolve()):
        raise InvalidRequestError("Choose an export destination outside the installed model directory.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for path in source.rglob("*"):
            if path.is_file() and not path.is_symlink():
                package_path = Path("model") / path.relative_to(source)
                archive.write(path, package_path)
    return destination


def import_model(settings: Settings, archive_path: Path) -> Path:
    temporary_root = settings.models_dir / f".import-{uuid.uuid4().hex}.tmp"
    temporary_root.mkdir(parents=True, exist_ok=True)
    try:
        model_root = temporary_root / "model"
        model_root.mkdir()
        total_bytes = 0
        seen_names: set[str] = set()
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                relative = PurePosixPath(member.filename)
                unix_mode = member.external_attr >> 16
                if (
                    member.filename.casefold() in seen_names
                    or relative.is_absolute()
                    or any(part in {"..", ""} for part in relative.parts)
                    or "\\" in member.filename
                ):
                    raise ValueError("Model package contains an unsafe path")
                seen_names.add(member.filename.casefold())
                if unix_mode & 0o170000 == 0o120000:
                    raise ValueError("Model package contains a symbolic link")
                total_bytes += member.file_size
                if total_bytes > MAX_EXTRACTED_BYTES:
                    raise ValueError("Model package exceeds the expanded size limit")
                target = temporary_root.joinpath(*relative.parts)
                if not target.resolve().is_relative_to(temporary_root.resolve()):
                    raise ValueError("Model package path escapes its temporary directory")
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
        manifest_path = model_root / "smartvoice-model.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        model_id = str(manifest["id"])
        spec = get_model_spec(model_id)
        if (
            manifest.get("schema_version") != "1.0"
            or manifest.get("task") != spec.task
            or manifest.get("source") != spec.source
            or manifest.get("archive_sha256") != spec.archive_sha256
        ):
            raise ValueError("Model package manifest is incompatible with the catalog")
        files = manifest.get("files", {})
        hashes = manifest.get("file_sha256", {})
        for required in spec.required_files:
            relative = files.get(required)
            if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise ValueError(f"Model package is missing a safe path for {required}")
            file_path = (model_root / relative).resolve()
            if not file_path.is_relative_to(model_root.resolve()):
                raise ValueError(f"Model package file path escapes model directory: {required}")
            if not file_path.is_file() or hashes.get(required) != _sha256(file_path):
                raise ValueError(f"Model package file failed integrity validation: {required}")
            if spec.file_sha256 and hashes.get(required) != spec.file_sha256.get(required):
                raise ValueError(f"Model package file does not match the catalog-pinned content: {required}")
        for required in spec.required_dirs:
            relative = files.get(required)
            directory = (model_root / relative).resolve() if isinstance(relative, str) else model_root
            if not directory.is_relative_to(model_root.resolve()) or not directory.is_dir():
                raise ValueError(f"Model package is missing a safe directory for {required}")
        for key, expected in hashes.items():
            relative = files.get(key)
            file_path = (model_root / relative).resolve() if isinstance(relative, str) else model_root
            if not file_path.is_relative_to(model_root.resolve()) or not file_path.is_file() or _sha256(file_path) != expected:
                raise ValueError(f"Model package file failed integrity validation: {key}")
        model_id = spec.id
        destination = settings.models_dir / model_id
        if model_directory(settings, model_id).exists():
            raise InvalidRequestError(f"Model directory already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(model_root, destination)
        return destination
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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
