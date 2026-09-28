"""Application assembly for the local SmartVoice API."""

import logging
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

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
        summarized = {}
        for key, item in value.items():
            item = _summarize_large_json_values(item)
            size = _debug_value_size(item)
            summarized[key] = f"<content omitted: {size} bytes>" if size > HTTP_DEBUG_FIELD_LIMIT_BYTES else item
        return summarized
    if isinstance(value, list):
        summarized = []
        for item in value:
            item = _summarize_large_json_values(item)
            size = _debug_value_size(item)
            summarized.append(f"<content omitted: {size} bytes>" if size > HTTP_DEBUG_FIELD_LIMIT_BYTES else item)
        return summarized
    return value


def _format_http_body(body: bytes) -> str:
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
    return {
        key.decode("latin-1"): "[REDACTED]" if key.decode("latin-1").lower() in sensitive else value.decode("latin-1")
        for key, value in headers
    }


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
        logger.error(
            "request_validation_failed request_id=%s method=%s path=%s internal_reason=%s",
            request_id, request.method, request.url.path, details,
        )
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Request validation failed.",
                    "details": details,
                    "request_id": request_id,
                }
            },
        )

    @app.exception_handler(SmartVoiceError)
    async def smartvoice_error_handler(request: Request, exc: SmartVoiceError) -> JSONResponse:
        logger.error(
            "smartvoice_error request_id=%s method=%s path=%s status=%d code=%s internal_reason=%s",
            getattr(request.state, "request_id", "unknown"), request.method, request.url.path,
            exc.http_status, exc.code, exc.detail or str(exc) or type(exc).__name__,
            exc_info=(type(exc), exc, exc.__traceback__) if exc.http_status >= 500 else None,
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
                _debug_headers(request.headers.raw), _format_http_body(request_body),
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
            "request_completed request_id=%s method=%s path=%s status=%d duration_ms=%.1f model=%s device=%s cpu_seconds=%.4f working_set_bytes=%s",
            request.state.request_id, request.method, request.url.path, response.status_code, duration_ms,
            getattr(request.state, "model_id", "-"), getattr(request.state, "actual_device", "-"),
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
                    _debug_headers(response.raw_headers), _format_http_body(b"".join(chunks)),
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
