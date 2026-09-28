"""Application assembly for the local SmartVoice API."""

import logging
import json
import time
from contextlib import asynccontextmanager
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.openapi.docs import get_swagger_ui_html

from smartvoice import __version__
from smartvoice.api.v1.routes import get_request_id, router as v1_router
from smartvoice.config.settings import Settings
from smartvoice.domain.errors import SmartVoiceError
from smartvoice.services.host_metrics import process_metrics
from smartvoice.services.inference_queue import InferenceQueue
from smartvoice.services.model_jobs import ModelJobManager
from smartvoice.services.model_router import ModelRouter

logger = logging.getLogger("smartvoice.api")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False

OPENAPI_DESCRIPTION = """![SmartVoice logo](/assets/smartvoice-logo.png)
**Private, local speech recognition and synthesis for edge devices.**

SmartVoice is an early-stage local speech-to-text (STT) and text-to-speech (TTS) service. It brings both tasks behind one HTTP API, with a replaceable inference backend and a model catalog for offline use.

Multilingual STT and TTS · Cross-platform goal · Offline inference · OpenAPI

[View the SmartVoice project on GitHub](https://github.com/dxhome/SmartVoice)
"""


def _validation_details(exc: RequestValidationError) -> list[dict[str, str]]:
    details = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ())) or "request"
        details.append({
            "field": location,
            "message": str(error.get("msg", "Invalid value")),
            "type": str(error.get("type", "validation_error")),
        })
    return details


HTTP_DEBUG_FIELD_LIMIT_BYTES = 128


def _compact_json_size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _debug_value_size(value) -> int:
    return len(value.encode("utf-8")) if isinstance(value, str) else _compact_json_size(value)


def _summarize_large_json_values(value):
    if isinstance(value, dict):
        # Objects are containers: decide which nested field values to omit,
        # rather than dropping an object because its combined JSON is large.
        return {key: _summarize_large_json_values(item) for key, item in value.items()}
    if isinstance(value, list):
        size = _debug_value_size(value)
        if size > HTTP_DEBUG_FIELD_LIMIT_BYTES:
            return f"<content omitted: {size} bytes>"
        return [_summarize_large_json_values(item) for item in value]
    if isinstance(value, str):
        size = _debug_value_size(value)
        if size > HTTP_DEBUG_FIELD_LIMIT_BYTES:
            return f"<content omitted: {size} bytes>"
    return value


def _format_multipart_body(body: bytes, content_type: str) -> str:
    headers = (
        f"Content-Type: {content_type}\r\n"
        "MIME-Version: 1.0\r\n\r\n"
    ).encode("latin-1")
    message = BytesParser(policy=policy.default).parsebytes(headers + body)
    if not message.is_multipart():
        return f"<multipart body could not be parsed: {len(body)} bytes>"

    fields = {}
    for index, part in enumerate(message.iter_parts(), start=1):
        name = part.get_param("name", header="content-disposition") or f"part_{index}"
        payload = part.get_payload(decode=True) or b""
        size = len(payload)
        if size > HTTP_DEBUG_FIELD_LIMIT_BYTES:
            value = f"<content omitted: {size} bytes>"
        elif part.get_filename() is not None:
            value = f"<binary content: {size} bytes>"
        else:
            charset = part.get_content_charset() or "utf-8"
            try:
                value = payload.decode(charset)
            except (LookupError, UnicodeDecodeError):
                value = f"<binary content: {size} bytes>"
        if name in fields:
            if not isinstance(fields[name], list):
                fields[name] = [fields[name]]
            fields[name].append(value)
        else:
            fields[name] = value
    return json.dumps(fields, ensure_ascii=False, separators=(",", ":"))


