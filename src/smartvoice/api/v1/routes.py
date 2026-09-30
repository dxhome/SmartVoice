"""Version 1 OpenAI-style audio and local model management routes."""

from __future__ import annotations

import json
import asyncio
import re
import time
import uuid
from typing import Literal

from fastapi import APIRouter, File, Form, Query, Request, UploadFile, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.concurrency import run_in_threadpool
from starlette.background import BackgroundTask
from pydantic import BaseModel, ConfigDict, Field, field_validator

from smartvoice.domain.errors import (
    AudioTooLargeError, InvalidRequestError, PayloadTooLargeError, UnsupportedFeatureError,
)
from smartvoice.services.model_router import ROUTER_MODEL_IDS
from smartvoice.services.model_catalog_constants import MAX_ARCHIVE_BYTES
from smartvoice.services.model_registry import supported_language_codes

router = APIRouter(prefix="/v1")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    input: str = Field(min_length=1, max_length=4000)
    voice: str = "default"
    language: str | None = None
    response_format: str = "wav"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)

    @field_validator("input")
    @classmethod
    def input_must_contain_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("input must contain non-whitespace text")
        return value


def get_request_id(request: Request) -> str:
    value = request.headers.get("x-request-id", "")
    return value if REQUEST_ID_RE.fullmatch(value) else str(uuid.uuid4())


def get_provider(request: Request):
    return request.app.state.provider


def _available_models_for_api(request: Request) -> list[dict[str, object]]:
    """List only models that the active inference adapters can currently use."""
    return list(get_provider(request).installed_models())


def _router_model(requested_model: str | None, task: str) -> str:
    virtual_id = ROUTER_MODEL_IDS[task]
    if requested_model in {None, virtual_id}:
        return virtual_id
    return requested_model


async def _run_request_inference(request: Request, operation):
    settings = request.app.state.settings
    task = asyncio.create_task(request.app.state.inference_queue.run(
        operation,
        settings.inference_queue_timeout_seconds,
        settings.inference_execution_timeout_seconds,
    ))
    while True:
        done, _ = await asyncio.wait({task}, timeout=0.2)
        if done:
            return task.result()
        if await request.is_disconnected():
            task.cancel()
            raise asyncio.CancelledError


@router.get("/capabilities", tags=["runtime"])
async def capabilities(
    request: Request,
    task: Literal["transcription", "speech"] | None = Query(
        default=None,
        description="Filter capabilities by task. Use transcription for STT or speech for TTS.",
    ),
) -> dict[str, object]:
    if task not in {None, "transcription", "speech"}:
        raise UnsupportedFeatureError(f"Task {task!r} is not supported.")
    result = get_provider(request).capabilities()
    language_identifier = request.app.state.language_identifier_status
    if language_identifier is None:
        result["language_identification"] = {
            "available": False,
            "reason": "unsupported_by_active_provider",
        }
    else:
        language_id_available = language_identifier.language_identification_available()
        result["language_identification"] = {
            "available": language_id_available,
            "reason": None if language_id_available else "optional_model_not_installed",
            "model": "sherpa-onnx-whisper-tiny-int8-language-id",
            "install_command": None if language_id_available else "python -m smartvoice models install-language-id",
        }
    result["router"] = request.app.state.model_router.public_status(get_provider(request).installed_models())
    if task is not None:
        result = {
            **result,
            "tasks": [
                item for item in result.get("tasks", [])
                if (item.get("task") if isinstance(item, dict) else item) == task
            ],
        }
    return result


@router.get("/runtime", tags=["runtime"])
async def runtime(request: Request) -> dict[str, object]:
    result = get_provider(request).runtime()
    result["router"] = request.app.state.model_router.public_status(get_provider(request).installed_models())
    return result


