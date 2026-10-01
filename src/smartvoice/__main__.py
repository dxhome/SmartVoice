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
from smartvoice.adapters.storage.catalog_model_repository import CatalogModelRepository
from smartvoice.domain.errors import SmartVoiceError
from smartvoice.services.model_download import ModelDownloadCancelled
from smartvoice.services.model_jobs import ModelJobManager
from smartvoice.services.model_management import ModelManagementService


def _format_model_list(
    models: list[dict[str, object]],
    native_models: list[dict[str, object]] | None = None,
) -> str:
    native_models = native_models or []
    all_models = [*models, *native_models]
    if not all_models:
        return "The model catalog is empty."

    noun = "model" if len(all_models) == 1 else "models"
    lines = [f"SmartVoice models ({len(all_models)} {noun})"]
    task_groups = (("transcription", "STT"), ("speech", "TTS"), ("native", "SmartVoice native"))
    for is_installed, state_title in (
        (True, "Installed"),
        (False, "Not installed"),
    ):
        state_models = [
            model for model in all_models
            if model.get("status", model.get("availability", "available" if model.get("installed") else "not_installed"))
            in ({"available", "unavailable"} if is_installed else {"not_installed"})
        ]
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
                if isinstance(size, int):
                    size_label = f"{float(size) / 1024**2:.0f} MiB on disk"
                else:
                    estimate = model.get("estimated_size_bytes")
                    size_label = f"~{float(estimate) / 1024**2:.0f} MiB estimated" if isinstance(estimate, int) else "Size unknown"
                status = model.get("status", model.get("availability", "available" if model.get("installed") else "not_installed"))
                if status == "unavailable":
                    status_label = f" | Unavailable ({model.get('availability_reason') or 'The active inference runtime cannot use this model.'})"
                elif status == "available":
                    status_label = " | Available"
                else:
                    status_label = " | Not installed"
                lines.append(f"    - {name} ({model_id}) | {languages} | {backend} | {size_label}{status_label}")
    return "\n".join(lines)


