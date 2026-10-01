"""Process runtime for the native Qwen3-TTS inference engine."""

from __future__ import annotations

import http.client
import json
import math
import os
import socket
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from smartvoice.adapters.storage.catalog_model_repository import CatalogModelRepository
from smartvoice.config.settings import Settings
from smartvoice.domain.errors import InferenceError, ModelUnavailableError, UnsupportedFeatureError
from smartvoice.ports.model_repository import ModelRepository


class QwenNativeRuntime:
    """Own binary discovery, the native process, and its loopback HTTP protocol."""

    def __init__(
        self,
        settings: Settings,
        model_repository: ModelRepository | None = None,
        *,
        backend: str = "qwen-tts",
    ) -> None:
        self.settings = settings
        self.model_repository = model_repository or CatalogModelRepository(settings)
        self.backend = backend
        self._inference_lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._port: int | None = None
        self._startup_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._log_lines: deque[str] = deque(maxlen=100)

    @staticmethod
    def binary_path() -> Path | None:
        source_root = Path(__file__).resolve().parents[5]
        package_root = Path(__file__).resolve().parents[3]
        binary_names = ("qwen_tts.exe", "qwen_tts") if os.name == "nt" else ("qwen_tts",)
        candidates = [
            base / name
            for base in (
                package_root / "resources" / "bin",
                source_root / "native" / "qwen3-tts",
            )
            for name in binary_names
        ]
        for candidate in candidates:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
        return None

    def is_running(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    def close(self) -> None:
        """Stop the lazily started native engine during application shutdown."""
        with self._startup_lock:
            process = self._process
            self._process = None
            self._port = None
            if process is None or process.poll() is not None:
                return
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def synthesize(self, model_id: str, text: str, speaker: str, language: str) -> tuple[bytes, float]:
        port = self._ensure_server(model_id)
        payload = json.dumps(
            {"text": text, "speaker": speaker, "language": language},
            ensure_ascii=False,
        ).encode("utf-8")
        wait_started = time.perf_counter()
        with self._inference_lock:
            runtime_wait = time.perf_counter() - wait_started
            connection = http.client.HTTPConnection(
                "127.0.0.1", port,
                timeout=self.settings.inference_execution_timeout_seconds,
            )
            try:
                connection.request(
                    "POST", "/v1/tts", body=payload,
                    headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
                )
                response = connection.getresponse()
                audio = response.read(self.settings.max_tts_output_bytes + 1)
                if response.status != 200:
                    detail = audio[:4096].decode("utf-8", errors="replace")
                    raise InferenceError("Qwen3-TTS speech synthesis failed.", detail=detail)
            except (OSError, http.client.HTTPException) as exc:
                raise InferenceError(
                    "The native Qwen3-TTS engine could not complete synthesis.",
                    detail=self._failure_detail(exc),
                ) from exc
            finally:
                connection.close()
        return audio, runtime_wait

    def _ensure_server(self, model_id: str) -> int:
        with self._startup_lock:
            process = self._process
            if process is not None and process.poll() is None and self._port is not None:
                return self._port
            if process is not None:
                self._process = None
                self._port = None
            binary = self.binary_path()
            if binary is None:
                raise UnsupportedFeatureError("The native C INT8 Qwen3-TTS runtime is unavailable for this platform.")
            if not self._model_exists(model_id):
                raise ModelUnavailableError(
                    f"Model {model_id!r} is not installed or failed integrity validation. "
                    f"Install it with `python -m smartvoice models install {model_id}`."
                )
            model_dir = self.model_repository.model_directory(model_id)
            port = self._free_loopback_port()
            command = [
                str(binary), "-d", str(model_dir), "-j", str(self.settings.num_threads),
                "--int8", "--serve", str(port),
                "--max-request-seconds", str(max(1, math.ceil(self.settings.max_tts_audio_seconds))),
                "--max-text-chars", str(max(1, self.settings.max_tts_characters)),
            ]
            with self._log_lock:
                self._log_lines.clear()
            try:
                process = subprocess.Popen(
                    command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1,
                )
            except OSError as exc:
                raise InferenceError("Could not start the native Qwen3-TTS engine.", detail=str(exc)) from exc
            self._process = process
            self._port = port
            threading.Thread(
                target=self._drain_logs, args=(process,), daemon=True,
                name="smartvoice-qwen3-tts-log",
            ).start()
            if self._wait_for_server(process, port):
                return port
            exit_code = process.poll()
            if exit_code is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            self._process = None
            self._port = None
            detail = self._failure_detail()
            if exit_code is not None:
                unsigned_exit_code = exit_code & 0xFFFFFFFF
                detail = f"Native process exited with code {exit_code} (0x{unsigned_exit_code:08X}). {detail}"
            raise InferenceError(
                "The native Qwen3-TTS engine failed to become ready.",
                detail=detail,
            )

    def _model_exists(self, model_id: str) -> bool:
        return any(
            str(model.get("id")) == model_id and model.get("backend") == self.backend
            for model in self.model_repository.installed_models()
        )

    @staticmethod
    def _free_loopback_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    def _wait_for_server(self, process: subprocess.Popen[str], port: int) -> bool:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return False
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            try:
                connection.request("GET", "/v1/health")
                response = connection.getresponse()
                response.read(1024)
                if response.status == 200:
                    return True
            except (OSError, http.client.HTTPException):
                pass
            finally:
                connection.close()
            time.sleep(0.1)
        return False

    def _drain_logs(self, process: subprocess.Popen[str]) -> None:
        if process.stderr is None:
            return
        for line in process.stderr:
            with self._log_lock:
                self._log_lines.append(line.rstrip())

    def _failure_detail(self, error: Exception | None = None) -> str:
        with self._log_lock:
            diagnostic_lines = [line for line in self._log_lines if "[HTTP] TTS:" not in line]
        diagnostics = "\n".join(diagnostic_lines[-30:])[-8192:]
        if error is not None:
            return f"{type(error).__name__}: {error}"
        return diagnostics or "No diagnostics were reported by the native engine."
