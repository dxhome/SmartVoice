"""Verified model storage, import/export and filesystem operations."""

from __future__ import annotations

import json
import os
import shutil
import uuid
import zipfile
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from smartvoice.config.settings import Settings
from smartvoice.domain.errors import InvalidRequestError
from smartvoice.services.model_catalog_constants import MAX_EXTRACTED_BYTES
from smartvoice.services.file_integrity import sha256 as _sha256
from smartvoice.services.model_registry import get_model_spec, load_catalog


@lru_cache(maxsize=4096)
def _cached_sha256(path: str, size: int, mtime_ns: int) -> str:
    """Reuse integrity checks until a model file's size or modification time changes."""
    return _sha256(Path(path))


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
                    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
                        valid = False
                        break
                    stat = resolved.stat()
                    if _cached_sha256(str(resolved), stat.st_size, stat.st_mtime_ns) != expected:
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
