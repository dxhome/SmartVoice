"""Three fresh-process paired window trials over continuous and stitched speech."""
from __future__ import annotations
import argparse
from dataclasses import replace
import io
import json
import logging
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import wave

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'src'))
from scripts.validation.compare_stt_policies import MODELS, combine, score
from scripts.validation.stt_validation_common import TraceStore, WindowTrace, check_coverage, fingerprint, provenance
from scripts.validation.stt_window_policies import POLICIES

CANDIDATE_MINIMUM={MODELS[0]:8,MODELS[1]:8,MODELS[2]:2}


def corpus(manifest_path):
    manifest=json.loads(manifest_path.read_text())
    if not manifest.get('completed'):raise RuntimeError('Continuous corpus incomplete')
    cases=[]
    for row in manifest['cases']:
        path=manifest_path.parent/row['path']
        if fingerprint(path)!=row['sha256']:raise ValueError('Continuous digest mismatch')
        cases.append({**row,'audio':path.read_bytes()})
    fleurs_path=ROOT/'sandbox/streaming/data/real.manifest.json'
    fleurs=json.loads(fleurs_path.read_text())
    selected={lang:[c for c in fleurs['cases'] if c['language']==lang and c['variant']=='clean'][:10] for lang in ('zh','en')}
    if any(len(rows)!=10 for rows in selected.values()):raise ValueError('Missing FLEURS originals')
    selected['mixed']=[c for pair in zip(selected['zh'],selected['en']) for c in pair]
    for lang,rows in selected.items():
        audio,reference=combine(rows,fleurs_path.parent.parent)
        with wave.open(io.BytesIO(audio),'rb') as source:duration=source.getnframes()/source.getframerate()
        cases.append({'id':f'fleurs-{lang}','language':lang,'group':'stitched','sha256':__import__('hashlib').sha256(audio).hexdigest(), 'duration':duration,'reference':reference,'audio':audio,'overlap_seconds':0,'endpoint_crossing_annotations':0,'human_audit':'pending'})
    return cases


