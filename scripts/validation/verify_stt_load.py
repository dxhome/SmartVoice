"""Fixed-arrival mixed HTTP workloads, paired policies and five-cycle ownership probes."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import gc
import io
import json
import logging
import math
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import weakref

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'src'))
from scripts.validation.compare_stt_policies import MODELS
from scripts.validation.stt_validation_common import TraceStore, provenance, TemporaryStorageTrace
from scripts.validation.verify_speech_interaction import percentiles
from scripts.validation.verify_stt_candidates import CANDIDATE_MINIMUM
ASR=MODELS[2];TTS='tts-supertonic-v3-multilingual-int8'


def stable_confirmation(row, interval):
    """A successful short screen is insufficient for a confirmed stable load."""
    return (row.get('completed', False) and row.get('success_rate') == 1
            and row.get('queue_stable', False)
            and row.get('max_scheduling_lag', math.inf) < interval)


def evaluate_pair(current, candidate, count):
    def mean_background(row):
        return sum(r['seconds'] for r in row['background']) / len(row['background'])
    ratios = {'p95': candidate['latency']['p95'] / current['latency']['p95'],
              'long_completion': mean_background(candidate) / mean_background(current),
              'rss': candidate['peak_rss_mib'] / current['peak_rss_mib']}
    stable = all(stable_confirmation(row, row['interval_seconds'])
                 for row in (current, candidate))
    return {'ratios': ratios, 'sample_gate': count >= 500,
            'stable_load_gate': stable,
            'resource_latency_gate': count >= 500 and stable
                and all(v <= 1.1 for v in ratios.values()),
            'production_default_changed': False}


def worker(scenario,policy,count,interval,cycles,instances=None,background_model=ASR,report_path=None):
    ASR=background_model
    import av,httpx,numpy as np,psutil,uvicorn
    from smartvoice.app import create_app
    from smartvoice.config.settings import Settings
    from smartvoice.adapters.audio.input import BoundedAudioInput
    from tests.inference_environment import isolated_runtime
    from tests.stt_regression_audio import build_audio
    source=Settings.from_env()
    if instances is not None:source=replace(source,max_instances=instances,min_instances=1)
    process=psutil.Process();stop=threading.Event();samples=[]
    report={'scenario':scenario,'background_model':ASR,'policy':policy,'count':count,'interval_seconds':interval,'provenance':provenance(source),'completed':False,'cycles':[]}
    if report_path:report_path.write_text(json.dumps(report,indent=2))
    short=(ROOT/'tests/fixtures/zh.wav').read_bytes();long=build_audio(120,'zh')
    def sampler():
        while not stop.wait(.1):samples.append((time.monotonic(),process.memory_info().rss/2**20))
    sample_thread=threading.Thread(target=sampler,daemon=True);sample_thread.start()
    try:
        for cycle in range(cycles if scenario=='lifecycle' else 1):
            began_memory=len(samples);cycle_row={'cycle':cycle+1,'rss_before_mib':process.memory_info().rss/2**20}
            with TemporaryStorageTrace() as storage, isolated_runtime(source) as (settings,provider):
                app=create_app(settings,provider);logging.getLogger('smartvoice.api').setLevel(logging.CRITICAL)
                trace=TraceStore();app.add_middleware(trace.middleware())
                if policy=='candidate':
                    app.state.transcription_service.audio_input=BoundedAudioInput(settings.max_audio_seconds,overlap_on_silence=False,minimum_window_seconds=CANDIDATE_MINIMUM[ASR],relative_quiet=True)
                sock=socket.socket();sock.bind(('127.0.0.1',0))
                server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
                thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
                try:
                    until=time.monotonic()+30
                    while not server.started:
                        if not thread.is_alive() or time.monotonic()>until:raise RuntimeError('Server startup failed')
                        time.sleep(.02)
                    with httpx.Client(base_url=f'http://127.0.0.1:{sock.getsockname()[1]}',timeout=650,limits=httpx.Limits(max_connections=64,max_keepalive_connections=64)) as client:
                        def request(key,kind='stt',audio=short,model=ASR,scheduled=None,timeout=None):
                            actual=time.monotonic();started=scheduled or actual
                            try:
                                if kind=='stt':response=client.post('/v1/audio/transcriptions',headers={'x-request-id':key},files={'file':('load.wav',audio,'audio/wav')},data={'model':model,'language':'zh','response_format':'verbose_json'},timeout=httpx.Timeout(650,read=timeout) if timeout is not None else 650)
                                else:response=client.post('/v1/audio/speech',headers={'x-request-id':key},json={'model':TTS,'input':'The next order is ready.','language':'en'},timeout=httpx.Timeout(650,read=timeout) if timeout is not None else 650)
                            except httpx.ReadTimeout:
                                return {'id':key,'status':'client-timeout','valid':False,'seconds':time.monotonic()-started,'scheduling_lag':actual-started}
                            row={'id':key,'status':response.status_code,'seconds':time.monotonic()-started,'scheduling_lag':actual-started,'valid':False}
                            if response.status_code==200:
                                if kind=='stt':
                                    body=response.json();row.update(valid=bool(body['text'].strip()),chunks=body.get('chunk_count',1),duration=body['duration'],queue_wait=body.get('queue_wait_seconds',0),runtime_wait=body.get('runtime_wait_seconds',0))
                                else:
                                    decoded=[]
                                    with av.open(io.BytesIO(response.content)) as audio_source:
                                        for frame in audio_source.decode(audio=0):decoded.append(frame.to_ndarray())
                                    row.update(valid=bool(decoded and any(np.any(a) for a in decoded)),queue_wait=float(response.headers['x-queue-wait-seconds']),runtime_wait=float(response.headers['x-runtime-wait-seconds']))
                            else:
                                # Preserve structured API failures without storing input or
                                # transcriptions. A status alone cannot identify the queue.
                                try:
                                    error=response.json().get('error',{})
                                    row['error']={k:error[k] for k in ('type','code','param') if k in error}
                                except (ValueError,AttributeError,TypeError):
                                    row['error']={'type':'unstructured_response'}
                            return row
                        def drain():
                            until=time.monotonic()+60
                            while time.monotonic()<until:
                                pools=client.get('/v1/runtime').json()['backends']['sherpa-onnx']['instance_pools']
                                if all(not p['active'] and not p['waiting'] for p in pools.values()) and not app.state.inference_queue._reserved:return pools
                                time.sleep(.02)
                            raise AssertionError('Resources did not drain')
                        warm=request('cold-asr');assert warm['valid'];cycle_row['cold']=warm
                        if scenario=='lifecycle':
                            cycle_row['long']=request('long',audio=long);assert cycle_row['long']['valid']
                            cycle_row['rss_loaded_mib']=process.memory_info().rss/2**20
                            # Actual TCP disconnect after native admission was separately
                            # proven; this loop repeats late-worker cleanup ownership.
                            app.state.settings=replace(settings,inference_execution_timeout_seconds=.1)
                            cycle_row['timeout']=request('timeout',audio=long)
                            assert cycle_row['timeout']['status']==504
                            drain();app.state.settings=settings
                            cycle_row['disconnect']=request('disconnect',audio=long,timeout=.05)
                            assert cycle_row['disconnect']['status']=='client-timeout'
                            time.sleep(.3);cycle_row['final_pools']=drain()
                            cycle_row['recovery']=request('recovery');assert cycle_row['recovery']['valid'];drain()
                            pool=provider.providers['sherpa-onnx'].pool
                            refs=[weakref.ref(i.runtime) for group in pool._groups.values() for i in group.instances]
                        else:
                            kind='tts' if scenario=='tts' else 'stt';model=next(m for m in reversed(MODELS) if m!=ASR) if scenario=='different' else ASR
                            for i in range(3):assert request(f'warm-{i}',kind=kind,model=model)['valid']
                            # Three serial controls are latency controls, not P99 evidence.
                            serial=[request(f'serial-{i}',kind=kind,model=model) for i in range(3)]
                            report['serial_control']=percentiles([r['seconds'] for r in serial])
                            backgrounds=[];background_stop=threading.Event();observed=threading.Event()
                            def background_loop():
                                i=0
                                while not background_stop.is_set():
                                    row=request(f'background-{i}',audio=long);backgrounds.append(row);i+=1
                                    if not row['valid']:raise AssertionError('Background failed')
                            with ThreadPoolExecutor(1) as background_executor,ThreadPoolExecutor(64) as executor:
                                background=background_executor.submit(background_loop)
                                until=time.monotonic()+15
                                while True:
                                    pools=client.get('/v1/runtime').json()['backends']['sherpa-onnx']['instance_pools']
                                    if pools.get(ASR,{}).get('active'):break
                                    if background.done() or time.monotonic()>until:raise AssertionError('Native background admission absent')
                                    time.sleep(.005)
                                jobs=[];epoch=time.monotonic()
                                try:
                                    for i in range(count):
                                        scheduled=epoch+i*interval;delay=scheduled-time.monotonic()
                                        if delay>0:time.sleep(delay)
                                        jobs.append(executor.submit(request,f'short-{i}',kind=kind,model=model,scheduled=scheduled))
                                    report['short']=[job.result() for job in jobs]
                                    if report_path:report_path.write_text(json.dumps(report,indent=2))
                                    report['offered_rps']=1/interval
                                    report['delivered_rps']=sum(r['valid'] for r in report['short'])/(time.monotonic()-epoch)
                                    report['latency_windows']=[{'first_index':i,'latency':percentiles([r['seconds'] for r in report['short'][i:i+50]]),'mean_wait':sum(r.get('queue_wait',0)+r.get('runtime_wait',0) for r in report['short'][i:i+50])/len(report['short'][i:i+50])} for i in range(0,count,50)]
                                    first,last=report['latency_windows'][0],report['latency_windows'][-1]
                                    report['queue_stable']=last['mean_wait']<=max(first['mean_wait']*1.2,first['mean_wait']+.05)
                                finally:background_stop.set()
                                background.result()
                            report['background']=backgrounds
                            valid=[r for r in report['short'] if r['valid']]
                            report['latency']=percentiles([r['seconds'] for r in report['short']])
                            report['success_rate']=len(valid)/count
                            report['queue_wait']=percentiles([r.get('queue_wait',0)+r.get('runtime_wait',0) for r in valid]) if valid else None
                            report['max_scheduling_lag']=max(r['scheduling_lag'] for r in report['short'])
                            report['final_pools']=drain()
                            report['stage_totals']={r['id']:trace.snapshot(r['id']) for r in report['short']+backgrounds}
                        cycle_row['audio_spools']=storage.assert_closed()
                finally:
                    server.should_exit=True;thread.join(30);sock.close()
                    if thread.is_alive():raise RuntimeError('Owned server did not stop')
            cycle_row['isolated_state_removed']=not settings.data_dir.exists()
            assert cycle_row['isolated_state_removed']
            gc.collect()
            cycle_row['rss_after_shutdown_mib']=process.memory_info().rss/2**20
            cycle_row['peak_rss_mib']=max((rss for _,rss in samples[began_memory:]),default=process.memory_info().rss/2**20)
            if scenario=='lifecycle':
                cycle_row['adapter_objects_remaining']=sum(ref() is not None for ref in refs)
                assert cycle_row['adapter_objects_remaining']==0
            report['cycles'].append(cycle_row)
            if report_path:report_path.write_text(json.dumps(report,indent=2))
        report['completed']=True
    finally:
        stop.set();sample_thread.join(5);report['peak_rss_mib']=max((rss for _,rss in samples),default=0)
        if report_path:report_path.write_text(json.dumps(report,indent=2))
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--worker',choices=('same','different','tts','lifecycle'))
    parser.add_argument('--policy',choices=('current','candidate'),default='current')
    parser.add_argument('--count',type=int,default=500)
    parser.add_argument('--interval',type=float,default=.6)
    parser.add_argument('--cycles',type=int,default=5)
    parser.add_argument('--background-model',choices=MODELS,default=ASR)
    parser.add_argument('--models',nargs='+',choices=MODELS,help='Explicit mixed-load background models; otherwise Qwen plus quality-passing candidates')
    parser.add_argument('--scenarios',nargs='+',choices=('same','different','tts'),default=['same','different','tts'],help='Explicit workload subset; other workloads remain uncovered')
    parser.add_argument('--quality-report',type=Path,default=ROOT/'sandbox/tts-output/runs/stt-candidates-final.json')
    parser.add_argument('--instances',type=int,choices=(1,2),help='Isolated instance experiment only; omitted uses production configuration')
    args=parser.parse_args()
    if args.count<1 or (not math.isfinite(args.interval) or args.interval<=0) or args.cycles<1:parser.error('Positive counts, intervals and cycles required')
    args.report.parent.mkdir(parents=True,exist_ok=True)
    instance_args=[] if args.instances is None else ['--instances',str(args.instances)]
    if args.worker:
        report={'completed':False,'scenario':args.worker,'policy':args.policy}
        try:report=worker(args.worker,args.policy,args.count,args.interval,args.cycles,args.instances,args.background_model,args.report)
        except BaseException as exc:
            if args.report.exists():report=json.loads(args.report.read_text())
            report['failure_type']=type(exc).__name__;raise
        finally:args.report.write_text(json.dumps(report,indent=2))
        return
    report={'completed':False,'tested_scenarios':args.scenarios,'cases':[],'evaluations':[]}
    try:
        models=args.models or [ASR]
        if args.models is None and args.quality_report.exists():
            quality=json.loads(args.quality_report.read_text())
            if not quality.get('completed'):raise RuntimeError('Quality trials must finish before adaptive load selection')
            models=list(dict.fromkeys([ASR,*[r['model'] for r in quality['evaluations'] if r['quality_non_regression']]]))
        report['tested_background_models']=models
        for background_model in models:
            for scenario in args.scenarios:
                # Both configurations share one rate chosen using current-policy screening.
                interval=args.interval
                for step in range(4):
                    path=args.report.with_name(f'{args.report.stem}-{background_model}-{scenario}-screen-{step}.json')
                    result=subprocess.run([sys.executable,__file__,'--worker',scenario,'--background-model',background_model,'--policy','current','--count','20','--interval',str(interval),'--report',str(path),*instance_args],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True,timeout=900)
                    screen=json.loads(path.read_text()) if path.exists() else {'completed':False}
                    report['cases'].append({'phase':'screen','exit_code':result.returncode,**screen})
                    args.report.write_text(json.dumps(report,indent=2))
                    if result.returncode:
                        interval*=2
                        continue
                    if screen['success_rate']==1 and screen['max_scheduling_lag']<interval:break
                    interval*=2
                else:raise RuntimeError('No stable screened rate')
                pair=[]
                for policy in ('current','candidate'):
                    path=args.report.with_name(f'{args.report.stem}-{background_model}-{scenario}-{policy}.json')
                    result=subprocess.run([sys.executable,__file__,'--worker',scenario,'--background-model',background_model,'--policy',policy,'--count',str(args.count),'--interval',str(interval),'--report',str(path),*instance_args],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True,timeout=3600)
                    if result.returncode:raise RuntimeError(f'Load worker failed: {result.stderr[-500:]}')
                    row=json.loads(path.read_text());pair.append(row);report['cases'].append({'phase':'confirmation' if args.count>=500 else 'pilot',**row})
                    args.report.write_text(json.dumps(report,indent=2));print(json.dumps({'scenario':scenario,'policy':policy,'count':args.count,'success_rate':row['success_rate'],'latency':row['latency']}),flush=True)
                current,candidate=pair
                report['evaluations'].append({'scenario':scenario,'background_model':background_model,**evaluate_pair(current,candidate,args.count)})
        path=args.report.with_name(args.report.stem+'-lifecycle.json')
        result=subprocess.run([sys.executable,__file__,'--worker','lifecycle','--background-model',models[0],'--cycles',str(args.cycles),'--report',str(path),*instance_args],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True,timeout=1800)
        if path.exists():report['lifecycle']=json.loads(path.read_text())
        if result.returncode:raise RuntimeError(f'Lifecycle failed: {result.stderr[-500:]}')
        report['completed']=True
    finally:args.report.write_text(json.dumps(report,indent=2))

if __name__=='__main__':main()
