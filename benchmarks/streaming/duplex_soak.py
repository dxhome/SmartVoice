"""Wall-clock, real-time bidirectional speech soak over the production WS API."""
import argparse
import asyncio
import hashlib
import json
import math
import tempfile
import time
import urllib.request
import wave
from pathlib import Path

from .run import replay


def make_repeated_case(case, manifest_root, seconds, output_dir):
    path=manifest_root/case['path']
    if hashlib.sha256(path.read_bytes()).hexdigest()!=case['sha256']:
        raise ValueError('Pinned fixture checksum mismatch: '+case['id'])
    with wave.open(str(path),'rb') as src:
        params=src.getparams();pcm=src.readframes(src.getnframes())
    if (params.framerate,params.nchannels,params.sampwidth)!=(16000,1,2):
        raise ValueError('Expected mono PCM16 at 16 kHz')
    target_frames=seconds*params.framerate;source_frames=len(pcm)//2
    repeats=math.ceil(target_frames/source_frames);remaining=target_frames*2
    target=output_dir/(case['id']+'-wall-soak.wav')
    with wave.open(str(target),'wb') as dst:
        dst.setparams(params)
        while remaining:
            block=pcm[:min(remaining,len(pcm))]
            dst.writeframesraw(block);remaining-=len(block)
    digest=hashlib.sha256(target.read_bytes()).hexdigest()
    return dict(case,id=case['id']+'_wall_'+str(seconds)+'s',path=target.name,
                sha256=digest,samples=target_frames,reference_repetitions=repeats),target


def capabilities_url(ws_url):
    return ws_url.replace('wss://','https://').replace('ws://','http://').split('/v1/audio/stream',1)[0]+'/v1/audio/stream/capabilities'


async def main(args):
    manifest=json.loads(args.manifest.read_text())
    cases={row['id']:row for row in manifest['cases']}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    tempdir=Path(tempfile.mkdtemp(prefix='stream-duplex-soak-',dir=args.output.parent))
    prepared=[]
    try:
        for lang in ('zh','en'):
            case,path=make_repeated_case(cases[lang+'_02_clean'],args.audio_root,args.seconds,tempdir)
            prepared.append((case,path))
        async def poll():
            rows=[];url=capabilities_url(args.url);started=time.monotonic()
            while True:
                try:
                    def fetch():
                        with urllib.request.urlopen(url,timeout=5) as response:return json.load(response)
                    body=await asyncio.to_thread(fetch)
                    rows.append(dict(elapsed_seconds=time.monotonic()-started,
                        active_sessions=body.get('active_sessions'),worker_pool=body.get('worker_pool'),
                        input_queues=body.get('input_queues'),service_limits=body.get('limits'),
                        process_resource_sample=body.get('process_resource_sample')))
                except Exception as exc:
                    rows.append(dict(elapsed_seconds=time.monotonic()-started,poll_error=type(exc).__name__))
                if args.progress:
                    args.progress.write_text(json.dumps(dict(schema='smartvoice.stream.duplex-soak-progress.v1',samples=rows),ensure_ascii=False))
                await asyncio.sleep(args.sample_interval_seconds)
        monitor=asyncio.create_task(poll())
        began=time.monotonic()
        try:
            results=await asyncio.gather(*(replay(args.url,case,tempdir,'spoken_interpretation',None)
                for case,_ in prepared))
        finally:
            monitor.cancel();await asyncio.gather(monitor,return_exceptions=True)
        elapsed=time.monotonic()-began
        report=dict(schema='smartvoice.stream.duplex-soak.v1',requested_audio_seconds=args.seconds,
            direction_results=results,wall_seconds=elapsed,
            process_samples=json.loads(args.progress.read_text()).get('samples',[]) if args.progress and args.progress.exists() else [],
            manifest_sha256=hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
            interpretation='Repeated pinned clean utterances are lifecycle/throughput evidence, not natural meeting-quality coverage.')
        args.output.write_text(json.dumps(report,indent=2,ensure_ascii=False))
        print(args.output,flush=True)
    finally:
        for _,path in prepared:path.unlink(missing_ok=True)
        try:tempdir.rmdir()
        except OSError:pass


def cli():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='ws://127.0.0.1:8766/v1/audio/stream')
    parser.add_argument('--manifest',type=Path,default=Path('benchmarks/streaming/data/fleurs/clean.manifest.json'))
    parser.add_argument('--audio-root',type=Path,default=Path('benchmarks/streaming'))
    parser.add_argument('--seconds',type=int,default=3600)
    parser.add_argument('--sample-interval-seconds',type=float,default=5)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--progress',type=Path)
    args=parser.parse_args()
    if args.seconds<1 or args.sample_interval_seconds<1:parser.error('duration and sample interval must be positive')
    if args.output.exists():parser.error('Output exists; use a fresh report file')
    asyncio.run(main(args))


if __name__=='__main__':cli()
