"""Version 1 OpenAI-style audio and local model management routes."""

from __future__ import annotations

import json
import asyncio
import re
import time
import uuid
from typing import Literal

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.concurrency import run_in_threadpool
from starlette.background import BackgroundTask
from pydantic import BaseModel, ConfigDict, Field, field_validator

from smartvoice.domain.errors import AudioTooLargeError, InvalidRequestError, ModelUnavailableError, PayloadTooLargeError
from smartvoice.services.model_catalog import (
    catalog_models, clear_model_default, default_model_ids, export_model, import_model,
    model_storage, set_default_model, uninstall_model,
)

router = APIRouter(prefix="/v1")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    input: str = Field(min_length=1, max_length=4000)
    voice: str = "default"
    language: str | None = None
    response_format: Literal["wav"] = "wav"
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


def select_installed_model(request: Request, task: str, requested_model: str | None) -> str:
    task_models = [
        str(model["id"])
        for model in get_provider(request).installed_models()
        if model.get("task") == task
    ]
    if requested_model:
        catalog_ids = {str(model["id"]): str(model["task"]) for model in catalog_models(request.app.state.settings)}
        if requested_model not in catalog_ids:
            raise InvalidRequestError(f"Unknown model ID: {requested_model}")
        if catalog_ids[requested_model] != task:
            raise InvalidRequestError(f"Model {requested_model!r} does not support the {task} task.")
        if requested_model not in task_models:
            raise ModelUnavailableError(f"Model {requested_model!r} is not installed or not available in the active provider.")
        return requested_model
    if not task_models:
        raise ModelUnavailableError(f"No installed model is available for the {task} task.")
    selection_file = request.app.state.settings.data_dir / "default_models.json"
    selected = default_model_ids(request.app.state.settings).get(task)
    if selected in task_models:
        return selected
    if selection_file.is_file():
        raise ModelUnavailableError(f"No default model is selected for the {task} task. Set a default model first.")
    return task_models[0]


def _tts_script_language(text: str) -> str | None:
    if any("\u3040" <= char <= "\u30ff" for char in text):
        return "ja"
    if any("\uac00" <= char <= "\ud7af" for char in text):
        return "ko"
    cjk = sum("\u3400" <= char <= "\u9fff" for char in text)
    if cjk:
        return "zh"
    return None


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
async def capabilities(request: Request) -> dict[str, object]:
    return get_provider(request).capabilities()


@router.get("/runtime", tags=["runtime"])
async def runtime(request: Request) -> dict[str, object]:
    return get_provider(request).runtime()


@router.get("/models", tags=["models"])
async def models(request: Request) -> dict[str, object]:
    return {"data": get_provider(request).installed_models()}


@router.get("/catalog", tags=["models"])
async def catalog(request: Request) -> dict[str, object]:
    return {
        "data": catalog_models(request.app.state.settings),
        "storage": model_storage(request.app.state.settings),
    }


@router.post("/models/{model_id}/download", status_code=202, tags=["models"])
async def download_model(request: Request, model_id: str) -> dict[str, object]:
    from smartvoice.services.model_catalog import get_model_spec

    get_model_spec(model_id)
    return request.app.state.model_jobs.start_download(model_id)


@router.get("/jobs/{job_id}", tags=["models"])
async def model_job(request: Request, job_id: str) -> dict[str, object]:
    return request.app.state.model_jobs.get(job_id)


@router.delete("/jobs/{job_id}", tags=["models"])
async def cancel_model_job(request: Request, job_id: str) -> dict[str, object]:
    return request.app.state.model_jobs.cancel(job_id)


@router.put("/models/{model_id}/default", tags=["models"])
async def set_default_model_route(request: Request, model_id: str) -> dict[str, object]:
    defaults = await run_in_threadpool(set_default_model, request.app.state.settings, model_id)
    return {"default_models": defaults}


@router.delete("/models/{model_id}/default", tags=["models"])
async def clear_default_model_route(request: Request, model_id: str) -> dict[str, object]:
    defaults = await run_in_threadpool(clear_model_default, request.app.state.settings, model_id)
    return {"default_models": defaults}


@router.delete("/models/{model_id}", tags=["models"])
async def uninstall_model_route(request: Request, model_id: str) -> dict[str, object]:
    def remove_if_unused():
        loaded = bool(getattr(get_provider(request), "is_model_loaded", lambda _model_id: False)(model_id))
        return uninstall_model(request.app.state.settings, model_id, loaded=loaded)

    size, _queue_wait = await request.app.state.inference_queue.run(
        remove_if_unused,
        request.app.state.settings.inference_queue_timeout_seconds,
        request.app.state.settings.inference_execution_timeout_seconds,
    )
    return {"id": model_id, "removed_bytes": size}


