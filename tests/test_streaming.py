"""Product protocol and lifecycle checks without model downloads or native inference."""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
import uuid
from fastapi.testclient import TestClient
from smartvoice.app import create_app
from smartvoice.config.settings import Settings
from smartvoice.domain.streaming import Hypothesis, TranslationResult, SynthesizedAudio, StageError
from smartvoice.services.model_registry import get_model_spec
from smartvoice.services.streaming.planning import resolve
from smartvoice.services.streaming.manager import StreamingManager
from smartvoice.adapters.audio.streaming import package_wav
from smartvoice.adapters.inference.runtime.stream_profile import Profiler
from smartvoice.adapters.inference.runtime.stream_workers import FairGate, ProcessAffinityWorker
from smartvoice.adapters.inference.runtime.compute_budget import ComputeBudget
from tests.test_api import FakeProvider


def configuration(mode='transcription', language='en', consumption='delivery'):
    row=dict(type='configure',protocol='smartvoice.stream.v1',mode=mode,source_language=language,
        audio=dict(encoding='pcm_s16le',sample_rate=16000,channels=1),output_consumption=consumption)
    if mode!='transcription': row['target_language']='zh' if language=='en' else 'en'
    return row

class Repository:
    def __init__(self):self.users=0
    get_spec=staticmethod(get_model_spec)
    def installed_models(self):return [{'id':spec.id} for spec in __import__('smartvoice.services.model_registry',fromlist=['load_catalog']).load_catalog()]
    @contextmanager
    def hold_models(self,ids):
        self.users+=1
        try:yield
        finally:self.users-=1