def worker(model,policy,repeat,manifest_path,report_path=None,candidate_policy=None):
    import httpx,psutil,sherpa_onnx,uvicorn
    from smartvoice.app import create_app
    from smartvoice.config.settings import Settings
    from smartvoice.adapters.audio.input import BoundedAudioInput
    from tests.inference_environment import isolated_runtime
    if model==MODELS[1] and sherpa_onnx.__version__!='1.13.8+smartvoice.whisper2':raise RuntimeError('Requires Whisper repair wheel')
    inputs=corpus(manifest_path);source=Settings.from_env()
    report={'model':model,'policy':policy,'candidate_policy':candidate_policy,'repeat':repeat,'provenance':provenance(source),'pid':os.getpid(),'parent_pid':os.getppid(),'cases':[],'completed':False}
    if report_path:report_path.write_text(json.dumps(report,indent=2))
    stop=threading.Event();memory=[];process=psutil.Process()
    def sample_memory():
        while not stop.wait(.1):memory.append(process.memory_info().rss/2**20)
    sampler=threading.Thread(target=sample_memory,daemon=True);sampler.start()
    try:
        with isolated_runtime(source) as (settings,provider):
            if model not in {m['id'] for m in provider.installed_models()}:raise RuntimeError('Missing installed model')
            native=provider.transcribe;native_results=[]
            def capture(*args,**kwargs):
                result=native(*args,**kwargs);native_results.append(result);return result
            provider.transcribe=capture
            named=POLICIES[candidate_policy] if candidate_policy and policy=='candidate' else None
            minimum=named.minimum_window_seconds if named else CANDIDATE_MINIMUM[model] if policy=='candidate' else None
            relative=named.relative_quiet if named else policy=='candidate'
            overlap=named.overlap_on_silence if named else policy=='current'
            report['policy_configuration']={'window_seconds':provider.segment_limits(model)['audio_seconds'], 'minimum_seconds':minimum,'relative_quiet':relative,'overlap_on_silence':overlap}
            app=create_app(settings,provider);logging.getLogger('smartvoice.api').setLevel(logging.WARNING)
            if policy=='candidate':
                app.state.transcription_service.audio_input=BoundedAudioInput(settings.max_audio_seconds,
                    overlap_on_silence=overlap,minimum_window_seconds=minimum,relative_quiet=relative)
            windows=WindowTrace(app.state.transcription_service.audio_input);app.state.transcription_service.audio_input=windows
            trace=TraceStore();app.add_middleware(trace.middleware())
            sock=socket.socket();sock.bind(('127.0.0.1',0))
            server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
            thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
            try:
                until=time.monotonic()+30
                while not server.started:
                    if not thread.is_alive() or time.monotonic()>until:raise RuntimeError('Server startup failed')
                    time.sleep(.05)
                with httpx.Client(base_url=f'http://127.0.0.1:{sock.getsockname()[1]}',timeout=650) as client:
                    # Explicit cold loading is separate from paired warm inference.
                    warm=client.post('/v1/audio/transcriptions',files={'file':('warm.wav',(ROOT/'tests/fixtures/zh.wav').read_bytes(),'audio/wav')}, data={'model':model,'language':'auto'}, headers={'x-request-id':'warmup'})
                    if warm.status_code!=200:raise RuntimeError('Warmup failed')
                    report['warmup_stages']=trace.snapshot('warmup')
                    for index,case in enumerate(inputs):
                        native_results.clear()
                        key=f'candidate-{index}';began=time.perf_counter();offset=len(memory)
                        before=client.get('/v1/runtime').json()['backends']['sherpa-onnx']['instance_pools']
                        response=client.post('/v1/audio/transcriptions',headers={'x-request-id':key},files={'file':('continuous.wav',case['audio'],'audio/wav')},data={'model':model,'language':'auto' if case['language']=='mixed' else case['language'],'response_format':'verbose_json','timestamps':'true'})
                        row={k:case[k] for k in ('id','language','group','sha256','duration','overlap_seconds','endpoint_crossing_annotations','human_audit')}
                        row['reference_sha256']=hashlib.sha256(case['reference'].encode()).hexdigest()
                        row.update(status=response.status_code,seconds=time.perf_counter()-began,stages=trace.snapshot(key),peak_rss_mib=max(memory[offset:] or [process.memory_info().rss/2**20]))
                        report['cases'].append(row)
                        if response.status_code!=200:
                            row['error']=response.json().get('error',{})
                            if report_path:report_path.write_text(json.dumps(report,indent=2))
                            raise AssertionError(row)
                        body=response.json();chunks=body.get('chunk_count',1)
                        row.update(chunks=chunks,quality=score(case['reference'],body['text'],case['language']),coverage_end=check_coverage(windows.rows,case['duration'],chunks),windows=list(windows.rows),text_characters=len(body['text']),native_segments=len(body.get('segments',[])))
                        row['raw_concat_quality']=score(case['reference'],' '.join(str(r.get('text','')) for r in native_results),case['language'])
                        row['raw_text_characters']=sum(len(str(r.get('text',''))) for r in native_results)
                        row['window_output_characters']=[len(str(r.get('text',''))) for r in native_results]
                        row['decoder_exit_reason']='not exposed by installed native binding'
                        after=client.get('/v1/runtime').json()['backends']['sherpa-onnx']['instance_pools']
                        row['native_calls']=after[model]['completed']-before.get(model,{}).get('completed',0)
                        row['adapter_calls']=row['native_calls']
                        row['native_decode_calls']=row['stages'].get('native_inference',{}).get('calls',0)
                        if row['adapter_calls']!=chunks:raise AssertionError('Adapter call mismatch')
                        if any(p['active'] or p['waiting'] for p in after.values()):raise AssertionError('Pool not drained')
                        if app.state.inference_queue._reserved:raise AssertionError('Transport not drained')
                        starts=[r['start'] for r in body.get('segments',[])]
                        if starts!=sorted(starts):raise AssertionError('Unordered native timestamps')
                        if report_path:report_path.write_text(json.dumps(report,indent=2))
            finally:
                server.should_exit=True;thread.join(30);sock.close()
                if thread.is_alive():raise RuntimeError('Owned server did not stop')
        report['completed']=True
    finally:
        stop.set();sampler.join(5);report['peak_rss_mib']=max(memory or [process.memory_info().rss/2**20])
    return report


def compare(rows, models=MODELS):
    evaluations=[]
    for model in models:
        baseline=[r for r in rows if r['model']==model and r['policy']=='current']
        candidate=[r for r in rows if r['model']==model and r['policy']=='candidate']
        groups=[]
        for group,language in sorted({(c['group'],c['language']) for r in baseline for c in r['cases']}):
            def totals(runs):
                cases=[c for r in runs for c in r['cases'] if (c['group'],c['language'])==(group,language)]
                errors=sum(c['quality']['errors'] for c in cases);units=sum(c['quality']['reference_units'] for c in cases)
                return errors/max(1,units)
            left,right=totals(baseline),totals(candidate)
            groups.append({'group':group,'language':language,'current_rate':left,'candidate_rate':right,'non_regression':right<=left})
        evaluations.append({'model':model,'groups':groups,'quality_non_regression':len(baseline)==3 and len(candidate)==3 and all(g['non_regression'] for g in groups), 'recommendation':'retain-current', 'promotion_blockers':['Human boundary audit pending','Interaction latency gate must be evaluated independently']})
    return evaluations


