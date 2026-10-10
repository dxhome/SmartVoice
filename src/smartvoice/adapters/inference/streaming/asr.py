"""Native online ASR adapter with session-owned recognizer state."""
import numpy as np
from smartvoice.domain.streaming import Hypothesis,StageError
from smartvoice.adapters.inference.runtime.stream_profile import span
from .frontend import CausalGain
RATE=16000

class OnlineASR:
    partial_support = True

    def __init__(self,settings,spec,plan,recognizer=None):
        import sherpa_onnx
        from .assets import model_files
        if recognizer is None:
            files=model_files(settings,spec);cap=spec.streaming
            kwargs=dict(tokens=str(files[spec.tokens_file]),encoder=str(files[spec.model_file]),decoder=str(files[spec.decoder_file]),
                num_threads=plan.asr_threads,enable_endpoint_detection=True,rule1_min_trailing_silence=2.4,
                rule2_min_trailing_silence=0.8,rule3_min_utterance_length=10.0)
            if cap['architecture']=='paraformer':self.recognizer=sherpa_onnx.OnlineRecognizer.from_paraformer(**kwargs)
            else:self.recognizer=sherpa_onnx.OnlineRecognizer.from_transducer(**kwargs,joiner=str(files[cap['joiner_file']]),model_type=cap['architecture'])
        else:
            self.recognizer=recognizer
        language=plan.source_language
        self.gain=CausalGain(plan.input_policy)
        self.stream = self.recognizer.create_stream()
        self.language, self.total, self.start, self.utterance, self.revision = language, 0, 0, 0, 0
        self.context_version = 0
        self.context_snapshot = None
        self.pending_context = None
        self.last = ''
        self.received = 0
        self.pending_pcm = b''
        self.plan={'model_id':spec.id,'partial_support':True}

    def _event(self, kind, text, reason=None):
        self.revision += 1
        return {'type': kind, 'utterance_id': self.utterance, 'revision': self.revision,
                'text': text, 'language': self.language, 'start_sample': self.start,
                'end_sample': self.total, 'final': kind == 'source_final', 'reason': reason, 'context_version': self.context_version}

    def _decode(self, endpoint=True):
        while self.recognizer.is_ready(self.stream):
            with span('asr.decode_stream'):self.recognizer.decode_stream(self.stream)
        with span('asr.get_result'):text = self.recognizer.get_result(self.stream)
        events = []
        if text and text != self.last:
            events.append(self._event('source_partial', text))
            self.last = text
        if endpoint and self.recognizer.is_endpoint(self.stream):
            events.extend(self._commit(text, 'forced_length' if self.total-self.start>=10*RATE else 'endpoint'))
            self.recognizer.reset(self.stream)
        return events

    def _commit(self, text, reason):
        events = [self._event('source_final', text, reason)] if text else []
        events.append({'type': 'utterance_complete', 'utterance_id': self.utterance,
                       'start_sample': self.start, 'end_sample': self.total, 'reason': reason})
        self.utterance += 1
        self.start, self.revision, self.last = self.total, 0, ''
        return events

    context_support = {"recent_text": False, "hotwords": False, "native_stream_state": True}

    def update_context(self, snapshot):
        # Current greedy adapters do not accept transcript prompts or hotwords.
        # Record a version at acoustic boundaries without altering recognition.
        self.pending_context = snapshot

    def release_stream(self):
        self.pending_pcm = b''
        self.stream = None
        self.recognizer = None

    def accept(self, samples):
        if self.total == self.start and self.pending_context is not None:
            self.context_snapshot = self.pending_context
            self.context_version = self.context_snapshot.version
            self.pending_context = None
        self.total += len(samples)
        with span('asr.accept_waveform'):self.stream.accept_waveform(RATE, samples)
        return self._decode()

    def _finish(self):
        # Feature/decoder tail padding is internal and excluded from source ranges.
        self.stream.accept_waveform(RATE, np.zeros(int(0.5 * RATE), dtype=np.float32))
        self.stream.input_finished()
        events = self._decode(endpoint=False)
        if self.total > self.start:
            events.extend(self._commit(self.recognizer.get_result(self.stream), 'input_finished'))
        return events

    def _push_pcm(self,pcm):
        samples=self.gain.process(np.frombuffer(pcm,dtype='<i2').astype(np.float32)/32768.0)
        return [Hypothesis.from_adapter(e,self.language) for e in self.accept(samples)]
    def push_audio(self,block):
        if block.start_sample!=self.received or not block.pcm or len(block.pcm)%2:
            raise StageError('invalid_frame','Unaligned PCM block')
        self.received+=len(block.pcm)//2
        pcm=self.pending_pcm+block.pcm;events=[];complete=len(pcm)//640*640
        # Preserve the pinned 20 ms frontend policy regardless of network packet size.
        for offset in range(0,complete,640):events.extend(self._push_pcm(pcm[offset:offset+640]))
        self.pending_pcm=pcm[complete:]
        return events
    def finish(self):
        events=self._push_pcm(self.pending_pcm) if self.pending_pcm else []
        self.pending_pcm=b''
        return events+[Hypothesis.from_adapter(e,self.language) for e in self._finish()]
    def close(self):
        self.pending_pcm = b''
        self.stream = None
        self.recognizer = None