@router.get("/models", tags=["models"])
async def models(request: Request) -> dict[str, object]:
    available = _available_models_for_api(request)
    data = [_openai_model_object(model) for model in available]
    available_tasks = [
        task for task in ("transcription", "speech")
        if any(model.get("task") == task for model in available)
    ]
    if available_tasks:
        data.insert(0, _virtual_model_object(ROUTER_MODEL_IDS["transcription"], available_tasks))
    task_order = {"transcription": 0, "speech": 1}
    data[1:] = sorted(data[1:], key=lambda model: (
        task_order.get(str(model.get("task")), 2),
        str(model.get("name") or model["id"]).casefold(),
        str(model["id"]).casefold(),
    ))
    return {"object": "list", "data": data}


def _openai_model_object(model: dict[str, object]) -> dict[str, object]:
    """Map local model metadata to the OpenAI model object shape plus extensions."""
    return {
        "id": str(model["id"]),
        "object": "model",
        # The local catalog does not track publication timestamps.
        "created": 0,
        "owned_by": "smartvoice",
        "availability": "available",
        **{key: value for key, value in model.items() if key not in {"id", "installed"}},
    }


def _virtual_model_object(model_id: str, tasks: list[str] | None = None) -> dict[str, object]:
    return {
        "id": model_id,
        "object": "model",
        "created": 0,
        "owned_by": "smartvoice",
        "name": "SmartVoice Auto",
        "tasks": tasks or ["transcription", "speech"],
        "virtual": True,
    }


@router.get("/models/{model_id}", tags=["models"])
async def retrieve_model(request: Request, model_id: str) -> dict[str, object]:
    if model_id == ROUTER_MODEL_IDS["transcription"]:
        available = _available_models_for_api(request)
        tasks = [
            task for task in ("transcription", "speech")
            if any(model.get("task") == task for model in available)
        ]
        if tasks:
            return _virtual_model_object(model_id, tasks)
        raise HTTPException(status_code=404, detail=f"Model {model_id!r} is unavailable because no task has an available route.")
    model = next(
        (item for item in _available_models_for_api(request) if item.get("id") == model_id),
        None,
    )
    if model is None:
        raise HTTPException(status_code=404, detail=f"Model {model_id!r} is not installed or unavailable for inference.")
    return _openai_model_object(model)


@router.post("/router/reload", tags=["runtime"])
async def reload_router(request: Request) -> dict[str, object]:
    return request.app.state.model_router.reload()


@router.get("/catalog", tags=["models"])
async def catalog(
    request: Request,
    task: Literal["transcription", "speech"] | None = Query(
        default=None,
        description="Filter catalog models by task. Use transcription for STT or speech for TTS.",
    ),
) -> dict[str, object]:
    if task not in {None, "transcription", "speech"}:
        raise UnsupportedFeatureError(f"Task {task!r} is not supported.")
    return request.app.state.model_management.catalog(task)


@router.post("/models/{model_id}/download", status_code=202, tags=["models"])
async def download_model(request: Request, model_id: str) -> dict[str, object]:
    return request.app.state.model_management.start_download(model_id)


@router.get("/jobs/{job_id}", tags=["models"])
async def model_job(request: Request, job_id: str) -> dict[str, object]:
    return request.app.state.model_management.get_job(job_id)


@router.delete("/jobs/{job_id}", tags=["models"])
async def cancel_model_job(request: Request, job_id: str) -> dict[str, object]:
    return request.app.state.model_management.cancel_job(job_id)


@router.delete("/models/{model_id}", tags=["models"])
async def uninstall_model_route(request: Request, model_id: str) -> dict[str, object]:
    result, _queue_wait = await request.app.state.inference_queue.run(
        lambda: request.app.state.model_management.uninstall(model_id),
        request.app.state.settings.inference_queue_timeout_seconds,
        request.app.state.settings.inference_execution_timeout_seconds,
    )
    return result


