"""Serve the API or manage local model files from the command line."""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

import uvicorn

from smartvoice.config.settings import Settings
from smartvoice.domain.errors import SmartVoiceError
from smartvoice.services.model_catalog import (
    ModelDownloadCancelled, catalog_models, clear_default_model, export_model,
    get_model_spec, import_model, install_model, uninstall_model,
    set_default_model,
)


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
                size = model.get("installed_size_bytes")
                size_label = f"{float(size) / 1024**2:.0f} MiB" if isinstance(size, int) else "Not installed"
                default_label = " | Default" if model.get("default") else ""
                status_label = " | Invalid files" if model.get("status") == "invalid" else ""
                lines.append(f"    - {name} ({model_id}) | {languages} | {backend} | {size_label}{default_label}{status_label}")
    return "\n".join(lines)


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _smartvoice_is_running(host: str, port: int) -> bool:
    display_host = f"[{host}]" if ":" in host else host
    try:
        with urllib.request.urlopen(f"http://{display_host}:{port}/health", timeout=0.4) as response:
            return json.loads(response.read().decode("utf-8")).get("status") == "ok"
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return False


def _serve(args: list[str]) -> None:
    parser = argparse.ArgumentParser(description="Run the SmartVoice local speech API")
    parser.add_argument("--config", type=Path, default=None, help="Optional JSON configuration file")
    parser.add_argument("--host", default=None, help="Bind address (loopback only in this release)")
    parser.add_argument("--port", type=int, default=None, help="HTTP port")
    parser.add_argument("--data-dir", type=Path, default=None, help="Override the local SmartVoice data directory")
    parser.add_argument("--num-threads", type=int, default=None, help="Override CPU inference threads")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"), default=None)
    parsed = parser.parse_args(args)
    try:
        settings = Settings.from_env(parsed.config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(f"Invalid configuration: {exc}")
    settings = replace(
        settings,
        server_host=parsed.host or settings.server_host,
        server_port=parsed.port or settings.server_port,
        data_dir=parsed.data_dir.expanduser().resolve() if parsed.data_dir else settings.data_dir,
        num_threads=max(1, parsed.num_threads) if parsed.num_threads else settings.num_threads,
        log_level=parsed.log_level or settings.log_level,
    )
    if not _is_loopback(settings.server_host):
        parser.error("Only loopback addresses are supported until remote access has authentication and risk controls.")
    display_host = f"[{settings.server_host}]" if ":" in settings.server_host else settings.server_host
    address = f"http://{display_host}:{settings.server_port}"
    try:
        with socket.create_connection((settings.server_host, settings.server_port), timeout=0.2):
            try:
                with urllib.request.urlopen(f"{address}/health", timeout=0.5) as response:
                    health = json.loads(response.read().decode("utf-8"))
                if health.get("status") == "ok":
                    parser.error(f"SmartVoice is already running at {address} (version {health.get('version', 'unknown')}).")
                parser.error(f"Port {settings.server_port} is already in use on {settings.server_host}.")
            except (OSError, urllib.error.URLError, json.JSONDecodeError):
                parser.error(f"Port {settings.server_port} is already in use on {settings.server_host}.")
    except OSError:
        pass
    settings.models_dir.mkdir(parents=True, exist_ok=True)
    print(f"SmartVoice data directory: {settings.data_dir}")
    print(f"Model directory: {settings.models_dir}")
    print(f"Starting SmartVoice API at {address} (CPU, {settings.num_threads} inference threads)")
    from smartvoice.app import create_app

    uvicorn.run(
        create_app(settings=settings), host=settings.server_host, port=settings.server_port,
        log_level=settings.log_level.lower(),
    )


def _models(args: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="python -m smartvoice models")
    parser.add_argument("--config", type=Path, default=None, help="Optional JSON configuration file")
    subparsers = parser.add_subparsers(dest="action", required=True)
    list_parser = subparsers.add_parser("list", help="List catalog entries and installation state")
    list_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    install_parser = subparsers.add_parser("install", help="Download and install a catalog model")
    install_parser.add_argument("model_id")
    uninstall_parser = subparsers.add_parser("uninstall", aliases=["remove"], help="Remove an installed model")
    uninstall_parser.add_argument("model_id")
    default_parser = subparsers.add_parser("default", help="Manage default STT and TTS models")
    default_actions = default_parser.add_subparsers(dest="default_action", required=True)
    set_default_parser = default_actions.add_parser("set", help="Set the default model for its task")
    set_default_parser.add_argument("model_id")
    clear_default_parser = default_actions.add_parser("clear", help="Clear a task's default model")
    clear_default_parser.add_argument("task", choices=("transcription", "speech"))
    export_parser = subparsers.add_parser("export", help="Create a portable offline model package")
    export_parser.add_argument("model_id")
    export_parser.add_argument("destination", type=Path)
    import_parser = subparsers.add_parser("import", help="Import and verify a portable offline model package")
    import_parser.add_argument("archive", type=Path)
    parsed = parser.parse_args(args)
    try:
        settings = Settings.from_env(parsed.config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(f"Invalid configuration: {exc}")
    if parsed.action == "list":
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        models = catalog_models(settings)
        if parsed.json:
            print(json.dumps(models, ensure_ascii=False, indent=2))
        else:
            print(_format_model_list(models))
        return

    if parsed.action in {"uninstall", "remove"}:
        if _smartvoice_is_running(settings.server_host, settings.server_port):
            parser.error("Stop the SmartVoice service before uninstalling a model so loaded files are not removed.")
        try:
            size = uninstall_model(settings, parsed.model_id)
        except (OSError, ValueError, SmartVoiceError) as exc:
            parser.error(str(exc))
        print(f"Removed {parsed.model_id}; released {size / 1024**2:.1f} MiB.")
        return
    if parsed.action == "default" and parsed.default_action == "set":
        try:
            defaults = set_default_model(settings, parsed.model_id)
        except (OSError, ValueError, SmartVoiceError) as exc:
            parser.error(str(exc))
        task = get_model_spec(parsed.model_id).task
        print(f"Default model for {task}: {defaults[task]}")
        return
    if parsed.action == "default" and parsed.default_action == "clear":
        try:
            defaults = clear_default_model(settings, parsed.task)
        except (OSError, ValueError, SmartVoiceError) as exc:
            parser.error(str(exc))
        label = defaults.get(parsed.task, "none")
        print(f"Default model for {parsed.task}: {label}")
        return
    if parsed.action == "export":
        try:
            export_model(settings, parsed.model_id, parsed.destination)
        except (OSError, ValueError, SmartVoiceError) as exc:
            parser.error(str(exc))
        print(f"Exported {parsed.model_id} to {parsed.destination}.")
        return
    if parsed.action == "import":
        try:
            destination = import_model(settings, parsed.archive)
        except (OSError, ValueError, SmartVoiceError, json.JSONDecodeError) as exc:
            parser.error(str(exc))
        print(f"Imported model to {destination}.")
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

    spec = get_model_spec(parsed.model_id)
    if spec.file_sources:
        print("Model files are fetched from fixed HTTPS catalog URLs and each file is checked against its catalog SHA-256.")
    else:
        print("The archive is fetched from its fixed HTTPS catalog URL and checked against the catalog SHA-256.")
    print("Review the model license before redistribution.")
    try:
        destination = install_model(settings, parsed.model_id, progress)
    except (OSError, ValueError, SmartVoiceError, ModelDownloadCancelled) as exc:
        parser.error(str(exc))
    print(f"\nInstalled at: {destination}")


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "models":
        _models(args[1:])
    else:
        _serve(args)


if __name__ == "__main__":
    main()
