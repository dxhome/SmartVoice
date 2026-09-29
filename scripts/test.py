"""Standard test entry points for CI and full local regression runs."""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))


def _regression_prerequisites() -> list[str]:
    from smartvoice.config.settings import Settings
    from smartvoice.services.model_storage import model_directory
    from smartvoice.services.spoken_language_identifier import installed_language_id_model_dir

    missing = [
        f"Python package '{name}' is missing; install with: python -m pip install -e '.[inference]'"
        for name in ("sherpa_onnx", "av", "numpy")
        if importlib.util.find_spec(name) is None
    ]
    settings = Settings.from_env()
    required_models = {
        "stt-sensevoice-small-int8": ("zh.wav", "en.wav"),
        "tts-melo-zh-en": (),
    }
    for model_id, required_samples in required_models.items():
        directory = model_directory(settings, model_id)
        if not (directory / "smartvoice-model.json").is_file():
            missing.append(f"Model '{model_id}' is not installed under {directory}")
            continue
        for sample_name in required_samples:
            if not any(directory.rglob(sample_name)):
                missing.append(f"Model '{model_id}' is missing its required test sample '{sample_name}'")
    if installed_language_id_model_dir(settings) is None:
        missing.append(
            "The verified Whisper Tiny language-ID assets are not installed; install them with: "
            "python -m smartvoice models install-language-id"
        )
    return missing


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
                "Install the listed models and inference dependencies, then rerun "
                "'python scripts/test.py regression'.",
                file=sys.stderr,
            )
            return 2
        os.environ["SMARTVOICE_RUN_REAL_INFERENCE"] = "1"
    else:
        os.environ.pop("SMARTVOICE_RUN_REAL_INFERENCE", None)

    suite = unittest.defaultTestLoader.discover(
        start_dir=str(TESTS), pattern="test_*.py", top_level_dir=str(ROOT)
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
