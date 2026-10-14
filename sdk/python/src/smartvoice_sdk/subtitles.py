"""Revision-aware subtitle state for incremental streaming events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class SubtitleUnit:
    unit_id: str
    text: str
    revision: int
    final: bool
    data: Mapping[str, Any]


class SubtitleState:
    """Apply revisions with a bounded per-kind history of subtitle units."""

    def __init__(self, *, max_units: int = 1000) -> None:
        if type(max_units) is not int or max_units < 1:
            raise ValueError("max_units must be a positive integer")
        self.max_units = max_units
        self._units: dict[str, dict[str, SubtitleUnit]] = {"source": {}, "target": {}}
        self._order: dict[str, list[str]] = {"source": [], "target": []}

    def apply(self, event: Any) -> bool:
        """Apply a ``StreamEvent`` or event mapping; return whether state changed."""
        data = getattr(event, "data", event)
        if not isinstance(data, Mapping):
            return False
        event_type = str(data.get("type", getattr(event, "type", "")))
        if event_type.startswith("source_unit_"):
            group = "source"
        elif event_type.startswith("target_"):
            group = "target"
        else:
            return False
        unit_id = data.get("unit_id")
        revision = data.get("revision")
        if unit_id is None or type(revision) is not int or revision < 0:
            return False
        key = str(unit_id)
        current = self._units[group].get(key)
        if current and (current.final or revision <= current.revision):
            return False
        if data.get("retracted") is True:
            if current is None:
                return False
            del self._units[group][key]
            self._order[group].remove(key)
            return True
        final = event_type.endswith("_final") or data.get("final") is True or data.get("committed") is True
        unit = SubtitleUnit(key, str(data.get("text", "")), revision, final, dict(data))
        if current is None:
            self._order[group].append(key)
        self._units[group][key] = unit
        while len(self._order[group]) > self.max_units:
            oldest = self._order[group].pop(0)
            self._units[group].pop(oldest, None)
        return True

    def units(self, kind: str = "source") -> tuple[SubtitleUnit, ...]:
        if kind not in self._units:
            raise ValueError("kind must be 'source' or 'target'")
        return tuple(self._units[kind][key] for key in self._order[kind] if key in self._units[kind])

    def text(self, kind: str = "source") -> str:
        return "".join(unit.text for unit in self.units(kind))

    def clear(self) -> None:
        for kind in self._units:
            self._units[kind].clear()
            self._order[kind].clear()
