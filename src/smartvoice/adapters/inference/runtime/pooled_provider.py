"""Provider facade: task translation stays in existing native adapters."""
import threading
import time
from dataclasses import replace

from smartvoice.adapters.inference.runtime.elastic_pool import ElasticRuntimePool


class PooledInferenceProvider:
    def __init__(self, metadata, factory, settings, *, shared_backend=False):
        self.metadata = metadata
        self.settings = settings
        self.shared_backend = shared_backend
        self._availability_lock = threading.Lock()
        self._availability = None
        self._availability_until = 0.0
        self.pool = ElasticRuntimePool(factory,
            min_instances=1 if shared_backend else settings.min_instances,
            max_instances=1 if shared_backend else settings.max_instances,
            max_waiting=settings.max_queued_inference,
            idle_seconds=settings.instance_idle_seconds,
            wait_seconds=settings.inference_queue_timeout_seconds)

    def installed_models(self):
        return self.metadata.installed_models()

    def inference_models(self):
        # Full catalog scans are expensive under concurrent routing. This cache
        # only selects candidates; unchanged native adapters verify assets again.
        with self._availability_lock:
            if self._availability is None or time.monotonic() >= self._availability_until:
                self._availability = list(self.metadata.installed_models())
                self._availability_until = time.monotonic() + 1.0
            return list(self._availability)

    def capabilities(self):
        return self.metadata.capabilities()

    def capabilities_for_models(self, models):
        method = getattr(self.metadata, "capabilities_for_models", None)
        return method(models) if callable(method) else self.metadata.capabilities()

    def runtime(self):
        return {**self.metadata.runtime(), "instance_pools": self.pool.snapshot(),
                "pool_limits": {"min_instances": self.pool.minimum, "max_instances": self.pool.maximum,
                                "max_waiting": self.pool.max_waiting,
                                "threads_per_instance": self.settings.num_threads}}

    def runtime_for_models(self, models):
        method = getattr(self.metadata, "runtime_for_models", None)
        runtime = method(models) if callable(method) else self.metadata.runtime()
        return {**runtime, "instance_pools": self.pool.snapshot(),
                "pool_limits": {"min_instances": self.pool.minimum, "max_instances": self.pool.maximum,
                                "max_waiting": self.pool.max_waiting,
                                "threads_per_instance": self.settings.num_threads}}

    def _key(self, model_id):
        return "backend" if self.shared_backend else model_id

    def transcribe(self, audio, language="auto", model_id=None):
        result, wait = self.pool.run(self._key(model_id), lambda runtime: runtime.transcribe(audio, language, model_id))
        return {**result, "runtime_wait_seconds": result.get("runtime_wait_seconds", 0.0) + wait}

    def synthesize(self, text, voice="default", speed=1.0, model_id=None, language="auto"):
        result, wait = self.pool.run(self._key(model_id), lambda runtime: runtime.synthesize(text, voice, speed, model_id, language))
        return replace(result, runtime_wait_seconds=result.runtime_wait_seconds + wait)

    def is_model_loaded(self, model_id):
        return self.pool.contains(self._key(model_id))

    def request_capacity(self):
        groups = 1
        if not self.shared_backend:
            # Include uninstalled catalog models so later installation does not
            # leave an undersized transport guard; one group is the detector.
            groups += sum(model.get("backend") == self.backend
                          for model in self.metadata.model_repository.catalog_models())
        return groups * (self.pool.maximum + self.pool.max_waiting)

    def close(self):
        self.pool.close()
        self.metadata.close()

    def reload_integrity_cache(self):
        method = getattr(self.metadata, "reload_integrity_cache", None)
        if callable(method):
            method()


class PooledSherpaProvider(PooledInferenceProvider):
    backend = "sherpa-onnx"

    def identify_language(self, audio):
        result, wait = self.pool.run("language-identification", lambda runtime: runtime.identify_language(audio))
        return {**result, "runtime_wait_seconds": result.get("runtime_wait_seconds", 0.0) + wait}

    def language_identification_available(self):
        return self.metadata.language_identification_available()
