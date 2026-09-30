"""Standard test entry points for CI and full local regression runs."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))


def _regression_prerequisites() -> list[str]:
    return [
        f"Python package '{name}' is missing; install with: python -m pip install -e '.[inference]'"
        for name in ("sherpa_onnx", "av", "numpy")
        if importlib.util.find_spec(name) is None
    ]


def _without_real_inference(suite: unittest.TestSuite) -> unittest.TestSuite:
    filtered = unittest.TestSuite()
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            filtered.addTests(_without_real_inference(test))
        elif not test.__class__.__module__.endswith("test_real_inference"):
            filtered.addTest(test)
    return filtered


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"ci", "regression"}:
        print("Usage: python scripts/test.py {ci|regression}", file=sys.stderr)
        return 2

    mode = sys.argv[1]
    if mode == "regression":
        missing = _regression_prerequisites()
        if missing:
            print("Regression test prerequisites are not met:", file=sys.stderr)
            for item in missing:
                print(f"- {item}", file=sys.stderr)
            print(
                "Install the listed inference dependencies, then rerun "
                "'python scripts/test.py regression'.",
                file=sys.stderr,
            )
            return 2
    suite = unittest.defaultTestLoader.discover(
        start_dir=str(TESTS), pattern="test_*.py", top_level_dir=str(ROOT)
    )
    if mode == "ci":
        suite = _without_real_inference(suite)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
