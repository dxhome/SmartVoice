"""Helpers for exercising the separately packaged JavaScript SDK."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from tests.streaming_long_audio import ROOT


TYPESCRIPT_SDK = ROOT / "sdk" / "typescript"
NODE = shutil.which("node")
NODE_SDK_READY = bool(
    NODE
    and (TYPESCRIPT_SDK / "node_modules" / "ws" / "package.json").is_file()
    and (TYPESCRIPT_SDK / "node_modules" / ".bin" / "tsc").exists()
)


def run_node_test(
    test_file: str,
    *,
    service_url: str,
    wav_path: Path,
    env: dict[str, str] | None = None,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    if not NODE:
        raise RuntimeError("Node.js is not installed")
    child_env = os.environ.copy()
    child_env.update({
        "SMARTVOICE_TS_BASE_URL": service_url,
        "SMARTVOICE_TS_WAV": str(wav_path),
    })
    child_env.update(env or {})
    return subprocess.run(
        [NODE, "--test", str(TYPESCRIPT_SDK / "tests" / test_file)],
        cwd=ROOT,
        env=child_env,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
