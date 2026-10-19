"""Recovery protocol, bounded replay and preservation of session-private state."""
import asyncio
import unittest
from fastapi.testclient import TestClient
from smartvoice.app import create_app
from smartvoice.domain.streaming import StageError
from tests.test_api import FakeProvider
from tests.test_streaming import configuration
from tests.test_streaming_resilience import manager, collect, ControlledWorkers


def request(session, cursor=None, played=0, **changes):
    return dict(type='resume',protocol='smartvoice.stream.v1',session_id=session.id,
                resume_token=session.recovery.token,last_event_sequence=session.last_ack if cursor is None else cursor,
                last_audio_played=played,**changes)


class RecoveryServiceTests(unittest.IsolatedAsyncioTestCase):
    async def open(self, mode='transcription', consumption='delivery', **settings):
        owner,repo,workers=manager(**settings);self.addAsyncCleanup(owner.close)
        config=configuration(mode,'en',consumption);config['resumable']=True
        session=owner.open(config);await session.ready.wait()
        ready=await session.output.get();session.acknowledge(ready['event_sequence'])
        return owner,repo,workers,session

    async def test_resume_preserves_handles_context_and_rejects_duplicate_audio(self):
        owner,repo,workers,session=await self.open()
        pcm=b'\0\0'*16000
        await session.recovery.push(1,0,pcm)
        while session.context.version==0:
            event=await asyncio.wait_for(session.output.get(),2)
            session.acknowledge(event['event_sequence'])
        snapshot=session.context.snapshot();asr=session.asr;handles=len(workers.created)
        owner.detach(session)
        recovered,response,replay=owner.resume(request(session))
        self.assertIs(recovered,session);self.assertIs(session.asr,asr)
        self.assertEqual(session.context.snapshot(),snapshot)
        self.assertEqual(response['context_version'],snapshot.version)
        self.assertEqual(len(workers.created),handles)
        accepted=await session.recovery.push(1,0,pcm)
        self.assertEqual(accepted['accepted_samples'],16000)
        self.assertEqual(session.input_sequence,1)
        with self.assertRaises(StageError):await session.recovery.push(1,0,b'\1\0'*16000)
        with self.assertRaises(StageError):await session.recovery.push(3,16000,pcm)
        with self.assertRaises(StageError):await session.recovery.push(2,0,pcm)
        await session.recovery.push(2,16000,pcm)
        self.assertEqual(session.samples,32000)

    async def test_bad_tokens_cursors_and_parallel_attach_do_not_cancel_session(self):
        owner,_,_,session=await self.open()
        with self.assertRaises(StageError):owner.resume(request(session))
        owner.detach(session)
        for token in ('不正确','\ud800'):
            bad=request(session);bad['resume_token']=token
            with self.assertRaises(StageError):owner.resume(bad)
        for cursor in (-1,True,session.sequence+1):
            with self.assertRaises(StageError):owner.resume(request(session,cursor))
        self.assertFalse(session.recovery.attached);self.assertFalse(session.done.is_set())
        owner.resume(request(session));self.assertTrue(session.recovery.attached)

    async def test_detached_admission_expiry_and_shutdown_release_resources(self):
        owner,repo,workers,session=await self.open(streaming_resume_grace_seconds=.04,streaming_max_sessions=1)
        owner.detach(session)
        with self.assertRaises(StageError):owner.open(configuration())
        await asyncio.wait_for(session.done.wait(),2)
        self.assertEqual(session.cancel_reason,'resume_expired')
        self.assertEqual(repo.users,0);self.assertFalse(owner.recoverable)
        with self.assertRaises(StageError):owner.resume(request(session))
        self.assertTrue(all(worker.closed for worker in workers.created))

    async def test_replay_unacked_event_without_mutating_original_audio(self):
        owner,_,_,session=await self.open('spoken_interpretation','playback')
        await session.recovery.push(1,0,b'\0\0'*16000)
        audio=None
        while audio is None:
            event=await asyncio.wait_for(session.output.get(),2)
            session.acknowledge(event['event_sequence'])
            if event['type']=='audio_segment':audio=event
        payload=audio['audio'];session.speech.started(audio['audio_sequence'])
        owner.detach(session)
        _,_,replay=owner.resume(request(session))
        self.assertIn(audio,replay);self.assertEqual(audio['audio'],payload)
        self.assertEqual(session.speech.last_started,0)
        session.speech.started(audio['audio_sequence']);session.speech.played(audio['audio_sequence'])
        session.recovery.prune()
        owner.detach(session)
        _,_,replay=owner.resume(request(session,played=audio['audio_sequence']))
        self.assertNotIn(audio,replay)

    async def test_lost_played_ack_reconciles_and_invalid_claim_is_atomic(self):
        owner,_,_,session=await self.open('spoken_interpretation','playback')
        await session.recovery.push(1,0,b'\0\0'*16000)
        while True:
            event=await asyncio.wait_for(session.output.get(),2)
            session.acknowledge(event['event_sequence'])
            if event['type']=='audio_segment':break
        owner.detach(session)
        with self.assertRaises(StageError):owner.resume(request(session,played=2))
        self.assertEqual(session.speech.last_played,0)
        _,_,replay=owner.resume(request(session,played=1))
        self.assertEqual(session.speech.last_played,1);self.assertFalse(session.speech.ledger)
        self.assertNotIn(event,replay)

    async def test_cache_and_input_fingerprints_are_bounded(self):
        owner,_,_,session=await self.open()
        for sequence in range(1,132):
            await session.recovery.push(sequence,sequence-1,b'\0\0')
            # Fake model may produce output on each frame; consume ACKs concurrently.
            await asyncio.sleep(.001)
            while not session.output.empty():
                event=session.output.get_nowait();session.acknowledge(event['event_sequence'])
        self.assertEqual(len(session.recovery.frames),128)
        with self.assertRaises(StageError):await session.recovery.push(1,0,b'\0\0')
        cache=session.recovery
        cache.max_bytes=1
        with self.assertRaises(StageError):cache.remember(dict(type='source_partial',event_sequence=999,text='x'))

    async def test_completed_terminal_can_be_resumed_after_disconnect(self):
        owner,_,_,session=await self.open()
        await session.recovery.push(1,0,b'\0\0'*16000)
        await session.finish_input();events=[]
        terminal=await collect(session,events)
        await session.done.wait();owner.detach(session)
        _,_,replay=owner.resume(request(session))
        self.assertIn(terminal,replay);self.assertEqual(terminal['status'],'complete')
        owner.forget(session);self.assertFalse(owner.recoverable)

    async def test_detach_suspends_idle_and_reconnect_cancels_expiry(self):
        owner,repo,_,session=await self.open(streaming_idle_seconds=.02,streaming_resume_grace_seconds=.2)
        owner.detach(session);await asyncio.sleep(.07)
        self.assertFalse(session.done.is_set())
        owner.resume(request(session))
        for _ in range(25):
            session.heartbeat();await asyncio.sleep(.01)
        self.assertFalse(session.done.is_set());self.assertEqual(repo.users,1)

    async def test_shutdown_cancels_detached_timers_and_releases_models(self):
        owner,repo,workers,session=await self.open()
        owner.detach(session);timer=session.recovery.expiry_task
        await owner.close()
        self.assertTrue(timer.done());self.assertFalse(owner.recoverable)
        self.assertTrue(session.done.is_set());self.assertEqual(repo.users,0)
        self.assertTrue(all(worker.closed for worker in workers.created))

    async def test_repeated_reconnect_cancels_every_old_expiry_without_reloading(self):
        owner,repo,workers,session=await self.open(streaming_resume_grace_seconds=.03)
        asr=session.asr;context=session.context;handles=len(workers.created)
        timers=[]
        for _ in range(4):
            owner.detach(session);timer=session.recovery.expiry_task;timers.append(timer)
            owner.detach(session)  # Duplicate close notification must not replace the timer.
            self.assertIs(session.recovery.expiry_task,timer)
            recovered,_,_=owner.resume(request(session))
            self.assertIs(recovered,session)
            await asyncio.sleep(0)
        await asyncio.sleep(.06)  # Beyond the canceled timers' grace periods.
        self.assertTrue(all(timer.done() for timer in timers))
        self.assertFalse(session.done.is_set());self.assertEqual(repo.users,1)
        self.assertIs(session.asr,asr);self.assertIs(session.context,context)
        self.assertEqual(len(workers.created),handles)

    async def test_expiring_detached_session_does_not_cancel_active_sibling(self):
        owner,repo,_,first=await self.open(streaming_resume_grace_seconds=.03,streaming_max_sessions=2)
        config=configuration();config['resumable']=True
        second=owner.open(config);await second.ready.wait()
        ready=await second.output.get();second.acknowledge(ready['event_sequence'])
        owner.detach(first)
        swapped=request(first);swapped['resume_token']=second.recovery.token
        with self.assertRaises(StageError) as rejected:owner.resume(swapped)
        self.assertEqual(rejected.exception.code,'resume_unavailable')
        await asyncio.wait_for(first.done.wait(),2)
        self.assertFalse(second.done.is_set());self.assertEqual(repo.users,1)
        self.assertNotIn(first.id,owner.active);self.assertIn(second.id,owner.active)
        await second.recovery.push(1,0,b'\0\0'*16000)
        await second.finish_input();events=[]
        self.assertEqual((await collect(second,events))['status'],'complete')
        await second.done.wait();self.assertEqual(repo.users,0)

    async def test_malformed_resume_requests_leave_expiry_and_private_state_unchanged(self):
        owner,_,_,session=await self.open()
        owner.detach(session);timer=session.recovery.expiry_task
        valid=request(session)
        invalid=[None,[],{**valid,'unexpected':True}]
        invalid.extend({k:v for k,v in valid.items() if k!=field} for field in valid)
        for field,value in (('type','configure'),('protocol','other'),('session_id',1),('resume_token',False)):
            invalid.append({**valid,field:value})
        for row in invalid:
            with self.subTest(request=row):
                with self.assertRaises(StageError) as rejected:owner.resume(row)
                self.assertEqual(rejected.exception.code,'invalid_resume')
                self.assertFalse(session.recovery.attached)
                self.assertIs(session.recovery.expiry_task,timer)
                self.assertFalse(timer.done());self.assertEqual(session.last_ack,1)
        for played in (True,-1,1,'0'):
            with self.assertRaises(StageError):owner.resume(request(session,played=played))
            self.assertFalse(session.recovery.attached)
        owner.resume(valid)

    async def test_failed_audio_validation_does_not_advance_deduplication_state(self):
        _,_,_,session=await self.open()
        for sequence,offset,pcm in ((True,0,b'\0\0'),(1,False,b'\0\0'),
                (0,0,b'\0\0'),(1,-1,b'\0\0'),(1,0,b''),(1,0,b'\0'),(1,0,b'\0'*32002)):
            with self.subTest(sequence=sequence,offset=offset,length=len(pcm)):
                with self.assertRaises(StageError):await session.recovery.push(sequence,offset,pcm)
                self.assertEqual(session.samples,0);self.assertEqual(session.input_sequence,0)
                self.assertFalse(session.recovery.frames)
        await session.recovery.push(1,0,b'\0\0')
        await session.finish_input()
        self.assertEqual((await session.recovery.push(1,0,b'\0\0'))['accepted_samples'],1)
        with self.assertRaises(StageError) as rejected:await session.recovery.push(2,1,b'\0\0')
        self.assertEqual(rejected.exception.code,'invalid_state')
        self.assertEqual(session.samples,1);self.assertEqual(list(session.recovery.frames),[1])

    async def test_cache_event_limit_reserves_terminal_and_prune_accounts_bytes(self):
        _,_,_,session=await self.open()
        cache=session.recovery;cache.max_events=2
        await session.emit(dict(type='source_partial',text='one'))
        await session.emit(dict(type='source_partial',text='two'))
        before=cache.bytes
        with self.assertRaises(StageError) as rejected:
            cache.remember(dict(type='source_partial',event_sequence=999,text='overflow'))
        self.assertEqual(rejected.exception.code,'resume_buffer_overload')
        self.assertEqual(len(cache.events),2);self.assertEqual(cache.bytes,before)
        # Reserve terminal space even when normal output filled the cache.
        terminal=await session.emit(dict(type='error',code='test_failure'),terminal=True)
        self.assertEqual(len(cache.events),3)
        session.acknowledge(terminal['event_sequence'])
        self.assertEqual(list(cache.events),[terminal['event_sequence']])
        self.assertEqual(cache.bytes,cache.events[terminal['event_sequence']][1])
        cache.prune();self.assertGreater(cache.bytes,0)

    async def test_replay_overload_is_explicit_recoverable_terminal_and_releases_models(self):
        owner,repo,workers,session=await self.open()
        session.recovery.max_events=1
        owner.detach(session)
        await session.recovery.push(1,0,b'\0\0'*16000)
        await asyncio.wait_for(session.done.wait(),2)
        _,_,replay=owner.resume(request(session))
        terminal=[row for row in replay if row['type']=='error']
        self.assertEqual(len(terminal),1)
        self.assertEqual(terminal[0]['code'],'resume_buffer_overload')
        self.assertEqual(repo.users,0);self.assertNotIn(session.id,owner.active)
        self.assertTrue(all(worker.closed for worker in workers.created))
        owner.forget(session)
        with self.assertRaises(StageError):owner.resume(request(session))

    async def test_resume_token_does_not_survive_manager_restart_or_explicit_forget(self):
        owner,repo,_,session=await self.open()
        owner.detach(session);saved=request(session)
        replacement,_,_=manager();self.addAsyncCleanup(replacement.close)
        with self.assertRaises(StageError) as rejected:replacement.resume(saved)
        self.assertEqual(rejected.exception.code,'resume_unavailable')
        owner.forget(session);await session.cancel('client_cancel')
        with self.assertRaises(StageError) as rejected:owner.resume(saved)
        self.assertEqual(rejected.exception.code,'resume_unavailable')
        self.assertFalse(owner.recoverable);self.assertEqual(repo.users,0)

    async def test_inflight_stage_failure_while_detached_replays_original_error(self):
        workers=ControlledWorkers(block=('asr','push_audio'),fail=('asr','push_audio'))
        owner,repo,_=manager(workers=workers);self.addAsyncCleanup(owner.close)
        config=configuration();config['resumable']=True
        session=owner.open(config);await session.ready.wait()
        ready=await session.output.get();session.acknowledge(ready['event_sequence'])
        await session.recovery.push(1,0,b'\0\0'*16000)
        await asyncio.wait_for(workers.entered.wait(),2)
        owner.detach(session);workers.resume.set()
        await asyncio.wait_for(session.done.wait(),2)
        _,response,replay=owner.resume(request(session))
        errors=[row for row in replay if row['type']=='error']
        self.assertEqual(len(errors),1);self.assertEqual(errors[0]['code'],'asr_failure')
        self.assertEqual(response['accepted_samples'],16000)
        self.assertEqual(repo.users,0);self.assertTrue(all(worker.closed for worker in workers.created))
        with self.assertRaises(StageError):await session.recovery.push(2,16000,b'\0\0')

    async def test_completed_terminal_expires_without_retaining_admission(self):
        owner,repo,_,session=await self.open(streaming_max_sessions=1,streaming_resume_grace_seconds=.03)
        await session.finish_input();events=[]
        terminal=await collect(session,events);await session.done.wait()
        self.assertEqual(terminal['status'],'no_speech');self.assertEqual(repo.users,0)
        config=configuration();config['resumable']=True
        with self.assertRaises(StageError):owner.open(config)
        owner.detach(session);timer=session.recovery.expiry_task
        await asyncio.wait_for(timer,2)
        self.assertFalse(owner.recoverable)
        replacement=owner.open(config);await replacement.ready.wait()
        self.assertNotEqual(replacement.id,session.id)

    async def test_unreceived_played_claim_does_not_mutate_ack_or_playback_state(self):
        owner,_,_,session=await self.open('spoken_interpretation','playback')
        await session.recovery.push(1,0,b'\0\0'*16000)
        while True:
            event=await asyncio.wait_for(session.output.get(),2)
            if event['type']=='audio_segment':break
            session.acknowledge(event['event_sequence'])
        owner.detach(session)
        ack=session.last_ack;ledger=dict(session.speech.ledger);size=session.recovery.bytes
        with self.assertRaises(StageError):owner.resume(request(session,played=1))
        self.assertEqual(session.last_ack,ack);self.assertEqual(session.speech.last_played,0)
        self.assertEqual(session.speech.last_started,0);self.assertEqual(session.speech.ledger,ledger)
        self.assertEqual(session.recovery.bytes,size);self.assertFalse(session.recovery.attached)
        _,_,replay=owner.resume(request(session,cursor=event['event_sequence'],played=1))
        self.assertEqual(session.speech.last_played,1);self.assertNotIn(event,replay)


