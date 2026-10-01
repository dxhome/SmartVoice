"""Build the bundled Qwen3-TTS CPU runtime for 64-bit Windows with MSYS2."""

from __future__ import annotations

import os
import platform
import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NATIVE_SOURCE = ROOT / "native" / "qwen3-tts"
FINGERPRINT_PATHS = ("*.c", "*.h", "Makefile", "tests", "tools", "configs", "third_party", "vendor")


def _source_fingerprint() -> str:
    """Preserve the vendored engine's source identity in the isolated build copy."""
    checked_in = NATIVE_SOURCE / ".source_fingerprint"
    git = shutil.which("git")
    if not git:
        return checked_in.read_text(encoding="utf-8").strip() if checked_in.is_file() else "unknown"

    commit = subprocess.run(
        [git, "-C", str(NATIVE_SOURCE), "rev-parse", "--short", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    changed = subprocess.run(
        [git, "-C", str(NATIVE_SOURCE), "status", "--porcelain", "--untracked-files=all", "--", *FINGERPRINT_PATHS],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if not changed:
        return f"{commit}:clean"

    digest_input = bytearray(
        subprocess.run(
            [git, "-C", str(NATIVE_SOURCE), "diff", "HEAD", "--", *FINGERPRINT_PATHS],
            check=True,
            capture_output=True,
        ).stdout
    )
    repository_prefix = NATIVE_SOURCE.relative_to(ROOT).as_posix() + "/"
    changed_paths = sorted(
        path[len(repository_prefix):] if path.startswith(repository_prefix) else path
        for path in (line[3:].strip() for line in changed.splitlines())
    )
    for relative in changed_paths:
        changed_file = NATIVE_SOURCE / relative
        if changed_file.is_file():
            digest_input.extend(f"{relative} {hashlib.sha256(changed_file.read_bytes()).hexdigest()}\n".encode())
    return f"{commit}-dirty:{hashlib.sha256(digest_input).hexdigest()[:12]}"


def _msys2_root() -> Path:
    configured = os.environ.get("SMARTVOICE_MSYS2_ROOT")
    candidates = [Path(configured)] if configured else []
    candidates.extend(
        Path(path)
        for path in (
            r"C:\msys64",
            str(Path.home() / "msys64"),
        )
    )
    for root in candidates:
        if (root / "usr" / "bin" / "bash.exe").is_file():
            return root.resolve()
    raise RuntimeError(
        "Building the Windows Qwen3-TTS runtime requires MSYS2 with the UCRT64 "
        "OpenBLAS package. Set SMARTVOICE_MSYS2_ROOT to the MSYS2 installation."
    )


def build_windows_runtime(destination: Path) -> Path:
    """Build in a temporary source copy and copy the executable plus runtime DLLs."""
    if platform.machine().lower() not in {"amd64", "x86_64"}:
        raise RuntimeError("The bundled Qwen3-TTS Windows runtime currently supports x64 only.")

    msys_root = _msys2_root()
    make = msys_root / "usr" / "bin" / "make.exe"
    openblas = msys_root / "ucrt64"
    compiler = msys_root / "usr" / "bin" / "gcc.exe"
    required = (
        make,
        compiler,
        msys_root / "usr" / "bin" / "pacman.exe",
        msys_root / "usr" / "bin" / "cmp.exe",
        openblas / "include" / "openblas" / "cblas.h",
        openblas / "lib" / "libopenblas.dll.a",
        openblas / "bin" / "libopenblas.dll",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(
            "Incomplete MSYS2 toolchain; install make, GCC and "
            "mingw-w64-ucrt-x86_64-openblas. Missing: " + ", ".join(missing)
        )

    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    path_entries = [msys_root / "usr" / "bin", openblas / "bin"]
    build_env = os.environ.copy()
    build_env["PATH"] = os.pathsep.join(map(str, path_entries)) + os.pathsep + build_env.get("PATH", "")
    build_env["MSYSTEM"] = "MSYS"

    with tempfile.TemporaryDirectory(prefix="smartvoice-qwen3-tts-") as temporary:
        build_source = Path(temporary) / "qwen3-tts"
        shutil.copytree(
            NATIVE_SOURCE,
            build_source,
            ignore=shutil.ignore_patterns(
                "*.o", "*.d", "qwen_tts", "qwen_tts.exe", "libingot.a",
                ".build_state", "qwen_build_id.h", "qwen_build_id.h.tmp",
            ),
        )
        (build_source / ".source_fingerprint").write_text(_source_fingerprint() + "\n", encoding="utf-8")
        subprocess.run(
            [
                str(make), "-j" + str(max(1, min(os.cpu_count() or 1, 8))),
                "blas", "CC=gcc", "SIMD=portable", "ARCH_FLAGS=-mavx2 -mfma",
                "EXTRA_CFLAGS=-DSMARTVOICE_QWEN_MSYS -include qwen_windows_compat.h -I/ucrt64/include/openblas",
                # MSYS pthread ABI uses 8-byte mutex objects; do not resolve its API
                # against UCRT's libwinpthread (which OpenBLAS itself still needs).
                "LDLIBS=-lm -L/ucrt64/lib -lopenblas",
            ],
            cwd=build_source,
            env=build_env,
            check=True,
        )

        binary = build_source / "qwen_tts.exe"
        if not binary.is_file():
            raise RuntimeError("MSYS2 build completed without producing qwen_tts.exe.")
        shutil.copy2(binary, destination / binary.name)

    # These are the non-system DLLs reported by the selected MSYS2/UCRT64 build.
    runtime_dlls = (
        (msys_root / "usr" / "bin" / "msys-2.0.dll"),
        (msys_root / "usr" / "bin" / "msys-gcc_s-seh-1.dll"),
        (openblas / "bin" / "libgcc_s_seh-1.dll"),
        (openblas / "bin" / "libwinpthread-1.dll"),
        (openblas / "bin" / "libopenblas.dll"),
        (openblas / "bin" / "libgomp-1.dll"),
        (openblas / "bin" / "libgfortran-5.dll"),
        (openblas / "bin" / "libquadmath-0.dll"),
    )
    absent = [str(path) for path in runtime_dlls if not path.is_file()]
    if absent:
        raise RuntimeError("MSYS2 runtime DLLs are missing: " + ", ".join(absent))
    for library in runtime_dlls:
        shutil.copy2(library, destination / library.name)

    license_sources = (
        (msys_root / "usr" / "share" / "doc" / "Cygwin" / "COPYING", "msys2-runtime-COPYING"),
        (msys_root / "usr" / "share" / "licenses" / "gcc-libs" / "RUNTIME.LIBRARY.EXCEPTION", "gcc-libs-RUNTIME.LIBRARY.EXCEPTION"),
        (openblas / "share" / "licenses" / "OpenBLAS" / "LICENSE", "OpenBLAS-LICENSE"),
        (openblas / "share" / "licenses" / "OpenBLAS" / "LICENSE-lapack", "OpenBLAS-LICENSE-lapack"),
        (openblas / "share" / "licenses" / "libgcc" / "COPYING.RUNTIME", "libgcc-COPYING.RUNTIME"),
        (openblas / "share" / "licenses" / "libgfortran" / "COPYING.RUNTIME", "libgfortran-COPYING.RUNTIME"),
        (openblas / "share" / "licenses" / "libgomp" / "COPYING.RUNTIME", "libgomp-COPYING.RUNTIME"),
        (openblas / "share" / "licenses" / "libquadmath" / "COPYING.LIB", "libquadmath-COPYING.LIB"),
        (openblas / "share" / "licenses" / "libwinpthread" / "COPYING", "libwinpthread-COPYING"),
    )
    missing_licenses = [str(path) for path, _ in license_sources if not path.is_file()]
    if missing_licenses:
        raise RuntimeError("MSYS2 runtime license files are missing: " + ", ".join(missing_licenses))
    license_dir = destination / "licenses"
    license_dir.mkdir(exist_ok=True)
    for source, name in license_sources:
        shutil.copy2(source, license_dir / name)

    pacman = msys_root / "usr" / "bin" / "pacman.exe"
    runtime_version = subprocess.run(
        [str(pacman), "-Q", "msys2-runtime"],
        env=build_env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip().split()[-1]
    (license_dir / "RUNTIME-SOURCES.txt").write_text(
        "The bundled msys-2.0.dll is distributed under GPL-3.0-or-later.\n"
        "MSYS2 identifies its components and their separate licenses at https://www.msys2.org/license/.\n"
        "Matching msys2-runtime source package: "
        f"https://mirror.msys2.org/msys/sources/msys2-runtime-{runtime_version}.src.tar.zst\n"
        "Other license texts for the bundled Windows runtime DLLs are in this directory.\n",
        encoding="utf-8",
    )

    packaged_binary = destination / "qwen_tts.exe"
    try:
        result = subprocess.run(
            [str(packaged_binary), "--self-test"],
            cwd=destination,
            env=build_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        output = (exc.stdout or "")[-8192:]
        raise RuntimeError(
            f"The packaged Windows Qwen3-TTS runtime self-test exited with code {exc.returncode}.\n{output}"
        ) from exc
    if "SELF-TEST PASSED" not in result.stdout:
        raise RuntimeError("The packaged Windows Qwen3-TTS runtime did not pass --self-test.")
    return packaged_binary


if __name__ == "__main__":
    output = Path(os.environ.get("SMARTVOICE_QWEN_OUTPUT", ROOT / ".smartvoice-dev" / "qwen-windows-bin"))
    print(build_windows_runtime(output))
