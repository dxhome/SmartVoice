"""Unified command-line entry point for local speech validation workflows.

Each subcommand delegates to its focused runner in a fresh Python process so
model runtimes, signal handling, and long-running experiment isolation retain
their existing behavior. Use ``python scripts/validate_speech.py --help`` to
list workflows and ``... <workflow> --help`` for runner-specific options.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

COMMANDS = {
    "streaming": ("validation/verify_streaming.py", "Six-route streaming quality, first-output latency and optional process-tree resources"),
    "stt-http": ("validation/verify_stt_http.py", "Real TCP HTTP transcription/synthesis and MP3 checks"),
    "stt-admission": ("validation/verify_stt_admission.py", "Queue admission, overload, timeout, and recovery checks"),
    "stt-policy": ("validation/compare_stt_policies.py", "Language, cache, window, and overlap comparisons"),
    "stt-quality": ("validation/verify_stt_candidates.py", "Repeated STT quality candidate comparisons"),
    "stt-load": ("validation/verify_stt_load.py", "Fixed-arrival model load measurements"),
    "stt-isolated-load": ("validation/verify_stt_isolated_load.py", "Isolated HTTP load and lifecycle measurements"),
    "sensevoice-release": ("validation/verify_sensevoice_release.py", "SenseVoice quality/load/lifecycle acceptance workflow"),
    "speech-interaction": ("validation/verify_speech_interaction.py", "Mixed STT/TTS interaction and resource probes"),
    "sensevoice-boundaries": ("validation/diagnose_sensevoice_boundaries.py", "SenseVoice cut-boundary diagnostics and review export"),
    "prepare-corpus": ("validation/prepare_stt_continuous.py", "Download and prepare the pinned continuous-speech corpus"),
}


def print_help() -> None:
    print("Usage: python scripts/validate_speech.py <workflow> [workflow options]\n")
    print("Workflows:")
    width = max(map(len, COMMANDS))
    for name, (_, description) in COMMANDS.items():
        print(f"  {name:<{width}}  {description}")
    print("\nPass runner options after the workflow name. Each runner accepts --help.")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print_help()
        return 0

    workflow = args.pop(0)
    if workflow not in COMMANDS:
        print(f"Unknown speech validation workflow: {workflow}\n", file=sys.stderr)
        print_help()
        return 2

    runner = ROOT / "scripts" / COMMANDS[workflow][0]
    if not runner.is_file():
        print(f"Validation runner is missing: {runner}", file=sys.stderr)
        return 2

    completed = subprocess.run([sys.executable, str(runner), *args], cwd=ROOT, check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