class RecoveryWebSocketTests(unittest.TestCase):
    def test_finish_retransmission_after_resume_drains_once(self):
        workers=ControlledWorkers(block=('asr','push_audio'))
        owner,repo,_=manager(workers=workers)
        app=create_app(owner.settings,FakeProvider());app.state.streaming=owner
        with TestClient(app) as client:
            config=configuration();config['resumable']=True
            with client.websocket_connect('/v1/audio/stream') as ws:
                ws.send_json(config);ready=ws.receive_json();sid=ready['session_id']
                cursor=ready['event_sequence'];ws.send_json({'type':'ack','event_sequence':cursor})
                ws.send_json({'type':'audio_frame','sequence':1,'start_sample':0});ws.send_bytes(b'\0\0'*16000)
                self.assertEqual(ws.receive_json()['type'],'input_ack')
                ws.send_json({'type':'finish'});ws.send_json({'type':'finish'})
                async def input_finished():
                    async with asyncio.timeout(2):
                        while not owner.active[sid].input_finished:await asyncio.sleep(.001)
                client.portal.call(input_finished)
                ws.close()
                async def detached():
                    async with asyncio.timeout(2):
                        while owner.recoverable[sid].attached:await asyncio.sleep(.001)
                client.portal.call(detached)
            with client.websocket_connect('/v1/audio/stream') as ws:
                ws.send_json(dict(type='resume',protocol='smartvoice.stream.v1',session_id=sid,
                    resume_token=ready['recovery']['resume_token'],last_event_sequence=cursor,last_audio_played=0))
                resumed=ws.receive_json();self.assertTrue(resumed['input_finished'])
                ws.send_json({'type':'finish'});ws.send_json({'type':'finish'})
                client.portal.call(workers.resume.set)
                events=[]
                while True:
                    row=ws.receive_json();events.append(row)
                    self.assertNotEqual(row['type'],'error',row)
                    ws.send_json({'type':'ack','event_sequence':row['event_sequence']})
                    if row['type']=='session_complete':break
                self.assertEqual(row['status'],'complete')
                self.assertEqual(sum(e['type']=='source_unit_final' for e in events),1)
                async def forgotten():
                    async with asyncio.timeout(2):
                        while sid in owner.recoverable:await asyncio.sleep(.001)
                client.portal.call(forgotten)
                self.assertEqual(repo.users,0)

    def test_six_routes_resume_existing_session_and_deduplicate_input(self):
        for mode in ('transcription','translated_subtitles','spoken_interpretation'):
            for language in ('zh','en'):
                with self.subTest(mode=mode,language=language):
                    owner,repo,workers=manager();app=create_app(owner.settings,FakeProvider());app.state.streaming=owner
                    with TestClient(app) as client:
                        config=configuration(mode,language);config['resumable']=True
                        with client.websocket_connect('/v1/audio/stream') as ws:
                            ws.send_json(config);ready=ws.receive_json()
                            sid=ready['session_id'];token=ready['recovery']['resume_token'];cursor=ready['event_sequence']
                            ws.send_json({'type':'ack','event_sequence':cursor})
                            ws.send_json({'type':'audio_frame','sequence':1,'start_sample':0});ws.send_bytes(b'\0\0'*16000)
                            while True:
                                event=ws.receive_json()
                                if event['type']=='input_ack':continue
                                if event['type']=='audio_segment':ws.receive_bytes()
                                cursor=event['event_sequence'];ws.send_json({'type':'ack','event_sequence':cursor})
                                if event['type']=='source_unit_final':break
                            ws.close()
                            async def detached():
                                for _ in range(100):
                                    if not owner.recoverable[sid].attached:return
                                    await asyncio.sleep(.01)
                                raise AssertionError('Transport did not detach')
                            client.portal.call(detached)
                        with client.websocket_connect('/v1/audio/stream') as ws:
                            ws.send_json(dict(type='resume',protocol='smartvoice.stream.v1',session_id=sid,
                                resume_token=token,last_event_sequence=cursor,last_audio_played=0))
                            restored=ws.receive_json();self.assertEqual(restored['type'],'session_resumed')
                            self.assertEqual(restored['input_sequence'],1);self.assertGreater(restored['context_version'],0)
                            ws.send_json({'type':'audio_frame','sequence':1,'start_sample':0});ws.send_bytes(b'\0\0'*16000)
                            ws.send_json({'type':'audio_frame','sequence':2,'start_sample':16000});ws.send_bytes(b'\0\0'*16000)
                            ws.send_json({'type':'finish'})
                            audio=[]
                            while True:
                                event=ws.receive_json()
                                if event['type']=='input_ack':continue
                                if event['type']=='audio_segment':
                                    audio.append(event['audio_sequence']);self.assertEqual(len(ws.receive_bytes()),event['audio_bytes'])
                                if event['type'] in ('error','session_complete'):
                                    self.assertEqual(event['type'],'session_complete',event)
                                    self.assertEqual(event['status'],'complete');break
                                cursor=max(cursor,event['event_sequence']);ws.send_json({'type':'ack','event_sequence':cursor})
                            self.assertEqual(audio,sorted(set(audio)))
                    self.assertEqual(repo.users,0);self.assertFalse(owner.recoverable)

