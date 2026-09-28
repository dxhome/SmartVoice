"""CPU inference adapter for catalogued sherpa-onnx models."""

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
from smartvoice.domain.errors import (
    InferenceError,
    InvalidAudioError,
    ModelUnavailableError,
    SpeechOutputTooLargeError,
    UnsupportedFeatureError,
)
from smartvoice.services.model_catalog import (
    get_model_spec, installed_models, model_directory,
)
from smartvoice.services.host_metrics import host_info, process_metrics, system_memory_info

STT_MODEL_ID = "stt-sensevoice-small-int8"
TTS_MODEL_ID = "tts-melo-zh-en"
TARGET_SAMPLE_RATE = 16000


class SherpaOnnxProvider:
    """Loads models on first use and serializes inference to bound CPU/memory use."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.RLock()
        self._recognizers: dict[tuple[str, str], object] = {}
        self._tts: dict[str, object] = {}
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

    def is_model_loaded(self, model_id: str) -> bool:
        with self._lock:
            return any(key[0] == model_id for key in self._recognizers) or model_id in self._tts

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
            "host": host_info(),
            "system_memory": system_memory_info(),
            "process": process_metrics(),
        }

    def capabilities(self) -> dict[str, object]:
        tasks = []
        for model in self.installed_models():
            task = {"task": model["task"], "model": model["id"], "languages": model["languages"], "streaming": False}
            if model["task"] == "speech":
                task["voices"] = ["default"]
                spec = get_model_spec(str(model["id"]))
                if spec.voice_count:
                    task["voice_count"] = spec.voice_count
            tasks.append(task)
        return {"api_version": "v1", "capability_schema_version": "1.0", "backend": "sherpa-onnx", "tasks": tasks}

    def transcribe(
        self, audio: bytes, language: str = "auto", model_id: str = STT_MODEL_ID
    ) -> dict[str, object]:
        import av
        import numpy as np

        spec = get_model_spec(model_id)
        model_id = spec.id
        model_dir = self._model_dir(model_id)
        if language not in {"auto", *spec.languages}:
            raise UnsupportedFeatureError(f"Language {language!r} is not supported by this model.")
        model_path = self._manifest_path(model_dir, spec.model_file)
        tokens_path = self._manifest_path(model_dir, spec.tokens_file, expect_directory=spec.model_type == "qwen3_asr")
        decoder_path = self._manifest_path(model_dir, spec.decoder_file) if spec.decoder_file else None
        try:
            samples = self._decode_audio(av, audio, self.settings.max_audio_seconds)
        except Exception as exc:
            raise InvalidAudioError(
                "Could not decode this audio. Try a valid WAV, MP3, M4A, or FLAC file.",
                detail=type(exc).__name__,
            ) from exc
        if samples.size == 0:
            raise InvalidAudioError("The uploaded audio is empty.")
        duration = samples.size / TARGET_SAMPLE_RATE
        if duration > self.settings.max_audio_seconds:
            raise InvalidAudioError(f"Audio duration exceeds the {self.settings.max_audio_seconds:g} second limit.")

        with self._lock:
            try:
                recognizer = self._get_recognizer(spec.model_type, model_path, decoder_path, tokens_path, language)
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
            "model": model_id,
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

    def synthesize(
        self, text: str, voice: str = "default", speed: float = 1.0, model_id: str = TTS_MODEL_ID,
        language: str = "auto",
    ) -> tuple[bytes, int, float]:
        import numpy as np

        spec = get_model_spec(model_id)
        model_id = spec.id
        if spec.task != "speech":
            raise UnsupportedFeatureError(f"Model {model_id!r} does not support speech synthesis.")
        if spec.model_type == "kokoro":
            if voice == "default":
                sid = 3 if language == "zh" else 0
            elif voice.isdecimal() and 0 <= int(voice) < 103:
                sid = int(voice)
            else:
                raise UnsupportedFeatureError("Kokoro voice must be default or an integer from 0 to 102.")
        else:
            if voice not in {"default", "0"}:
                raise UnsupportedFeatureError("The selected voice is not available in the installed TTS model.")
            sid = 0
        model_dir = self._model_dir(model_id)
        spec = get_model_spec(model_id)
        model_path = self._manifest_path(model_dir, spec.model_file)
        lexicon_path = ",".join(
            str(self._manifest_path(model_dir, name)) for name in spec.lexicon_file.split(",")
        ) if spec.lexicon_file else None
        tokens_path = self._manifest_path(model_dir, spec.tokens_file) if spec.model_type in {"vits", "kokoro", "matcha"} else None
        data_dir = self._manifest_path(model_dir, spec.data_dir, expect_directory=True) if spec.data_dir else None
        if spec.model_type == "supertonic":
            model_files = {name: self._manifest_path(model_dir, name) for name in spec.tts_files}
        elif spec.model_type == "kokoro":
            model_files = {"model": model_path, "voices": self._manifest_path(model_dir, spec.voices_file or "")}
        elif spec.model_type == "matcha":
            model_files = {"model": model_path, "vocoder": self._manifest_path(model_dir, spec.vocoder_file or "")}
        else:
            model_files = {"model": model_path}
        rule_fsts = ",".join(
            str(self._manifest_path(model_dir, name)) for name in spec.rule_fsts.split(",")
        ) if spec.rule_fsts else None
        with self._lock:
            try:
                tts = self._get_tts(model_id, spec.model_type, model_files or {"model": model_path}, lexicon_path, tokens_path, data_dir, rule_fsts)
                start = time.perf_counter()
                text_chunks = self._split_tts_text(text, 200)
                audio_chunks = []
                sample_rate = 0
                total_samples = 0
                for text_chunk in text_chunks:
                    if spec.model_type == "supertonic":
                        config = self._sherpa().GenerationConfig()
                        config.sid = sid
                        config.speed = speed
                        config.num_steps = 8
                        config.extra["lang"] = language if language != "auto" else "en"
                        generated = tts.generate(text_chunk, config=config)
                    else:
                        generated = tts.generate(text_chunk, sid=sid, speed=speed)
                    chunk_samples = np.asarray(generated.samples, dtype=np.float32)
                    chunk_sample_rate = int(generated.sample_rate)
                    if chunk_samples.size == 0 or chunk_sample_rate <= 0:
                        raise InferenceError("The TTS runtime returned empty audio.")
                    if sample_rate and chunk_sample_rate != sample_rate:
                        raise InferenceError("The TTS runtime changed sample rate between text segments.")
                    sample_rate = chunk_sample_rate
                    total_samples += chunk_samples.size
                    duration_so_far = total_samples / sample_rate
                    pcm_bytes_so_far = total_samples * 2 + 44
                    if duration_so_far > self.settings.max_tts_audio_seconds or pcm_bytes_so_far > self.settings.max_tts_output_bytes:
                        raise SpeechOutputTooLargeError("Synthesized speech exceeds the configured audio output limit.")
                    audio_chunks.append(chunk_samples)
                elapsed = time.perf_counter() - start
            except SpeechOutputTooLargeError:
                raise
            except Exception as exc:
                raise InferenceError("Speech synthesis failed.", detail=type(exc).__name__) from exc
        samples = np.concatenate(audio_chunks)
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

    @staticmethod
    def _split_tts_text(text: str, max_characters: int) -> list[str]:
        chunks = []
        start = 0
        sentence_boundaries = "。！？!?；;"
        while start < len(text):
            end = min(start + max_characters, len(text))
            if end < len(text):
                boundary = max(text.rfind(mark, start + 1, end) for mark in sentence_boundaries)
                if boundary <= start:
                    boundary = text.rfind(" ", start + 1, end)
                if boundary > start:
                    end = boundary + 1
            chunks.append(text[start:end])
            start = end
        return chunks

    def _get_recognizer(self, model_type: str, model_path: Path, decoder_path: Path | None, tokens_path: Path, language: str):
        key = (model_path.parent.name, language)
        if key not in self._recognizers:
            sherpa_onnx = self._sherpa()
            if model_type == "sense_voice":
                recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=str(model_path), tokens=str(tokens_path), num_threads=self.settings.num_threads,
                    provider=self.settings.provider, language=language, use_itn=True,
                )
            elif model_type == "whisper" and decoder_path:
                recognizer = sherpa_onnx.OfflineRecognizer.from_whisper(
                    encoder=str(model_path), decoder=str(decoder_path), tokens=str(tokens_path),
                    num_threads=self.settings.num_threads, provider=self.settings.provider,
                    language="" if language == "auto" else language, task="transcribe",
                )
            elif model_type == "qwen3_asr":
                if decoder_path is None:
                    raise UnsupportedFeatureError("Qwen3-ASR requires its encoder and decoder assets.")
                recognizer = sherpa_onnx.OfflineRecognizer.from_qwen3_asr(
                    conv_frontend=str(model_path),
                    encoder=str(self._manifest_path(model_path.parent, "encoder.int8.onnx")),
                    decoder=str(decoder_path), tokenizer=str(tokens_path),
                    num_threads=self.settings.num_threads, feature_dim=128,
                    max_new_tokens=512, provider=self.settings.provider,
                )
            else:
                raise UnsupportedFeatureError(f"Unsupported sherpa-onnx STT model type {model_type!r}.")
            self._recognizers[key] = recognizer
        return self._recognizers[key]

    def _get_tts(self, model_id: str, model_type: str, paths: dict[str, Path], lexicon_path: str | None, tokens_path: Path | None, data_dir: Path | None, rule_fsts: str | None = None):
        if model_id not in self._tts:
            sherpa_onnx = self._sherpa()
            if model_type == "supertonic":
                model = sherpa_onnx.OfflineTtsSupertonicModelConfig(
                    duration_predictor=str(paths["duration_predictor.int8.onnx"]),
                    text_encoder=str(paths["text_encoder.int8.onnx"]),
                    vector_estimator=str(paths["vector_estimator.int8.onnx"]),
                    vocoder=str(paths["vocoder.int8.onnx"]), tts_json=str(paths["tts.json"]),
                    unicode_indexer=str(paths["unicode_indexer.bin"]), voice_style=str(paths["voice.bin"]),
                )
                model_config = sherpa_onnx.OfflineTtsModelConfig(supertonic=model, provider=self.settings.provider, num_threads=self.settings.num_threads)
            elif model_type == "vits":
                model_config = sherpa_onnx.OfflineTtsModelConfig(
                    vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                        model=str(paths["model"]), lexicon=str(lexicon_path) if lexicon_path else "",
                        tokens=str(tokens_path), data_dir=str(data_dir) if data_dir else "",
                    ), provider=self.settings.provider, num_threads=self.settings.num_threads,
                )
            elif model_type == "kokoro":
                model_config = sherpa_onnx.OfflineTtsModelConfig(
                    kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                        model=str(paths["model"]), voices=str(paths["voices"]),
                        tokens=str(tokens_path), data_dir=str(data_dir) if data_dir else "",
                        lexicon=lexicon_path or "",
                    ), provider=self.settings.provider, num_threads=self.settings.num_threads,
                )
            elif model_type == "matcha":
                model_config = sherpa_onnx.OfflineTtsModelConfig(
                    matcha=sherpa_onnx.OfflineTtsMatchaModelConfig(
                        acoustic_model=str(paths["model"]), vocoder=str(paths["vocoder"]),
                        tokens=str(tokens_path), data_dir=str(data_dir) if data_dir else "",
                        lexicon=lexicon_path or "",
                    ), provider=self.settings.provider, num_threads=self.settings.num_threads,
                )
            else:
                raise UnsupportedFeatureError(f"Unsupported sherpa-onnx TTS model type {model_type!r}.")
            config = sherpa_onnx.OfflineTtsConfig(
                model=model_config,
                max_num_sentences=1,
                rule_fsts=rule_fsts or "",
            )
            if not config.validate():
                raise ValueError("Invalid sherpa-onnx TTS model configuration")
            self._tts[model_id] = sherpa_onnx.OfflineTts(config)
        return self._tts[model_id]

    def _model_dir(self, model_id: str) -> Path:
        if self.settings.provider != "cpu":
            raise UnsupportedFeatureError("This release does not implement inference on the requested device; only CPU is supported.")
        spec = get_model_spec(model_id)
        model_id = spec.id
        directory = model_directory(self.settings, model_id)
        manifest = directory / "smartvoice-model.json"
        if not manifest.is_file():
            raise ModelUnavailableError(f"Model {model_id!r} is not installed. Install it with `python -m smartvoice models install {model_id}`.")
        try:
            metadata = json.loads(manifest.read_text(encoding="utf-8"))
            if metadata.get("id") != spec.id:
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
