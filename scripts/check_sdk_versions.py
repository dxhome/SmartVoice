"""Ensure published SDK metadata matches the SmartVoice product release."""

from __future__ import annotations

import json
import runpy
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
product_version = runpy.run_path(str(ROOT / "src/smartvoice/_version.py"))["__version__"]
python_project = tomllib.loads((ROOT / "sdk/python/pyproject.toml").read_text())
typescript_package = json.loads((ROOT / "sdk/typescript/package.json").read_text())
typescript_lock = json.loads((ROOT / "sdk/typescript/package-lock.json").read_text())

versions = {
    "SmartVoice product": product_version,
    "Python SDK": python_project["project"]["version"],
    "TypeScript SDK": typescript_package["version"],
    "TypeScript lockfile root": typescript_lock["packages"][""]["version"],
}
for name, version in versions.items():
    if version != product_version:
        raise SystemExit(f"{name} version {version} does not match product version {product_version}")

descriptions = [
    python_project["project"].get("description", ""),
    typescript_package.get("description", ""),
]
if any(term in description.lower() for description in descriptions for term in ("prototype", "experimental")):
    raise SystemExit("SDK package descriptions must not label the SDK as a prototype or experimental")

print(f"SDK package versions match SmartVoice {product_version}")