@router.get("/models/{model_id}/export", tags=["models"])
async def export_model_route(request: Request, model_id: str) -> FileResponse:
    from tempfile import NamedTemporaryFile

    temporary_dir = request.app.state.settings.data_dir / "tmp"
    temporary_dir.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(prefix="smartvoice-model-", suffix=".zip", dir=temporary_dir, delete=False) as temporary:
        archive_path = __import__("pathlib").Path(temporary.name)
    try:
        await run_in_threadpool(request.app.state.model_management.export, model_id, archive_path)
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise
    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename=f"{model_id}.smartvoice.zip",
        background=BackgroundTask(archive_path.unlink, missing_ok=True),
    )


@router.post("/models/import", status_code=201, tags=["models"])
async def import_model_route(request: Request, file: UploadFile = File(...)) -> dict[str, str]:
    from pathlib import Path
    from tempfile import NamedTemporaryFile

    temporary_path: Path | None = None
    try:
        temporary_dir = request.app.state.settings.data_dir / "tmp"
        temporary_dir.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(prefix="smartvoice-import-", suffix=".zip", dir=temporary_dir, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            total = 0
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise PayloadTooLargeError("Offline model package exceeds the 3 GiB import limit.")
                temporary.write(chunk)
        return await run_in_threadpool(request.app.state.model_management.import_archive, temporary_path)
    finally:
        await file.close()
        if temporary_path:
            temporary_path.unlink(missing_ok=True)


@router.post("/audio/transcriptions", tags=["audio"])
async def transcriptions(
    request: Request,
    file: UploadFile = File(...),
    model: str = Form(...),
    language: str | None = Form(default=None),
    response_format: str = Form(default="json"),
    timestamps: bool = Form(default=False),
) -> Response:
    settings = request.app.state.settings
    try:
        form = await request.form()
        unexpected_fields = sorted(set(form.keys()) - {"file", "model", "language", "response_format", "timestamps"})
        if unexpected_fields:
            raise UnsupportedFeatureError(f"Unsupported transcription field(s): {', '.join(unexpected_fields)}")
        if response_format not in {"json", "text", "verbose_json"}:
            raise UnsupportedFeatureError(f"Transcription response format {response_format!r} is not supported.")
        requested_model = _router_model(model, "transcription")
        routed = requested_model == ROUTER_MODEL_IDS["transcription"]
        router_config = request.app.state.model_router.snapshot() if routed else None
        if language not in {None, "", "auto", *supported_language_codes("transcription")}:
            raise UnsupportedFeatureError(f"Unsupported STT language code: {language!r}.")
        requested_language = language or "auto"
        request.state.model_id = requested_model
        request.state.model_mode = "router" if routed else "direct"
        request.state.router_sha256 = router_config.digest if router_config else None
        request.state.route_language = requested_language
        request.state.actual_device = "cpu"
        raw = await file.read(settings.max_upload_bytes + 1)
    finally:
        await file.close()
    if len(raw) > settings.max_upload_bytes:
        raise AudioTooLargeError("Audio upload exceeds the configured size limit.")
    inference_started = time.perf_counter()
    route_candidates: list[dict[str, object]] = []
    def transcribe_request():
        outcome = request.app.state.transcription_service.execute(
            raw, requested_model, requested_language, router_config,
        )
        request.state.model_id = outcome.model_id
        request.state.route_language = outcome.language or "auto"
        request.state.route_candidates = list(outcome.candidates)
        return outcome.result, outcome.model_id, outcome.language, list(outcome.candidates), outcome.language_source

    (result, actual_model, resolved_language, route_candidates, resolved_language_source), queue_wait = await _run_request_inference(request, transcribe_request)
    result["request_processing_seconds"] = round(time.perf_counter() - inference_started - queue_wait, 4)
    result["queue_wait_seconds"] = round(queue_wait, 4)
    result["runtime_wait_seconds"] = round(float(result.get("runtime_wait_seconds") or 0.0), 4)
    result["requested_model"] = requested_model
    result["model_mode"] = "router" if routed else "direct"
    result["language_source"] = resolved_language_source if resolved_language else "undetermined"
    result["router_sha256"] = router_config.digest if router_config else None
    result["route_candidates"] = route_candidates if routed else None
    request.state.model_id = str(result.get("model", actual_model))
    request.state.actual_device = str(result.get("device", "unknown"))
    if response_format == "text":
        return Response(content=str(result["text"]), media_type="text/plain; charset=utf-8", headers={
            "X-Model-Id": actual_model, "X-Requested-Model": requested_model,
            "X-Model-Mode": "router" if routed else "direct",
            "X-Resolved-Language": resolved_language or "unknown",
            "X-Language-Source": result["language_source"],
            "X-Route-Candidates": ",".join(
                f"{candidate['model']}:{'installed' if candidate['installed'] else 'uninstalled'}"
                for candidate in route_candidates
            ) if routed else "not_applicable",
        })
    if response_format == "json" or not timestamps:
        result.pop("segments", None)
    return Response(content=json.dumps(result, ensure_ascii=False), media_type="application/json")


@router.post("/audio/speech", tags=["audio"])
async def speech(request: Request, payload: SpeechRequest) -> Response:
    if payload.response_format != "wav":
        raise UnsupportedFeatureError(f"Speech response format {payload.response_format!r} is not supported; use 'wav'.")
    requested_model = _router_model(payload.model, "speech")
    routed = requested_model == ROUTER_MODEL_IDS["speech"]
    router_config = request.app.state.model_router.snapshot() if routed else None
    requested_language = payload.language or "auto"
    request.state.model_id = requested_model
    request.state.model_mode = "router" if routed else "direct"
    request.state.router_sha256 = router_config.digest if router_config else None
    request.state.route_language = requested_language
    if len(payload.input) > request.app.state.settings.max_tts_characters:
        raise InvalidRequestError(
            f"Text exceeds the {request.app.state.settings.max_tts_characters} character limit; split it into shorter requests."
        )
    request.state.route_language = requested_language
    inference_started = time.perf_counter()
    def synthesize_selected():
        return request.app.state.speech_service.execute(
            payload.input, requested_model, requested_language, payload.voice, payload.speed, router_config,
        )

    outcome, queue_wait = await _run_request_inference(request, synthesize_selected)
    model = outcome.model_id
    requested_language = outcome.language
    route_candidates = list(outcome.candidates)
    request.state.model_id = model
    request.state.model_mode = "router" if routed else "direct"
    request.state.router_sha256 = router_config.digest if router_config else None
    request.state.route_language = requested_language
    request.state.route_candidates = route_candidates
    request.state.actual_device = "cpu"
    audio, sample_rate, duration = outcome.audio, outcome.sample_rate, outcome.duration
    inference_seconds = max(0.0, time.perf_counter() - inference_started - queue_wait)
    return StreamingResponse(
        iter([audio]),
        media_type="audio/wav",
        headers={
            "X-Audio-Sample-Rate": str(sample_rate),
            "X-Audio-Duration": f"{duration:.3f}",
            "X-Model-Id": model,
            "X-Requested-Model": requested_model,
            "X-Model-Mode": "router" if routed else "direct",
            "X-Inference-Time-Seconds": f"{inference_seconds:.4f}",
            "X-Queue-Wait-Seconds": f"{queue_wait:.4f}",
            "X-Runtime-Wait-Seconds": f"{outcome.runtime_wait_seconds:.4f}",
            "X-Real-Time-Factor": f"{inference_seconds / duration:.4f}" if duration else "0",
            "X-Requested-Language": payload.language or "auto",
            "X-Resolved-Language": requested_language,
            "X-Language-Source": outcome.language_source,
            "X-Language-Confidence": f"{outcome.language_confidence:.3f}" if outcome.language_confidence is not None else "not_applicable",
            "X-Text-Language": outcome.text_language or (requested_language if requested_language != "auto" else "mixed_or_undetermined"),
            "X-Router-SHA256": router_config.digest if router_config else "not_applicable",
            "X-Route-Candidates": ",".join(
                f"{candidate['model']}:{'installed' if candidate['installed'] else 'uninstalled'}"
                for candidate in route_candidates
            ) if routed else "not_applicable",
        },
    )
