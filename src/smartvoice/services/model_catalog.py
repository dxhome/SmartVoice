"""Pinned model catalog and safe local installation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import urllib.request
import uuid
from urllib.parse import urlparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable

from smartvoice.config.settings import Settings
from smartvoice.domain.errors import InvalidRequestError

MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_EXTRACTED_BYTES = 3 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 50_000


@dataclass(frozen=True, slots=True)
class ModelSpec:
    id: str
    task: str
    name: str
    languages: tuple[str, ...]
    backend: str
    source: str
    archive_name: str
    archive_sha256: str
    required_files: tuple[str, ...]
    model_file: str
    tokens_file: str
    license_note: str
    lexicon_file: str | None = None


def load_catalog() -> list[ModelSpec]:
    catalog_path = Path(__file__).resolve().parents[3] / "catalog" / "models.json"
    raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    if raw.get("schema_version") != "1.0":
        raise RuntimeError("Unsupported model catalog schema")
    specs = []
    for item in raw["models"]:
        source = item["source"]
        if not source.startswith("https://github.com/k2-fsa/sherpa-onnx/releases/download/"):
            raise RuntimeError(f"Catalog source is not allowlisted for {item['id']}")
        specs.append(ModelSpec(
            id=item["id"], task=item["task"], name=item["name"],
            languages=tuple(item["languages"]), backend=item["backend"],
            source=source, archive_name=item["archive_name"], archive_sha256=item["archive_sha256"],
            required_files=tuple(item["required_files"]), model_file=item["model_file"],
            tokens_file=item["tokens_file"], license_note=item["license_note"],
            lexicon_file=item.get("lexicon_file"),
        ))
    return specs


def get_model_spec(model_id: str) -> ModelSpec:
    for spec in load_catalog():
        if spec.id == model_id:
            return spec
    raise InvalidRequestError(f"Unknown model ID: {model_id}")


def installed_models(settings: Settings) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for spec in load_catalog():
        manifest_path = settings.models_dir / spec.id / "smartvoice-model.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            root = settings.models_dir / spec.id
            valid = all(
                (root / rel).is_dir() if key == "data_dir" else (root / rel).is_file()
                for key, rel in manifest["files"].items()
            )
        except (OSError, KeyError, json.JSONDecodeError):
            valid = False
        if valid:
            results.append({
                "id": spec.id,
                "name": spec.name,
                "task": spec.task,
                "languages": list(spec.languages),
                "backend": spec.backend,
                "installed": True,
                "license_note": spec.license_note,
            })
    return results


def catalog_models(settings: Settings) -> list[dict[str, object]]:
    installed_ids = {str(model["id"]) for model in installed_models(settings)}
    return [{
        "id": spec.id, "name": spec.name, "task": spec.task,
        "languages": list(spec.languages), "backend": spec.backend,
        "source": spec.source, "installed": spec.id in installed_ids,
        "license_note": spec.license_note,
    } for spec in load_catalog()]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(url: str, path: Path, progress: Callable[[int, int | None], None] | None) -> None:
    class SafeHttpsRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            parsed = urlparse(newurl)
            allowed_hosts = {"github.com", "release-assets.githubusercontent.com"}
            if parsed.scheme != "https" or parsed.hostname not in allowed_hosts or parsed.username or parsed.password:
                raise ValueError("Model download redirected outside the approved HTTPS hosts")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    request = urllib.request.Request(url, headers={"User-Agent": "SmartVoice local model manager"})
    opener = urllib.request.build_opener(SafeHttpsRedirect())
    with opener.open(request, timeout=60) as response, path.open("wb") as output:
        content_length = response.headers.get("Content-Length")
        expected = int(content_length) if content_length and content_length.isdigit() else None
        if expected and expected > MAX_ARCHIVE_BYTES:
            raise ValueError("Model archive exceeds the 2 GiB download limit")
        downloaded = 0
        while block := response.read(1024 * 1024):
            downloaded += len(block)
            if downloaded > MAX_ARCHIVE_BYTES:
                raise ValueError("Model archive exceeds the 2 GiB download limit")
            output.write(block)
            if progress:
                progress(downloaded, expected)
        if expected and downloaded != expected:
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
) -> Path:
    spec = get_model_spec(model_id)
    settings.models_dir.mkdir(parents=True, exist_ok=True)
    destination = settings.models_dir / spec.id
    if destination.exists():
        raise InvalidRequestError(f"Model directory already exists: {destination}")
    free = shutil.disk_usage(settings.models_dir).free
    if free < 1024 * 1024 * 1024:
        raise OSError("At least 1 GiB free disk space is required to install a model")

    temporary = settings.models_dir / f".{spec.id}-{uuid.uuid4().hex}.tmp"
    temporary.mkdir()
    try:
        archive_path = temporary / spec.archive_name
        extracted = temporary / "extracted"
        extracted.mkdir()
        _download(spec.source, archive_path, progress)
        archive_digest = _sha256(archive_path)
        if archive_digest != spec.archive_sha256:
            raise ValueError(f"Model archive SHA-256 mismatch for {spec.id}")
        _safe_extract(archive_path, extracted)

        found: dict[str, Path] = {}
        for required in spec.required_files:
            matches = list(extracted.rglob(required))
            if len(matches) != 1:
                raise ValueError(f"Expected one {required!r} in model archive, found {len(matches)}")
            found[required] = matches[0]
        file_map: dict[str, str] = {}
        for relative_name, source_path in found.items():
            rel = source_path.relative_to(extracted)
            file_map[relative_name] = rel.as_posix()
        manifest = {
            "schema_version": "1.0", "id": spec.id, "task": spec.task,
            "source": spec.source, "archive_sha256": archive_digest,
            "hash_scope": "verified against the SHA-256 pinned in the SmartVoice source catalog",
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "files": file_map,
            "file_sha256": {key: _sha256(path) for key, path in found.items()},
            "license_review_required": True,
        }
        (extracted / "smartvoice-model.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(extracted, destination)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    return destination
