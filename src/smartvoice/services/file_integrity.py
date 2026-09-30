"""File integrity primitives shared by model acquisition and storage."""

import hashlib
import json
import os
import uuid
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_hash_cache(path: Path) -> dict[str, dict[str, object]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        key: value for key, value in data.items()
        if isinstance(key, str) and isinstance(value, dict)
    }


def save_hash_cache(path: Path, cache: dict[str, dict[str, object]]) -> None:
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(cache, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def cache_verified_files(cache_path: Path, files: dict[Path, str]) -> None:
    """Persist hashes for files whose integrity was verified during installation."""
    if not files:
        return
    cache = load_hash_cache(cache_path)
    for path, digest in files.items():
        stat = path.stat()
        cache[str(path.resolve())] = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": digest,
        }
    save_hash_cache(cache_path, cache)


def matches_cached_sha256(path: Path, expected: str, cache_path: Path) -> bool:
    """Verify a file, reusing its digest while size and modification time are stable."""
    stat = path.stat()
    key = str(path.resolve())
    cache = load_hash_cache(cache_path)
    cached = cache.get(key, {})
    if (
        cached.get("size") == stat.st_size
        and cached.get("mtime_ns") == stat.st_mtime_ns
        and cached.get("sha256") == expected
    ):
        return True
    actual = sha256(path)
    if actual == expected:
        cache[key] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": actual}
    else:
        cache.pop(key, None)
    try:
        save_hash_cache(cache_path, cache)
    except OSError:
        pass
    return actual == expected
