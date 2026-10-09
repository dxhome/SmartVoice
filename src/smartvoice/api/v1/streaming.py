"""Versioned WebSocket boundary; audio format/runtime details never reach routes."""
import asyncio
import json
from urllib.parse import urlsplit
from fastapi import APIRouter, Request, WebSocket
from starlette.websockets import WebSocketDisconnect
from smartvoice.domain.streaming import StageError

router=APIRouter(prefix='/v1')

@router.get('/audio/stream/capabilities',tags=['streaming'])
async def capabilities(request: Request):
    return request.app.state.streaming.capabilities()

@router.websocket('/audio/stream')
async def stream(websocket: WebSocket):
    # Match local console origin. Non-browser clients may omit Origin.
    origin=websocket.headers.get('origin')
    if origin:
        try:
            parsed=urlsplit(origin)
            valid=parsed.scheme in ('http','https') and parsed.netloc==websocket.headers.get('host')
        except ValueError: valid=False
        if not valid:
            await websocket.close(code=1008);return
    await websocket.accept()
    session=None;receiver=None;sender=None
    async def receive():
        while not session.done.is_set():
            message=await websocket.receive()
            if message['type']=='websocket.disconnect':raise WebSocketDisconnect()
            if message.get('bytes') is not None:
                await session.push_audio(message['bytes']);continue
            raw=message.get('text','')
            if len(raw.encode('utf-8'))>8192: raise StageError('input_limit','Control message exceeds 8 KiB')
            try: row=json.loads(raw)
            except ValueError as exc: raise StageError('invalid_message','Expected a JSON control object') from exc
            if not isinstance(row,dict): raise StageError('invalid_message','Expected an object')
            kind=row.get('type')
            fields={'ack':{'type','event_sequence'},'finish':{'type'},'cancel':{'type'},
                    'audio_started':{'type','audio_sequence'},'audio_played':{'type','audio_sequence'}}
            if not isinstance(kind,str) or kind not in fields or row.keys()!=fields[kind]:
                raise StageError('invalid_message','Unexpected control fields')
            if kind=='ack': session.acknowledge(row['event_sequence'])
            elif kind=='finish': await session.finish_input()
            elif kind=='cancel': await session.cancel('client_canceled');return
            else:
                if not session.speech or session.plan.output_consumption!='playback':
                    raise StageError('invalid_playback','Playback acknowledgements require playback consumption')
                sequence=row['audio_sequence']
                if type(sequence) is not int: raise StageError('invalid_playback','Expected an integer audio sequence')
                if kind=='audio_started': session.speech.started(sequence)
                else: session.speech.played(sequence)
    async def send():
        while True:
            event=await session.output.get()
            payload=event.pop('audio',None)
            if not session.plan.include_source_text and event['type'].startswith('source_'):
                event.pop('text',None);event.pop('raw_text',None)
            if payload is not None: event['audio_bytes']=len(payload)
            await asyncio.wait_for(websocket.send_json(event),10)
            if payload is not None: await asyncio.wait_for(websocket.send_bytes(payload),10)
            if event['type'] in ('error','session_complete'): return
    try:
        message=await asyncio.wait_for(websocket.receive(),10)
        if message['type']=='websocket.disconnect':raise WebSocketDisconnect()
        initial=message.get('text')
        if not isinstance(initial,str):raise StageError('invalid_config','First message must be a JSON configure object')
        if len(initial.encode('utf-8'))>8192: raise StageError('input_limit','Configuration exceeds 8 KiB')
        try: config=json.loads(initial)
        except ValueError as exc: raise StageError('invalid_config','Expected a JSON configure object') from exc
        session=websocket.app.state.streaming.open(config)
        receiver=asyncio.create_task(receive());sender=asyncio.create_task(send())
        completed,_=await asyncio.wait((receiver,sender),return_when=asyncio.FIRST_COMPLETED)
        for task in completed: task.result()
        if receiver in completed and not sender.done(): await sender
    except (WebSocketDisconnect, RuntimeError): pass
    except StageError as exc:
        if session:
            session.protocol_failure=exc
            await session.cancel('protocol_error')
            if sender and not sender.done(): await sender
        else:
            await websocket.send_json(dict(type='error',protocol='smartvoice.stream.v1',code=exc.code,
                message=exc.message,stage='protocol',retryable=exc.code.endswith(('timeout','overload'))))
    except TimeoutError:
        try: await websocket.send_json(dict(type='error',protocol='smartvoice.stream.v1',code='transport_timeout',
            message='Transport deadline exceeded',stage='protocol',retryable=True))
        except (WebSocketDisconnect,RuntimeError):pass
    finally:
        for task in (receiver,sender):
            if task and not task.done():task.cancel()
        await asyncio.gather(*(t for t in (receiver,sender) if t),return_exceptions=True)
        if session: await session.cancel('client_disconnected')
        try:await websocket.close()
        except RuntimeError:pass
