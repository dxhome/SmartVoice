"""Cancellation signal shared by the HTTP boundary and adapter admission."""
from contextvars import ContextVar
from threading import Event
import time
from contextlib import contextmanager
from smartvoice.domain.errors import InferenceTimeoutError

request_deadline: ContextVar[float | None] = ContextVar("inference_deadline", default=None)
request_segmented: ContextVar[bool] = ContextVar("inference_segmented", default=False)
request_continuation: ContextVar[bool] = ContextVar("inference_continuation", default=False)

def check_execution() -> None:
    event = request_cancelled.get()
    deadline = request_deadline.get()
    if event is not None and event.is_set():
        raise InferenceTimeoutError("Inference was cancelled.")
    if deadline is not None and time.monotonic() >= deadline:
        raise InferenceTimeoutError("Inference exceeded the configured execution time limit.")

@contextmanager
def segment_scope(index: int):
    check_execution()
    token = request_continuation.set(index > 0)
    segmented_token = request_segmented.set(True)
    try:
        yield
        check_execution()
    finally:
        request_continuation.reset(token)
        request_segmented.reset(segmented_token)


request_cancelled: ContextVar[Event | None] = ContextVar("inference_cancelled", default=None)
