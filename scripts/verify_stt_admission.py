"""Real TCP admission failures with installed native ASR; no mocked inference.

Exercises transport and model queue rejection/timeout in isolated applications.
Reports metadata only. Keep this explicit probe outside routine CI.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import logging
from pathlib import Path
import socket
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'src'))
import httpx
import uvicorn
from smartvoice.app import create_app
from smartvoice.config.settings import Settings
from tests.inference_environment import isolated_runtime
from tests.stt_regression_audio import build_audio
from scripts.stt_validation_common import provenance, TemporaryStorageTrace, TraceStore

MODEL='stt-qwen3-asr-600m-int8'


def probe(label, parallel, waiting, model=MODEL):
    MODEL=model
    source=replace(Settings.from_env(),max_concurrent_inference=parallel,
                   min_instances=1,max_instances=1,max_queued_inference=waiting,
                   inference_queue_timeout_seconds=.1,inference_execution_timeout_seconds=120)
    with TemporaryStorageTrace() as storage, isolated_runtime(source) as (settings,provider):
        app=create_app(settings,provider)
        tracing=TraceStore();app.add_middleware(tracing.middleware())
        logging.getLogger('smartvoice.api').setLevel(logging.CRITICAL)
        sock=socket.socket();sock.bind(('127.0.0.1',0))
        server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True)
        thread.start()
        try:
            until=time.monotonic()+30
            while not server.started:
                if not thread.is_alive() or time.monotonic()>until:raise RuntimeError('Server startup failed')
                time.sleep(.02)
            with httpx.Client(base_url=f'http://127.0.0.1:{sock.getsockname()[1]}',timeout=130) as client:
                short=(ROOT/'tests/fixtures/zh.wav').read_bytes();long=build_audio(70,'zh')
                def post(raw,key):
                    return client.post('/v1/audio/transcriptions',headers={'x-request-id':key},files={'file':('probe.wav',raw,'audio/wav')},
                        data={'model':MODEL,'language':'zh','response_format':'verbose_json'})
                def group():return client.get('/v1/runtime').json()['backends']['sherpa-onnx']['instance_pools'].get(MODEL,{})
                warm=post(short,"warmup");assert warm.status_code==200
                before=group()['completed']
                with ThreadPoolExecutor(1) as executor:
                    job=executor.submit(post,long,"background")
                    until=time.monotonic()+10
                    while not group().get('active'):
                        if job.done() or time.monotonic()>until:raise AssertionError('Background native admission not observed')
                        time.sleep(.005)
                    started=time.perf_counter();rejected=post(short,"rejected");elapsed=time.perf_counter()-started
                    assert rejected.status_code==503,(label,rejected.status_code)
                    error=rejected.json()['error'];assert error['code']=='inference_overloaded'
                    expected_message={'transport-full':'The inference queue is full.',
                        'transport-wait-timeout':'Timed out while waiting for an inference slot.',
                        'model-full':'The model inference queue is full.',
                        'model-wait-timeout':'Timed out waiting for a model instance.'}[label]
                    assert error['message'].startswith(expected_message),(label,error['message'])
                    # A queue timeout must actually wait; an immediate rejection
                    # must not be mistaken for the timeout scenario.
                    if waiting:assert elapsed>=.08,(label,elapsed)
                    background=job.result();assert background.status_code==200,(label,background.status_code)
                after=group();chunks=background.json()['chunk_count']
                assert after['completed']-before==chunks,'Rejected request entered native inference'
                assert not after['active'] and not after['waiting']
                assert app.state.inference_queue._reserved==0
                row={'case':label,'parallel_enabled':parallel,'max_waiting':waiting,
                     'wait_timeout_seconds':.1,'rejection_status':rejected.status_code,'error_code':error['code'],
                     'rejection_source_verified':True,
                     'rejection_seconds':round(elapsed,4),'background_status':background.status_code,
                     'background_chunks':chunks,'successful_native_calls':after['completed']-before,
                     'final_active':after['active'],'final_waiting':after['waiting'],'final_reserved':0}
                recovery=post(short,"recovery");assert recovery.status_code==200
                assert not group()['active'] and not group()['waiting']
                assert app.state.inference_queue._reserved==0
                row['recovery_status']=recovery.status_code
                row['audio_spools']=storage.assert_closed()
                row['provenance']=provenance(source)
                row['stages']={key:tracing.snapshot(key) for key in ('warmup','background','rejected','recovery')}
                assert row['stages']['rejected'].get('native_inference',{}).get('calls',0)==0
        finally:
            server.should_exit=True;thread.join(30);sock.close()
            if thread.is_alive():raise RuntimeError('Owned server did not stop')

    row['owned_server_stopped']=True
    row['isolated_state_removed']=not settings.data_dir.exists()
    assert row['isolated_state_removed']
    return row

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--model',choices=('stt-sensevoice-small-int8',MODEL),default=MODEL)
    args=parser.parse_args();args.report.parent.mkdir(parents=True,exist_ok=True)
    report={'model':args.model,'fixture':'synthetic 70-second Chinese background and short Chinese probe',
            'cases':[],'completed':False}
    try:
        for label,parallel,waiting in [('transport-full',0,0),('transport-wait-timeout',0,1),
                                      ('model-full',1,0),('model-wait-timeout',1,1)]:
            row=probe(label,parallel,waiting,args.model);report['cases'].append(row);print(json.dumps(row),flush=True)
        report['completed']=True
    finally:args.report.write_text(json.dumps(report,indent=2))


if __name__=='__main__':main()
