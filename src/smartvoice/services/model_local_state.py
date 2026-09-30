"""Persistent filesystem state for models in the local model library."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from smartvoice.config.settings import Settings

STATE_FILENAME = ".model-state.json"


def state_path(settings: Settings) -> Path:
    return settings.models_dir / STATE_FILENAME


def load_state(settings: Settings) -> dict[str, dict[str, object]]:
    try:
        value = json.loads(state_path(settings).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        key: item for key, item in value.items()
        if isinstance(key, str) and isinstance(item, dict)
    }


def record_state(
    settings: Settings,
    model_id: str,
    status: str,
    *,
    backend: str | None = None,
    reason: str | None = None,
    manifest_path: Path | None = None,
    installed_size_bytes: int | None = None,
) -> None:
    """Record verified filesystem state; runtime availability is intentionally dynamic."""
    cache = load_state(settings)
    entry: dict[str, object] = {
        "status": status,
        "reason": reason,
        "availability": "runtime_check_required" if status == "installed" else status,
    }
    if backend is not None:
        entry["backend"] = backend
    if manifest_path is not None and manifest_path.is_file():
        stat = manifest_path.stat()
        entry.update({"manifest_size": stat.st_size, "manifest_mtime_ns": stat.st_mtime_ns})
    if installed_size_bytes is not None:
        entry["installed_size_bytes"] = installed_size_bytes
    cache[model_id] = entry
    path = state_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(cache, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
