"""CPU adapter for the catalogued Qwen3-TTS CustomVoice model."""

from __future__ import annotations

import http.client
import io
import json
import math
import os
import socket
import subprocess
import threading
import time
import wave
from collections import deque
from pathlib import Path

from smartvoice.config.settings import Settings
from smartvoice.domain.contracts import InstalledModel, SynthesizedSpeech
from smartvoice.domain.errors import (
    InferenceError,
    ModelUnavailableError,
    SpeechOutputTooLargeError,
    UnsupportedFeatureError,
)
from smartvoice.ports.model_repository import ModelRepository
from smartvoice.adapters.storage.catalog_model_repository import CatalogModelRepository

MODEL_ID = "tts-qwen3-0-6b-customvoice"
BACKEND = "qwen-tts"
NATIVE_ENGINE_VERSION = "ef339be58a778b062e1c14382347964552eae007"
SPEAKERS = {
    "aiden": "Aiden",
    "dylan": "Dylan",
    "eric": "Eric",
    "ono_anna": "Ono_Anna",
    "ryan": "Ryan",
    "serena": "Serena",
    "sohee": "Sohee",
    "uncle_fu": "Uncle_Fu",
    "vivian": "Vivian",
}
LANGUAGES = {
    "de": "German",
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "pt": "Portuguese",
    "ru": "Russian",
    "zh": "Chinese",
}
DEFAULT_SPEAKER = "Vivian"