class FakeASR:
    def push_audio(self,block):return [Hypothesis('source_partial',1,1,'Hello world','en',0,len(block.pcm)//2,False)]
    def finish(self):return [Hypothesis('source_final',1,2,'Hello world','en',0,1600,True,'end_of_input')]
    def close(self):pass
class FakeFormatter:
    def format(self,text,language,final=False):return text.rstrip('.')+'.' if final else text
    def close(self):pass
class FakeTranslator:
    def translate(self,text):return TranslationResult('你好，世界。')
    def close(self):pass
class FakeTTS:
    def synthesize(self,target):return SynthesizedAudio(target.translation_id,target.language,b'\0\0'*2205,22050,1)
    def close(self):pass
class FakeWorker:
    def __init__(self,owner):self.owner=owner;self.closed=False
    async def construct(self,factory,deadline=None):return factory()
    async def call(self,function,*args,**kwargs):return function(*args)
    async def aclose(self):self.closed=True
class FakeWorkers:
    def __init__(self):self.created=[]
    def preflight(self,specs):pass
    def create(self,name,profiler):
        worker=FakeWorker(self);self.created.append(worker);return worker
    def binding(self,spec,plan):return {'asr':FakeASR,'formatting':FakeFormatter,'translation':FakeTranslator,'tts':FakeTTS}[(spec.streaming or {}).get('stage','tts')]

def return_answer():return 42

def hang_forever():
    import time
    while True:time.sleep(.1)

class StreamingContractTests(unittest.TestCase):
    def settings(self,**changes):return Settings(data_dir=Path('.smartvoice-dev')/('stream-tests-'+uuid.uuid4().hex),streaming_enabled=True,**changes)
    def test_six_plans_and_generic_model_validation(self):
        repository=Repository()
        for mode in ('transcription','translated_subtitles','spoken_interpretation'):
            for language in ('zh','en'):
                plan,specs,resident=resolve(configuration(mode,language),repository)
                self.assertEqual(plan.source_language,language)
                self.assertGreater(resident,0)
                self.assertTrue(set(plan.model_ids).issubset({r['id'] for r in repository.installed_models()}))
        for change in ({'source_language':'auto'},{'unexpected':1},{'models':{'asr':'tts-matcha-zh-baker'}},{'ack_window':True},{'models':{'asr':None}},{'mode':[]},{'audio':{'encoding':'pcm_s16le','sample_rate':16000,'channels':True}}):
            with self.subTest(change=change),self.assertRaises(StageError):resolve({**configuration(),**change},repository)
    def test_default_disabled_and_cross_origin_rejected(self):
        app=create_app(replace(self.settings(),streaming_enabled=False),FakeProvider())
        with TestClient(app) as client:
            self.assertFalse(client.get('/v1/audio/stream/capabilities').json()['enabled'])
            with client.websocket_connect('/v1/audio/stream') as ws:
                ws.send_json(configuration());self.assertEqual(ws.receive_json()['code'],'streaming_disabled')
            with self.assertRaises(Exception):
                with client.websocket_connect('/v1/audio/stream',headers={'origin':'https://untrusted.example'}):pass
            self.assertEqual(client.get('/console/streaming').status_code,200)
    def test_websocket_text_and_speech_delivery_and_playback(self):
        for mode in ('transcription','translated_subtitles','spoken_interpretation'):
            for consumption in ('delivery','playback') if mode=='spoken_interpretation' else ('delivery',):
                with self.subTest(mode=mode,consumption=consumption):
                    settings=self.settings();repo=Repository();workers=FakeWorkers()
                    app=create_app(settings,FakeProvider())
                    app.state.streaming=StreamingManager(settings,repo,workers,Profiler,FairGate,package_wav)
                    with TestClient(app) as client,client.websocket_connect('/v1/audio/stream') as ws:
                        ws.send_json(configuration(mode,consumption=consumption))
                        ready=ws.receive_json();self.assertEqual(ready['type'],'session_ready')
                        ws.send_json({'type':'ack','event_sequence':ready['event_sequence']})
                        ws.send_bytes(b'\0\0'*1600);ws.send_json({'type':'finish'})
                        events=[];audio=[]
                        while True:
                            event=ws.receive_json();events.append(event)
                            if event['type']=='audio_segment':
                                payload=ws.receive_bytes();self.assertTrue(payload.startswith(b'RIFF'));audio.append(event)
                                if consumption=='playback':
                                    ws.send_json(dict(type='audio_started',audio_sequence=event['audio_sequence']))
                                    ws.send_json(dict(type='audio_played',audio_sequence=event['audio_sequence']))
                            if event['type'] in ('error','session_complete'):break
                            ws.send_json(dict(type='ack',event_sequence=event['event_sequence']))
                        self.assertEqual(events[-1]['type'],'session_complete',events[-1])
                        self.assertEqual(events[-1]['status'],'complete')
                        self.assertTrue(any(e['type']=='source_unit_final' for e in events))
                        if mode!='transcription':self.assertTrue(any(e['type']=='target_final' for e in events))
                        if mode=='spoken_interpretation':self.assertEqual(len(audio),1)
                        self.assertEqual(repo.users,0);self.assertTrue(all(w.closed for w in workers.created))
    def test_protocol_failure_is_one_terminal(self):
        settings=self.settings();app=create_app(settings,FakeProvider());repo=Repository()
        app.state.streaming=StreamingManager(settings,repo,FakeWorkers(),Profiler,FairGate,package_wav)
        with TestClient(app) as client,client.websocket_connect('/v1/audio/stream') as ws:
            ws.send_json(configuration());ready=ws.receive_json()
            ws.send_json(dict(type='ack',event_sequence=ready['event_sequence']+10))
            event=ws.receive_json();self.assertEqual(event['type'],'error');self.assertEqual(event['code'],'invalid_ack')
        self.assertEqual(repo.users,0)

    def test_malformed_controls_and_binary_config_are_rejected(self):
        settings=self.settings();repo=Repository();app=create_app(settings,FakeProvider())
        app.state.streaming=StreamingManager(settings,repo,FakeWorkers(),Profiler,FairGate,package_wav)
        with TestClient(app) as client:
            with client.websocket_connect('/v1/audio/stream') as ws:
                ws.send_bytes(b'\0\0');self.assertEqual(ws.receive_json()['code'],'invalid_config')
            with client.websocket_connect('/v1/audio/stream') as ws:
                ws.send_json(configuration());ws.receive_json();ws.send_json({'type':[]})
                self.assertEqual(ws.receive_json()['code'],'invalid_message')
        self.assertEqual(repo.users,0)

    def test_streaming_model_use_blocks_uninstall_until_released(self):
        from unittest.mock import patch
        from smartvoice.adapters.storage.catalog_model_repository import CatalogModelRepository
        repository=CatalogModelRepository(self.settings());mid='streaming-stt-zipformer-en-int8'
        with patch.object(repository,'installed_models',return_value=[{'id':mid}]),patch(
            'smartvoice.adapters.storage.catalog_model_repository.uninstall_model',return_value=0) as uninstall:
            with repository.hold_models([mid]):
                repository.uninstall_model(mid);self.assertTrue(uninstall.call_args.kwargs['loaded'])
            repository.uninstall_model(mid);self.assertFalse(uninstall.call_args.kwargs['loaded'])

    def test_frontend_packetization_preserves_gain_and_sample_ranges(self):
        import numpy as np
        from smartvoice.adapters.inference.streaming.asr import OnlineASR
        from smartvoice.adapters.inference.streaming.frontend import CausalGain
        from smartvoice.domain.streaming import AudioBlock
        pcm=np.arange(-501,501,dtype='<i2').tobytes()
        def feed(sizes):
            adapter=OnlineASR.__new__(OnlineASR);adapter.received=0;adapter.total=0
            adapter.pending_pcm=b'';adapter.language='en';adapter.gain=CausalGain('gain_v4');outputs=[]
            def accept(samples):adapter.total+=len(samples);outputs.extend(samples.tolist());return []
            adapter.accept=accept;adapter._finish=lambda:[]
            offset=0
            for sequence,size in enumerate(sizes):
                chunk=pcm[offset:offset+size]
                adapter.push_audio(AudioBlock(sequence,chunk,offset//2,0));offset+=size
            adapter.finish();return outputs,adapter.total
        self.assertEqual(feed([len(pcm)]),feed([640,640,640,len(pcm)-1920]))
        self.assertEqual(feed([2]*1002),feed([len(pcm)]))

class StreamingLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def make(self,**changes):
        settings=Settings(data_dir=Path('.smartvoice-dev/streaming-tests'),streaming_enabled=True,**changes)
        repo=Repository();workers=FakeWorkers()
        return StreamingManager(settings,repo,workers,Profiler,FairGate,package_wav),repo,workers
    async def test_cancel_before_task_start_and_reopen(self):
        manager,repo,workers=self.make()
        session=manager.open(configuration());await session.cancel();self.assertEqual(repo.users,0);self.assertFalse(manager.active)
        session=manager.open(configuration());await session.ready.wait();await session.cancel();self.assertEqual(repo.users,0)
        self.assertTrue(all(w.closed for w in workers.created));await manager.close()
    async def test_capacity_and_no_input_idle_cleanup(self):
        manager,repo,workers=self.make(streaming_idle_seconds=.03)
        session=manager.open(configuration())
        with self.assertRaisesRegex(StageError,'capacity'):manager.open(configuration())
        await asyncio.wait_for(session.done.wait(),2)
        events=[]
        while not session.output.empty():events.append(session.output.get_nowait())
        self.assertEqual(events[-1]['code'],'input_timeout');self.assertEqual(repo.users,0)
        self.assertFalse(manager.active);await manager.close()
    async def test_compute_capacity_cancellation_and_rest_sync_share(self):
        budget=ComputeBudget(1)
        async with budget.async_permit():
            async def wait():
                async with budget.async_permit():pass
            task=asyncio.create_task(wait());await asyncio.sleep(.02);self.assertEqual(budget.snapshot()['waiting'],1)
            task.cancel();await asyncio.gather(task,return_exceptions=True)
        self.assertEqual(budget.snapshot()['active'],0);self.assertEqual(budget.snapshot()['waiting'],0)
        def sync():
            with budget.permit():return budget.snapshot()['active']
        self.assertEqual(await asyncio.to_thread(sync),1)
    async def test_normal_native_completion_closes_without_join_race(self):
        for _ in range(3):
            worker=ProcessAffinityWorker('test',Profiler(),grace_seconds=.1)
            self.assertEqual(await worker.call(return_answer,deadline=5),42)
            await worker.aclose();self.assertTrue(worker.closed)
            self.assertFalse(worker.owned_pids())

    async def test_native_hang_terminates_owned_process(self):
        worker=ProcessAffinityWorker('test',Profiler(),grace_seconds=.05)
        with self.assertRaisesRegex(StageError,'deadline'):
            await worker.call(hang_forever,deadline=1)
        await worker.aclose();self.assertTrue(worker.closed)
        self.assertFalse(worker.owned_pids())

    async def test_initialization_failure_releases_all_resources(self):
        manager,repo,workers=self.make()
        async def fail(factory,deadline=None):raise StageError('asr_failure','Initialization failed')
        worker=FakeWorker(workers);worker.construct=fail
        workers.create=lambda name,profiler:worker
        session=manager.open(configuration());await asyncio.wait_for(session.done.wait(),2)
        event=session.output.get_nowait();self.assertEqual(event['code'],'asr_failure')
        self.assertEqual(repo.users,0);self.assertFalse(manager.active);self.assertTrue(worker.closed)
        await manager.close()

    async def test_unproven_cleanup_quarantines_capacity_and_model_lease(self):
        manager,repo,workers=self.make()
        worker=FakeWorker(workers)
        async def fail_construct(factory,deadline=None):raise StageError('asr_failure', 'Initialization failed')
        async def fail_close():raise StageError('cleanup_failure', 'Owned worker still alive')
        worker.construct=fail_construct;worker.aclose=fail_close
        workers.create=lambda name,profiler:worker
        session=manager.open(configuration());await asyncio.wait_for(session.done.wait(),2)
        event=session.output.get_nowait();self.assertEqual(event['code'], 'cleanup_failure')
        self.assertEqual(repo.users,1);self.assertTrue(manager.active)
        with self.assertRaises(StageError):manager.open(configuration())
        await manager.close()

class StreamingQualityPolicyTests(unittest.TestCase):
    def test_years_numbers_names_negation_and_sentence_scope(self):
        from smartvoice.domain.translation_policy import TranslationPolicy, normalize_calendar, normalize_spoken_english_years, clauses
        self.assertEqual(normalize_calendar('二零一一年八月、二零一七年三月')[0], '2011年8月、2017年3月')
        self.assertEqual(normalize_spoken_english_years('Built in nineteen sixty three.')[0], 'Built in 1963.')
        self.assertEqual(normalize_spoken_english_years('There were twenty one people.')[0], 'There were twenty one people.')
        self.assertEqual(clauses('Pay 3.14 dollars. Do not leave.', 'en'), ['Pay 3.14 dollars.', 'Do not leave.'])
        self.assertEqual(len(clauses('如果下雨，那么我们不出发，但直到明天才决定。', 'zh')), 1)
        policy=TranslationPolicy('zh', 'en')
        self.assertEqual(policy.prepare('来顿说话').text, '来顿说话')
        prepared=policy.prepare('二零一一年完工')
        self.assertIn('calendar_year_not_preserved:2011', policy.inspect(prepared.text, 'Completed in 2001.', prepared))
        prepared=policy.prepare('这并不是告别')
        self.assertIn('negation_requires_review', policy.inspect(prepared.text, 'This is goodbye.', prepared))
        names=TranslationPolicy('en', 'zh', (('Ann', '安'),))
        self.assertEqual(names.prepare('Annual Ann announcement').text, 'Annual 安 announcement')

class StreamingBenchmarkTests(unittest.TestCase):
    def test_nearest_rank_and_resource_units(self):
        from benchmarks.streaming.run import percentiles
        from benchmarks.streaming.resources import numeric_quantiles
        self.assertEqual(percentiles(list(range(1,11)))['p90_seconds'],9)
        self.assertEqual(numeric_quantiles([1024,2048])['p90'],2048)
        self.assertNotIn('p90_seconds',numeric_quantiles([1,2]))
    def test_missing_audio_stays_in_success_denominator(self):
        from benchmarks.streaming.run import summarize
        def row(ok):
            return dict(scenario='speech_en_zh',functional_success=ok,failure=None,
                first_output_seconds={'audio_segment':4} if ok else {},onset_first_output_seconds={},
                speech_onset_method=None,output_audio_success=ok,full_audio_success=ok,
                final_snapshot={'audio_segments':[{}] if ok else [],'audio_skipped':[]})
        result=summarize([row(True),row(False)])['speech_en_zh']
        self.assertEqual(result['total'],2);self.assertEqual(result['failed'],1)
        self.assertEqual(result['output_audio_success_rate'],.5);self.assertEqual(result['no_audio_rate'],.5)
        self.assertEqual(result['first_pcm_latency']['n'],1)