def _models_with_availability(
    models: list[dict[str, object]],
    runtime_by_backend: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    results = []
    for original in models:
        model = dict(original)
        if model.get("status") == "uninstalled":
            model["availability"] = "not_installed"
        elif model.get("status") == "invalid":
            model["availability"] = "unavailable"
            model["availability_reason"] = "Model files are incomplete or fail integrity checks. Reinstall this model."
        elif model.get("status") == "installed" and not runtime_by_backend.get(
            str(model.get("backend")), {}
        ).get("reason"):
            model["availability"] = "available"
        else:
            model["availability"] = "unavailable"
            runtime = runtime_by_backend.get(str(model.get("backend")), {})
            model["availability_reason"] = runtime.get("reason") or (
                "The model files or required inference runtime failed verification."
            )
        model["status"] = model.pop("availability")
        model.pop("installed", None)
        results.append(model)
    return results


def _native_model_status(settings: Settings, sherpa_runtime: dict[str, object]) -> dict[str, object]:
    from smartvoice.services.spoken_language_identifier import (
        installed_language_id_model_dir,
        language_id_model_dir,
    )

    directory = language_id_model_dir(settings)
    installed = installed_language_id_model_dir(settings)
    runtime_issue = sherpa_runtime.get("reason")
    if installed is not None and runtime_issue is None:
        availability = "available"
        reason = None
    elif installed is not None:
        availability = "unavailable"
        reason = runtime_issue
    elif directory.exists():
        availability = "unavailable"
        reason = "Whisper Tiny files are incomplete or fail integrity checks. Run models install-language-id to repair them."
    else:
        availability = "not_installed"
        reason = None
    return {
        "id": "sherpa-onnx-whisper-tiny-int8-language-id",
        "name": "Whisper Tiny (spoken-language identification)",
        "task": "native",
        "languages": ["multilingual"],
        "backend": "SmartVoice native",
        "status": availability,
        "availability_reason": reason,
        "installed_size_bytes": (
            sum((installed / filename).stat().st_size for filename in (
                "tiny-encoder.int8.onnx", "tiny-decoder.int8.onnx", "tiny-tokens.txt"
            ))
            if installed else None
        ),
    }


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _print_startup_banner(address: str, settings: Settings, debug: bool) -> None:
    art_path = Path(__file__).with_name("ascii-art.txt")
    source_lines = art_path.read_text(encoding="utf-8").strip("\n").splitlines()
    scale = 1.0
    source_width = max((len(line) for line in source_lines), default=0)
    art_width = max(1, round(source_width * scale))
    art_height = max(1, round(len(source_lines) * scale))
    art_lines = []
    for row in range(art_height):
        source_line = source_lines[min(len(source_lines) - 1, int(row / scale))].ljust(source_width)
        art_lines.append("".join(
            source_line[min(source_width - 1, int(column / scale))]
            for column in range(art_width)
        ).rstrip())
    art_width = max((len(line) for line in art_lines), default=0)
    border = f"+{'-' * (art_width + 2)}+"
    print(border)
    for line in art_lines:
        print(f"| {line.ljust(art_width)} |")
    print(border)
    debug_label = " | DEBUG MODE" if debug else ""
    print(f"Starting SmartVoice API at {address} (CPU, {settings.num_threads} inference threads{debug_label})")
    print(f"Docs: {address}/docs | Test: {address}/test")


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
    parser.add_argument("--debug", action="store_true", help="Log HTTP request/response headers and bodies and enable DEBUG logging")
    parsed = parser.parse_args(args)
    try:
        settings = Settings.from_env(parsed.config, parsed.data_dir, initialize_user_config=True)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(f"Invalid configuration: {exc}")
    settings = replace(
        settings,
        server_host=parsed.host or settings.server_host,
        server_port=parsed.port or settings.server_port,
        data_dir=settings.data_dir,
        num_threads=max(1, parsed.num_threads) if parsed.num_threads else settings.num_threads,
        log_level="DEBUG" if parsed.debug else settings.log_level,
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
    _print_startup_banner(address, settings, parsed.debug)
    print(f"SmartVoice data directory: {settings.data_dir}")
    print(f"Model directory: {settings.models_dir}")
    from smartvoice.app import create_app

    uvicorn.run(
        create_app(settings=settings, debug_http=parsed.debug), host=settings.server_host, port=settings.server_port,
        log_level=settings.log_level.lower(),
    )


def _models(args: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="python -m smartvoice models")
    parser.add_argument("--config", type=Path, default=None, help="Optional JSON configuration file")
    subparsers = parser.add_subparsers(dest="action", required=True)
    list_parser = subparsers.add_parser("list", help="List catalog entries and installation state")
    list_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    install_parser = subparsers.add_parser("install", help="Download and install a catalog model")
    install_parser.add_argument("model_id", help="Catalog model ID, or 'all' to install every uninstalled catalog model")
    install_parser.add_argument("--source", help="Optional HTTPS base URL for a Hugging Face-compatible model mirror")
    uninstall_parser = subparsers.add_parser("uninstall", help="Remove an installed model")
    uninstall_parser.add_argument("model_id")
    export_parser = subparsers.add_parser("export", help="Create a portable offline model package")
    export_parser.add_argument("model_id")
    export_parser.add_argument("destination", type=Path)
    import_parser = subparsers.add_parser("import", help="Import and verify a portable offline model package")
    import_parser.add_argument("archive", type=Path)
    subparsers.add_parser("install-language-id", help="Install the Whisper Tiny spoken-language detection assets")
    parsed = parser.parse_args(args)
    try:
        settings = Settings.from_env(parsed.config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(f"Invalid configuration: {exc}")
    model_management = ModelManagementService(
        ModelJobManager(settings), CatalogModelRepository(settings)
    )
    last_output = 0.0

    def progress(downloaded: int, total: int | None) -> None:
        nonlocal last_output
        now = time.monotonic()
        if now - last_output < 0.5 and total and downloaded < total:
            return
        if total:
            percent = downloaded * 100 / total
            end = "\n" if downloaded >= total else ""
            print(f"\rDownloading {downloaded / 1024**2:.1f}/{total / 1024**2:.1f} MiB ({percent:.1f}%)", end=end, flush=True)
        else:
            print(f"\rDownloaded {downloaded / 1024**2:.1f} MiB", end="", flush=True)
        last_output = now

    def status(message: str) -> None:
        print(f"\n{message}", flush=True)

    if parsed.action == "list":
        from smartvoice.adapters.inference.factory import create_inference_provider
        from smartvoice.services.model_storage import catalog_models

        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        inference_provider = create_inference_provider(settings, model_management.model_repository)
        backend_runtimes = inference_provider.runtime().get("backends", {})
        if not isinstance(backend_runtimes, dict):
            backend_runtimes = {}
        models = _models_with_availability(catalog_models(settings), backend_runtimes)
        native_models = [_native_model_status(settings, backend_runtimes.get("sherpa-onnx", {}))]
        if parsed.json:
            print(json.dumps([*models, *native_models], ensure_ascii=False, indent=2))
        else:
            print(_format_model_list(models, native_models))
        return

    if parsed.action == "install-language-id":
        from smartvoice.services.spoken_language_identifier import ensure_language_id_model

        try:
            destination = ensure_language_id_model(settings, progress, status)
        except (OSError, ValueError, SmartVoiceError) as exc:
            parser.error(str(exc))
        print(f"\nInstalled spoken-language detection assets at: {destination}")
        return

    if parsed.action == "uninstall":
        if _smartvoice_is_running(settings.server_host, settings.server_port):
            parser.error("Stop the SmartVoice service before uninstalling a model so loaded files are not removed.")
        try:
            result = model_management.uninstall(parsed.model_id)
        except (OSError, ValueError, SmartVoiceError) as exc:
            parser.error(str(exc))
        size = int(result["removed_bytes"])
        print(f"Removed {parsed.model_id}; released {size / 1024**2:.1f} MiB.")
        return
    if parsed.action == "export":
        try:
            model_management.export(parsed.model_id, parsed.destination)
        except (OSError, ValueError, SmartVoiceError) as exc:
            parser.error(str(exc))
        print(f"Exported {parsed.model_id} to {parsed.destination}.")
        return
    if parsed.action == "import":
        try:
            result = model_management.import_archive(parsed.archive)
        except (OSError, ValueError, SmartVoiceError, json.JSONDecodeError) as exc:
            parser.error(str(exc))
        print(f"Imported model {result['id']}.")
        return

    if parsed.model_id == "all":
        if parsed.source:
            parser.error("--source cannot be used with 'install all'; each model uses its catalog source.")
        catalog = model_management.catalog()["data"]
        pending = [model for model in catalog if model.get("status") == "uninstalled"]
        invalid = [model for model in catalog if model.get("status") == "invalid"]
        if invalid:
            print(
                "Skipping models with existing invalid files: "
                + ", ".join(str(model.get("id")) for model in invalid)
                + ". Remove them with 'models uninstall <model-id>' before retrying."
            )
        if not pending:
            print("No uninstalled catalog models to install.")
        else:
            print(f"Installing {len(pending)} uninstalled catalog models.")
        model_ids = [str(model["id"]) for model in pending]
    else:
        model_ids = [parsed.model_id]

    for model_id in model_ids:
        last_output = 0.0
        try:
            spec = model_management.get_spec(model_id)
            source_label = parsed.source or "catalog source"
            if parsed.model_id == "all":
                print(f"\nInstalling {spec.name} ({spec.id})")
            if spec.file_sources:
                print(f"Model files are fetched from the {source_label}; each file is checked against its catalog SHA-256.")
            else:
                print(f"The archive is fetched from the {source_label} and checked against its catalog SHA-256.")
            print("Review the model license before redistribution.")
            destination = model_management.install(model_id, progress, source=parsed.source)
        except (OSError, ValueError, SmartVoiceError, ModelDownloadCancelled) as exc:
            parser.error(f"Failed to install {model_id}: {exc}")
        print(f"\nInstalled at: {destination}")

    if parsed.model_id == "all":
        from smartvoice.services.spoken_language_identifier import ensure_language_id_model

        print("\nEnsuring Whisper Tiny is installed for automatic language identification.")
        try:
            destination = ensure_language_id_model(settings, progress, status)
        except (OSError, ValueError) as exc:
            parser.error(f"Failed to install Whisper Tiny language-identification assets: {exc}")
        print(f"\nWhisper Tiny language-identification assets ready at: {destination}")


def _router(args: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="python -m smartvoice router")
    parser.add_argument("--config", type=Path, default=None, help="Optional JSON configuration file")
    parser.add_argument("--host", default=None, help="Running service bind address")
    parser.add_argument("--port", type=int, default=None, help="Running service port")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("reload", help="Reload router.json in the running SmartVoice service")
    parsed = parser.parse_args(args)
    try:
        settings = Settings.from_env(parsed.config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(f"Invalid configuration: {exc}")
    host = parsed.host or settings.server_host
    port = parsed.port or settings.server_port
    if not _is_loopback(host):
        parser.error("Router reload is restricted to a local SmartVoice service.")
    display_host = f"[{host}]" if ":" in host else host
    address = f"http://{display_host}:{port}/v1/router/reload"
    try:
        request = urllib.request.Request(address, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            message = payload.get("error", {}).get("message", payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            message = exc.reason
        parser.error(f"Router reload failed: {message}")
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        parser.error(f"Could not reload router configuration from the running service at {address}: {exc}")
    print(f"Router configuration reloaded (sha256 {payload.get('sha256', 'unknown')}).")


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "models":
        _models(args[1:])
    elif args and args[0] == "router":
        _router(args[1:])
    else:
        _serve(args)


if __name__ == "__main__":
    main()
