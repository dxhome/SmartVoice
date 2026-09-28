"""Static language-to-model routing and atomic runtime configuration reload."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from smartvoice.config.settings import Settings
from smartvoice.domain.errors import InvalidRequestError, ModelUnavailableError, UnsupportedFeatureError
from smartvoice.services.model_catalog import MODEL_ID_PATTERN, load_catalog

logger = logging.getLogger("smartvoice.router")
ROUTER_MODEL_IDS = {"transcription": "stt-smartvoice-auto", "speech": "tts-smartvoice-auto"}
WHISPER_LANGUAGE_CODES = frozenset(
    "en zh de es ru ko fr ja pt tr pl ca nl ar sv it id hi fi vi he uk el ms cs ro da hu ta "
    "no th ur hr bg lt la mi ml cy sk te fa lv bn sr az sl kn et mk br eu is hy ne mn bs kk fil "
    "sq sw gl mr pa si km sn yo so af oc ka be tg sd gu am yi lo uz fo ht ps tk nn mt sa lb "
    "my bo tl mg as tt haw ln ha ba jw su yue"
    .split()
)
LANGUAGE_RE = re.compile(r"^[a-z]{2,3}$")
MAX_CANDIDATES = 3


class RouterConfigError(InvalidRequestError):
    code = "router_config_invalid"


class LanguageDetectionError(InvalidRequestError):
    code = "language_detection_failed"


@dataclass(frozen=True, slots=True)
class RouterConfig:
    tasks: dict[str, dict[str, tuple[str, ...]]]
    digest: str


def default_router_path() -> Path:
    return Path(__file__).resolve().parents[3] / "catalog" / "router.json"


def _parse_router_config(raw: Any) -> RouterConfig:
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "tasks"} or raw.get("schema_version") != "1.0":
        raise RouterConfigError("Router configuration must be a JSON object with schema_version '1.0'.")
    tasks = raw.get("tasks")
    if not isinstance(tasks, dict) or set(tasks) != {"transcription", "speech"}:
        raise RouterConfigError("Router configuration tasks must contain transcription and speech maps.")
    specs = {spec.id: spec for spec in load_catalog()}
    normalized: dict[str, dict[str, tuple[str, ...]]] = {}
    for task, languages in tasks.items():
        if not isinstance(languages, dict):
            raise RouterConfigError(f"Router task {task!r} must contain a language-to-model map.")
        normalized[task] = {}
        for language, model_ids in languages.items():
            if not isinstance(language, str) or language not in WHISPER_LANGUAGE_CODES:
                raise RouterConfigError(f"Unsupported Whisper language code: {language!r}.")
            if not isinstance(model_ids, list) or not 1 <= len(model_ids) <= MAX_CANDIDATES:
                raise RouterConfigError(f"Language {language!r} must have between 1 and {MAX_CANDIDATES} model candidates.")
            if any(not isinstance(model_id, str) for model_id in model_ids):
                raise RouterConfigError(f"Language {language!r} contains a non-string model ID.")
            if len(model_ids) != len(set(model_ids)):
                raise RouterConfigError(f"Language {language!r} contains duplicate model IDs.")
            for model_id in model_ids:
                if not MODEL_ID_PATTERN.fullmatch(model_id):
                    raise RouterConfigError(f"Invalid model ID {model_id!r} in router configuration.")
                spec = specs.get(model_id)
                if spec is None:
                    raise RouterConfigError(f"Unknown model ID {model_id!r} in router configuration.")
                if spec.task != task:
                    raise RouterConfigError(f"Model {model_id!r} does not support task {task!r}.")
                if language not in spec.languages:
                    raise RouterConfigError(f"Model {model_id!r} does not declare support for language {language!r}.")
            normalized[task][language] = tuple(model_ids)
    canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return RouterConfig(normalized, hashlib.sha256(canonical.encode("utf-8")).hexdigest())


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ModelRouter:
    """Owns one validated immutable routing snapshot for the running service."""

    def __init__(self, settings: Settings, *, config_path: Path | None = None, default_path: Path | None = None):
        self.path = config_path or settings.data_dir / "router.json"
        self.default_path = default_path or default_router_path()
        self._lock = threading.RLock()
        self.last_error: str | None = None
        try:
            self._config = self._load_or_initialize()
        except RouterConfigError as exc:
            # Keep the service available so the user can repair router.json and
            # apply it through `python -m smartvoice router reload`.
            self.last_error = "The saved router configuration is invalid; the built-in configuration is active."
            try:
                raw = json.loads(self.default_path.read_text(encoding="utf-8"))
                self._config = _parse_router_config(raw)
            except (OSError, json.JSONDecodeError, RouterConfigError) as fallback_exc:
                raise RouterConfigError("Both user and built-in router configurations are invalid.") from fallback_exc
            logger.error("router_config_invalid_using_builtin path=%s reason=%s", self.path.name, exc)

    def _load_or_initialize(self) -> RouterConfig:
        if not self.path.exists():
            try:
                raw = self.default_path.read_bytes()
            except OSError as exc:
                raise RouterConfigError("Built-in router configuration is unavailable.") from exc
            _atomic_write(self.path, raw)
        return self._read_and_validate()

    def _read_and_validate(self) -> RouterConfig:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise RouterConfigError(f"Could not read router configuration at {self.path}.") from exc
        return _parse_router_config(raw)

    def snapshot(self) -> RouterConfig:
        with self._lock:
            return self._config

    def reload(self) -> dict[str, object]:
        replacement = self._read_and_validate()
        with self._lock:
            self._config = replacement
            self.last_error = None
        logger.info("router_config_reloaded path=%s sha256=%s", self.path.name, replacement.digest)
        return {"status": "reloaded", "sha256": replacement.digest}

    def choose(
        self, task: str, language: str, installed_models: list[dict[str, object]],
        *, config: RouterConfig | None = None,
    ) -> tuple[str, list[dict[str, object]]]:
        config = config or self.snapshot()
        candidates = config.tasks.get(task, {}).get(language, ())
        if not candidates:
            raise UnsupportedFeatureError(
                f"SmartVoice does not support routing {task} requests for language {language!r}."
            )
        installed = {str(model.get("id")) for model in installed_models if model.get("task") == task}
        states = [{"model": model_id, "installed": model_id in installed} for model_id in candidates]
        selected = next((model_id for model_id in candidates if model_id in installed), None)
        if selected is None:
            configured = ", ".join(candidates) if candidates else "none"
            raise ModelUnavailableError(
                f"No installed and verified {task} model is configured for language {language!r}. "
                f"Configured candidates: {configured}. Install a configured model or update router.json."
            )
        return selected, states

    def public_status(self, installed_models: list[dict[str, object]]) -> dict[str, object]:
        config = self.snapshot()
        installed = {str(model.get("id")) for model in installed_models}
        return {
            "sha256": config.digest,
            "warning": self.last_error,
            "models": ROUTER_MODEL_IDS,
            "tasks": {
                task: {
                    language: [{"model": model_id, "installed": model_id in installed} for model_id in candidates]
                    for language, candidates in languages.items()
                }
                for task, languages in config.tasks.items()
            },
        }


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result
