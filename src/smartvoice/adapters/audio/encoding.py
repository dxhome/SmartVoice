"""All WAV/codec/file implementation details live in this adapter."""
import io
import tempfile
import wave
from contextlib import contextmanager
from pathlib import Path


from smartvoice.domain.contracts import SynthesizedSpeech as Audio
from smartvoice.domain.errors import SmartVoiceError, InferenceError, UnsupportedFeatureError, SpeechOutputTooLargeError
from smartvoice.ports.inference_context import check_execution

def _failure(code, message, status=400):
    if code == "output_limit": return SpeechOutputTooLargeError(message)
    if code == "format_unavailable": return UnsupportedFeatureError(message)
    return InferenceError(message)



def pcm_wav(samples, rate):
    import numpy as np
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(rate)
        out.writeframes(pcm.tobytes())
    return Audio(buf.getvalue(), rate, len(pcm) / rate)


class AudioAssembler:
    def __init__(self, work: Path, max_bytes: int, max_seconds: float):
        self.work, self.max_bytes, self.max_seconds = work, max_bytes, max_seconds

    @contextmanager
    def session(self):
        self.work.mkdir(parents=True, exist_ok=True)
        # Bounded RAM spill, private unique files, automatic close on every path.
        with tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b", dir=self.work) as buf:
            with wave.open(buf, "wb") as out:
                out.setnchannels(1); out.setsampwidth(2); out.setframerate(24000)
                state = {"rate": 0, "samples": 0}

                def append(audio):
                    check_execution()
                    if audio.audio_format != "wav":
                        raise _failure("invalid_audio_contract", "Expected canonical WAV", 500)
                    with wave.open(io.BytesIO(audio.audio), "rb") as src:
                        rate = src.getframerate()
                        if src.getnchannels() != 1 or src.getsampwidth() != 2 or rate <= 0:
                            raise _failure("invalid_audio_contract", "Expected mono PCM16", 500)
                        if state["rate"] and rate != state["rate"]:
                            raise _failure("sample_rate_changed", "Segment sample rates differ", 500)
                        if not src.getnframes():
                            raise _failure("empty_audio", "Empty segment", 500)
                        total = state["samples"] + src.getnframes()
                        if total / rate > self.max_seconds or total * 2 + 44 > self.max_bytes:
                            raise _failure("output_limit", "Cumulative audio exceeds limit", 413)
                        if not state["rate"]: out.setframerate(rate)
                        state.update(rate=rate, samples=total)
                        pcm = src.readframes(src.getnframes())
                        if len(pcm) != src.getnframes() * 2:
                            raise InferenceError("The TTS adapter returned truncated PCM audio.")
                        out.writeframes(pcm)

                def finish():
                    check_execution()
                    out.close(); buf.seek(0)
                    content = buf.read(self.max_bytes + 1)
                    if len(content) > self.max_bytes:
                        raise _failure("output_limit", "Internal audio exceeds limit", 413)
                    return Audio(content, state["rate"], state["samples"] / state["rate"])

                yield append, finish


class AudioEncoder:
    def __init__(self, bitrate=96000, max_bytes=32 * 1024 * 1024, max_internal_bytes=32 * 1024 * 1024, max_seconds=180):
        self.bitrate, self.max_bytes = bitrate, max_bytes
        self.max_internal_bytes, self.max_seconds = max_internal_bytes, max_seconds
        try:
            import av
            av.codec.Codec("libmp3lame", "w")
            self.reason = None
        except Exception as exc:
            self.reason = type(exc).__name__

    def capabilities(self):
        return {"default_format": "mp3", "bitrate": self.bitrate, "streaming": False, "formats": {
            "wav": {"available": True, "reason": None},
            "mp3": {"available": self.reason is None, "reason": self.reason}}}

    def validate(self, target):
        item = self.capabilities()["formats"].get(target)
        if item is None or not item["available"]:
            raise _failure("format_unavailable", "Requested output format is unavailable", 501)

    def encode(self, audio: Audio, target: str):
        self.validate(target); check_execution()
        if audio.audio_format != "wav":
            raise _failure("invalid_audio_contract", "Expected canonical WAV", 500)
        if len(audio.audio) > self.max_internal_bytes or audio.duration > self.max_seconds:
            raise SpeechOutputTooLargeError("Canonical audio exceeds the configured output limit.")
        if target == "wav":
            if len(audio.audio) > self.max_bytes:
                raise _failure("output_limit", "Response exceeds limit", 413)
            return audio
        output = io.BytesIO()
        try:
            import av
            with av.open(io.BytesIO(audio.audio)) as src, av.open(output, "w", format="mp3") as dst:
                stream = dst.add_stream("libmp3lame", rate=audio.sample_rate)
                stream.layout = "mono"; stream.bit_rate = self.bitrate
                resampler = av.AudioResampler(format=stream.codec_context.format.name,
                                              layout="mono", rate=stream.rate)

                def write(frame):
                    check_execution()
                    for packet in stream.encode(frame):
                        dst.mux(packet)
                    if output.tell() > self.max_bytes:
                        raise _failure("output_limit", "Encoded response exceeds limit", 413)

                for frame in src.decode(audio=0):
                    for converted in resampler.resample(frame):
                        write(converted)
                for frame in resampler.resample(None):
                    write(frame)
                write(None)
            check_execution()
            if len(output.getvalue()) > self.max_bytes:
                raise _failure("output_limit", "Encoded response exceeds limit", 413)
            return Audio(output.getvalue(), stream.rate, audio.duration, audio.runtime_wait_seconds, "mp3")
        except SmartVoiceError:
            raise
        except Exception as exc:
            raise _failure("encoding_failed", type(exc).__name__, 500) from exc
