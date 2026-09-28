"""Version 1 OpenAI-style audio and local model management routes."""

from __future__ import annotations

import json
import asyncio
import logging
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
    AudioTooLargeError, InvalidRequestError, ModelUnavailableError, PayloadTooLargeError,
    UnsupportedFeatureError,
)
from smartvoice.services.language_detection import detect_text_language
from smartvoice.services.model_router import (
    ROUTER_MODEL_IDS, WHISPER_LANGUAGE_CODES, LanguageDetectionError, RouterConfig,
)
from smartvoice.services.model_catalog import (
    catalog_models, export_model, get_model_spec, import_model, model_storage, uninstall_model,
)

router = APIRouter(prefix="/v1")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
MAX_STT_LANGUAGE_DETECTION_ATTEMPTS = 3
MAX_TTS_LANGUAGE_DETECTION_ATTEMPTS = 3
TTS_LANGUAGE_CONFIDENCE_THRESHOLD = 0.70
logger = logging.getLogger("smartvoice.api")


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


def select_installed_model(request: Request, task: str, requested_model: str | None) -> str:
    if not requested_model:
        raise InvalidRequestError("A model ID is required; use the SmartVoice routing model or specify an installed model ID.")
    spec = get_model_spec(requested_model)
    if spec.task != task:
        raise UnsupportedFeatureError(f"Model {requested_model!r} does not support the {task} task.")
    installed = {str(model["id"]) for model in get_provider(request).installed_models() if model.get("task") == task}
    if spec.id not in installed:
        raise ModelUnavailableError(f"Model {requested_model!r} is not installed or not available in the active provider.")
    return spec.id


def _tts_script_language(text: str) -> str | None:
    from smartvoice.services.language_detection import script_language

    return script_language(text)


def _router_model(requested_model: str | None, task: str) -> str:
    virtual_id = ROUTER_MODEL_IDS[task]
    if requested_model in {None, virtual_id}:
        return virtual_id
    return requested_model


