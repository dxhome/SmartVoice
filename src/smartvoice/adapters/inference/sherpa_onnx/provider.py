"""CPU inference adapter for the initial SenseVoice and VITS model pair."""

from __future__ import annotations

import io
import json
import threading
import time
import wave
import hashlib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from smartvoice.config.settings import Settings
from smartvoice.domain.errors import InferenceError, InvalidAudioError, InvalidRequestError, ModelUnavailableError
from smartvoice.services.model_catalog import get_model_spec, installed_models

STT_MODEL_ID = "sensevoice-small-local"
TTS_MODEL_ID = "melo-tts-zh-en-local"
TARGET_SAMPLE_RATE = 16000


class SherpaOnnxProvider:
    """Loads models on first use and serializes inference to bound CPU/memory use."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.RLock()
        self._recognizers: dict[str, object] = {}
        self._tts = None
        self._verified_files: dict[str, tuple[int, int, str]] = {}

    def installed_models(self) -> list[dict[str, object]]:
        available = installed_models(self.settings)
        verified = []
        for model in available:
            try:
                self._model_dir(str(model["id"]))
            except ModelUnavailableError:
                continue
            verified.append(model)
        return verified

    def runtime(self) -> dict[str, object]:
        models = self.installed_models()
        return {
            "backend": "sherpa-onnx",
            "requested_device": self.settings.provider,
            "actual_device": "cpu" if self.settings.provider == "cpu" else None,
            "provider_status": "available" if self.settings.provider == "cpu" else "unsupported",
            "installed_model_count": len(models),
            "runtime_version": self._runtime_version(),
            "reason": None if self.settings.provider == "cpu" else "The initial release supports CPU only.",
        }

    def capabilities(self) -> dict[str, object]:
        installed = {str(model["id"]) for model in self.installed_models()}
        tasks = []
        if STT_MODEL_ID in installed:
            tasks.append({"task": "transcription", "model": STT_MODEL_ID, "languages": ["zh", "en", "yue", "ja", "ko"], "streaming": False})
        if TTS_MODEL_ID in installed:
            tasks.append({"task": "speech", "model": TTS_MODEL_ID, "languages": ["zh", "en"], "voices": ["default"], "streaming": False})
        return {"api_version": "v1", "capability_schema_version": "1.0", "backend": "sherpa-onnx", "tasks": tasks}

    def transcribe(self, audio: bytes, language: str = "auto") -> dict[str, object]:
        import av
        import numpy as np

        model_dir = self._model_dir(STT_MODEL_ID)
        spec = get_model_spec(STT_MODEL_ID)
        model_path = self._manifest_path(model_dir, spec.model_file)
        tokens_path = self._manifest_path(model_dir, spec.tokens_file)
        try:
            samples = self._decode_audio(av, audio, self.settings.max_audio_seconds)
        except Exception as exc:
            raise InvalidAudioError("Could not decode this audio. Try a valid WAV, MP3, M4A, or FLAC file.") from exc
        if samples.size == 0:
            raise InvalidAudioError("The uploaded audio is empty.")
        duration = samples.size / TARGET_SAMPLE_RATE
        if duration > self.settings.max_audio_seconds:
            raise InvalidAudioError(f"Audio duration exceeds the {self.settings.max_audio_seconds:g} second limit.")

        with self._lock:
            try:
                recognizer = self._get_recognizer(model_path, tokens_path, language)
                stream = recognizer.create_stream()
                stream.accept_waveform(TARGET_SAMPLE_RATE, samples)
                start = time.perf_counter()
                recognizer.decode_stream(stream)
                elapsed = time.perf_counter() - start
                result = stream.result
            except Exception as exc:
                raise InferenceError("Speech recognition failed.", detail=type(exc).__name__) from exc
        output: dict[str, object] = {
            "text": result.text,
            "language": self._normalize_language(getattr(result, "lang", None)) or (language if language != "auto" else None),
            "duration": round(duration, 3),
            "model": STT_MODEL_ID,
            "device": "cpu",
            "processing_seconds": round(elapsed, 3),
            "rtf": round(elapsed / duration, 4) if duration else None,
        }
        timestamps = list(getattr(result, "timestamps", []) or [])
        tokens = list(getattr(result, "tokens", []) or [])
        if timestamps and tokens and len(timestamps) == len(tokens):
            output["segments"] = [
                {"text": token, "start": round(float(start_time), 3)}
                for token, start_time in zip(tokens, timestamps, strict=False)
            ]
        return output

    @staticmethod
    def _decode_audio(av, audio: bytes, max_audio_seconds: float):
        import numpy as np

        container = av.open(io.BytesIO(audio), mode="r")
        try:
            if not container.streams.audio:
                raise ValueError("No audio stream found")
            stream = container.streams.audio[0]
            resampler = av.AudioResampler(format="fltp", layout="mono", rate=TARGET_SAMPLE_RATE)
            blocks = []
            sample_count = 0
            for frame in container.decode(stream):
                for converted in resampler.resample(frame):
                    block = converted.to_ndarray().reshape(-1).astype(np.float32, copy=False)
                    sample_count += block.size
                    if sample_count > max_audio_seconds * TARGET_SAMPLE_RATE:
                        raise ValueError("Audio exceeds the configured duration limit")
                    blocks.append(block)
            for converted in resampler.resample(None):
                block = converted.to_ndarray().reshape(-1).astype(np.float32, copy=False)
                sample_count += block.size
                if sample_count > max_audio_seconds * TARGET_SAMPLE_RATE:
                    raise ValueError("Audio exceeds the configured duration limit")
                blocks.append(block)
            return np.concatenate(blocks) if blocks else np.empty(0, dtype=np.float32)
        finally:
            container.close()

    def synthesize(self, text: str, voice: str = "default", speed: float = 1.0) -> tuple[bytes, int, float]:
        import numpy as np

        if voice not in {"default", "0"}:
            raise InvalidRequestError("The selected voice is not available in the installed TTS model.")
        model_dir = self._model_dir(TTS_MODEL_ID)
        spec = get_model_spec(TTS_MODEL_ID)
        model_path = self._manifest_path(model_dir, spec.model_file)
        lexicon_path = self._manifest_path(model_dir, spec.lexicon_file or "")
        tokens_path = self._manifest_path(model_dir, spec.tokens_file)
        with self._lock:
            try:
                tts = self._get_tts(model_path, lexicon_path, tokens_path)
                start = time.perf_counter()
                generated = tts.generate(text, sid=0, speed=speed)
                elapsed = time.perf_counter() - start
            except Exception as exc:
                raise InferenceError("Speech synthesis failed.", detail=type(exc).__name__) from exc
        samples = np.asarray(generated.samples, dtype=np.float32)
        sample_rate = int(generated.sample_rate)
        if samples.size == 0 or sample_rate <= 0:
            raise InferenceError("The TTS runtime returned empty audio.")
        duration = samples.size / sample_rate
        pcm = np.clip(samples, -1.0, 1.0)
        pcm = (pcm * 32767.0).astype("<i2", copy=False)
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(pcm.tobytes())
        return output.getvalue(), sample_rate, duration

    def _get_recognizer(self, model_path: Path, tokens_path: Path, language: str):
        if language not in {"auto", "zh", "en", "ja", "ko", "yue"}:
            raise InvalidRequestError(f"Language {language!r} is not supported by this model.")
        if language not in self._recognizers:
            sherpa_onnx = self._sherpa()
            self._recognizers[language] = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(model_path), tokens=str(tokens_path), num_threads=self.settings.num_threads,
                provider=self.settings.provider, language=language, use_itn=True,
            )
        return self._recognizers[language]

    def _get_tts(self, model_path: Path, lexicon_path: Path, tokens_path: Path):
        if self._tts is None:
            sherpa_onnx = self._sherpa()
            config = sherpa_onnx.OfflineTtsConfig(
                model=sherpa_onnx.OfflineTtsModelConfig(
                    vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                        model=str(model_path), lexicon=str(lexicon_path),
                        tokens=str(tokens_path),
                    ),
                    provider=self.settings.provider,
                    num_threads=self.settings.num_threads,
                ),
                max_num_sentences=1,
            )
            if not config.validate():
                raise ValueError("Invalid sherpa-onnx TTS model configuration")
            self._tts = sherpa_onnx.OfflineTts(config)
        return self._tts

    def _model_dir(self, model_id: str) -> Path:
        if self.settings.provider != "cpu":
            raise ModelUnavailableError("Only CPU inference is enabled in this release.")
        directory = self.settings.models_dir / model_id
        manifest = directory / "smartvoice-model.json"
        if not manifest.is_file():
            raise ModelUnavailableError(f"Model {model_id!r} is not installed. Install it with `python -m smartvoice models install {model_id}`.")
        try:
            metadata = json.loads(manifest.read_text(encoding="utf-8"))
            if metadata.get("id") != model_id:
                raise ModelUnavailableError("The installed model manifest ID does not match its directory.")
            for relative, expected_hash in metadata.get("file_sha256", {}).items():
                checked = self._manifest_path(directory, relative)
                stat = checked.stat()
                signature = (stat.st_size, stat.st_mtime_ns)
                cached = self._verified_files.get(str(checked))
                if cached and cached[:2] == signature:
                    digest = cached[2]
                else:
                    with checked.open("rb") as stream:
                        digest = hashlib.file_digest(stream, "sha256").hexdigest()
                    self._verified_files[str(checked)] = (*signature, digest)
                if digest != expected_hash:
                    raise ModelUnavailableError(f"Installed model integrity check failed for {relative!r}.")
        except (OSError, json.JSONDecodeError) as exc:
            raise ModelUnavailableError("The installed model manifest cannot be read.") from exc
        return directory

    @staticmethod
    def _normalize_language(value: str | None) -> str | None:
        if not value:
            return None
        if value.startswith("<|") and value.endswith("|>"):
            return value[2:-2]
        return value

    @staticmethod
    def _manifest_path(model_dir: Path, key: str, *, expect_directory: bool = False) -> Path:
        try:
            manifest = json.loads((model_dir / "smartvoice-model.json").read_text(encoding="utf-8"))
            relative = manifest["files"][key]
        except (OSError, KeyError, json.JSONDecodeError) as exc:
            raise ModelUnavailableError(f"The installed model is incomplete (missing {key}).") from exc
        path = (model_dir / relative).resolve()
        valid_type = path.is_dir() if expect_directory else path.is_file()
        if not path.is_relative_to(model_dir.resolve()) or not valid_type:
            raise ModelUnavailableError(f"The installed model file {key!r} is missing or invalid.")
        return path

    @staticmethod
    def _sherpa():
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise ModelUnavailableError("Install the inference extra with `python -m pip install -e .[inference]`.") from exc
        return sherpa_onnx

    @staticmethod
    def _runtime_version() -> str | None:
        try:
            return version("sherpa-onnx")
        except PackageNotFoundError:
            return None
