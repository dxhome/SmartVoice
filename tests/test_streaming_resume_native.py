"""Full-only native-stage recovery check; short pinned speech, not a soak."""
import asyncio
import json
import os
import time
import unittest
from tests import test_streaming_real_long as hour_tests
from tests.streaming_long_audio import configuration, fixture, repeated_blocks, input_capacity, OutputAudit


@unittest.skipUnless(os.environ.get('SMARTVOICE_TEST_SUITE')=='full','Native streaming recovery runs in full only')
class NativeRecoveryTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        hour_tests.RealStreamingHourTests.setUpClass.__func__(cls)

    async def test_six_native_routes_keep_asr_and_context_on_resume(self):
        from smartvoice.adapters.storage.catalog_model_repository import CatalogModelRepository
        from smartvoice.adapters.inference.streaming.factory import StageWorkers
        from smartvoice.adapters.inference.runtime.compute_budget import ComputeBudget
        from smartvoice.adapters.inference.runtime.stream_profile import Profiler
        from smartvoice.adapters.inference.runtime.stream_workers import FairGate
        from smartvoice.adapters.audio.streaming import package_wav
        from smartvoice.services.streaming.manager import StreamingManager
        for mode in ('transcription','translated_subtitles','spoken_interpretation'):
            for language in ('zh','en'):
                with self.subTest(mode=mode,language=language):
                    repo=CatalogModelRepository(self.settings);compute=ComputeBudget(2)
                    workers=StageWorkers(self.settings,compute,repo)
                    owner=StreamingManager(self.settings,repo,workers,Profiler,FairGate,package_wav)
                    config=configuration(mode,language);config['resumable']=True
                    case,pcm=fixture(language)
                    session=owner.open(config);audit=OutputAudit(mode);began=time.monotonic();failure=None;resume_count=0
                    async def consume():
                        while True:
                            event=await session.output.get()
                            # Replay may already have consumed an event still in the output queue.
                            if event['event_sequence']<=audit.sequence:continue
                            audit.event(event,time.monotonic()-began)
                            if event['type'] in ('session_complete','error'):return
                            session.acknowledge(event['event_sequence'])
                    consumer=asyncio.create_task(consume())
                    try:
                        async with asyncio.timeout(180):
                            await session.ready.wait()
                            blocks=list(repeated_blocks(pcm,case['samples']/16000))
                            for index,block in enumerate(blocks):
                                while not input_capacity(session):
                                    if consumer.done():consumer.result();raise AssertionError(audit.terminal)
                                    await asyncio.sleep(.002)
                                sequence=session.input_sequence+1;offset=session.samples
                                await session.recovery.push(sequence,offset,block)
                                if index in {len(blocks)//3,2*len(blocks)//3}:
                                    consumer.cancel();await asyncio.gather(consumer,return_exceptions=True)
                                    asr=session.asr;context=session.context.snapshot();handles=workers.metrics()['stage_handles']
                                    owner.detach(session)
                                    _,response,replay=owner.resume(dict(type='resume',protocol='smartvoice.stream.v1',
                                        session_id=session.id,resume_token=session.recovery.token,
                                        last_event_sequence=session.last_ack,last_audio_played=0))
                                    self.assertIs(session.asr,asr);self.assertEqual(session.context.snapshot(),context)
                                    self.assertEqual(workers.metrics()['stage_handles'],handles)
                                    self.assertEqual(response['context_version'],context.version)
                                    self.assertEqual(response['input_sequence'],sequence)
                                    self.assertEqual(response['accepted_samples'],offset+len(block)//2)
                                    resume_count+=1
                                    await session.recovery.push(sequence,offset,block)  # Accepted before disconnect, ACK lost.
                                    self.assertEqual(session.input_sequence,sequence)
                                    self.assertEqual(session.samples,offset+len(block)//2)
                                    for event in replay:
                                        if event['event_sequence']>audit.sequence:
                                            audit.event(event,time.monotonic()-began)
                                            session.acknowledge(event['event_sequence'])
                                    consumer=asyncio.create_task(consume())
                            while session.input.full():await asyncio.sleep(.002)
                            await session.finish_input();await consumer;await session.done.wait()
                            self.assertEqual(audit.terminal['status'],'complete',audit.terminal)
                            self.assertEqual(resume_count,2)
                            self.assertEqual(session.samples,case['samples'])
                            self.assertEqual(audit.last_source_end,case['samples'])
                            self.assertGreater(audit.counts.get('source_unit_final',0),0)
                            if mode!='transcription':self.assertGreater(audit.counts.get('target_final',0),0)
                            if mode=='spoken_interpretation':self.assertGreater(audit.audio_sequence,0)
                    except BaseException as exc:
                        failure=type(exc).__name__+': '+str(exc)[:1000];raise
                    finally:
                        if not consumer.done():consumer.cancel()
                        await asyncio.gather(consumer,return_exceptions=True)
                        pids=workers.pool.owned_pids();await owner.close();alive=[]
                        for pid in pids:
                            try:os.kill(pid,0);alive.append(pid)
                            except ProcessLookupError:pass
                        report=dict(mode=mode,language=language,failure=failure,output=audit.summary(),
                            fixture_id=case['id'],fixture_sha256=case['sha256'],model_manifests=self.model_manifests,
                            input_samples=session.samples,context_version=session.context.version,resume_count=resume_count,
                            pool_after_close=workers.metrics(),owned_workers_still_alive=alive,
                            scope='Native product stages, short pinned fixture and same-process reconnect; not browser/TCP/quality/soak acceptance')
                        (self.report_dir/('resume-'+mode+'-'+language+'.json')).write_text(json.dumps(report,indent=2,ensure_ascii=False))
                        if failure is None:
                            self.assertFalse(alive);self.assertFalse(owner.recoverable);self.assertFalse(repo._model_users)
                            self.assertEqual(compute.snapshot()['active'],0)