def _format_http_body(body: bytes, content_type: str | None = None) -> str:
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if media_type == "multipart/form-data":
        return _format_multipart_body(body, content_type)
    if media_type == "application/x-www-form-urlencoded":
        try:
            decoded = body.decode("utf-8")
        except UnicodeDecodeError:
            return f"<binary body omitted: {len(body)} bytes>"
        fields = {}
        for key, value in parse_qsl(decoded, keep_blank_values=True):
            field_value = (
                f"<content omitted: {len(value.encode('utf-8'))} bytes>"
                if len(value.encode("utf-8")) > HTTP_DEBUG_FIELD_LIMIT_BYTES else value
            )
            if key in fields:
                if not isinstance(fields[key], list):
                    fields[key] = [fields[key]]
                fields[key].append(field_value)
            else:
                fields[key] = field_value
        return json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
    try:
        decoded = body.decode("utf-8")
    except UnicodeDecodeError:
        return f"<binary body omitted: {len(body)} bytes>"
    try:
        value = json.loads(decoded)
    except json.JSONDecodeError:
        if len(body) > HTTP_DEBUG_FIELD_LIMIT_BYTES:
            return f"<body omitted: {len(body)} bytes>"
        return decoded
    if not isinstance(value, (dict, list)):
        if len(body) > HTTP_DEBUG_FIELD_LIMIT_BYTES:
            return f"<body omitted: {len(body)} bytes>"
        return decoded
    return json.dumps(_summarize_large_json_values(value), ensure_ascii=False, separators=(",", ":"))


def _debug_headers(headers) -> dict[str, str]:
    sensitive = {"authorization", "proxy-authorization", "cookie", "set-cookie"}
    summarized = {}
    for raw_key, raw_value in headers:
        key = raw_key.decode("latin-1")
        if key.lower() in sensitive:
            summarized[key] = "[REDACTED]"
            continue
        value = raw_value.decode("latin-1")
        size = len(raw_value)
        summarized[key] = (
            f"<content omitted: {size} bytes>"
            if size > HTTP_DEBUG_FIELD_LIMIT_BYTES else value
        )
    return summarized


