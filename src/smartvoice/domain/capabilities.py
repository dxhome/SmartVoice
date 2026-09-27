"""Provider-neutral description of supported inference operations."""

from dataclasses import dataclass
from typing import Literal

TaskName = Literal["transcription", "speech"]


@dataclass(frozen=True, slots=True)
class TaskCapability:
    task: TaskName
    languages: tuple[str, ...]
    streaming: bool = False


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    schema_version: str
    provider_id: str
    tasks: tuple[TaskCapability, ...]
    devices: tuple[str, ...]
