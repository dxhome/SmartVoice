"""Build the bundled Qwen3-TTS CPU runtime for Linux x86_64."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NATIVE_SOURCE = ROOT / "native" / "qwen3-tts"


def build_linux_runtime(destination: Path) -> Path:
    """Build against the host OpenBLAS and stage a verified executable."""
    if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise RuntimeError("The bundled Qwen3-TTS Linux runtime currently supports x86_64 only.")

    missing = [name for name in ("make", "cc", "bash") if shutil.which(name) is None]
    if missing:
        raise RuntimeError(
            "Building the Linux Qwen3-TTS runtime requires make, a C compiler, and bash. "
            "Install build-essential and retry. Missing: " + ", ".join(missing)
        )
    openblas_headers = Path("/usr/include/openblas/cblas.h").is_file() or any(
        Path("/usr/include").glob("*-linux-gnu/openblas-*/cblas.h")
    )
    if not openblas_headers:
        raise RuntimeError(
            "Building the Linux Qwen3-TTS runtime requires OpenBLAS development headers. "
            "On Ubuntu, install libopenblas-dev."
        )

    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    # A developer may have built another platform into this ignored resource directory.
    # Do not let Windows executables or DLLs leak into a Linux source tree or wheel.
    for stale_name in (
        "qwen_tts.exe",
        "msys-2.0.dll",
        "msys-gcc_s-seh-1.dll",
        "libgcc_s_seh-1.dll",
        "libwinpthread-1.dll",
        "libopenblas.dll",
        "libgomp-1.dll",
        "libgfortran-5.dll",
        "libquadmath-0.dll",
    ):
        stale_binary = destination / stale_name
        if stale_binary.is_file():
            stale_binary.unlink()
    library_dir = destination / "lib"
    if library_dir.exists():
        shutil.rmtree(library_dir)
    library_dir.mkdir(parents=True)
    fingerprint = subprocess.run(
        ["bash", str(NATIVE_SOURCE / "tools" / "source_fingerprint.sh")],
        cwd=NATIVE_SOURCE,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    with tempfile.TemporaryDirectory(prefix="smartvoice-qwen3-tts-linux-") as temporary:
        build_source = Path(temporary) / "qwen3-tts"
        shutil.copytree(
            NATIVE_SOURCE,
            build_source,
            ignore=shutil.ignore_patterns(
                "*.o", "*.d", "qwen_tts", "libingot.a", ".build_state",
                "qwen_build_id.h", "qwen_build_id.h.tmp",
            ),
        )
        (build_source / ".source_fingerprint").write_text(fingerprint + "\n", encoding="utf-8")
        subprocess.run(
            [
                "make",
                "-j" + str(max(1, min(os.cpu_count() or 1, 8))),
                "blas",
                "CC=cc",
                # These wheels use the generic linux_x86_64 tag, whose baseline does
                # not guarantee AVX2.  The Makefile's auto profile is build-host
                # specific and can put unsupported instructions into the wheel.
                # Keep the packaged executable on the x86_64 baseline so it also
                # starts on older and virtualized CPUs without AVX2.
                "SIMD=scalar",
                "LDFLAGS=-Wl,-rpath,'$$ORIGIN/lib'",
                # The Makefile's default auto mode detects ISA features exposed to this
                # host. Do not pass SIMD=auto on the command line: GNU make would prevent
                # the Makefile from resolving it to the detected profile.
            ],
            cwd=build_source,
            check=True,
        )
        binary = build_source / "qwen_tts"
        if not binary.is_file():
            raise RuntimeError("The Linux Qwen3-TTS build completed without producing qwen_tts.")

        result = subprocess.run(
            [str(binary), "--self-test"],
            cwd=build_source,
            check=True,
            capture_output=True,
            text=True,
            errors="replace",
        )
        if "SELF-TEST PASSED" not in result.stdout:
            raise RuntimeError("The Linux Qwen3-TTS runtime did not pass --self-test.")

        dependencies = subprocess.run(["ldd", str(binary)], check=True, capture_output=True, text=True)
        bundled_libraries: set[str] = set()
        for line in dependencies.stdout.splitlines():
            match = re.match(r"\s*(lib(?:openblas|gfortran|gomp|quadmath)[^\s]*)\s+=>\s+(\S+)", line)
            if not match or match.group(2) == "not":
                continue
            library_name, source = match.groups()
            shutil.copy2(Path(source).resolve(), library_dir / library_name)
            bundled_libraries.add(library_name)
        if "libopenblas.so.0" not in bundled_libraries:
            raise RuntimeError("The Linux Qwen3-TTS runtime does not link to the expected OpenBLAS shared library.")

        packaged_binary = destination / "qwen_tts"
        shutil.copy2(binary, packaged_binary)
        packaged_binary.chmod(packaged_binary.stat().st_mode | 0o111)

    license_dir = destination / "licenses"
    license_dir.mkdir(exist_ok=True)
    shutil.copy2(NATIVE_SOURCE / "LICENSE", license_dir / "qwen3-tts-LICENSE")
    runtime_notices = []
    for package in ("libopenblas0-pthread", "libopenblas0-serial", "libgfortran5", "libgomp1", "libquadmath0"):
        notice = Path("/usr/share/doc") / package / "copyright"
        if notice.is_file():
            runtime_notices.append(f"===== {package} =====\n{notice.read_text(encoding='utf-8', errors='replace').strip()}")
    if not any("openblas" in notice.lower() for notice in runtime_notices):
        raise RuntimeError("The Linux Qwen wheel requires the OpenBLAS copyright notice from the build image.")
    (license_dir / "linux-runtime-dependencies.txt").write_text(
        "\n\n".join(runtime_notices) + "\n",
        encoding="utf-8",
    )
    return packaged_binary


if __name__ == "__main__":
    output = Path(os.environ.get("SMARTVOICE_QWEN_OUTPUT", ROOT / ".smartvoice-dev" / "qwen-linux-bin"))
    print(build_linux_runtime(output))
