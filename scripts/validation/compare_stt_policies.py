"""Local FLEURS policy experiments; does not change product defaults."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import hashlib
import io
import json
import logging
import math
from pathlib import Path
import re
import sys
import time
import wave
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'src'))
from fastapi.testclient import TestClient
from smartvoice.app import create_app
from smartvoice.config.settings import Settings
from smartvoice.adapters.audio.input import BoundedAudioInput
from smartvoice.adapters.inference.sherpa_onnx.provider import SherpaOnnxProvider
from tests.inference_environment import isolated_runtime

MODELS=('stt-sensevoice-small-int8','stt-whisper-base-multilingual-int8','stt-qwen3-asr-600m-int8')


class TracedAudioInput:
    """Record window metadata at the audio port, never audio or transcript text."""
    def __init__(self,inner):self.inner=inner;self.windows=[]
    @contextmanager
    def prepare(self,audio):
        self.windows.clear()
        with self.inner.prepare(audio) as prepared:
            trace=self.windows
            class Traced:
                duration=prepared.duration
                def sample(self,seconds):return prepared.sample(seconds)
                def windows(self,seconds):
                    for window in prepared.windows(seconds):
                        trace.append({'start_seconds':window.start_seconds,'overlap_seconds':window.overlap_seconds})
                        yield window
            yield Traced()

def distance(a,b):
    # Optional benchmark dependency; the fallback preserves identical scoring.
    try:
        from rapidfuzz.distance import Levenshtein
    except ImportError:
        previous=list(range(len(b)+1))
        for i,left in enumerate(a,1):
            current=[i]
            for j,right in enumerate(b,1):
                current.append(min(current[-1]+1,previous[j]+1,previous[j-1]+(left!=right)))
            previous=current
        return previous[-1]
    return Levenshtein.distance(a,b)

def normalize(text):return re.sub(r'[^\w\s]','',text.casefold())

def score(reference,text,language):
    a,b=normalize(reference),normalize(text)
    if language=='en':a,b=a.split(),b.split()
    else:a,b=re.sub(r'\s','',a),re.sub(r'\s','',b)
    errors=distance(a,b)
    return {'metric':'WER' if language=='en' else 'CER','errors':errors,'reference_units':len(a),
            'rate':errors/max(1,len(a))}

def combine(cases,base):
    pcm=[];references=[]
    for case in cases:
        path=base/case['path'];raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=case['sha256']:raise ValueError('Fixture digest mismatch')
        with wave.open(io.BytesIO(raw),'rb') as source:
            assert (source.getframerate(),source.getnchannels(),source.getsampwidth())==(16000,1,2)
            assert source.getnframes()==case['samples']
            pcm.extend([source.readframes(source.getnframes()),b'\0\0'*4800])
        references.append(case['reference'])
    buffer=io.BytesIO()
    with wave.open(buffer,'wb') as destination:
        destination.setnchannels(1);destination.setsampwidth(2);destination.setframerate(16000)
        destination.writeframes(b''.join(pcm))
    return buffer.getvalue(),' '.join(references)


def recording_spans(cases):
    spans=[];offset=0
    for case in cases:
        end=offset+case['samples']/16000
        spans.append({'id':case['id'],'language':case['language'],'start':offset,'end':end})
        offset=end+.3
    return spans


def window_signal(audio):
    """Acoustic energy metadata, not asserted speech or word alignment."""
    import numpy as np
    with wave.open(io.BytesIO(audio),'rb') as source:
        assert (source.getframerate(),source.getnchannels(),source.getsampwidth())==(16000,1,2)
        samples=np.frombuffer(source.readframes(source.getnframes()),dtype='<i2').astype(np.float32)/32768
    usable=len(samples)//320*320
    rms=np.sqrt(np.mean(samples[:usable].reshape(-1,320)**2,axis=1))
    return {'above_quiet_threshold_seconds':round(float(np.sum(rms>=.006))*.02,3)}


def describe_recording_intersections(spans,start,duration):
    end=start+duration
    matching=[span for span in spans if min(end,span['end'])>max(start,span['start'])]
    return {'recording_languages':sorted({span['language'] for span in matching}),
            'recording_ids':[span['id'] for span in matching],
            'partially_covered_recordings':sum(span['start']<start or span['end']>end for span in matching)}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,default=ROOT/'sandbox/streaming/data/real.manifest.json')
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--legacy-only',action='store_true',help='Probe the previous first-window language pinning on mixed input')
    parser.add_argument('--diagnostic',action='store_true',help='Compare all clean original clips with concatenation under the declared policy')
    parser.add_argument('--quiet-boundaries',action='store_true',help='Compare declared windows with early quiet cuts on ten recordings per language')
    parser.add_argument('--relative-boundaries',action='store_true',help='Probe a relative-energy threshold on the same ten recordings per language')
    parser.add_argument('--minimum-quiet-seconds',type=float,default=2,help='Minimum search position for early-cut candidates (default: 2)')
    args=parser.parse_args();manifest=json.loads(args.manifest.read_text())
    if sum((args.legacy_only,args.diagnostic,args.quiet_boundaries,args.relative_boundaries))>1:parser.error('Choose one experiment mode')
    if not math.isfinite(args.minimum_quiet_seconds) or not 1<args.minimum_quiet_seconds<15:
        parser.error('Minimum quiet search must be greater than the overlap and below the smallest tested window (15 seconds)')
    logging.getLogger('smartvoice.api').setLevel(logging.WARNING)
    import sherpa_onnx
    if sherpa_onnx.__version__!='1.13.8+smartvoice.whisper2':raise RuntimeError('Requires validated Whisper repair wheel')
    sample_count=10 if args.diagnostic or args.quiet_boundaries or args.relative_boundaries else 4
    selected={language:[c for c in manifest['cases'] if c['language']==language and c['variant']=='clean'][:sample_count]
              for language in ('zh','en')}
    if any(len(cases)!=sample_count for cases in selected.values()):raise ValueError('Missing clean cases')
    mixed=[case for pair in zip(selected['zh'],selected['en']) for case in pair]
    inputs=[(lang,cases,*combine(cases,args.manifest.parent.parent)) for lang,cases in selected.items()]
    inputs.append(('mixed',mixed,*combine(mixed,args.manifest.parent.parent)))
    report={'corpus':{k:manifest[k] for k in ('dataset','revision','license','attribution','split')},'cases':[],
            'normalization':'casefold and punctuation removal; no simplified/traditional conversion',
            'completed':False}
    args.report.parent.mkdir(parents=True,exist_ok=True)
    try:
        # One model per environment avoids accumulating unrelated native caches.
        for model in MODELS:
            initializations=[]
            original=SherpaOnnxProvider._get_recognizer
            def tracked(runtime,*args):
                before=len(runtime._recognizers)
                result=original(runtime,*args)
                if len(runtime._recognizers)>before:initializations.append({'model':args[1].parent.name,'requested_language':args[-1]})
                return result
            with patch.object(SherpaOnnxProvider,'_get_recognizer',tracked), isolated_runtime(Settings.from_env()) as (settings,provider):
                if model not in {m['id'] for m in provider.installed_models()}:raise RuntimeError(f'Missing runtime: {model}')
                declared=provider.segment_limits
                app=create_app(settings,provider)
                logging.getLogger('smartvoice.api').setLevel(logging.WARNING)
                with TestClient(app) as client:
                    if args.diagnostic:
                        original_texts={}
                        for language,cases in selected.items():
                            for requested in (language,'auto'):
                                total_errors=total_units=0
                                for case in cases:
                                    raw=(args.manifest.parent.parent/case['path']).read_bytes()
                                    if hashlib.sha256(raw).hexdigest()!=case['sha256']:raise ValueError('Fixture digest mismatch')
                                    with wave.open(io.BytesIO(raw),'rb') as source:
                                        duration=source.getnframes()/source.getframerate()
                                    if duration>float(declared(model)['audio_seconds']):raise ValueError('Baseline must fit one declared window')
                                    started=time.perf_counter()
                                    response=client.post('/v1/audio/transcriptions',files={'file':('original.wav',raw,'audio/wav')},
                                        data={'model':model,'language':requested,'response_format':'verbose_json','timestamps':'true'})
                                    assert response.status_code==200,(model,case['id'],response.status_code)
                                    result=response.json();assert result.get('chunk_count',1)==1
                                    original_texts[(case['id'],requested)]=result['text']
                                    quality=score(case['reference'],result['text'],language)
                                    total_errors+=quality['errors'];total_units+=quality['reference_units']
                                    report['cases'].append({'model':model,'policy':'original-unsegmented','id':case['id'],
                                        'language':language,'requested_language':requested,'duration':duration,
                                        'seconds':round(time.perf_counter()-started,3),'quality':quality,
                                        'native_segment_count':len(result.get('segments',[]))})
                                row={'model':model,'policy':'original-aggregate','language':language,
                                     'requested_language':requested,'errors':total_errors,'reference_units':total_units,
                                     'rate':total_errors/max(1,total_units)}
                                report['cases'].append(row);print(json.dumps(row),flush=True)
                        for language,cases,raw,reference in inputs:
                            requested='auto' if language=='mixed' else language
                            row={'model':model,'policy':'original-recording-boundaries','language':language,
                                 'quality':score(reference,' '.join(original_texts[(case['id'],requested)] for case in cases),language)}
                            report['cases'].append(row);print(json.dumps(row),flush=True)
                    policies=([('legacy-first-language',float(declared(model)['audio_seconds']),True)] if args.legacy_only
                              else [('declared',float(declared(model)['audio_seconds']),True)] if args.diagnostic
                              else [('declared',float(declared(model)['audio_seconds']),True),
                                    ('early-quiet-all',float(declared(model)['audio_seconds']),True),
                                    ('early-quiet-forced',float(declared(model)['audio_seconds']),False)] if args.quiet_boundaries
                              else [('declared-relative-all',float(declared(model)['audio_seconds']),True),
                                    ('early-relative-forced',float(declared(model)['audio_seconds']),False)] if args.relative_boundaries
                              else [('15-all',15,True),('25-all',25,True),('25-forced',25,False)])
                    for label,seconds,all_cuts in policies:
                        # Explicit experimental overrides, never persisted.
                        provider.segment_limits=lambda model_id,seconds=seconds:{**declared(model_id),'audio_seconds':seconds}
                        traced=TracedAudioInput(BoundedAudioInput(settings.max_audio_seconds,overlap_on_silence=all_cuts,
                            minimum_window_seconds=args.minimum_quiet_seconds if label.startswith('early-') else None,
                            relative_quiet='relative' in label))
                        app.state.transcription_service.audio_input=traced
                        for language,cases,raw,reference in inputs:
                            if args.legacy_only and language!='mixed':continue
                            if args.legacy_only:
                                # Reconstruct only the prior native-call language
                                # sequence; do not restore old response semantics.
                                native=provider.transcribe;locked=[None];requested=[]
                                supported=set(app.state.model_repository.get_spec(model).languages)
                                def legacy(audio,language,model_id):
                                    effective=locked[0] or language;requested.append(effective)
                                    result=native(audio,effective,model_id)
                                    detected=str(result.get('language') or '').lower()
                                    if locked[0] is None and detected in supported and detected!='auto':locked[0]=detected
                                    return result
                                provider.transcribe=legacy
                            if args.diagnostic or args.quiet_boundaries or args.relative_boundaries:
                                native=provider.transcribe;window_results=[];signals=[]
                                def capture(*args,**kwargs):
                                    result=native(*args,**kwargs);window_results.append(result)
                                    signals.append(window_signal(args[0]));return result
                                provider.transcribe=capture
                            started=time.perf_counter()
                            response=client.post('/v1/audio/transcriptions',files={'file':('human.wav',raw,'audio/wav')},
                                data={'model':model,'language':'auto' if language=='mixed' else language,'response_format':'verbose_json','timestamps':'true'})
                            row={'model':model,'policy':label,'language':language,'ids':[c['id'] for c in cases],
                                 'status':response.status_code,'seconds':round(time.perf_counter()-started,3),
                                 'window_limit_seconds':seconds,'overlap_on_silence':all_cuts,
                                 'minimum_window_seconds':args.minimum_quiet_seconds if label.startswith('early-') else None,
                                 'relative_quiet':'relative' in label}
                            report['cases'].append(row)
                            assert response.status_code==200,row
                            result=response.json()
                            row.update(duration=result['duration'],chunks=result.get('chunk_count',1),resolved_language=result.get('language'),
                                       text_characters=len(result['text']),quality=score(reference,result['text'],language),
                                       recognizer_initializations=list(initializations))
                            if args.legacy_only:
                                row['native_requested_languages']=requested
                                provider.transcribe=native
                            if args.diagnostic or args.quiet_boundaries or args.relative_boundaries:
                                provider.transcribe=native
                                row['raw_window_concatenation_quality']=score(reference,' '.join(str(r.get('text','')) for r in window_results),language)
                                row['native_segment_count']=sum(len(r.get('segments',[])) for r in window_results)
                                row['returned_segment_count']=len(result.get('segments',[]))
                                row['merge_removed_characters']=sum(len(str(r.get('text',''))) for r in window_results)+max(0,len(window_results)-1)-len(result['text'])
                                assert len(traced.windows)==len(window_results)
                                spans=recording_spans(cases)
                                row['windows']=[{**position,**signal,
                                                **describe_recording_intersections(spans,position['start_seconds'],float(r['duration'])),
                                                'duration':r.get('duration'),'language':r.get('language'),
                                                'text_characters':len(str(r.get('text',''))),
                                                'native_segments':len(r.get('segments',[]))}
                                               for position,r,signal in zip(traced.windows,window_results,signals)]
                                row['multi_language_recording_windows']=sum(len(w['recording_languages'])>1 for w in row['windows'])
                                assert abs(row['windows'][-1]['start_seconds']+float(row['windows'][-1]['duration'])-result['duration'])<.01
                                if result.get('segments'):
                                    starts=[item['start'] for item in result['segments']]
                                    assert starts==sorted(starts),'Global timestamps must remain ordered'
                            print(json.dumps(row),flush=True)
                    provider.segment_limits=declared
        report['completed']=True
    finally:args.report.write_text(json.dumps(report,indent=2),encoding='utf-8')

if __name__=='__main__':main()
