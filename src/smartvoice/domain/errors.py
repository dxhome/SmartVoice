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


class ResourceNotFoundError(SmartVoiceError):
    code = "not_found"
    http_status = 404


class InferenceOverloadedError(SmartVoiceError):
    code = "inference_overloaded"
    http_status = 503


class InferenceTimeoutError(SmartVoiceError):
    code = "inference_timeout"
    http_status = 504


class AudioTooLargeError(SmartVoiceError):
    code = "file_too_large"
    http_status = 413


class SpeechOutputTooLargeError(SmartVoiceError):
    code = "speech_output_too_large"
    http_status = 413


class PayloadTooLargeError(SmartVoiceError):
    code = "payload_too_large"
    http_status = 413


class InvalidAudioError(SmartVoiceError):
    code = "invalid_audio"
    http_status = 422


class InvalidRequestError(SmartVoiceError):
    code = "invalid_request"
    http_status = 400


class UnsupportedFeatureError(InvalidRequestError):
    """The request asks for a valid capability this release does not implement."""

    code = "not_implemented"
    http_status = 501


class InferenceError(SmartVoiceError):
    code = "inference_failed"
    http_status = 500