@router.get("/models/{model_id}/export", tags=["models"])
async def export_model_route(request: Request, model_id: str) -> FileResponse:
    from tempfile import NamedTemporaryFile

    temporary_dir = request.app.state.settings.data_dir / "tmp"
    temporary_dir.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(prefix="smartvoice-model-", suffix=".zip", dir=temporary_dir, delete=False) as temporary:
        archive_path = __import__("pathlib").Path(temporary.name)
    try:
        await run_in_threadpool(export_model, request.app.state.settings, model_id, archive_path)
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
    from smartvoice.services.model_catalog import MAX_ARCHIVE_BYTES

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
                    raise PayloadTooLargeError("Offline model package exceeds the 2 GiB import limit.")
                temporary.write(chunk)
        destination = await run_in_threadpool(import_model, request.app.state.settings, temporary_path)
        return {"id": destination.name, "status": "installed"}
    finally:
        await file.close()
        if temporary_path:
            temporary_path.unlink(missing_ok=True)


@router.post("/audio/transcriptions", tags=["audio"])
async def transcriptions(
    request: Request,
    file: UploadFile = File(...),
    model: str | None = Form(default=None),
    language: str = Form(default="auto"),
    response_format: Literal["json", "text", "verbose_json"] = Form(default="json"),
    timestamps: bool = Form(default=False),
) -> Response:
    settings = request.app.state.settings
    try:
        form = await request.form()
        unexpected_fields = sorted(set(form.keys()) - {"file", "model", "language", "response_format", "timestamps"})
        if unexpected_fields:
            raise InvalidRequestError(f"Unsupported transcription field(s): {', '.join(unexpected_fields)}")
        model = select_installed_model(request, "transcription", model)
        request.state.model_id = model
        request.state.actual_device = "cpu"
        raw = await file.read(settings.max_upload_bytes + 1)
    finally:
        await file.close()
    if len(raw) > settings.max_upload_bytes:
        raise AudioTooLargeError("Audio upload exceeds the configured size limit.")
    inference_started = time.perf_counter()
    result, queue_wait = await _run_request_inference(
        request, lambda: get_provider(request).transcribe(raw, language, model)
    )
    result["request_processing_seconds"] = round(time.perf_counter() - inference_started - queue_wait, 4)
    result["queue_wait_seconds"] = round(queue_wait, 4)
    request.state.model_id = str(result.get("model", model))
    request.state.actual_device = str(result.get("device", "unknown"))
    if response_format == "text":
        return Response(content=str(result["text"]), media_type="text/plain; charset=utf-8")
    if response_format == "json" or not timestamps:
        result.pop("segments", None)
    return Response(content=json.dumps(result, ensure_ascii=False), media_type="application/json")


@router.post("/audio/speech", tags=["audio"])
async def speech(request: Request, payload: SpeechRequest) -> Response:
    model = select_installed_model(request, "speech", payload.model)
    from smartvoice.services.model_catalog import get_model_spec

    spec = get_model_spec(model)
    if payload.language not in {None, "auto", *spec.languages}:
        raise InvalidRequestError(f"The selected TTS model supports: {', '.join(spec.languages)}.")
    detected_language = _tts_script_language(payload.input)
    # Latin scripts do not distinguish English, French, and German; validate
    # only scripts that identify a language unambiguously.
    if payload.language not in {None, "auto"} and detected_language in {"zh", "ja", "ko"} and payload.language != detected_language:
        raise InvalidRequestError(
            f"The input text appears to be {detected_language}, which conflicts with requested language {payload.language}."
        )
    requested_language = payload.language if payload.language not in {None, "auto"} else detected_language
    if requested_language is None and len(spec.languages) == 1:
        requested_language = spec.languages[0]
    if requested_language and requested_language not in spec.languages:
        raise InvalidRequestError(f"The selected TTS model does not support language {requested_language!r}.")
    if len(payload.input) > request.app.state.settings.max_tts_characters:
        raise InvalidRequestError(
            f"Text exceeds the {request.app.state.settings.max_tts_characters} character limit; split it into shorter requests."
        )
    request.state.model_id = model
    request.state.actual_device = "cpu"
    inference_started = time.perf_counter()
    (audio, sample_rate, duration), queue_wait = await _run_request_inference(
        request, lambda: get_provider(request).synthesize(payload.input, payload.voice, payload.speed, model, requested_language or "auto")
    )
    inference_seconds = max(0.0, time.perf_counter() - inference_started - queue_wait)
    return StreamingResponse(
        iter([audio]),
        media_type="audio/wav",
        headers={
            "X-Audio-Sample-Rate": str(sample_rate),
            "X-Audio-Duration": f"{duration:.3f}",
            "X-Model-Id": model,
            "X-Inference-Time-Seconds": f"{inference_seconds:.4f}",
            "X-Queue-Wait-Seconds": f"{queue_wait:.4f}",
            "X-Real-Time-Factor": f"{inference_seconds / duration:.4f}" if duration else "0",
            "X-Requested-Language": payload.language or "auto",
            "X-Text-Language": detected_language or "mixed_or_undetermined",
        },
    )