def create_app(settings: Settings | None = None, provider=None, *, debug_http: bool = False) -> FastAPI:
    settings = settings or Settings.from_env()
    if provider is None:
        from smartvoice.adapters.inference.sherpa_onnx.provider import SherpaOnnxProvider

        provider = SherpaOnnxProvider(settings)
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        yield
        application.state.model_jobs.cancel_all()

    logger.setLevel(logging.DEBUG if debug_http else getattr(logging, settings.log_level, logging.INFO))
    app = FastAPI(
        title="SmartVoice API",
        description=OPENAPI_DESCRIPTION,
        version=__version__,
        docs_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.provider = provider
    app.state.inference_queue = InferenceQueue(
        settings.max_concurrent_inference, settings.max_queued_inference
    )
    app.state.model_jobs = ModelJobManager(settings)
    app.state.model_router = ModelRouter(settings)
    app.include_router(v1_router)

    logo_path = Path(__file__).resolve().parents[2] / "assets" / "smartvoice-logo.png"

    @app.get("/assets/smartvoice-logo.png", include_in_schema=False)
    async def smartvoice_logo() -> FileResponse:
        return FileResponse(logo_path, media_type="image/png")

    @app.get("/docs", include_in_schema=False)
    async def swagger_docs() -> HTMLResponse:
        page = get_swagger_ui_html(
            openapi_url=app.openapi_url or "/openapi.json",
            title="SmartVoice | API documentation",
        )
        html = page.body.decode("utf-8")
        style = """<style>
        .swagger-ui img[src*="smartvoice-logo.png"] {
            display: block !important;
            width: 200px !important;
            height: 200px !important;
            max-width: 200px !important;
            max-height: 200px !important;
            object-fit: contain !important;
            margin: 0 0 18px !important;
        }
        .swagger-ui .info .markdown p { max-width: 900px; }
        </style>"""
        return HTMLResponse(html.replace("</head>", f"{style}</head>"))

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "unknown")
        details = _validation_details(exc)
        unsupported = any(error.get("type") in {"literal_error", "extra_forbidden"} for error in exc.errors())
        status_code = 501 if unsupported else 422
        error_code = "not_implemented" if unsupported else "validation_error"
        message = "The requested feature or value is not supported." if unsupported else "Request validation failed."
        logger.error(
            "request_validation_failed request_id=%s method=%s path=%s status=%d internal_reason=%s",
            request_id, request.method, request.url.path, status_code, details,
        )
        return JSONResponse(
            status_code=status_code,
            content={
                "error": {
                    "code": error_code,
                    "message": message,
                    "details": details,
                    "request_id": request_id,
                }
            },
        )

    @app.exception_handler(SmartVoiceError)
    async def smartvoice_error_handler(request: Request, exc: SmartVoiceError) -> JSONResponse:
        logger.error(
            "smartvoice_error request_id=%s method=%s path=%s status=%d code=%s mode=%s model=%s router_sha256=%s language=%s candidates=%s internal_reason=%s",
            getattr(request.state, "request_id", "unknown"), request.method, request.url.path,
            exc.http_status, exc.code, getattr(request.state, "model_mode", "-"),
            getattr(request.state, "model_id", "-"), getattr(request.state, "router_sha256", "-"),
            getattr(request.state, "route_language", "-"), getattr(request.state, "route_candidates", []),
            exc.detail or str(exc) or type(exc).__name__,
            exc_info=(type(exc), exc, exc.__traceback__) if exc.http_status >= 500 and exc.http_status != 501 else None,
        )
        return JSONResponse(
            status_code=exc.http_status,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "request_id": getattr(request.state, "request_id", "unknown"),
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        logger.error(
            "http_request_error request_id=%s method=%s path=%s status=%d internal_reason=%s",
            getattr(request.state, "request_id", "unknown"), request.method, request.url.path,
            exc.status_code, exc.detail,
        )
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "unknown")
        logger.exception(
            "unhandled_request_error request_id=%s method=%s path=%s",
            request_id, request.method, request.url.path,
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "An internal error occurred. Check the server log and request ID.",
                    "request_id": request_id,
                }
            },
        )

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):
        request.state.request_id = get_request_id(request)
        request_body = await request.body() if debug_http else b""
        if debug_http:
            logger.debug(
                "http_request request_id=%s method=%s url=%s headers=%s body=%s",
                request.state.request_id, request.method, str(request.url),
                _debug_headers(request.headers.raw),
                _format_http_body(request_body, request.headers.get("content-type")),
            )
        started = time.perf_counter()
        before = process_metrics()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        duration_ms = (time.perf_counter() - started) * 1000
        after = process_metrics()
        cpu_seconds = max(0.0, float(after["cpu_time_seconds"]) - float(before["cpu_time_seconds"]))
        response.headers["X-Process-CPU-Time-Seconds"] = f"{cpu_seconds:.6f}"
        for key, header in (
            ("working_set_bytes", "X-Process-Working-Set-Bytes"),
            ("peak_working_set_bytes", "X-Process-Peak-Working-Set-Bytes"),
        ):
            if key in after:
                response.headers[header] = str(after[key])
        log_method = logger.warning if response.status_code >= 400 else logger.info
        log_method(
            "request_completed request_id=%s method=%s path=%s status=%d duration_ms=%.1f mode=%s model=%s router_sha256=%s language=%s candidates=%s device=%s cpu_seconds=%.4f working_set_bytes=%s",
            request.state.request_id, request.method, request.url.path, response.status_code, duration_ms,
            getattr(request.state, "model_mode", "-"), getattr(request.state, "model_id", "-"),
            getattr(request.state, "router_sha256", "-"), getattr(request.state, "route_language", "-"),
            getattr(request.state, "route_candidates", []), getattr(request.state, "actual_device", "-"),
            cpu_seconds, after.get("working_set_bytes", "unknown"),
        )
        if debug_http:
            original_iterator = response.body_iterator

            async def log_response_body():
                chunks = []
                async for chunk in original_iterator:
                    chunks.append(chunk if isinstance(chunk, bytes) else str(chunk).encode())
                    yield chunk
                logger.debug(
                    "http_response request_id=%s status=%d headers=%s body=%s",
                    request.state.request_id, response.status_code,
                    _debug_headers(response.raw_headers),
                    _format_http_body(b"".join(chunks), response.headers.get("content-type")),
                )

            response.body_iterator = log_response_body()
        return response

    @app.get("/health", tags=["service"])
    async def health() -> dict[str, str]:
        """Liveness probe: confirms that the HTTP process responds."""
        return {"status": "ok", "version": __version__}

    @app.get("/ready", tags=["service"])
    async def readiness() -> JSONResponse:
        installed = app.state.provider.installed_models()
        available_tasks = sorted({str(model.get("task")) for model in installed if model.get("task")})
        required_tasks = {"transcription", "speech"}
        missing = sorted(required_tasks - set(available_tasks))
        runtime = app.state.provider.runtime()
        return JSONResponse(
            status_code=503 if missing else 200,
            content={
                "status": "not_ready" if missing else "ready",
                "inference_backend": runtime.get("backend"),
                "available_tasks": available_tasks,
                "available_models": [str(model.get("id")) for model in installed],
                "reasons": [f"model_not_available_for_task:{task}" for task in missing],
            },
        )

    return app


app = create_app()
