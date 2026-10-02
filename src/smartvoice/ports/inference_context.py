"""Cancellation signal shared by the HTTP boundary and adapter admission."""
from contextvars import ContextVar
from threading import Event

request_cancelled: ContextVar[Event | None] = ContextVar("inference_cancelled", default=None)
