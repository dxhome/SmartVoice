"""Version 1 OpenAI-style audio and local model management routes."""

from __future__ import annotations

import json
import re
import uuid
from typing import Literal

from fastapi import APIRouter, File, Form, Request, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from smartvoice.domain.errors import InvalidRequestError, SmartVoiceError
from smartvoice.services.model_catalog import catalog_models

router = APIRouter(prefix="/v1")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = "melo-tts-zh-en-local"
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
    return {"data": catalog_models(request.app.state.settings)}


@router.post("/audio/transcriptions", tags=["audio"])
async def transcriptions(
    request: Request,
    file: UploadFile = File(...),
    model: str = Form(default="sensevoice-small-local"),
    language: str = Form(default="auto"),
    response_format: Literal["json", "text", "verbose_json"] = Form(default="json"),
    timestamps: bool = Form(default=False),
) -> Response:
    if model != "sensevoice-small-local":
        raise InvalidRequestError(f"Unknown transcription model: {model}")
    settings = request.app.state.settings
    raw = await file.read(settings.max_upload_bytes + 1)
    await file.close()
    if len(raw) > settings.max_upload_bytes:
        return Response(
            content='{"error":{"code":"file_too_large","message":"Audio upload exceeds the configured size limit."}}',
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            media_type="application/json",
        )
    result = await run_in_threadpool(get_provider(request).transcribe, raw, language)
    if response_format == "text":
        return Response(content=str(result["text"]), media_type="text/plain; charset=utf-8")
    if response_format == "json" or not timestamps:
        result.pop("segments", None)
    return Response(content=json.dumps(result, ensure_ascii=False), media_type="application/json")


@router.post("/audio/speech", tags=["audio"])
async def speech(request: Request, payload: SpeechRequest) -> Response:
    if payload.model != "melo-tts-zh-en-local":
        raise InvalidRequestError(f"Unknown speech model: {payload.model}")
    if payload.language not in {None, "auto", "zh", "en"}:
        raise InvalidRequestError("The selected TTS model supports only Chinese and English.")
    if len(payload.input) > request.app.state.settings.max_tts_characters:
        raise InvalidRequestError(
            f"Text exceeds the {request.app.state.settings.max_tts_characters} character limit; split it into shorter requests."
        )
    audio, sample_rate, duration = await run_in_threadpool(
        get_provider(request).synthesize, payload.input, payload.voice, payload.speed
    )
    return StreamingResponse(
        iter([audio]),
        media_type="audio/wav",
        headers={
            "X-Audio-Sample-Rate": str(sample_rate),
            "X-Audio-Duration": f"{duration:.3f}",
            "X-Model-Id": payload.model,
        },
    )