def reusable_trial(previous, model, policy, repeat, inputs, settings, candidate_policy=None):
    if not previous.get('completed') or (previous.get('model'),previous.get('policy'),previous.get('repeat'))!=(model,policy,repeat):
        return False
    if previous.get('candidate_policy')!=candidate_policy:
        return False
    recorded=previous.get('provenance',{})
    current=provenance(settings)
    for key in ('settings','dependencies','models'):
        if recorded.get(key)!=current.get(key):return False
    source={p:h for p,h in current['python_files_sha256'].items() if p.startswith('src/')}
    if any(recorded.get('python_files_sha256',{}).get(p)!=h for p,h in source.items()):return False
    expected=[(c['id'],c['sha256']) for c in inputs]
    actual=[(c.get('id'),c.get('sha256')) for c in previous.get('cases',[])]
    return expected==actual and all(c.get('status')==200 and 'raw_concat_quality' in c for c in previous['cases'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,default=ROOT/'sandbox/stt-continuous/manifest.json')
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--resume',action='store_true')
    parser.add_argument('--models',nargs='+',choices=MODELS,default=list(MODELS))
    parser.add_argument('--worker',choices=MODELS)
    parser.add_argument('--policy',choices=('current','candidate'))
    parser.add_argument('--repeat',type=int,default=3)
    parser.add_argument('--candidate-policy',choices=POLICIES,help='Named isolated candidate; omitted preserves legacy candidate')
    args=parser.parse_args()
    if args.repeat<1:parser.error('Positive repeat required')
    args.report.parent.mkdir(parents=True,exist_ok=True)
    if args.worker:
        report={'completed':False,'model':args.worker,'policy':args.policy,'repeat':args.repeat}
        try:
            report=worker(args.worker,args.policy,args.repeat,args.manifest,args.report,args.candidate_policy)
        except BaseException as exc:
            if args.report.exists():report=json.loads(args.report.read_text())
            report['failure_type']=type(exc).__name__;raise
        finally:args.report.write_text(json.dumps(report,indent=2))
        return
    inputs=corpus(args.manifest)
    from smartvoice.config.settings import Settings
    source_settings=Settings.from_env()
    report={'completed':False,'candidate_policy':args.candidate_policy,'corpus_manifest_sha256':fingerprint(args.manifest),'rounds':[],'normalization':'casefold, remove punctuation; no numeral or script equivalence','human_audit':'pending','tested_models':args.models,'deferred_models':[m for m in MODELS if m not in args.models]}
    try:
        for repeat in range(1,args.repeat+1):
            for model in args.models:
                # Alternate order to reduce systematic thermal/order bias.
                policies=('current','candidate') if repeat%2 else ('candidate','current')
                for policy in policies:
                    path=args.report.with_name(f'{args.report.stem}-{model}-{policy}-{repeat}.json')
                    if args.resume and path.exists():
                        previous=json.loads(path.read_text())
                        if reusable_trial(previous,model,policy,repeat,inputs,source_settings,args.candidate_policy):
                            report['rounds'].append(previous)
                            args.report.write_text(json.dumps(report,indent=2))
                            continue
                        archived=path.with_name(path.stem+f'-previous-{int(time.time())}.json')
                        path.rename(archived)
                    named_args=['--candidate-policy',args.candidate_policy] if args.candidate_policy else []
                    result=subprocess.run([sys.executable,__file__,'--worker',model,'--policy',policy,'--repeat',str(repeat),'--manifest',str(args.manifest),'--report',str(path),*named_args],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True,timeout=1800)
                    if path.exists():report['rounds'].append(json.loads(path.read_text()))
                    args.report.write_text(json.dumps(report,indent=2))
                    if result.returncode:raise RuntimeError(f'Worker failed: {model}/{policy}/{repeat}: {result.stderr[-600:]}')
                    print(json.dumps({'model':model,'policy':policy,'repeat':repeat,'cases':len(report['rounds'][-1]['cases'])}),flush=True)
        report['evaluations']=compare(report['rounds'],args.models);report['completed']=True
    finally:args.report.write_text(json.dumps(report,indent=2))

if __name__=='__main__':main()
