"""Fixed arrivals against an owned server process; server/client RSS are separate."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import io
import json
import math
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT)); sys.path.insert(0,str(ROOT/'src'))

from scripts.validation.stt_isolated_http import OwnedServer
from scripts.validation.stt_validation_common import check_coverage, fingerprint, provenance
from scripts.validation.stt_window_policies import POLICIES
from scripts.validation.verify_speech_interaction import percentiles

ASR='stt-sensevoice-small-int8'
OTHER='stt-qwen3-asr-600m-int8'
TTS='tts-supertonic-v3-multilingual-int8'


def latency_summary(rows):
    valid=[r for r in rows if r['valid']]
    rejected=[r for r in rows if not r['valid']]
    return {'latency':percentiles([r['seconds'] for r in rows]),
            'successful_latency':percentiles([r['seconds'] for r in valid]) if valid else None,
            'rejected_latency':percentiles([r['seconds'] for r in rejected]) if rejected else None,
            'success_rate':len(valid)/len(rows)}


def stable_windows(rows, width=50):
    windows=[]
    for index in range(0,len(rows),width):
        batch=rows[index:index+width]
        valid=[r for r in batch if r['valid']]
        windows.append({'first_index':index,'requests':len(batch),
                        'latency':latency_summary(batch),
                        'mean_wait':sum(r.get('queue_wait',0)+r.get('runtime_wait',0) for r in valid)/max(1,len(valid))})
    first,last=windows[0],windows[-1]
    stable=last['mean_wait']<=max(first['mean_wait']*1.2,first['mean_wait']+.05)
    return windows,stable


def verify_completed(row, metadata):
    assert metadata['adapter_completed']==row['chunks'],'Completed adapter/window count mismatch'
    assert metadata['attempts_after_cancellation']==0
    return check_coverage(metadata['windows'],row['duration'],row['chunks'])


def run(args):
    import av, httpx, numpy as np, psutil
    from smartvoice.config.settings import Settings
    from tests.stt_regression_audio import build_audio
    source=Settings.from_env()
    if args.instances is not None: source=replace(source,max_instances=args.instances,min_instances=1)
    short=(ROOT/'tests/fixtures/zh.wav').read_bytes();long=build_audio(120,'zh')
    report={'completed':False,'scenario':args.scenario,'policy':args.policy,
            'count':args.count,'interval_seconds':args.interval,'configuration':asdict(POLICIES[args.policy]),
            'provenance':provenance(source),'cycles':[], 'rss_scope':'owned server process',
            'short_sha256':fingerprint(ROOT/'tests/fixtures/zh.wav'),
            'production_defaults_changed':False}
    args.report.write_text(json.dumps(report,indent=2))
    server=None;stop=threading.Event();memory=[]
    try:
        server=OwnedServer(source,asdict(POLICIES[args.policy]),profile_python=args.scenario=='lifecycle')
        report['server_pid']=server.process.pid
        server_process=psutil.Process(server.process.pid);client_process=psutil.Process()
        def sampler():
            while not stop.wait(.1):
                try:memory.append((time.monotonic(),server_process.memory_info().rss/2**20,client_process.memory_info().rss/2**20))
                except psutil.NoSuchProcess:break
        thread=threading.Thread(target=sampler,daemon=True);thread.start()
        for cycle in range(args.cycles if args.scenario=='lifecycle' else 1):
            cycle_row={'cycle':cycle+1,'before':server.command('memory')}
            report['cycles'].append(cycle_row)
            with httpx.Client(base_url=server.url,timeout=650,
                    limits=httpx.Limits(max_connections=64,max_keepalive_connections=64)) as client:
                def request(key,kind='stt',audio=short,model=ASR,scheduled=None,read_timeout=None):
                    actual=time.monotonic();started=actual if scheduled is None else scheduled
                    try:
                        if kind=='stt':
                            response=client.post('/v1/audio/transcriptions',headers={'x-request-id':key},
                                files={'file':('load.wav',audio,'audio/wav')},
                                data={'model':model,'language':'zh','response_format':'verbose_json'},
                                timeout=httpx.Timeout(650,read=read_timeout) if read_timeout is not None else 650)
                        else:
                            response=client.post('/v1/audio/speech',headers={'x-request-id':key},
                                json={'model':TTS,'language':'en','input':'The next order is ready.'})
                    except httpx.ReadTimeout:
                        return {'id':key,'status':'client-timeout','valid':False,
                                'seconds':time.monotonic()-started,'scheduling_lag':actual-started}
                    row={'id':key,'status':response.status_code,'valid':False,
                         'seconds':time.monotonic()-started,'scheduling_lag':actual-started}
                    if response.status_code==200:
                        if kind=='stt':
                            body=response.json()
                            row.update(valid=bool(body['text'].strip()),chunks=body.get('chunk_count',1),
                                duration=body['duration'],queue_wait=body.get('queue_wait_seconds',0),
                                runtime_wait=body.get('runtime_wait_seconds',0),selected_model=body['model'])
                        else:
                            with av.open(io.BytesIO(response.content)) as audio:
                                frames=[frame.to_ndarray() for frame in audio.decode(audio=0)]
                            row.update(valid=bool(frames and any(np.any(f) for f in frames)),
                                queue_wait=float(response.headers['x-queue-wait-seconds']),
                                runtime_wait=float(response.headers['x-runtime-wait-seconds']))
                    else:
                        error=response.json().get('error',{})
                        row['error']={k:error[k] for k in ('type','code','param') if k in error}
                    return row
                def drain():
                    deadline=time.monotonic()+60
                    while time.monotonic()<deadline:
                        pools=client.get('/v1/runtime').json()['backends']['sherpa-onnx']['instance_pools']
                        control=server.command('snapshot',keys=[])
                        if not control['reserved'] and all(not p['active'] and not p['waiting'] for p in pools.values()):
                            return pools
                        time.sleep(.02)
                    raise AssertionError('Server resources did not drain')
                cold=request('cold');assert cold['valid'];cycle_row['cold']=cold
                if args.scenario=='lifecycle':
                    cycle_row['long']=request('long',audio=long);assert cycle_row['long']['valid']
                    cycle_row['loaded']=server.command('memory')
                    # Calibrate against a hot 30-second operation. Profiling can
                    # make a fixed .1-second budget expire during decode, leaving
                    # late native-call ownership untested.
                    probe_audio=build_audio(30,'zh')
                    probe=request('timeout-control',audio=probe_audio);assert probe['valid']
                    probe_metadata=server.command('snapshot',keys=['timeout-control'])['requests']['timeout-control']
                    timeout_budget=probe_metadata['operation_wall_seconds']*.5
                    cycle_row['timeout_budget_seconds']=timeout_budget
                    server.command('timeout',seconds=timeout_budget)
                    cycle_row['timeout']=request('timeout',audio=probe_audio)
                    assert cycle_row['timeout']['status']==504
                    drain();server.command('timeout',seconds=None)
                    cycle_row['disconnect']=request('disconnect',audio=probe_audio,read_timeout=probe['seconds']*.5)
                    assert cycle_row['disconnect']['status']=='client-timeout'
                    time.sleep(.1);cycle_row['final_pools']=drain()
                    recovery=request('recovery');assert recovery['valid'];drain()
                    snapshot=server.command('snapshot',keys=['long','timeout','disconnect','recovery'])
                    cycle_row['snapshot']=snapshot
                    args.report.write_text(json.dumps(report,indent=2))
                    assert not snapshot['reserved']
                    for metadata in snapshot['requests'].values():
                        assert metadata['attempts_after_cancellation']==0
                    for key in ('timeout','disconnect'):
                        assert snapshot['requests'][key]['stages'].get('native_inference',{}).get('calls',0)>=1
                    cycle_row['long']['coverage_end']=verify_completed(cycle_row['long'],snapshot['requests']['long'])
                    cycle_row['recovery']=recovery;cycle_row['snapshot']=snapshot
                else:
                    kind='tts' if args.scenario=='tts' else 'stt'
                    model=OTHER if args.scenario=='different' else ASR
                    # Warm every configured replica at the start. Production
                    # idle eviction remains enabled during the measured run.
                    with ThreadPoolExecutor(source.max_instances) as prewarm:
                        warm_jobs=[prewarm.submit(request,f'prewarm-long-{i}',audio=long)
                                   for i in range(source.max_instances)]
                        assert all(job.result()['valid'] for job in warm_jobs)
                        warm_jobs=[prewarm.submit(request,f'prewarm-short-{i}',kind=kind,model=model)
                                   for i in range(source.max_instances)]
                        assert all(job.result()['valid'] for job in warm_jobs)
                    drain()
                    report['prewarmed_pools']=client.get('/v1/runtime').json()['backends']['sherpa-onnx']['instance_pools']
                    for i in range(3):assert request(f'warm-{i}',kind=kind,model=model)['valid']
                    serial=[request(f'serial-{i}',kind=kind,model=model) for i in range(3)]
                    report['serial_control']=latency_summary(serial)
                    background_rows=[];background_stop=threading.Event()
                    def background():
                        while not background_stop.is_set():
                            row=request(f'background-{len(background_rows)}',audio=long)
                            background_rows.append(row)
                            if not row['valid']:raise AssertionError('Long background failed')
                    with ThreadPoolExecutor(1) as background_executor,ThreadPoolExecutor(64) as executor:
                        task=background_executor.submit(background)
                        deadline=time.monotonic()+15
                        while True:
                            pools=client.get('/v1/runtime').json()['backends']['sherpa-onnx']['instance_pools']
                            if pools.get(ASR,{}).get('active'):break
                            if task.done() or time.monotonic()>deadline:raise AssertionError('Background admission absent')
                            time.sleep(.005)
                        epoch=time.monotonic();jobs=[]
                        try:
                            for index in range(args.count):
                                scheduled=epoch+index*args.interval;delay=scheduled-time.monotonic()
                                if delay>0:time.sleep(delay)
                                jobs.append(executor.submit(request,f'short-{index}',kind=kind,model=model,scheduled=scheduled))
                                if (index+1)%25==0:
                                    finished=[job.result() for job in jobs if job.done()]
                                    report['progress']={'scheduled':index+1,'completed':len(finished),
                                        'valid':sum(r['valid'] for r in finished),
                                        'elapsed_seconds':round(time.monotonic()-epoch,1)}
                                    args.report.write_text(json.dumps(report,indent=2))
                                    print(json.dumps(report['progress']),flush=True)
                            report['short']=[job.result() for job in jobs]
                            report['offered_rps']=1/args.interval
                            report['delivered_rps']=sum(r['valid'] for r in report['short'])/(time.monotonic()-epoch)
                            args.report.write_text(json.dumps(report,indent=2))
                        finally:background_stop.set()
                        task.result()
                    report['background']=background_rows
                    report.update(latency_summary(report['short']))
                    report['latency_windows'],report['queue_stable']=stable_windows(report['short'])
                    report['max_scheduling_lag']=max(r['scheduling_lag'] for r in report['short'])
                    report['final_pools']=drain()
                    snapshot=server.command('snapshot',keys=[r['id'] for r in report['short']+background_rows])
                    assert not snapshot['reserved']
                    for row in report['short']+background_rows:
                        metadata=snapshot['requests'][row['id']]
                        if row['valid'] and 'chunks' in row:
                            row['coverage_end']=verify_completed(row,metadata)
                    report['snapshot']=snapshot
            if args.scenario=='lifecycle' and cycle+1<args.cycles:
                cycle_row['cleanup']=server.restart()
            else:
                cycle_row['cleanup']=server.close()
            args.report.write_text(json.dumps(report,indent=2))
        report['completed']=True
    except BaseException as exc:
        report['failure_type']=type(exc).__name__
        raise
    finally:
        stop.set()
        if 'thread' in locals():thread.join(5)
        if server is not None and not server.closed:server.close()
        report['peak_rss_mib']=max((s for _,s,_ in memory),default=0)
        report['client_peak_rss_mib']=max((c for _,_,c in memory),default=0)
        report['server_process_exited']=server is not None and not server.process.is_alive()
        args.report.write_text(json.dumps(report,indent=2))
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario',choices=('same','different','tts','lifecycle'),required=True)
    parser.add_argument('--policy',choices=POLICIES,default='current')
    parser.add_argument('--count',type=int,default=500)
    parser.add_argument('--interval',type=float,default=.6)
    parser.add_argument('--instances',type=int,choices=(1,2))
    parser.add_argument('--cycles',type=int,default=5)
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    if args.count<1 or args.cycles<1 or not math.isfinite(args.interval) or args.interval<=0:
        parser.error('Positive count, cycles and finite interval required')
    args.report.parent.mkdir(parents=True,exist_ok=True)
    report=run(args)
    print(json.dumps({'scenario':args.scenario,'policy':args.policy,'completed':report['completed'],
                      'success_rate':report.get('success_rate'),'latency':report.get('successful_latency'),
                      'server_peak_rss_mib':report['peak_rss_mib']}),flush=True)


if __name__=='__main__':main()
