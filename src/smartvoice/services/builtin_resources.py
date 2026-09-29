"""Load built-in JSON from the source tree or the installed package resources."""

from importlib.resources import files
from pathlib import Path


def read_builtin_json(name: str) -> str:
    if Path(name).name != name or not name.endswith(".json"):
        raise ValueError("Built-in resource name must be a JSON filename")
    project_root = Path(__file__).resolve().parents[3]
    source_copy = project_root / "catalog" / name
    if name == "smartvoice.json":
        source_copy = project_root / "config" / "smartvoice.example.json"
    if source_copy.is_file():
        return source_copy.read_text(encoding="utf-8")
    return files("smartvoice").joinpath("resources", name).read_text(encoding="utf-8")
