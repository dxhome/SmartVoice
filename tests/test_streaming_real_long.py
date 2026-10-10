"""Full-only real models: one hour of PCM per route, with accelerated ingestion.

No mocked stage, wall-clock patch, offline-ASR substitution, or unbounded feed.
This exercises the product session service; WebSocket/browser checks remain
separate. Repeated licensed speech is a lifecycle probe, not meeting quality.
"""
import asyncio
import hashlib
import importlib.metadata
import json
import os
import shutil
import tempfile
import time
import unittest
import uuid
from dataclasses import replace
from pathlib import Path

from smartvoice.config.settings import Settings
from smartvoice.services.model_storage import model_directory
from tests.streaming_long_audio import (
    ROOT, MANIFEST, configuration, required_models, fixture, repeated_blocks,
    input_capacity, OutputAudit,
)


@unittest.skipUnless(os.environ.get('SMARTVOICE_TEST_SUITE')=='full',
                    'One-hour real streaming inference is required in full only')
class RealStreamingHourTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        source=Settings.from_env()
        cls.work=tempfile.TemporaryDirectory(prefix='smartvoice-streaming-hour-')
        cls.addClassCleanup(cls.work.cleanup)
        home=Path(cls.work.name);models=home/'models';models.mkdir()
        # Independent state/cache, read-only use of hardlinked immutable assets.
        # The adapter rejects symlink model roots; never weaken that guard.
        cls.model_manifests={}
        for model in required_models():
            original=model_directory(source,model)
            if not (original/'smartvoice-model.json').is_file():
                raise RuntimeError('Full streaming requires installed model: '+model)
            def link_or_copy(src,dst):
                try: os.link(src,dst)
                except OSError: shutil.copy2(src,dst)
                return dst
            shutil.copytree(original,models/model,copy_function=link_or_copy)
            cls.model_manifests[model]=hashlib.sha256((original/'smartvoice-model.json').read_bytes()).hexdigest()
        cls.settings=replace(source,data_dir=home,num_threads=2,streaming_max_sessions=2,
            streaming_compute_slots=2,streaming_lifetime_seconds=0,streaming_max_audio_seconds=0,
            streaming_memory_mib=4096,streaming_max_model_workers=8,streaming_max_pending_jobs=32)
        cls.report_dir=ROOT/'.smartvoice-dev'/'full-streaming'/uuid.uuid4().hex
        cls.report_dir.mkdir(parents=True)
        print('\nFull streaming hour reports: '+str(cls.report_dir),flush=True)

    async def exercise(self,mode,language):
        from smartvoice.adapters.storage.catalog_model_repository import CatalogModelRepository
        from smartvoice.adapters.inference.streaming.factory import StageWorkers
        from smartvoice.adapters.inference.runtime.compute_budget import ComputeBudget
        from smartvoice.adapters.inference.runtime.stream_profile import Profiler
        from smartvoice.adapters.inference.runtime.stream_workers import FairGate
        from smartvoice.adapters.audio.streaming import package_wav
        from smartvoice.services.streaming.manager import StreamingManager
        repo=CatalogModelRepository(self.settings)
        compute=ComputeBudget(self.settings.streaming_compute_slots)
        workers=StageWorkers(self.settings,compute,repo)
        manager=StreamingManager(self.settings,repo,workers,Profiler,FairGate,package_wav)
        case,pcm=fixture(language);audit=OutputAudit(mode)
        session=manager.open(configuration(mode,language));began=time.monotonic()
        samples=0;peak=dict(input_frames=0,retained_units=0,context_chars=0,context_terms=0)
        owned=set();failure=None;last_progress=began;before_finish=None
        async def consume():
            while True:
                event=await asyncio.wait_for(session.output.get(),180)
                audit.event(event,time.monotonic()-began)
                if event['type'] in ('session_complete','error'):return
                session.acknowledge(event['event_sequence'])
        consumer=asyncio.create_task(consume())
        try:
            async with asyncio.timeout(3600):  # Test wall deadline, not an input/session limit.
                while not session.ready.is_set():
                    if consumer.done():consumer.result();raise AssertionError(audit.terminal)
                    await asyncio.sleep(.01)
                for block in repeated_blocks(pcm,3600):
                    if consumer.done():consumer.result();raise AssertionError(audit.terminal)
                    while not input_capacity(session):
                        if consumer.done():consumer.result();raise AssertionError(audit.terminal)
                        await asyncio.sleep(.002)
                    if session.stop.is_set():
                        await consumer;raise AssertionError(audit.terminal)
                    await session.push_audio(block);samples+=len(block)//2
                    context=session.context.snapshot()
                    peak['input_frames']=max(peak['input_frames'],session.input.qsize())
                    peak['retained_units']=max(peak['retained_units'],len(session.pipeline.latest))
                    peak['context_chars']=max(peak['context_chars'],sum(map(len,context.recent_text)))
                    peak['context_terms']=max(peak['context_terms'],len(context.terms))
                    owned.update(workers.pool.owned_pids())
                    self.assertLessEqual(len(session.pipeline.committed),128)
                    self.assertLessEqual(len(session.context.seen),128)
                    self.assertLessEqual(len(session.pipeline.coordinator.consumed_lexical),1)
                    if time.monotonic()-last_progress>30:
                        print(f'  {mode}/{language}: {samples/16000:.0f}/3600 audio seconds, {time.monotonic()-began:.1f}s wall',flush=True)
                        last_progress=time.monotonic()
                    await asyncio.sleep(0)
                # Finish admission must not race a full input window.
                while session.input.full():await asyncio.sleep(.002)
                before_finish=dict(audio_seconds_sent=samples/16000,counts=audit.counts.copy(),
                    wall_seconds=time.monotonic()-began)
                first_kind={'transcription':'source_unit_partial',
                    'translated_subtitles':'target_partial','spoken_interpretation':'audio_segment'}[mode]
                # Under accelerated input the translator may coalesce draft work
                # directly into finals. Either is useful streaming target output.
                useful_count=audit.counts.get(first_kind,0)
                if mode=='translated_subtitles':
                    useful_count+=audit.counts.get('target_final',0)
                self.assertGreater(useful_count,0,
                    'No useful streaming output before finish: '+first_kind)
                await session.finish_input();await consumer;await session.done.wait()
                self.assertEqual(samples,3600*16000)
                self.assertEqual(session.samples,samples)
                self.assertEqual(audit.terminal['type'],'session_complete',audit.terminal)
                self.assertEqual(audit.terminal['status'],'complete',audit.terminal)
                self.assertEqual(audit.last_source_end,samples)
                self.assertGreater(audit.counts.get('source_unit_final',0),100)
                if mode!='transcription':self.assertGreater(audit.counts.get('target_final',0),100)
                if mode=='spoken_interpretation':
                    self.assertGreater(audit.audio_sequence,100)
                    self.assertFalse(audit.targets)
                    self.assertFalse(session.speech.ledger)
                self.assertFalse(manager.active)
                self.assertEqual(peak['input_frames']<=2,True)
                self.assertLessEqual(peak['retained_units'],128)
                self.assertLessEqual(peak['context_chars'],512)
                self.assertLessEqual(peak['context_terms'],64)
        except BaseException as exc:
            failure=type(exc).__name__+': '+str(exc)[:2000]
            raise
        finally:
            if not consumer.done():consumer.cancel()
            await asyncio.gather(consumer,return_exceptions=True)
            await manager.close()
            alive=[]
            for pid in owned:
                try:os.kill(pid,0);alive.append(pid)
                except ProcessLookupError:pass
            report=dict(schema='smartvoice.stream.full-hour.v1',mode=mode,language=language,
                fixture_id=case['id'],fixture_sha256=case['sha256'],manifest_sha256=hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
                model_manifests=self.model_manifests,plan=session.plan.public(),
                input_audio_seconds=samples/16000,wall_seconds=time.monotonic()-began,
                effective_audio_speedup=(samples/16000)/(time.monotonic()-began),failure=failure,
                input_policy='accelerated, bounded 2-frame window; original native frontend and clocks',
                peak_state=peak,before_finish=before_finish,output=audit.summary(),pool_after_close=workers.metrics(),
                owned_worker_pids=sorted(owned),owned_workers_still_alive=alive,
                versions={name:importlib.metadata.version(name) for name in ('sherpa-onnx','ctranslate2','sentencepiece')},
                scope='Real production session/stages, one hour cyclic pinned PCM; not real-time latency SLA, natural meeting quality, browser playback or one-hour wall soak')
            (self.report_dir/(mode+'-'+language+'.json')).write_text(json.dumps(report,indent=2,ensure_ascii=False))
            if failure is None:
                self.assertFalse(alive,'Owned native workers remain after shutdown')
                self.assertFalse(repo._model_users,'Model leases remain after shutdown')
                self.assertEqual(compute.snapshot()['active'],0)
                self.assertEqual(workers.metrics()['model_workers'],0)


def make_test(mode,language):
    async def test(self):await self.exercise(mode,language)
    return test

for mode in ('transcription','translated_subtitles','spoken_interpretation'):
    for language in ('zh','en'):
        setattr(RealStreamingHourTests,'test_hour_'+mode+'_'+language,make_test(mode,language))