def _select_routed_model(
    request: Request, task: str, language: str, config: RouterConfig,
) -> tuple[str, list[dict[str, object]]]:
    return request.app.state.model_router.choose(
        task, language, get_provider(request).installed_models(), config=config,
    )


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
    data = [_openai_model_object(model) for model in get_provider(request).installed_models()]
    data.extend(_virtual_model_object(task, model_id) for task, model_id in ROUTER_MODEL_IDS.items())
    task_order = {"transcription": 0, "speech": 1}
    data.sort(key=lambda model: (
        task_order.get(str(model.get("task")), 2),
        0 if model.get("virtual") else 1,
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
        **{key: value for key, value in model.items() if key != "id"},
    }


def _virtual_model_object(task: str, model_id: str) -> dict[str, object]:
    return {
        "id": model_id,
        "object": "model",
        "created": 0,
        "owned_by": "smartvoice",
        "name": "SmartVoice Auto STT" if task == "transcription" else "SmartVoice Auto TTS",
        "task": task,
        "virtual": True,
    }


@router.get("/models/{model_id}", tags=["models"])
async def retrieve_model(request: Request, model_id: str) -> dict[str, object]:
    for task, virtual_id in ROUTER_MODEL_IDS.items():
        if model_id == virtual_id:
            return _virtual_model_object(task, model_id)
    model = next(
        (item for item in get_provider(request).installed_models() if item.get("id") == model_id),
        None,
    )
    if model is None:
        raise HTTPException(status_code=404, detail=f"Model {model_id!r} is not installed.")
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
    entries = catalog_models(request.app.state.settings)
    if task is not None:
        entries = [model for model in entries if model.get("task") == task]
    return {
        "data": entries,
        "storage": model_storage(request.app.state.settings),
    }


@router.post("/models/{model_id}/download", status_code=202, tags=["models"])
async def download_model(request: Request, model_id: str) -> dict[str, object]:
    spec = get_model_spec(model_id)
    return request.app.state.model_jobs.start_download(spec.id)


@router.get("/jobs/{job_id}", tags=["models"])
async def model_job(request: Request, job_id: str) -> dict[str, object]:
    return request.app.state.model_jobs.get(job_id)


@router.delete("/jobs/{job_id}", tags=["models"])
async def cancel_model_job(request: Request, job_id: str) -> dict[str, object]:
    return request.app.state.model_jobs.cancel(job_id)


@router.delete("/models/{model_id}", tags=["models"])
async def uninstall_model_route(request: Request, model_id: str) -> dict[str, object]:
    spec = get_model_spec(model_id)
    canonical_id = spec.id
    def remove_if_unused():
        loaded = bool(getattr(get_provider(request), "is_model_loaded", lambda _model_id: False)(canonical_id))
        return uninstall_model(request.app.state.settings, canonical_id, loaded=loaded)

    size, _queue_wait = await request.app.state.inference_queue.run(
        remove_if_unused,
        request.app.state.settings.inference_queue_timeout_seconds,
        request.app.state.settings.inference_execution_timeout_seconds,
    )
    return {"id": canonical_id, "removed_bytes": size}


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
    model_id = get_model_spec(model_id).id
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
        if language not in {None, "auto", *WHISPER_LANGUAGE_CODES}:
            raise UnsupportedFeatureError(f"Unsupported Whisper language code: {language!r}.")
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
    language_source = "request" if requested_language != "auto" else "model_detection"

    def transcribe_request():
        provider = get_provider(request)
        if not routed:
            direct_model = select_installed_model(request, "transcription", requested_model)
            direct_spec = get_model_spec(direct_model)
            if requested_language not in {"auto", *direct_spec.languages}:
                raise UnsupportedFeatureError(f"The selected STT model does not support language {requested_language!r}.")
            request.state.model_id = direct_model
            direct_result = provider.transcribe(raw, requested_language, direct_model)
            detected = direct_result.get("language") if requested_language == "auto" else requested_language
            return direct_result, direct_model, str(detected) if detected else None, []

        if requested_language != "auto":
            selected, states = _select_routed_model(request, "transcription", requested_language, router_config)
            request.state.model_id = selected
            request.state.route_candidates = states
            return provider.transcribe(raw, requested_language, selected), selected, requested_language, states

        installed = provider.installed_models()
        detector = next((
            str(item["id"]) for item in installed
            if item.get("task") == "transcription" and "auto" in get_model_spec(str(item["id"])).languages
        ), None)
        if detector is None:
            supported_auto_models = [
                item for item in catalog_models(request.app.state.settings)
                if item.get("task") == "transcription" and "auto" in item.get("languages", [])
            ]
            if not supported_auto_models:
                raise UnsupportedFeatureError("Automatic STT language detection is not supported by this release.")
            raise ModelUnavailableError("Automatic STT language detection requires an installed model with auto-detection support.")
        request.state.model_id = detector
        detected: dict[str, object] | None = None
        detected_language = ""
        unsupported_results: list[str] = []
        for attempt in range(1, MAX_STT_LANGUAGE_DETECTION_ATTEMPTS + 1):
            detected = provider.transcribe(raw, "auto", detector)
            detected_language = str(detected.get("language") or "").lower()
            logger.debug(
                "language_detection_completed request_id=%s task=transcription requested_model=%s detector_model=%s attempt=%d max_attempts=%d language=%s confidence=%s",
                getattr(request.state, "request_id", "unknown"), requested_model, detector,
                attempt, MAX_STT_LANGUAGE_DETECTION_ATTEMPTS, detected_language or "unknown",
                detected.get("language_confidence", "unavailable"),
            )
            request.state.route_language = detected_language
            if (
                detected_language in WHISPER_LANGUAGE_CODES
                and router_config.tasks.get("transcription", {}).get(detected_language)
            ):
                break
            unsupported_results.append(detected_language or "unknown")
            detected = None
        if detected is None:
            results = ", ".join(unsupported_results)
            raise UnsupportedFeatureError(
                f"STT language detection did not find a language with a configured route after "
                f"{MAX_STT_LANGUAGE_DETECTION_ATTEMPTS} attempts (results: {results}). "
                "Specify language explicitly or update router.json."
            )
        selected, states = request.app.state.model_router.choose(
            "transcription", detected_language, installed, config=router_config,
        )
        request.state.model_id = selected
        request.state.route_candidates = states
        if selected == detector:
            result = detected
        else:
            result = provider.transcribe(raw, detected_language, selected)
        return result, selected, detected_language, states

    (result, actual_model, resolved_language, route_candidates), queue_wait = await _run_request_inference(request, transcribe_request)
    result["request_processing_seconds"] = round(time.perf_counter() - inference_started - queue_wait, 4)
    result["queue_wait_seconds"] = round(queue_wait, 4)
    result["requested_model"] = requested_model
    result["model_mode"] = "router" if routed else "direct"
    result["language_source"] = ("model_detection" if not routed and requested_language == "auto" else language_source) if resolved_language else "undetermined"
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
    if requested_language not in {"auto", *WHISPER_LANGUAGE_CODES}:
        raise UnsupportedFeatureError(f"Unsupported Whisper language code: {requested_language!r}.")
    route_candidates: list[dict[str, object]] = []
    language_confidence: float | None = None
    if routed:
        if requested_language == "auto":
            detector = "script" if _tts_script_language(payload.input) else "langid"
            best_language: str | None = None
            best_confidence = -1.0
            for attempt in range(1, MAX_TTS_LANGUAGE_DETECTION_ATTEMPTS + 1):
                detected_language, confidence = await run_in_threadpool(detect_text_language, payload.input)
                logger.debug(
                    "language_detection_completed request_id=%s task=speech requested_model=%s detector=%s attempt=%d max_attempts=%d language=%s confidence=%.3f",
                    getattr(request.state, "request_id", "unknown"), requested_model, detector,
                    attempt, MAX_TTS_LANGUAGE_DETECTION_ATTEMPTS, detected_language, confidence,
                )
                if confidence > best_confidence:
                    best_language, best_confidence = detected_language, confidence
                if detected_language in WHISPER_LANGUAGE_CODES and confidence >= TTS_LANGUAGE_CONFIDENCE_THRESHOLD:
                    requested_language, language_confidence = detected_language, confidence
                    break
            else:
                if best_language is None:
                    raise LanguageDetectionError("Unable to determine the text language.")
                requested_language, language_confidence = best_language, best_confidence
                logger.debug(
                    "language_detection_fallback request_id=%s task=speech requested_model=%s language=%s confidence=%.3f attempts=%d reason=best_confidence",
                    getattr(request.state, "request_id", "unknown"), requested_model,
                    requested_language, language_confidence, MAX_TTS_LANGUAGE_DETECTION_ATTEMPTS,
                )
            language_source = "text_detection"
        else:
            language_source = "request"
        model: str | None = None
    else:
        model = select_installed_model(request, "speech", requested_model)
        spec = get_model_spec(model)
        detected_language = _tts_script_language(payload.input)
        if requested_language not in {"auto", *spec.languages}:
            raise UnsupportedFeatureError(f"The selected TTS model supports: {', '.join(spec.languages)}.")
        if requested_language != "auto" and detected_language in {"zh", "ja", "ko"} and requested_language != detected_language:
            raise InvalidRequestError(
                f"The input text appears to be {detected_language}, which conflicts with requested language {requested_language}."
            )
        if requested_language == "auto":
            requested_language = detected_language or (spec.languages[0] if len(spec.languages) == 1 else "auto")
            if requested_language == "auto" and spec.model_type == "kokoro":
                detected, confidence = await run_in_threadpool(detect_text_language, payload.input)
                requested_language = detected if detected in spec.languages and confidence >= TTS_LANGUAGE_CONFIDENCE_THRESHOLD else "en"
        if requested_language != "auto" and requested_language not in spec.languages:
            raise UnsupportedFeatureError(f"The selected TTS model does not support language {requested_language!r}.")
        language_source = "request" if payload.language not in {None, "auto"} else "model_inference"
        spec = get_model_spec(model)
        if requested_language not in spec.languages and requested_language != "auto":
            raise UnsupportedFeatureError(f"The selected TTS model does not support language {requested_language!r}.")
    if len(payload.input) > request.app.state.settings.max_tts_characters:
        raise InvalidRequestError(
            f"Text exceeds the {request.app.state.settings.max_tts_characters} character limit; split it into shorter requests."
        )
    request.state.model_mode = "router" if routed else "direct"
    request.state.router_sha256 = router_config.digest if router_config else None
    request.state.route_language = requested_language
    request.state.route_candidates = route_candidates
    request.state.actual_device = "cpu"
    inference_started = time.perf_counter()
    def synthesize_selected():
        nonlocal model, route_candidates
        if routed:
            model, route_candidates = _select_routed_model(request, "speech", requested_language, router_config)
            request.state.model_id = model
            request.state.route_candidates = route_candidates
        assert model is not None
        return get_provider(request).synthesize(payload.input, payload.voice, payload.speed, model, requested_language or "auto")

    (audio, sample_rate, duration), queue_wait = await _run_request_inference(request, synthesize_selected)
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
            "X-Real-Time-Factor": f"{inference_seconds / duration:.4f}" if duration else "0",
            "X-Requested-Language": payload.language or "auto",
            "X-Resolved-Language": requested_language,
            "X-Language-Source": language_source,
            "X-Language-Confidence": f"{language_confidence:.3f}" if language_confidence is not None else "not_applicable",
            "X-Text-Language": _tts_script_language(payload.input) or (requested_language if requested_language != "auto" else "mixed_or_undetermined"),
            "X-Router-SHA256": router_config.digest if router_config else "not_applicable",
            "X-Route-Candidates": ",".join(
                f"{candidate['model']}:{'installed' if candidate['installed'] else 'uninstalled'}"
                for candidate in route_candidates
            ) if routed else "not_applicable",
        },
    )
