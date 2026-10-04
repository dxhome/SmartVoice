"""P3: decode once to bounded private storage, plan windows and merge results."""
import io
import math
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass


from smartvoice.domain.errors import SmartVoiceError, InvalidAudioError, AudioTooLargeError
from smartvoice.ports.audio import (
    AudioWindow,
    DEFAULT_TRANSCRIPTION_OVERLAP_SECONDS,
    DEFAULT_TRANSCRIPTION_WINDOW_SECONDS,
)
from smartvoice.ports.diagnostics import stage
from smartvoice.ports.inference_context import check_execution
import wave
from smartvoice.domain.contracts import SynthesizedSpeech

def pcm_wav(samples, rate):
    import numpy as np
    buffer=io.BytesIO()
    with wave.open(buffer,"wb") as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(rate)
        out.writeframes((np.clip(samples,-1,32767/32768)*32768).astype("<i2").tobytes())
    return SynthesizedSpeech(buffer.getvalue(),rate,len(samples)/rate)

def _failure(code, message, status=400):
    return AudioTooLargeError(message) if code == "input_limit" else InvalidAudioError(message)


RATE = 16000

@dataclass(frozen=True)
class Window:
    start: int
    end: int
    overlap: int = 0

@contextmanager
def _decode(audio, maximum=600):
    import av
    import numpy as np
    # 1 MiB in RAM; long input spills to a private, automatically removed file.
    with tempfile.SpooledTemporaryFile(max_size=1024*1024, mode='w+b') as storage:
        total=0
        with stage("decode"):
            try:
                with av.open(io.BytesIO(audio)) as source:
                    if not source.streams.audio: raise ValueError('No audio stream')
                    resampler=av.AudioResampler(format='s16',layout='mono',rate=RATE)
                    def append(frame):
                        nonlocal total
                        check_execution()
                        block=frame.to_ndarray().reshape(-1).astype('<i2',copy=False)
                        total+=block.size
                        if total>int(maximum*RATE): raise _failure('input_limit','Audio duration exceeds limit',413)
                        storage.write(block.tobytes())
                    for frame in source.decode(source.streams.audio[0]):
                        for converted in resampler.resample(frame): append(converted)
                    for converted in resampler.resample(None): append(converted)
                if not total: raise ValueError('Empty audio')
            except SmartVoiceError: raise
            except Exception as exc: raise _failure('invalid_audio','Could not decode audio',400) from exc
        def read(start,end):
            storage.seek(start*2)
            return np.frombuffer(storage.read((end-start)*2),dtype='<i2').astype(np.float32)/32768
        yield total,read

def plan(total,read,seconds=DEFAULT_TRANSCRIPTION_WINDOW_SECONDS,
         overlap_seconds=DEFAULT_TRANSCRIPTION_OVERLAP_SECONDS,minimum_seconds=None,
         overlap_on_silence=True,relative_quiet=False):
    import numpy as np
    if (not isinstance(overlap_on_silence,bool) or not isinstance(relative_quiet,bool)
            or not math.isfinite(seconds) or not math.isfinite(overlap_seconds)
            or overlap_seconds < 0 or seconds < 2
            or (minimum_seconds is not None and
                (not math.isfinite(minimum_seconds) or not 0 <= minimum_seconds < seconds))):
        raise ValueError('Invalid window policy')
    maximum=int(seconds*RATE); overlap=int(overlap_seconds*RATE)
    if maximum<=overlap or seconds<2: raise ValueError('Invalid window policy')
    start=0; previous_end=0
    while start<total:
        quiet_cut=False
        end=min(start+maximum,total)
        if end<total:
            # Prefer >=200ms quiet intervals in the last third of a window.
            search_start=start+int((minimum_seconds if minimum_seconds is not None else seconds*2/3)*RATE)
            samples=read(search_start,end)
            width=320; usable=len(samples)//width*width
            rms=np.sqrt(np.mean(samples[:usable].reshape(-1,width)**2,axis=1))
            threshold=.006
            if relative_quiet and rms.size:
                peak=float(np.percentile(rms,95))
                # An explicit experimental policy: low-amplitude speech must
                # not become silence solely because of the absolute threshold.
                if peak>0:threshold=min(threshold,peak*.1)
            quiet=rms<threshold; runs=[]; begin=None
            for index,value in enumerate(list(quiet)+[False]):
                if value and begin is None: begin=index
                if not value and begin is not None:
                    if index-begin>=10: runs.append((begin,index))
                    begin=None
            if runs:
                left,right=runs[0]; end=search_start+(left+right)//2*width
                quiet_cut=True
        if end <= start or end <= previous_end:
            raise ValueError('Window policy does not advance audio coverage')
        next_start=end-overlap if overlap_on_silence or not quiet_cut else end
        if end < total and next_start <= start:
            raise ValueError('Window policy does not advance the next window')
        yield Window(start,end,max(0,previous_end-start))
        # The final window already covers the tail. Rewinding it would repeat
        # that same tail forever when overlap is nonzero.
        if end == total:
            return
        previous_end=end
        # The production default overlaps all cuts. Experiments can compare
        # forced-cut-only overlap without changing the service orchestration.
        start=next_start


class BoundedAudioInput:
    def __init__(self, maximum_seconds=600, *, overlap_on_silence=True,
                 minimum_window_seconds=None,relative_quiet=False):
        if not isinstance(relative_quiet,bool):raise ValueError('Invalid relative quiet policy')
        if minimum_window_seconds is not None and (
                not math.isfinite(minimum_window_seconds) or minimum_window_seconds < 0):
            raise ValueError('Invalid minimum window seconds')
        self.maximum_seconds=maximum_seconds
        self.overlap_on_silence=overlap_on_silence
        self.minimum_window_seconds=minimum_window_seconds
        self.relative_quiet=relative_quiet

    @contextmanager
    def prepare(self, audio):
        overlap_on_silence=self.overlap_on_silence
        minimum=self.minimum_window_seconds
        relative=self.relative_quiet
        with _decode(audio,self.maximum_seconds) as (total,read):
            class Prepared:
                duration=total/RATE
                def sample(self,seconds):
                    return pcm_wav(read(0,min(total,int(seconds*RATE))),RATE).audio
                def windows(self,seconds=DEFAULT_TRANSCRIPTION_WINDOW_SECONDS,
                            overlap_seconds=DEFAULT_TRANSCRIPTION_OVERLAP_SECONDS):
                    for window in plan(total,read,seconds,overlap_seconds,minimum_seconds=minimum,
                                       overlap_on_silence=overlap_on_silence,relative_quiet=relative):
                        check_execution()
                        samples=read(window.start,window.end)
                        # Exact digital silence only; quiet speech is retained.
                        yield AudioWindow(pcm_wav(samples,RATE).audio,
                                          window.start/RATE,window.overlap/RATE)
            yield Prepared()
