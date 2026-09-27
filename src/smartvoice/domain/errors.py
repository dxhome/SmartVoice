"""Stable public errors raised by domain services and adapters."""


class SmartVoiceError(Exception):
    code = "smartvoice_error"
    http_status = 500

    def __init__(self, message: str, *, detail: str | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail


class ModelUnavailableError(SmartVoiceError):
    code = "model_unavailable"
    http_status = 503


class InvalidAudioError(SmartVoiceError):
    code = "invalid_audio"
    http_status = 422


class InvalidRequestError(SmartVoiceError):
    code = "invalid_request"
    http_status = 400


class InferenceError(SmartVoiceError):
    code = "inference_failed"
    http_status = 500
