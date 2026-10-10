"""Pinned, bounded-memory audio replay helpers for full streaming regression."""
import hashlib
import json
import wave
from pathlib import Path
from smartvoice.services.streaming.planning import resolve, MODES
from smartvoice.services.model_registry import get_model_spec

ROOT=Path(__file__).resolve().parents[1]
MANIFEST=ROOT/'benchmarks/streaming/data/fleurs/clean.manifest.json'


def configuration(mode, language):
    row=dict(type='configure',protocol='smartvoice.stream.v1',mode=mode,
        source_language=language,audio=dict(encoding='pcm_s16le',sample_rate=16000,channels=1),
        output_consumption='delivery')
    if mode!='transcription':row['target_language']='en' if language=='zh' else 'zh'
    return row


def required_models():
    class Catalog:
        get_spec=staticmethod(get_model_spec)
    models=set()
    for mode in MODES:
        for language in ('zh','en'):
            models.update(resolve(configuration(mode,language),Catalog())[0].model_ids)
    return sorted(models)


def fixture(language):
    manifest=json.loads(MANIFEST.read_text())
    case=next(c for c in manifest['cases'] if c['id']==language+'_02_clean')
    path=ROOT/'benchmarks/streaming'/case['path']
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=case['sha256']:
        raise ValueError('Pinned streaming fixture checksum mismatch: '+case['id'])
    with wave.open(str(path),'rb') as source:
        if (source.getframerate(),source.getnchannels(),source.getsampwidth())!=(16000,1,2):
            raise ValueError('Expected PCM16 mono 16 kHz')
        pcm=source.readframes(source.getnframes())
    return case,pcm


def repeated_blocks(pcm, seconds, frame_samples=16000):
    """Generate cyclic PCM without allocating the full hour (115.2 MB)."""
    if not pcm or len(pcm)%2 or seconds<=0 or frame_samples<=0 or frame_samples>16000:
        raise ValueError('Expected nonempty PCM16 and positive bounded duration/frame')
    remaining=round(seconds*16000)*2;cursor=0
    while remaining:
        wanted=min(frame_samples*2,remaining)
        if cursor+wanted<=len(pcm):block=pcm[cursor:cursor+wanted]
        else:
            prefix=pcm[cursor:];cycles,tail=divmod(wanted-len(prefix),len(pcm))
            block=prefix+pcm*cycles+pcm[:tail]
        cursor=(cursor+wanted)%len(pcm)
        yield block
        remaining-=wanted


def input_capacity(session):
    """A test-side credit window; no production queues/limits are disabled."""
    if session.input.qsize()>=2:return False
    pipeline=session.pipeline
    if pipeline and (len(pipeline.coordinator.raw_text())>256 or pipeline.finals.qsize()>=2):return False
    if session.translation_input.qsize()>=2 or session.pending_translation_chars>1024:return False
    if session.speech and (session.speech.queue.qsize()>=2 or session.speech.pending_chars>256):return False
    return True


class OutputAudit:
    """Incremental output audit; retain only text waiting for synthesis."""
    def __init__(self,mode):
        self.mode=mode;self.sequence=0;self.audio_sequence=0;self.counts={}
        self.targets={};self.last_source_end=0;self.digest=hashlib.sha256()
        self.source_chars=self.target_chars=0;self.audio_seconds=0.
        self.first={};self.terminal=None

    def event(self,event,elapsed):
        import io
        kind=event['type'];self.counts[kind]=self.counts.get(kind,0)+1
        if event['event_sequence']!=self.sequence+1:raise AssertionError('Event sequence gap')
        self.sequence=event['event_sequence']
        if kind in ('source_unit_partial','target_partial','target_final','audio_segment') and not event.get('retracted') and (event.get('text','').strip() or kind=='audio_segment'):
            self.first.setdefault(kind,elapsed)
        if kind in ('source_final','utterance_complete'):
            self.last_source_end=max(self.last_source_end,event['end_sample'])
        if kind=='source_final':
            self.source_chars+=len(event['text'])
        if kind in ('source_final','source_unit_final','target_final'):
            self.digest.update(json.dumps((kind,event.get('text','')),ensure_ascii=False).encode())
        if kind=='target_final':
            self.target_chars+=len(event['text'])
            if self.mode=='spoken_interpretation':
                self.targets[event['translation_id']]=dict(text=event['text'],cursor=0,
                    revision=event['revision'],refs=list(event['refs']),chunks=None,next_chunk=0)
                if len(self.targets)>128:raise AssertionError('Unconsumed targets exceed bounded audit window')
        if kind=='audio_skipped':raise AssertionError('Unexpected skipped audio: '+event['reason'])
        if kind=='audio_segment':
            if event['audio_sequence']!=self.audio_sequence+1:raise AssertionError('Audio sequence gap')
            self.audio_sequence=event['audio_sequence']
            target=self.targets[event['translation_id']]
            start,end=event['target_text_start'],event['target_text_end']
            if start<target['cursor'] or target['text'][target['cursor']:start].strip():raise AssertionError('Unspoken target interval')
            if target['text'][start:end].strip()!=event['text']:raise AssertionError('Spoken text differs from committed target')
            if event['revision']!=target['revision'] or list(event['refs'])!=target['refs']:raise AssertionError('Audio source alignment mismatch')
            if event['chunk_index']!=target['next_chunk']:raise AssertionError('Chunk order mismatch')
            if target['chunks'] is not None and target['chunks']!=event['chunk_count']:raise AssertionError('Chunk count changed')
            target['chunks']=event['chunk_count'];target['next_chunk']+=1;target['cursor']=end
            with wave.open(io.BytesIO(event['audio']),'rb') as wav:
                if (wav.getnchannels(),wav.getsampwidth(),wav.getframerate())!=(1,2,event['sample_rate']) or not wav.getnframes():raise AssertionError('Invalid WAV payload')
                duration=wav.getnframes()/wav.getframerate()
            if abs(duration-event['duration_seconds'])>.02:raise AssertionError('Audio duration mismatch')
            self.audio_seconds+=duration
            if event['chunk_index']==event['chunk_count']-1:
                if target['text'][end:].strip():raise AssertionError('Unspoken target suffix')
                del self.targets[event['translation_id']]
        if kind in ('session_complete','error'):self.terminal=event

    def summary(self):
        return dict(counts=self.counts,source_chars=self.source_chars,target_chars=self.target_chars,
            final_text_sha256=self.digest.hexdigest(),audio_chunks=self.audio_sequence,
            audio_seconds=self.audio_seconds,first_output_wall_seconds=self.first,
            unspoken_targets=len(self.targets),last_source_end_sample=self.last_source_end,
            terminal=self.terminal)