class RecoveryPlaybackTransportTests(unittest.TestCase):
    def test_disconnect_between_descriptor_and_binary_replays_same_chunk(self):
        owner,repo,workers=manager();app=create_app(owner.settings,FakeProvider());app.state.streaming=owner
        with TestClient(app) as client:
            config=configuration('spoken_interpretation','en','playback');config['resumable']=True
            with client.websocket_connect('/v1/audio/stream') as ws:
                ws.send_json(config);ready=ws.receive_json();sid=ready['session_id'];cursor=ready['event_sequence']
                ws.send_json({'type':'ack','event_sequence':cursor})
                ws.send_json({'type':'audio_frame','sequence':1,'start_sample':0});ws.send_bytes(b'\0\0'*16000)
                while True:
                    event=ws.receive_json()
                    if event['type']=='input_ack':continue
                    if event['type']=='audio_segment':break
                    cursor=event['event_sequence'];ws.send_json({'type':'ack','event_sequence':cursor})
                descriptor=event
                # Neither receive the binary payload nor ACK this descriptor.
                ws.close()
                async def detached():
                    for _ in range(100):
                        if not owner.recoverable[sid].attached:return
                        await asyncio.sleep(.01)
                    raise AssertionError('No detach')
                client.portal.call(detached)
            with client.websocket_connect('/v1/audio/stream') as ws:
                ws.send_json(dict(type='resume',protocol='smartvoice.stream.v1',session_id=sid,
                    resume_token=ready['recovery']['resume_token'],last_event_sequence=cursor,last_audio_played=0))
                self.assertEqual(ws.receive_json()['type'],'session_resumed')
                replay=ws.receive_json();self.assertEqual(replay,descriptor)
                self.assertEqual(len(ws.receive_bytes()),replay['audio_bytes'])
                ws.send_json({'type':'ack','event_sequence':replay['event_sequence']})
                ws.send_json({'type':'audio_started','audio_sequence':replay['audio_sequence']})
                ws.send_json({'type':'audio_played','audio_sequence':replay['audio_sequence']})
                ws.send_json({'type':'finish'})
                while True:
                    row=ws.receive_json()
                    self.assertNotEqual(row['type'],'error',row)
                    if row['type']=='audio_segment':
                        ws.receive_bytes();ws.send_json({'type':'audio_started','audio_sequence':row['audio_sequence']})
                        ws.send_json({'type':'audio_played','audio_sequence':row['audio_sequence']})
                    ws.send_json({'type':'ack','event_sequence':row['event_sequence']})
                    if row['type']=='session_complete':self.assertEqual(row['status'],'complete');break
        self.assertEqual(repo.users,0);self.assertFalse(owner.recoverable)

class RecoveryTCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_tcp_abort_replays_unreceived_audio_payload(self):
        import json
        import socket
        import threading
        import uvicorn
        import websockets
        owner,repo,_=manager();app=create_app(owner.settings,FakeProvider());app.state.streaming=owner
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(8)
        port=listener.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,log_level='critical',access_log=False,timeout_graceful_shutdown=2))
        thread=threading.Thread(target=lambda:asyncio.run(server.serve(sockets=[listener])),daemon=True)
        thread.start()
        try:
            async with asyncio.timeout(15):
                while not server.started:await asyncio.sleep(.01)
                url=f'ws://127.0.0.1:{port}/v1/audio/stream'
                ws=await websockets.connect(url)
                config=configuration('spoken_interpretation','en','playback');config['resumable']=True
                await ws.send(json.dumps(config));ready=json.loads(await ws.recv());sid=ready['session_id'];cursor=ready['event_sequence']
                await ws.send(json.dumps({'type':'ack','event_sequence':cursor}))
                await ws.send(json.dumps({'type':'audio_frame','sequence':1,'start_sample':0}));await ws.send(b'\0\0'*16000)
                while True:
                    event=json.loads(await ws.recv())
                    if event['type']=='input_ack':continue
                    if event['type']=='audio_segment':break
                    cursor=event['event_sequence'];await ws.send(json.dumps({'type':'ack','event_sequence':cursor}))
                descriptor=event
                ws.transport.abort()  # Abrupt TCP loss, before consuming the binary payload.
                await ws.wait_closed()
                while owner.recoverable[sid].attached:await asyncio.sleep(.01)
                async with websockets.connect(url) as resumed:
                    await resumed.send(json.dumps(dict(type='resume',protocol='smartvoice.stream.v1',
                        session_id=sid,resume_token=ready['recovery']['resume_token'],
                        last_event_sequence=cursor,last_audio_played=0)))
                    self.assertEqual(json.loads(await resumed.recv())['type'],'session_resumed')
                    replay=json.loads(await resumed.recv());self.assertEqual(replay,descriptor)
                    self.assertEqual(len(await resumed.recv()),replay['audio_bytes'])
                    for row in ({'type':'ack','event_sequence':replay['event_sequence']},
                                {'type':'audio_started','audio_sequence':replay['audio_sequence']},
                                {'type':'audio_played','audio_sequence':replay['audio_sequence']}, {'type':'finish'}):
                        await resumed.send(json.dumps(row))
                    while True:
                        row=json.loads(await resumed.recv());self.assertNotEqual(row['type'],'error',row)
                        if row['type']=='audio_segment':
                            await resumed.recv()
                            await resumed.send(json.dumps({'type':'audio_started','audio_sequence':row['audio_sequence']}))
                            await resumed.send(json.dumps({'type':'audio_played','audio_sequence':row['audio_sequence']}))
                        await resumed.send(json.dumps({'type':'ack','event_sequence':row['event_sequence']}))
                        if row['type']=='session_complete':self.assertEqual(row['status'],'complete');break
                for _ in range(100):
                    if not owner.recoverable:break
                    await asyncio.sleep(.01)
                self.assertFalse(owner.recoverable);self.assertEqual(repo.users,0)
        finally:
            server.should_exit=True
            await asyncio.to_thread(thread.join,5)
            listener.close()
            self.assertFalse(thread.is_alive(),'Owned test server did not exit')