class QwenTTSProvider:
    """Lazily loads Qwen3-TTS and translates it to SmartVoice's speech contract."""

    backend = BACKEND

    def __init__(self, settings: Settings, model_repository: ModelRepository | None = None) -> None:
        self.settings = settings
        self.model_repository = model_repository or CatalogModelRepository(settings)
        self._inference_lock = threading.Lock()
        self._native_process: subprocess.Popen[str] | None = None
        self._native_port: int | None = None
        self._native_startup_lock = threading.Lock()
        self._native_log_lock = threading.Lock()
        self._native_log_lines: deque[str] = deque(maxlen=100)

    @staticmethod
    def _native_binary() -> Path | None:
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

    def installed_models(self) -> list[InstalledModel]:
        if self.settings.provider != "cpu":
            return []
        if self._native_binary() is None:
            return []
        return self._catalogued_models()

    def _catalogued_models(self) -> list[InstalledModel]:
        return [
            model for model in self.model_repository.installed_models()
            if model.get("backend") == self.backend
        ]

    def runtime(self) -> dict[str, object]:
        if self.settings.provider != "cpu":
            return {
                "backend": self.backend,
                "requested_device": self.settings.provider,
                "actual_device": None,
                "provider_status": "unsupported",
                "runtime_version": None,
                "installed_model_count": 0,
                "reason": "Qwen3-TTS currently supports CPU inference in SmartVoice.",
            }
        binary = self._native_binary()
        available = binary is not None
        return {
            "backend": self.backend,
            "requested_device": "cpu",
            "actual_device": "cpu" if available else None,
            "provider_status": "available" if available else "dependency_missing",
            "runtime_version": f"native-c:{NATIVE_ENGINE_VERSION[:12]}" if available else None,
            "installed_model_count": len(self.installed_models()),
            "reason": None if available else "The native C INT8 Qwen3-TTS runtime is not installed for this platform.",
        }

    def capabilities(self) -> dict[str, object]:
        tasks = []
        runtime_available = bool(self.installed_models())
        for model in self._catalogued_models():
            tasks.append({
                "task": model["task"],
                "model": model["id"],
                "backend": self.backend,
                "languages": model["languages"],
                "streaming": False,
                "voices": ["default", *SPEAKERS.values()],
                "voice_count": len(SPEAKERS),
                "speed_control": False,
                "available": runtime_available,
            })
        return {
            "api_version": "v1",
            "capability_schema_version": "1.0",
            "backend": self.backend,
            "tasks": tasks,
        }

    def transcribe(self, audio: bytes, language: str = "auto", model_id: str | None = None):
        raise UnsupportedFeatureError("Qwen3-TTS does not support transcription.")

    def synthesize(
        self,
        text: str,
        voice: str = "default",
        speed: float = 1.0,
        model_id: str | None = None,
        language: str = "auto",
    ) -> SynthesizedSpeech:
        canonical_id = model_id or MODEL_ID
        spec = self.model_repository.get_spec(canonical_id)
        if spec.backend != self.backend or spec.model_type != "qwen3_tts":
            raise UnsupportedFeatureError(f"Model {canonical_id!r} is not supported by the Qwen3-TTS adapter.")
        if self.settings.provider != "cpu":
            raise UnsupportedFeatureError("Qwen3-TTS currently supports CPU inference in SmartVoice.")
        if speed != 1.0:
            raise UnsupportedFeatureError("Qwen3-TTS does not support speed adjustment; use speed 1.0.")
        qwen_language = "Auto" if language == "auto" else LANGUAGES.get(language)
        if qwen_language is None:
            raise UnsupportedFeatureError(f"Qwen3-TTS does not support language {language!r}.")
        speaker = DEFAULT_SPEAKER if voice == "default" else SPEAKERS.get(voice.casefold())
        if speaker is None:
            available = ", ".join(SPEAKERS.values())
            raise UnsupportedFeatureError(f"Qwen3-TTS voice must be default or one of: {available}.")

        return self._synthesize_native(canonical_id, text, speaker, qwen_language)

    def is_model_loaded(self, model_id: str) -> bool:
        process = self._native_process
        return model_id == MODEL_ID and process is not None and process.poll() is None

    def close(self) -> None:
        """Stop the lazily started native engine during application shutdown."""
        with self._native_startup_lock:
            process = self._native_process
            self._native_process = None
            self._native_port = None
            if process is None or process.poll() is not None:
                return
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def _synthesize_native(self, model_id: str, text: str, speaker: str, language: str) -> SynthesizedSpeech:
        if model_id not in {str(model.get("id")) for model in self.installed_models()}:
            raise ModelUnavailableError(
                f"Model {model_id!r} is not installed or failed integrity validation. "
                f"Install it with `python -m smartvoice models install {model_id}`."
            )
        port = self._ensure_native_server(model_id)
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
                    detail=self._native_failure_detail(exc),
                ) from exc
            finally:
                connection.close()
        if len(audio) > self.settings.max_tts_output_bytes:
            raise SpeechOutputTooLargeError("Synthesized speech exceeds the configured audio output limit.")
        try:
            with wave.open(io.BytesIO(audio), "rb") as wav:
                sample_rate = wav.getframerate()
                if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or sample_rate <= 0:
                    raise ValueError("The native runtime returned an unsupported WAV format.")
                duration = wav.getnframes() / sample_rate
        except (wave.Error, EOFError, ValueError, ZeroDivisionError) as exc:
            raise InferenceError(
                "The native Qwen3-TTS engine returned invalid WAV audio.", detail=type(exc).__name__
            ) from exc
        if duration > self.settings.max_tts_audio_seconds:
            raise SpeechOutputTooLargeError("Synthesized speech exceeds the configured audio duration limit.")
        return SynthesizedSpeech(
            audio=audio, sample_rate=sample_rate, duration=duration,
            runtime_wait_seconds=runtime_wait,
        )

    def _ensure_native_server(self, model_id: str) -> int:
        with self._native_startup_lock:
            process = self._native_process
            if process is not None and process.poll() is None and self._native_port is not None:
                return self._native_port
            if process is not None:
                self._native_process = None
                self._native_port = None
            binary = self._native_binary()
            if binary is None:
                raise UnsupportedFeatureError("The native C INT8 Qwen3-TTS runtime is unavailable for this platform.")
            if model_id not in {str(model.get("id")) for model in self.installed_models()}:
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
            with self._native_log_lock:
                self._native_log_lines.clear()
            try:
                process = subprocess.Popen(
                    command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1,
                )
            except OSError as exc:
                raise InferenceError("Could not start the native Qwen3-TTS engine.", detail=str(exc)) from exc
            self._native_process = process
            self._native_port = port
            threading.Thread(
                target=self._drain_native_logs, args=(process,), daemon=True,
                name="smartvoice-qwen3-tts-log",
            ).start()
            if self._wait_for_native_server(process, port):
                return port
            exit_code = process.poll()
            if exit_code is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            self._native_process = None
            self._native_port = None
            raise InferenceError(
                "The native Qwen3-TTS engine failed to become ready.",
                detail=self._native_failure_detail(),
            )

    @staticmethod
    def _free_loopback_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    def _wait_for_native_server(self, process: subprocess.Popen[str], port: int) -> bool:
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

    def _drain_native_logs(self, process: subprocess.Popen[str]) -> None:
        if process.stderr is None:
            return
        for line in process.stderr:
            with self._native_log_lock:
                self._native_log_lines.append(line.rstrip())

    def _native_failure_detail(self, error: Exception | None = None) -> str:
        with self._native_log_lock:
            diagnostic_lines = [line for line in self._native_log_lines if "[HTTP] TTS:" not in line]
        diagnostics = "\n".join(diagnostic_lines[-30:])[-8192:]
        if error is not None:
            return f"{type(error).__name__}: {error}"
        return diagnostics or "No diagnostics were reported by the native engine."
