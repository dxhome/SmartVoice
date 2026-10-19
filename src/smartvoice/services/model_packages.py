"""Resolve install packages from the active router and streaming plan."""

from __future__ import annotations

from smartvoice.services.model_registry import load_catalog
from smartvoice.services.spoken_language_identifier import (
    LANGUAGE_ID_RESOURCE_ID,
    LANGUAGE_ID_ESTIMATED_SIZE_BYTES,
    installed_language_id_model_dir,
)


PACKAGE_ORDER = ("static-speech", "streaming", "all")
PACKAGE_NAMES = {
    "static-speech": "Static speech (STT + TTS)",
    "streaming": "Streaming speech",
    "all": "All models",
}
PACKAGE_DESCRIPTIONS = {
    "static-speech": "Chinese and English static transcription and speech synthesis using the active router's first-choice models.",
    "streaming": "Chinese and English streaming transcription, translated subtitles, and spoken interpretation.",
    "all": "Every catalog model plus automatic spoken-language identification assets.",
}


def _streaming_model_ids(repository) -> set[str]:
    # Use the same planner that validates live sessions so package requirements
    # follow its default models and fallback dependencies.
    from smartvoice.services.streaming.planning import resolve

    scenarios = (
        ("transcription", "zh", None),
        ("transcription", "en", None),
        ("translated_subtitles", "zh", "en"),
        ("translated_subtitles", "en", "zh"),
        ("spoken_interpretation", "zh", "en"),
        ("spoken_interpretation", "en", "zh"),
    )
    result: set[str] = set()
    for mode, source, target in scenarios:
        config = {
            "type": "configure",
            "protocol": "smartvoice.stream.v1",
            "mode": mode,
            "source_language": source,
            "audio": {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1},
        }
        if target is not None:
            config["target_language"] = target
        plan, _specs, _resident = resolve(config, repository, threads=1)
        result.update(plan.model_ids)
    return result


def package_model_ids(package_id: str, repository, model_router) -> set[str]:
    specs = load_catalog()
    catalog_ids = {spec.id for spec in specs}
    if package_id == "all":
        return catalog_ids
    if package_id == "static-speech":
        config = model_router.snapshot()
        result: set[str] = set()
        for task in ("transcription", "speech"):
            for language in ("zh", "en"):
                candidates = config.tasks.get(task, {}).get(language, ())
                if candidates:
                    result.add(candidates[0])
        if not result.issubset(catalog_ids):
            raise RuntimeError("Active router references a model outside the catalog")
        return result
    if package_id == "streaming":
        return _streaming_model_ids(repository)
    raise KeyError(package_id)


def resolve_packages(repository, model_router, settings) -> list[dict[str, object]]:
    catalog = {str(item["id"]): item for item in repository.catalog_models()}
    installed = {model_id for model_id, item in catalog.items() if item.get("status") == "installed"}
    language_id_dir = installed_language_id_model_dir(settings)
    language_id_installed = language_id_dir is not None
    language_id_size = (
        sum((language_id_dir / name).stat().st_size for name in (
            "tiny-encoder.int8.onnx", "tiny-decoder.int8.onnx", "tiny-tokens.txt"
        ))
        if language_id_dir else None
    )
    results = []
    for package_id in PACKAGE_ORDER:
        model_ids = package_model_ids(package_id, repository, model_router)
        if package_id == "all":
            model_ids |= set(catalog)
        assets = []
        for model_id in sorted(model_ids):
            model = catalog[model_id]
            assets.append({
                "id": model_id,
                "name": model.get("name", model_id),
                "kind": "model",
                "status": model.get("status", "uninstalled"),
                "installed": model_id in installed,
                "estimated_size_bytes": model.get("estimated_size_bytes"),
                "estimated_download_size_bytes": model.get("estimated_download_size_bytes"),
                "estimated_download_size_complete": model.get("estimated_download_size_complete", False),
                "installed_size_bytes": model.get("installed_size_bytes"),
                "installation_method": model.get("installation_method", "download"),
            })
        if package_id in {"static-speech", "all"}:
            assets.append({
                "id": LANGUAGE_ID_RESOURCE_ID,
                "name": "Whisper Tiny spoken-language identification",
                "kind": "resource",
                "status": "installed" if language_id_installed else "uninstalled",
                "installed": language_id_installed,
                "estimated_size_bytes": LANGUAGE_ID_ESTIMATED_SIZE_BYTES,
                "estimated_download_size_bytes": LANGUAGE_ID_ESTIMATED_SIZE_BYTES,
                "estimated_download_size_complete": True,
                "installed_size_bytes": language_id_size,
                "installation_method": "download",
            })
        # Package estimates are for network transfer. Existing model-size
        # metadata remains the approximate installed payload size.
        download_estimates = [asset["estimated_download_size_bytes"] for asset in assets]
        estimate_complete = all(
            isinstance(size, int) and size >= 0 and asset["estimated_download_size_complete"]
            for size, asset in zip(download_estimates, assets)
        )
        estimated_size = sum(int(size) for size in download_estimates if isinstance(size, int))
        installed_estimates = [asset["estimated_size_bytes"] for asset in assets]
        installed_estimate = (
            sum(int(size) for size in installed_estimates)
            if all(isinstance(size, int) and size >= 0 for size in installed_estimates)
            else None
        )
        installed_size = sum(
            int(asset["installed_size_bytes"] or 0)
            for asset in assets if asset["installed"]
        ) if all(asset["installed"] for asset in assets) else None
        missing = [asset["id"] for asset in assets if not asset["installed"]]
        invalid = [asset["id"] for asset in assets if asset["status"] == "invalid"]
        status = "installed" if not missing else "invalid" if invalid else "partial" if any(asset["installed"] for asset in assets) else "not_installed"
        results.append({
            "id": package_id,
            "name": PACKAGE_NAMES[package_id],
            "description": PACKAGE_DESCRIPTIONS[package_id],
            "status": status,
            "estimated_size_bytes": estimated_size,
            "estimated_size_complete": estimate_complete,
            "estimated_size_basis": "download",
            "estimated_installed_size_bytes": installed_estimate,
            "installed_size_bytes": installed_size,
            "asset_count": len(assets),
            "model_ids": sorted(model_ids),
            "assets": assets,
            "missing_assets": missing,
        })
    return results