class RecoveryBrowserClientTests(unittest.TestCase):
    def test_browser_microphone_capture_and_stop_drain_contract(self):
        import shutil
        import subprocess
        from tests.streaming_long_audio import ROOT
        node=shutil.which('node')
        if not node:self.skipTest('Node.js is required for the browser-client simulation')
        import os
        for sample_rate in ('48000','16000'):
            with self.subTest(sample_rate=sample_rate):
                env={**os.environ,'MIC_SAMPLE_RATE':sample_rate}
                result=subprocess.run([node,'tests/streaming_microphone_browser.js'],cwd=ROOT,env=env,
                                      capture_output=True,text=True,timeout=8)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_browser_reconnect_and_lost_input_ack_contract(self):
        import shutil
        import subprocess
        from tests.streaming_long_audio import ROOT
        node=shutil.which('node')
        if not node:self.skipTest('Node.js is required for the browser-client simulation')
        result=subprocess.run([node,'tests/streaming_resume_browser.js'],cwd=ROOT,
                              capture_output=True,text=True,timeout=8)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

class RecoveryTerminalTransportTests(unittest.TestCase):
    def test_terminal_without_ack_remains_recoverable(self):
        owner,repo,_=manager();app=create_app(owner.settings,FakeProvider());app.state.streaming=owner
        with TestClient(app) as client:
            config=configuration();config['resumable']=True
            with client.websocket_connect('/v1/audio/stream') as ws:
                ws.send_json(config);ready=ws.receive_json();sid=ready['session_id'];cursor=ready['event_sequence']
                ws.send_json({'type':'ack','event_sequence':cursor})
                ws.send_json({'type':'audio_frame','sequence':1,'start_sample':0});ws.send_bytes(b'\0\0'*16000)
                ws.send_json({'type':'finish'})
                while True:
                    row=ws.receive_json()
                    if row['type']=='input_ack':continue
                    if row['type']=='session_complete':break
                    cursor=row['event_sequence'];ws.send_json({'type':'ack','event_sequence':cursor})
                terminal=row;ws.close()
                async def detached():
                    for _ in range(100):
                        if not owner.recoverable[sid].attached:return
                        await asyncio.sleep(.01)
                    raise AssertionError('No terminal detach')
                client.portal.call(detached)
            with client.websocket_connect('/v1/audio/stream') as ws:
                ws.send_json(dict(type='resume',protocol='smartvoice.stream.v1',session_id=sid,
                    resume_token=ready['recovery']['resume_token'],last_event_sequence=cursor,last_audio_played=0))
                self.assertEqual(ws.receive_json()['type'],'session_resumed')
                self.assertEqual(ws.receive_json(),terminal)
                ws.send_json({'type':'ack','event_sequence':terminal['event_sequence']})
                async def acknowledged():
                    for _ in range(100):
                        if sid not in owner.recoverable:return
                        await asyncio.sleep(.01)
                    raise AssertionError('Terminal ACK did not release recovery')
                client.portal.call(acknowledged)
            self.assertEqual(repo.users,0);self.assertFalse(owner.recoverable)
