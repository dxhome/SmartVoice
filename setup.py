"""Copy repository model metadata into wheel package resources at build time."""

from pathlib import Path
import os
import platform
import shutil
import subprocess
import sys

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.dist import Distribution
from wheel.bdist_wheel import bdist_wheel


def is_apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine().lower() in {"arm64", "aarch64"}


def is_windows_x64() -> bool:
    return sys.platform == "win32" and platform.machine().lower() in {"amd64", "x86_64"}


class SmartVoiceDistribution(Distribution):
    def has_ext_modules(self):
        # These platform wheels contain a native Qwen inference executable.
        return is_apple_silicon() or is_windows_x64()


class SmartVoiceBdistWheel(bdist_wheel):
    def get_tag(self):
        if is_apple_silicon():
            return "py3", "none", "macosx_11_0_arm64"
        if is_windows_x64():
            return "py3", "none", "win_amd64"
        return super().get_tag()


class BuildPyWithResources(build_py):
    def run(self):
        super().run()
        root = Path(__file__).parent
        destination = Path(self.build_lib) / "smartvoice" / "resources"
        destination.mkdir(parents=True, exist_ok=True)
        for name in ("models.json", "router.json"):
            shutil.copy2(root / "catalog" / name, destination / name)
        shutil.copy2(root / "config" / "smartvoice.example.json", destination / "smartvoice.json")
        if is_apple_silicon():
            make = shutil.which("make")
            clang = shutil.which("clang")
            if not make or not clang:
                raise RuntimeError("Building the Qwen3-TTS Apple Silicon runtime requires Xcode Command Line Tools.")
            native_source = root / "native" / "qwen3-tts"
            jobs = str(max(1, min(os.cpu_count() or 1, 8)))
            build_env = os.environ.copy()
            build_env["MACOSX_DEPLOYMENT_TARGET"] = "11.0"
            arch_flags = ["-march=native", "-mmacosx-version-min=11.0"]
            compiler_macros = subprocess.run(
                [clang, *arch_flags, "-dM", "-E", "-x", "c", "/dev/null"],
                check=True,
                capture_output=True,
                text=True,
                env=build_env,
            ).stdout
            has_bf16 = any(line.startswith("#define __ARM_FEATURE_BF16 ") for line in compiler_macros.splitlines())
            subprocess.run(
                [
                    make, "blas", f"CC={clang}",
                    f"ARCH_FLAGS={' '.join(arch_flags)}",
                    f"KAI_HAS_BF16={int(has_bf16)}",
                    f"-j{jobs}",
                ],
                cwd=native_source,
                check=True,
                env=build_env,
            )
            binary_destination = destination / "bin" / "qwen_tts"
            binary_destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(native_source / "qwen_tts", binary_destination)
            binary_destination.chmod(binary_destination.stat().st_mode | 0o111)
        elif is_windows_x64():
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from scripts.build_qwen3_tts_windows import build_windows_runtime

            build_windows_runtime(destination / "bin")


setup(
    distclass=SmartVoiceDistribution,
    cmdclass={"build_py": BuildPyWithResources, "bdist_wheel": SmartVoiceBdistWheel},
)
