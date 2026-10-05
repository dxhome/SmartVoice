"""Named, explicit validation policies; production never imports this module."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Policy:
    relative_quiet: bool = False
    minimum_window_seconds: float | None = None
    overlap_on_silence: bool = True


BASE_POLICIES = {
    'current': Policy(),
    'relative': Policy(relative_quiet=True),
    'minimum8': Policy(minimum_window_seconds=8),
    'forced': Policy(overlap_on_silence=False),
    'relative-minimum8': Policy(True, 8, True),
    'relative-forced': Policy(True, None, False),
    'minimum8-forced': Policy(False, 8, False),
    'candidate': Policy(True, 8, False),
}
POLICIES = {**BASE_POLICIES,
            'relative-minimum9': Policy(True, 9, True),
            'relative-minimum10': Policy(True, 10, True),
            'relative-minimum12': Policy(True, 12, True)}
