"""Measure warm idle/one/two-session memory without conflating model load."""
import argparse
import asyncio
import json
import statistics
from pathlib import Path
import httpx
import websockets

async def measure(url,output):
    http=url.replace('ws://','http://').split('/v1/audio/stream')[0]
    sockets=[];snapshots={}
    async def sample():
        async with httpx.AsyncClient() as client:
            body=(await client.get(http+'/v1/audio/stream/capabilities')).json()
        return {key:body[key] for key in ('active_sessions','worker_pool','process_resource_sample')}
    async def collect(name):
        snapshots[name]=[]
        for _ in range(5):
            snapshots[name].append(await sample());await asyncio.sleep(.2)
    async def open_session(lang):
        socket=await websockets.connect(url)
        await socket.send(json.dumps(dict(type='configure',protocol='smartvoice.stream.v1',mode='spoken_interpretation',
            source_language=lang,target_language='en' if lang=='zh' else 'zh',
            audio=dict(encoding='pcm_s16le',sample_rate=16000,channels=1),output_consumption='delivery')))
        while True:
            event=json.loads(await asyncio.wait_for(socket.recv(),30))
            if event.get('event_sequence'):await socket.send(json.dumps(dict(type='ack',event_sequence=event['event_sequence'])))
            if event['type']=='error':raise RuntimeError(event['code'])
            if event['type']=='session_ready':break
        sockets.append(socket)
    try:
        # Ensure both directions' assets are resident before the idle sample;
        # otherwise an expired model can dwarf the actual context cost.
        await open_session('en');await open_session('zh')
        for socket in sockets:
            await socket.send(json.dumps(dict(type='cancel')));await socket.close()
        sockets.clear()
        for _ in range(100):
            if (await sample())['active_sessions']==0:break
            await asyncio.sleep(.05)
        else:raise RuntimeError('Warm sessions did not release')
        await collect('warm_idle');await open_session('en');await collect('one_session')
        await open_session('zh');await collect('two_sessions')
    finally:
        for socket in sockets:
            try:await socket.send(json.dumps(dict(type='cancel')));await socket.close()
            except Exception:pass
    rss={name:statistics.median(r['process_resource_sample']['rss_bytes'] for r in rows) for name,rows in snapshots.items()}
    worker_counts={name:sorted({r['process_resource_sample'].get('known_owned_worker_count') for r in rows}) for name,rows in snapshots.items()}
    comparable=all(count==worker_counts['warm_idle'] for count in worker_counts.values()) and worker_counts['warm_idle']!=[0]
    report=dict(schema='smartvoice.stream.session-cost.v1',samples=snapshots,median_rss_bytes=rss,worker_counts=worker_counts,
        matched_resident_workers=comparable,
        first_session_delta_bytes=rss['one_session']-rss['warm_idle'] if comparable else None,
        second_session_delta_bytes=rss['two_sessions']-rss['one_session'] if comparable else None,
        scope='warm initialized empty sessions; signed RSS differences include allocator/reclaim noise; not active-context worst case')
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2))


def cli():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--url',default='ws://127.0.0.1:9191/v1/audio/stream')
    p.add_argument('--output',type=Path,required=True);args=p.parse_args();asyncio.run(measure(args.url,args.output))
if __name__=='__main__':cli()
