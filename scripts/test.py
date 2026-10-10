"""Standard test entry points for CI and full local regression runs."""

from __future__ import annotations

import importlib.util
import argparse
import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))


def _regression_prerequisites() -> list[str]:
    return [
        f"Python package '{name}' is missing; install SmartVoice with: python -m pip install -e ."
        for name in ("sherpa_onnx", "av", "numpy")
        if importlib.util.find_spec(name) is None
    ]


def _without_real_inference(suite: unittest.TestSuite) -> unittest.TestSuite:
    filtered = unittest.TestSuite()
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            filtered.addTests(_without_real_inference(test))
        elif not test.__class__.__module__.endswith(("test_real_inference", "test_stt_audio_regression", "test_streaming_real_long")):
            filtered.addTest(test)
    return filtered


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('ci','regression','full'))
    parser.add_argument('--stt-models',nargs='+',choices=('stt-sensevoice-small-int8','stt-qwen3-asr-600m-int8','stt-whisper-base-multilingual-int8'))
    parser.add_argument('--streaming-only',action='store_true',help='Full streaming contracts and six real-model one-hour audio routes; omit REST/STT suites')
    args=parser.parse_args()
    mode=args.mode
    if args.streaming_only and (mode!='full' or args.stt_models):
        parser.error('--streaming-only requires full and cannot be combined with --stt-models')
    if args.stt_models:
        os.environ['SMARTVOICE_TEST_STT_MODELS']=','.join(args.stt_models)
        print('Selected STT scope: '+', '.join(args.stt_models),flush=True)
    else:
        os.environ.pop('SMARTVOICE_TEST_STT_MODELS',None)
    os.environ["SMARTVOICE_TEST_SUITE"] = mode
    if mode in {"regression", "full"}:
        missing = _regression_prerequisites()
        if missing:
            print("Regression test prerequisites are not met:", file=sys.stderr)
            for item in missing:
                print(f"- {item}", file=sys.stderr)
            print(
                f"Install the listed inference dependencies, then rerun 'python scripts/test.py {mode}'.",
                file=sys.stderr,
            )
            return 2
    if mode == "full":
        from smartvoice.config.settings import Settings
        from smartvoice.services.model_storage import model_directory
        import sherpa_onnx

        settings = Settings.from_env()
        required_models = (
            "stt-sensevoice-small-int8",
            "stt-qwen3-asr-600m-int8",
            "stt-whisper-base-multilingual-int8",
        )
        required_models=() if args.streaming_only else tuple(args.stt_models or required_models)
        missing_models = [model for model in required_models
                          if not (model_directory(settings, model)/"smartvoice-model.json").is_file()]
        if missing_models:
            print("Full suite requires selected STT models (all three when unfiltered); missing:", file=sys.stderr)
            for model in missing_models:
                print(f"- {model}", file=sys.stderr)
            return 2
        from tests.streaming_long_audio import required_models as streaming_models, fixture
        missing_streaming=[model for model in streaming_models()
            if not (model_directory(settings,model)/'smartvoice-model.json').is_file()]
        if missing_streaming:
            print('Full suite requires streaming models; install the selected chains first:',file=sys.stderr)
            for model in missing_streaming:print('- '+model,file=sys.stderr)
            return 2
        missing_dependencies=[name for name in ('ctranslate2','sentencepiece') if importlib.util.find_spec(name) is None]
        if missing_dependencies:
            print('Full streaming requires the streaming extra: '+', '.join(missing_dependencies),file=sys.stderr)
            return 2
        try:
            for language in ('zh','en'):fixture(language)
        except (OSError,ValueError,KeyError,StopIteration) as exc:
            print('Full streaming pinned audio prerequisite failed: '+str(exc),file=sys.stderr)
            return 2
        if "stt-whisper-base-multilingual-int8" in required_models and sherpa_onnx.__version__ != "1.13.8+smartvoice.whisper2":
            print("Full suite requires the validated Whisper whisper2 repair wheel; see doc/whisper-chinese-decoding-fix.md.", file=sys.stderr)
            return 2
    suite = unittest.defaultTestLoader.discover(
        start_dir=str(TESTS), pattern="test_streaming*.py" if args.streaming_only else "test_*.py",
        top_level_dir=str(ROOT)
    )
    if mode == "ci":
        suite = _without_real_inference(suite)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
