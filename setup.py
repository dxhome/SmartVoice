"""Copy repository model metadata into wheel package resources at build time."""

from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPyWithResources(build_py):
    def run(self):
        super().run()
        root = Path(__file__).parent
        destination = Path(self.build_lib) / "smartvoice" / "resources"
        destination.mkdir(parents=True, exist_ok=True)
        for name in ("models.json", "router.json"):
            shutil.copy2(root / "catalog" / name, destination / name)
        shutil.copy2(root / "config" / "smartvoice.example.json", destination / "smartvoice.json")


setup(cmdclass={"build_py": BuildPyWithResources})
