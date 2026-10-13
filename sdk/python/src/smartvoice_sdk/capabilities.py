"""Typed capability snapshots returned by SmartVoice read-only endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class TaskModelCapability:
    """One installed runtime model's inference capabilities."""

    task: str
    model: str
    languages: tuple[str, ...]
    streaming: bool
    details: Mapping[str, Any]

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "TaskModelCapability":
        return cls(
            task=str(row.get("task", "")),
            model=str(row.get("model", "")),
            languages=tuple(str(code) for code in row.get("languages", ()) if isinstance(code, str)),
            streaming=bool(row.get("streaming", False)),
            details=dict(row),
        )


@dataclass(frozen=True)
class ServiceCapabilities:
    """Runtime STT/TTS capabilities; this is not the model catalog or manager."""

    tasks: tuple[TaskModelCapability, ...]
    raw: Mapping[str, Any]

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ServiceCapabilities":
        rows = payload.get("tasks", ())
        capabilities = tuple(
            TaskModelCapability.from_dict(row)
            for row in rows
            if isinstance(row, Mapping) and row.get("task") in {"transcription", "speech"}
        ) if isinstance(rows, (list, tuple)) else ()
        return cls(capabilities, dict(payload))

    def models_for(self, task: str) -> tuple[TaskModelCapability, ...]:
        """Return installed runtime models advertising ``task``."""
        _validate_task(task)
        return tuple(item for item in self.tasks if item.task == task)

    def languages_for(
        self,
        task: str,
        *,
        model: str | None = None,
        include_auto: bool = False,
    ) -> tuple[str, ...]:
        """Return language codes from runtime capabilities.

        With ``model=None``, this is the union across installed models. For the
        virtual ``smartvoice-auto`` model, languages are derived from read-only
        route status and include only routes with an installed candidate.
        ``auto`` is a request option, not a model language, so it is opt-in.
        """
        _validate_task(task)
        if model == "smartvoice-auto":
            routes = self.raw.get("router", {}).get("tasks", {}).get(task, {})
            languages = {
                language for language, candidates in routes.items()
                if isinstance(candidates, list)
                and any(isinstance(candidate, Mapping) and candidate.get("installed") is True for candidate in candidates)
            }
        else:
            rows = self.models_for(task)
            if model is not None:
                rows = tuple(row for row in rows if row.model == model)
            languages = {language for row in rows for language in row.languages}
        if include_auto:
            languages.add("auto")
        return tuple(sorted(languages))

    def supports_language(self, task: str, language: str, *, model: str | None = None) -> bool:
        """Check whether an installed model or configured auto route supports a language."""
        return language in self.languages_for(task, model=model)


@dataclass(frozen=True)
class StreamingCapabilities:
    """Supported stream combinations and their current model-chain readiness."""

    chains: tuple[Mapping[str, Any], ...]
    raw: Mapping[str, Any]

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "StreamingCapabilities":
        rows = payload.get("chains", ())
        chains = tuple(dict(row) for row in rows if isinstance(row, Mapping)) if isinstance(rows, (list, tuple)) else ()
        return cls(chains=chains, raw=dict(payload))

    def chain(self, mode: str, source_language: str, target_language: str | None = None) -> Mapping[str, Any] | None:
        """Find a supported mode/language combination, whether or not models are ready."""
        return next((
            row for row in self.chains
            if row.get("mode") == mode
            and row.get("source_language") == source_language
            and row.get("target_language") == target_language
        ), None)

    def source_languages(self, mode: str, *, available_only: bool = False) -> tuple[str, ...]:
        rows = [row for row in self.chains if row.get("mode") == mode]
        if available_only:
            rows = [row for row in rows if row.get("available") is True]
        return tuple(sorted({str(row["source_language"]) for row in rows if row.get("source_language")}))

    def language_pairs(self, mode: str, *, available_only: bool = False) -> tuple[tuple[str, str | None], ...]:
        rows = [row for row in self.chains if row.get("mode") == mode]
        if available_only:
            rows = [row for row in rows if row.get("available") is True]
        return tuple(sorted({
            (str(row["source_language"]), row.get("target_language"))
            for row in rows if row.get("source_language")
        }))


def _validate_task(task: str) -> None:
    if task not in {"transcription", "speech"}:
        raise ValueError("task must be 'transcription' (STT) or 'speech' (TTS)")
