"""Application assembly for the local SmartVoice API."""

import logging
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from smartvoice import __version__
from smartvoice.api.v1.routes import get_request_id, router as v1_router
from smartvoice.config.settings import Settings
from smartvoice.domain.errors import SmartVoiceError

logger = logging.getLogger("smartvoice.api")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False


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


def create_app(settings: Settings | None = None, provider=None) -> FastAPI:
    settings = settings or Settings.from_env()
    if provider is None:
        from smartvoice.adapters.inference.sherpa_onnx.provider import SherpaOnnxProvider

        provider = SherpaOnnxProvider(settings)
    app = FastAPI(
        title="SmartVoice API",
        description="Local Chinese and English speech API powered by optional sherpa-onnx models.",
        version=__version__,
    )
    app.state.settings = settings
    app.state.provider = provider
    app.include_router(v1_router)

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "unknown")
        details = _validation_details(exc)
        logger.warning(
            "request_validation_failed request_id=%s method=%s path=%s details=%s",
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
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        duration_ms = (time.perf_counter() - started) * 1000
        log_method = logger.warning if response.status_code >= 400 else logger.info
        log_method(
            "request_completed request_id=%s method=%s path=%s status=%d duration_ms=%.1f",
            request.state.request_id, request.method, request.url.path, response.status_code, duration_ms,
        )
        return response

    @app.get("/health", tags=["service"])
    async def health() -> dict[str, str]:
        """Liveness probe: confirms that the HTTP process responds."""
        return {"status": "ok", "version": __version__}

    @app.get("/ready", tags=["service"])
    async def readiness() -> JSONResponse:
        installed_ids = {str(model["id"]) for model in app.state.provider.installed_models()}
        required = {"sensevoice-small-local", "melo-tts-zh-en-local"}
        missing = sorted(required - installed_ids)
        return JSONResponse(
            status_code=503 if missing else 200,
            content={
                "status": "not_ready" if missing else "ready",
                "inference_backend": "sherpa-onnx",
                "reasons": [f"model_not_installed:{model_id}" for model_id in missing],
            },
        )

    return app


app = create_app()
