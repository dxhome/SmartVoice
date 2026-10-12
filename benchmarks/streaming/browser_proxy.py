"""Loopback-only real-browser QA proxy with measured WS impairment.

Serves the shipped HTML with an observational QA panel. Model inference stays
in the separately started production API. No production endpoint is added.
"""
import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx
import uvicorn
import websockets
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect


class ShapedLink:
    """Separate receive from delivery so latency does not multiply per frame."""
    def __init__(self, rate, latency, maximum_bytes=4*1024*1024):
        self.rate=rate;self.latency=latency;self.maximum_bytes=maximum_bytes
        self.queue=asyncio.Queue();self.bytes=0;self.peak_bytes=0
        self.delivered_bytes=0;self.delivered_messages=0;self.max_delay=0.

    async def collect(self, iterator):
        async for value in iterator:
            size=len(value.encode() if isinstance(value,str) else value)
            if self.bytes+size>self.maximum_bytes:raise RuntimeError('QA link buffer exceeded')
            self.bytes+=size;self.peak_bytes=max(self.peak_bytes,self.bytes)
            self.queue.put_nowait((time.monotonic(),value,size))
        self.queue.put_nowait(None)

    async def deliver(self, send):
        next_free=0.
        while True:
            entry=await self.queue.get()
            if entry is None:return
            received,value,size=entry
            due=max(received+self.latency,next_free)+size/self.rate if self.rate else received+self.latency
            await asyncio.sleep(max(0.,due-time.monotonic()))
            await send(value);self.bytes-=size;next_free=due
            self.delivered_bytes+=size;self.delivered_messages+=1
            self.max_delay=max(self.max_delay,time.monotonic()-received)

    def metrics(self):
        return dict(bytes_per_second=self.rate,one_way_latency_seconds=self.latency,
                    peak_buffer_bytes=self.peak_bytes,delivered_bytes=self.delivered_bytes,
                    delivered_messages=self.delivered_messages,max_message_delay_seconds=self.max_delay)


def create_app(upstream,output):
    profile={'name':'normal','upload':0,'download':0,'latency':0.,'disconnect':None}
    connections=set();history=[]
    script=Path(__file__).with_name('browser_observer.js').read_text()
    async def page(request):
        async with httpx.AsyncClient() as client:
            response=await client.get(upstream+'/console/streaming')
            response.raise_for_status()
        return HTMLResponse(response.text.replace('</body>','<script>'+script+'</script></body>'))
    async def capabilities(request):
        async with httpx.AsyncClient() as client:
            response=await client.get(upstream+'/v1/audio/stream/capabilities')
        return JSONResponse(response.json(),status_code=response.status_code)
    async def control(request):
        action=request.path_params['action']
        if action=='disconnect':
            for socket in list(connections):await socket.close(code=1012)
        elif action in ('slow','normal'):
            profile.update(name=action,upload=48000 if action=='slow' else 0,
                download=64000 if action=='slow' else 0,latency=.08 if action=='slow' else 0.,disconnect=None)
        elif action in ('disconnect-upload','disconnect-audio'):
            profile.update(name=action,upload=0,download=0,latency=0.,disconnect=action)
        else:return JSONResponse({'error':'unknown action'},status_code=400)
        return JSONResponse(dict(profile=profile,connections=len(connections)))
    async def evidence(request:Request):
        data=await request.json()
        # Browser data is bounded and never contains recovery secrets.
        if len(json.dumps(data))>4*1024*1024:return JSONResponse({'error':'evidence too large'},status_code=413)
        connection_metrics=[{**{k:v for k,v in row.items() if not k.startswith('_')},
            'upload':row['_up'].metrics(),'download':row['_down'].metrics()} for row in history]
        async with httpx.AsyncClient() as client:
            capability=(await client.get(upstream+'/v1/audio/stream/capabilities')).json()
        report=dict(schema='smartvoice.stream.browser-qa.v2',browser=data,proxy_connections=connection_metrics,
                    service_limits=capability.get('limits'),
                    scope='real DOM, WebSocket and Web Audio callbacks; physical audibility not verified')
        path=output/('browser-'+str(time.time_ns())+'.json');output.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        return JSONResponse({'saved':path.name})
    async def stream(socket:WebSocket):
        await socket.accept();connections.add(socket)
        cfg=dict(profile);up=ShapedLink(cfg['upload'],cfg['latency']);down=ShapedLink(cfg['download'],cfg['latency'])
        record=dict(profile=cfg,started=time.time(),_up=up,_down=down);history.append(record)
        tasks=[];cut_task=None
        async def arm(kind):
            nonlocal cut_task
            if cfg.get('disconnect')!=kind or cut_task:return
            profile['disconnect']=None
            async def cut():
                await asyncio.sleep(3 if kind=='disconnect-upload' else .15)
                record['injected_disconnect']=kind
                await socket.close(code=1012)
            cut_task=asyncio.create_task(cut())
        async def incoming():
            while True:
                message=await socket.receive()
                if message['type']=='websocket.disconnect':return
                yield message.get('text') if message.get('text') is not None else message['bytes']
        async def send(value):
            if isinstance(value,str):await socket.send_text(value)
            else:
                await socket.send_bytes(value);await arm('disconnect-audio')
        try:
            async with websockets.connect(upstream.replace('http://','ws://')+'/v1/audio/stream',max_size=4*1024*1024) as backend:
                async def send_input(value):
                    await backend.send(value)
                    if isinstance(value,bytes):await arm('disconnect-upload')
                tasks=[asyncio.create_task(up.collect(incoming())),asyncio.create_task(up.deliver(send_input)),
                       asyncio.create_task(down.collect(backend)),asyncio.create_task(down.deliver(send))]
                # A reader can finish before its shaped queue drains. Continue
                # delivering queued descriptors/binary/terminal in that case.
                readers={tasks[0],tasks[2]};deliverers={tasks[1],tasks[3]}
                while True:
                    done,_=await asyncio.wait(set(tasks),return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        if task.exception():raise task.exception()
                    if done & deliverers:break
                    for task in done:tasks.remove(task)
        except (WebSocketDisconnect,websockets.ConnectionClosed,RuntimeError):pass
        finally:
            for task in tasks:task.cancel()
            if cut_task and not cut_task.done():cut_task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            connections.discard(socket);record.update(upload=up.metrics(),download=down.metrics(),ended=time.time())
            try:await socket.close()
            except RuntimeError:pass
    return Starlette(routes=[Route('/console/streaming',page),Route('/v1/audio/stream/capabilities',capabilities),
        Route('/qa/{action}',control,methods=['POST']),Route('/evidence',evidence,methods=['POST']),
        WebSocketRoute('/v1/audio/stream',stream)])


def cli():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--upstream',default='http://127.0.0.1:9191')
    p.add_argument('--port',type=int,default=9292)
    p.add_argument('--output',type=Path,default=Path('.smartvoice-dev/streaming-product/browser-qa'))
    args=p.parse_args();uvicorn.run(create_app(args.upstream,args.output),host='127.0.0.1',port=args.port)

if __name__=='__main__':cli()
