"""Serve the API or manage local model files from the command line."""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
import time

import uvicorn

from smartvoice.config.settings import Settings
from smartvoice.services.model_catalog import catalog_models, install_model


def _format_model_list(models: list[dict[str, object]]) -> str:
    if not models:
        return "The model catalog is empty."

    noun = "model" if len(models) == 1 else "models"
    lines = [f"SmartVoice model catalog ({len(models)} {noun})"]
    task_groups = (("transcription", "STT"), ("speech", "TTS"))
    for installed, state_title in ((True, "Installed"), (False, "Uninstalled")):
        state_models = [model for model in models if bool(model.get("installed")) is installed]
        lines.extend(["", f"{state_title} ({len(state_models)})"])
        if not state_models:
            lines.append("  (none)")
            continue
        for task, task_title in task_groups:
            task_models = [model for model in state_models if model.get("task") == task]
            if not task_models:
                continue
            lines.extend(["", f"  {task_title} ({len(task_models)})"])
            for model in task_models:
                languages = ", ".join(str(language) for language in model.get("languages", [])) or "Not specified"
                name = model.get("name", "Unnamed model")
                model_id = model.get("id", "Unknown")
                backend = model.get("backend", "Unknown")
                lines.append(f"    - {name} ({model_id}) | {languages} | {backend}")
    return "\n".join(lines)


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _serve(args: list[str]) -> None:
    parser = argparse.ArgumentParser(description="Run the SmartVoice local speech API")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (loopback only in this release)")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port")
    parsed = parser.parse_args(args)
    if not _is_loopback(parsed.host):
        parser.error("Only loopback addresses are supported until remote access has authentication and risk controls.")
    uvicorn.run("smartvoice.app:app", host=parsed.host, port=parsed.port)


def _models(args: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="python -m smartvoice models")
    subparsers = parser.add_subparsers(dest="action", required=True)
    list_parser = subparsers.add_parser("list", help="List catalog entries and installation state")
    list_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    install_parser = subparsers.add_parser("install", help="Download and install a catalog model")
    install_parser.add_argument("model_id")
    parsed = parser.parse_args(args)
    settings = Settings.from_env()
    if parsed.action == "list":
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        models = catalog_models(settings)
        if parsed.json:
            print(json.dumps(models, ensure_ascii=False, indent=2))
        else:
            print(_format_model_list(models))
        return

    last_output = 0.0

    def progress(downloaded: int, total: int | None) -> None:
        nonlocal last_output
        now = time.monotonic()
        if now - last_output < 0.5 and total and downloaded < total:
            return
        if total:
            percent = downloaded * 100 / total
            print(f"\rDownloading {downloaded / 1024**2:.1f}/{total / 1024**2:.1f} MiB ({percent:.1f}%)", end="", flush=True)
        else:
            print(f"\rDownloaded {downloaded / 1024**2:.1f} MiB", end="", flush=True)
        last_output = now

    print("The archive is fetched from its fixed HTTPS catalog URL and checked against the catalog SHA-256.")
    print("The pinned digest was captured from the tested HTTPS archive; review the model license before redistribution.")
    destination = install_model(settings, parsed.model_id, progress)
    print(f"\nInstalled at: {destination}")


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "models":
        _models(args[1:])
    else:
        _serve(args)


if __name__ == "__main__":
    main()
