"""Isolated loopback SmartVoice service for SDK/model integration tests."""

from __future__ import annotations

import socket
import os
import shutil
import threading
import time
from contextlib import ExitStack

import uvicorn

from smartvoice.app import create_app
from smartvoice.config.settings import Settings
from smartvoice.services.model_storage import model_directory
from tests.inference_environment import isolated_runtime


class RealService:
    def __init__(self, model_ids: tuple[str, ...] = ()) -> None:
        self._runtime = ExitStack()
        source = Settings.from_env()
        settings, provider = self._runtime.enter_context(isolated_runtime(source))
        for model_id in model_ids:
            original = model_directory(source, model_id)
            if not (original / "smartvoice-model.json").is_file():
                raise RuntimeError(f"Required SDK integration model is not installed: {model_id}")

            def link_or_copy(src, dst):
                try:
                    os.link(src, dst)
                except OSError:
                    shutil.copy2(src, dst)
                return dst

            shutil.copytree(original, settings.models_dir / model_id, copy_function=link_or_copy)
        self.app = create_app(settings=settings, provider=provider)
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(128)
        self.base_url = f"http://127.0.0.1:{self._listener.getsockname()[1]}"
        self._server = uvicorn.Server(uvicorn.Config(
            self.app, log_level="error", access_log=False, lifespan="on",
        ))
        self._thread = threading.Thread(
            target=self._server.run, kwargs={"sockets": [self._listener]}, daemon=True,
        )

    def start(self) -> "RealService":
        self._thread.start()
        deadline = time.monotonic() + 30
        while not self._server.started and self._thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not self._server.started:
            self.close()
            raise RuntimeError("Isolated SmartVoice service did not start within 30 seconds")
        return self

    def close(self) -> None:
        if self._thread.is_alive():
            self._server.should_exit = True
            self._thread.join(timeout=30)
            if self._thread.is_alive():
                self._server.force_exit = True
                self._thread.join(timeout=5)
        try:
            self._listener.close()
        finally:
            self._runtime.close()

