"""Deterministic streaming fault and sustained-session contracts.

Stages are simulated. Native-process tests in test_streaming exercise actual
owned processes; these tests do not claim model quality or latency acceptance.
"""
import asyncio
import io
import re
import unittest
import uuid
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from smartvoice.app import create_app
from smartvoice.config.settings import Settings
from smartvoice.domain.streaming import Hypothesis, SourceRef, TextUnit, StageError, TranslationResult
from smartvoice.adapters.audio.streaming import package_wav
from smartvoice.adapters.inference.runtime.stream_profile import Profiler
from smartvoice.adapters.inference.runtime.stream_workers import FairGate
from smartvoice.services.streaming.manager import StreamingManager
from smartvoice.services.streaming.speech import SpeechPipeline
from tests.test_api import FakeProvider
from tests.test_streaming import configuration, Repository, FakeWorker, FakeWorkers, FakeTTS


class SegmentASR:
    def __init__(self, language): self.language = language; self.sequence = 0
    def push_audio(self, block):
        self.sequence += 1
        text = (f'第{self.sequence}句已经说完。' if self.language == 'zh'
                else f'Sentence number {self.sequence} is complete.')
        return [Hypothesis('source_final', self.sequence, 1, text, self.language,
                           block.start_sample, block.start_sample + len(block.pcm) // 2,
                           True, 'endpoint')]
    def finish(self): return []
    def close(self): pass


class Formatter:
    def format(self, text, language, final=False): return text
    def close(self): pass


class Translator:
    def __init__(self, target): self.target = target
    def translate(self, text):
        number = re.search(r'\d+', text)[0]
        return TranslationResult(f'译文第{number}句。' if self.target == 'zh' else f'Translation number {number}.')
    def close(self): pass


class TerminalIncompleteASR:
    """Returns a semantically open final only when input is explicitly finished."""
    def __init__(self, language): self.language = language
    def push_audio(self, block): return []
    def finish(self):
        return [Hypothesis('source_final', 1, 1, '这项政策并不', self.language,
                           0, 16000, True, 'end_of_input')]
    def close(self): pass


class TerminalTailTranslator:
    def __init__(self, target): self.target = target
    def translate(self, text): return TranslationResult('This policy is not.' if self.target == 'en' else '这项政策并不。')
    def close(self): pass


class ControlledWorker(FakeWorker):
    def __init__(self, owner, name): super().__init__(owner); self.name = name
    async def construct(self, factory, deadline=None):
        if self.owner.block == (self.name, 'initialize'):
            self.owner.entered.set(); await self.owner.resume.wait()
        return await super().construct(factory, deadline)
    async def call(self, function, *args, **kwargs):
        key = (self.name, function.__name__)
        if self.owner.block == key:
            self.owner.entered.set(); await self.owner.resume.wait()
        if self.owner.fail == key: raise StageError(self.name + '_failure', 'Injected stage failure', self.name)
        return await super().call(function, *args, **kwargs)


class ControlledWorkers(FakeWorkers):
    def __init__(self, block=None, fail=None):
        super().__init__(); self.block = block; self.fail = fail
        self.entered = asyncio.Event(); self.resume = asyncio.Event()
    def create(self, name, profiler):
        worker = ControlledWorker(self, name); self.created.append(worker); return worker
    def binding(self, spec, plan):
        stage = (spec.streaming or {}).get('stage', 'tts')
        return {'asr': lambda: SegmentASR(plan.source_language), 'formatting': Formatter,
                'translation': lambda: Translator(plan.target_language), 'tts': FakeTTS}[stage]


def settings(**changes):
    return Settings(data_dir=Path('.smartvoice-dev') / ('stream-resilience-' + uuid.uuid4().hex),
                    **changes)


def manager(workers=None, **changes):
    repo = Repository(); workers = workers or ControlledWorkers()
    instance = StreamingManager(settings(**changes), repo, workers, Profiler, FairGate, package_wav)
    return instance, repo, workers


async def collect(session, events, milestones=None, playback=True):
    while True:
        event = await asyncio.wait_for(session.output.get(), 4)
        events.append(event)
        if event['type'] in ('error', 'session_complete'): return event
        session.acknowledge(event['event_sequence'])
        if event['type'] == 'audio_segment' and playback and session.plan.output_consumption == 'playback':
            session.speech.started(event['audio_sequence']); session.speech.played(event['audio_sequence'])
        if milestones and event['type'] == 'source_unit_final': await milestones.put(event)


class SustainedStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_keeps_silent_connection_alive_without_audio_cap(self):
        instance,_,_=manager(streaming_lifetime_seconds=0,streaming_idle_seconds=.06)
        self.addAsyncCleanup(instance.close)
        session=instance.open(configuration())
        events=[];consumer=asyncio.create_task(collect(session,events))
        await session.ready.wait()
        for _ in range(12):
            session.heartbeat();await asyncio.sleep(.02)
        self.assertFalse(session.done.is_set())
        await session.finish_input()
        self.assertEqual((await consumer)['status'],'no_speech')

    async def exercise(self, mode, language, count):
        instance, repo, workers = manager()
        session = instance.open(configuration(mode, language, 'playback' if mode == 'spoken_interpretation' else 'delivery'))
        events = []; milestones = asyncio.Queue()
        consumer = asyncio.create_task(collect(session, events, milestones))
        self.addAsyncCleanup(instance.close)
        await asyncio.wait_for(session.ready.wait(), 2)
        for _ in range(count):
            await session.push_audio(b'\0\0' * 16000)  # One logical second, no 1x wall-clock replay.
            await asyncio.wait_for(milestones.get(), 2)
        await session.finish_input()
        terminal = await asyncio.wait_for(consumer, 5)
        self.assertEqual(terminal['type'], 'session_complete', terminal)
        self.assertEqual(terminal['status'], 'complete', terminal)
        self.assertEqual(sum(e['type'] == 'session_complete' for e in events), 1)
        sequences = [e['event_sequence'] for e in events]
        self.assertEqual(sequences, list(range(1, len(events) + 1)))
        for kind in ('source_final', 'source_unit_final'):
            finals = [e for e in events if e['type'] == kind]
            self.assertEqual(len(finals), count)
            self.assertEqual(len({e.get('unit_id', e.get('utterance_id')) for e in finals}), count)
            self.assertTrue(all(e['language'] == language for e in finals))
        targets = [e for e in events if e['type'] == 'target_final']
        if mode != 'transcription':
            self.assertEqual(len(targets), count)
            self.assertTrue(all(e['committed'] and e['refs'] for e in targets))
            self.assertEqual([re.search(r'\d+', e['text'])[0] for e in targets], [str(i) for i in range(1, count + 1)])
        if mode == 'spoken_interpretation':
            audio = [e for e in events if e['type'] == 'audio_segment']
            self.assertEqual(len(audio), count)
            self.assertEqual([e['audio_sequence'] for e in audio], list(range(1, count + 1)))
            for event, target in zip(audio, targets, strict=True):
                self.assertEqual(event['translation_id'], target['translation_id'])
                self.assertEqual(event['text'], target['text'][event['target_text_start']:event['target_text_end']])
                with wave.open(io.BytesIO(event['audio']), 'rb') as wav: self.assertGreater(wav.getnframes(), 0)
            self.assertEqual(session.speech.last_played, count)
            self.assertFalse(session.speech.ledger)
        self.assertLessEqual(len(session.pipeline.latest), 128)
        self.assertLessEqual(len(session.pipeline.committed),128)
        self.assertLessEqual(len(session.context.seen),128)
        self.assertEqual(repo.users, 0); self.assertFalse(instance.active)
        self.assertTrue(all(w.closed for w in workers.created))

    async def test_six_routes_multiple_confirmed_segments(self):
        for mode in ('transcription', 'translated_subtitles', 'spoken_interpretation'):
            for language in ('zh', 'en'):
                with self.subTest(mode=mode, language=language): await self.exercise(mode, language, 3)

    async def test_hour_logical_input_evicts_history_and_drains_both_speech_directions(self):
        for language in ('zh', 'en'):
            with self.subTest(language=language): await self.exercise('spoken_interpretation', language, 3600)

    async def test_cancel_during_initialization_and_reopen(self):
        for stage in ('asr', 'translation', 'formatting', 'tts'):
            with self.subTest(stage=stage):
                workers = ControlledWorkers(block=(stage, 'initialize')); instance, repo, _ = manager(workers)
                session = instance.open(configuration('spoken_interpretation'))
                await asyncio.wait_for(workers.entered.wait(), 2)
                await asyncio.wait_for(session.cancel(), 2)
                events = []; terminal = await collect(session, events)
                self.assertEqual(terminal['status'], 'canceled'); self.assertFalse(session.ready.is_set())
                self.assertEqual(repo.users, 0); self.assertTrue(all(w.closed for w in workers.created))
                workers.block = None
                next_session = instance.open(configuration()); await asyncio.wait_for(next_session.ready.wait(), 2)
                await next_session.cancel(); await instance.close()

    async def test_cancel_during_each_inference_stage(self):
        for stage, operation in (('asr', 'push_audio'), ('formatting', 'format'), ('translation', 'translate'), ('tts', 'synthesize')):
            with self.subTest(stage=stage):
                workers = ControlledWorkers(block=(stage, operation)); instance, repo, _ = manager(workers)
                session = instance.open(configuration('spoken_interpretation')); events = []
                consumer = asyncio.create_task(collect(session, events))
                await asyncio.wait_for(session.ready.wait(), 2); await session.push_audio(b'\0\0' * 16000)
                await asyncio.wait_for(workers.entered.wait(), 2)
                await asyncio.wait_for(session.cancel(), 2); terminal = await asyncio.wait_for(consumer, 2)
                self.assertEqual(terminal['status'], 'canceled')
                self.assertEqual(repo.users, 0); self.assertFalse(instance.active)
                self.assertTrue(all(w.closed for w in workers.created)); await instance.close()

    async def test_stage_failures_emit_one_error_and_release(self):
        for stage, operation in (('asr', 'push_audio'), ('formatting', 'format'), ('translation', 'translate'), ('tts', 'synthesize')):
            with self.subTest(stage=stage):
                instance, repo, workers = manager(ControlledWorkers(fail=(stage, operation)))
                session = instance.open(configuration('spoken_interpretation')); events = []
                consumer = asyncio.create_task(collect(session, events))
                await asyncio.wait_for(session.ready.wait(), 2); await session.push_audio(b'\0\0' * 16000)
                await session.finish_input(); terminal = await asyncio.wait_for(consumer, 3)
                self.assertEqual(terminal['code'], stage + '_failure', terminal)
                self.assertEqual(sum(e['type'] in ('error', 'session_complete') for e in events), 1)
                self.assertEqual(repo.users, 0); self.assertFalse(instance.active)
                self.assertTrue(all(w.closed for w in workers.created)); await instance.close()

    async def test_shutdown_cancels_active_sessions_and_refuses_new_sessions(self):
        instance, repo, workers = manager(streaming_max_sessions=2)
        sessions = [instance.open(configuration()) for _ in range(2)]
        await asyncio.gather(*(s.ready.wait() for s in sessions)); await instance.close()
        self.assertTrue(all(s.done.is_set() for s in sessions)); self.assertEqual(repo.users, 0)
        self.assertTrue(all(w.closed for w in workers.created))
        with self.assertRaises(StageError): instance.open(configuration())

    async def test_input_queue_duration_and_frame_bounds(self):
        workers = ControlledWorkers(block=('asr', 'push_audio')); instance, repo, _ = manager(workers)
        session = instance.open(configuration()); await session.ready.wait()
        for payload in (b'', b'\0', b'\0' * 32002):
            with self.assertRaises(StageError): await session.push_audio(payload)
        await session.push_audio(b'\0\0'); await workers.entered.wait()
        for _ in range(50): await session.push_audio(b'\0\0')
        with self.assertRaisesRegex(StageError, 'full'): await session.push_audio(b'\0\0')
        await session.cancel(); self.assertEqual(repo.users, 0)
        session = instance.open(configuration()); await session.ready.wait()
        from dataclasses import replace as replace_settings
        session.settings=replace_settings(session.settings,streaming_max_audio_seconds=300)
        session.samples = int(16000 * session.settings.streaming_max_audio_seconds)
        with self.assertRaisesRegex(StageError, 'duration'): await session.push_audio(b'\0\0')
        await session.cancel(); await instance.close()

    async def test_event_ack_backpressure_and_timeout(self):
        instance, repo, workers = manager(); config = {**configuration(), 'ack_window': 1}
        session = instance.open(config); await session.ready.wait()
        with patch('smartvoice.services.streaming.session.time', SimpleNamespace(monotonic=lambda: 0.0)):
            pending = asyncio.create_task(session.emit({'type': 'test'}))
            await asyncio.sleep(.01); self.assertFalse(pending.done())
            ready = session.output.get_nowait(); session.acknowledge(ready['event_sequence'])
            event = await asyncio.wait_for(pending, 1)
        with patch('smartvoice.services.streaming.session.time', SimpleNamespace(monotonic=iter((0., 31.)).__next__)):
            with self.assertRaisesRegex(StageError, 'ACK'): await session.emit({'type': 'test'})
        session.acknowledge(event['event_sequence']); await session.cancel(); await instance.close()
        self.assertEqual(repo.users, 0)

    async def test_cancel_while_waiting_for_playback_completion(self):
        instance, repo, workers = manager(); session = instance.open(configuration('spoken_interpretation', consumption='playback'))
        await session.ready.wait(); ready = await session.output.get(); session.acknowledge(ready['event_sequence'])
        await session.push_audio(b'\0\0' * 16000); await session.finish_input()
        while True:
            event = await asyncio.wait_for(session.output.get(), 2); session.acknowledge(event['event_sequence'])
            if event['type'] == 'audio_segment': break
        self.assertFalse(session.done.is_set()); self.assertTrue(session.speech.ledger)
        await asyncio.wait_for(session.cancel(), 2)
        terminal = await session.output.get(); self.assertEqual(terminal['status'], 'canceled')
        self.assertEqual(repo.users, 0); self.assertTrue(all(w.closed for w in workers.created)); await instance.close()


class RevisionAndPlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_retract_revised_draft_and_suppress_late_results_after_commit(self):
        instance, repo, _ = manager(); session = instance.open(configuration('translated_subtitles'))
        await session.ready.wait(); ready = await session.output.get(); session.acknowledge(ready['event_sequence'])
        pipeline = session.pipeline
        ref = SourceRef(1, 1, 1, 0, 16, 0, 16000)
        draft = TextUnit(1, 1, 'Hello old words', 'Hello old words', 'en', False, (ref,), 'partial')
        pipeline.latest[1] = draft
        await pipeline.target_event(draft, '旧草稿')
        old = session.output.get_nowait(); session.acknowledge(old['event_sequence'])
        revised = replace(draft, revision=2, raw_text='Completely revised words', text='Completely revised words')
        await pipeline.emit_unit(revised)
        events = []
        while not session.output.empty():
            event = session.output.get_nowait(); events.append(event); session.acknowledge(event['event_sequence'])
        self.assertTrue(any(e['type'] == 'target_partial' and e.get('retracted') for e in events))
        self.assertFalse(pipeline.valid(draft))
        final = replace(revised, revision=3, committed=True)
        pipeline.latest[1] = final
        await pipeline.target_event(final, '固定终稿', final=True)
        terminal_text = session.output.get_nowait(); session.acknowledge(terminal_text['event_sequence'])
        await pipeline.target_event(revised, '迟到的草稿')
        await pipeline.target_event(final, '重复定稿', final=True)
        self.assertTrue(session.output.empty())
        self.assertEqual(terminal_text['text'], '固定终稿'); self.assertEqual(session.translation_completed, 1)
        await session.cancel(); await instance.close(); self.assertEqual(repo.users, 0)

    async def test_playback_order_and_slow_consumer_capacity(self):
        owner = SimpleNamespace(workers=ControlledWorkers(), profiler=Profiler(), stop=asyncio.Event(),
                                plan=SimpleNamespace(output_consumption='playback'))
        speech = SpeechPipeline(owner, package_wav)
        speech.ledger = {1: {'duration_seconds': 8, 'event_sequence': 1}, 2: {'duration_seconds': 4, 'event_sequence': 2}}
        with self.assertRaises(StageError): speech.started(2)
        with self.assertRaises(StageError): speech.played(1)
        speech.started(1); speech.played(1)
        with self.assertRaises(StageError): speech.played(1)
        speech.started(2); speech.played(2); self.assertFalse(speech.ledger)
        speech.ledger = {3: {'duration_seconds': 8, 'event_sequence': 3}}
        with patch('smartvoice.services.streaming.speech.time', SimpleNamespace(monotonic=iter((0., 31.)).__next__)):
            with self.assertRaisesRegex(StageError, 'consumer'): await speech.wait_capacity(10)
        speech.owner.plan.output_consumption = 'delivery'
        speech.ledger = {1: {'duration_seconds': 1, 'event_sequence': 7}}
        speech.confirm_delivery(6); self.assertTrue(speech.ledger)
        speech.confirm_delivery(7); self.assertFalse(speech.ledger)
        await speech.worker.aclose()


class WebSocketDisconnectTests(unittest.TestCase):
    def test_real_websocket_disconnect_then_reopen(self):
        config = settings(); repo = Repository(); workers = ControlledWorkers()
        app = create_app(config, FakeProvider())
        app.state.streaming = StreamingManager(config, repo, workers, Profiler, FairGate, package_wav)
        with TestClient(app) as client:
            for _ in range(3):
                with client.websocket_connect('/v1/audio/stream') as ws:
                    ws.send_json(configuration('spoken_interpretation', 'zh'))
                    self.assertEqual(ws.receive_json()['type'], 'session_ready')
                    # Deliver disconnect, then await handler cleanup before
                    # TestClient cancels its own context task scope.
                    ws.close()
                    async def wait_cleanup():
                        active = list(app.state.streaming.active.values())
                        if active: await asyncio.wait_for(asyncio.gather(*(s.done.wait() for s in active)), 2)
                    client.portal.call(wait_cleanup)
                self.assertEqual(repo.users, 0); self.assertFalse(app.state.streaming.active)
            self.assertTrue(all(w.closed for w in workers.created))


class AdditionalBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_final_queue_and_pending_tts_char_limits(self):
        instance, repo, _ = manager(); session = instance.open(configuration())
        await session.ready.wait()
        event = Hypothesis('source_final', 1, 1, 'A complete sentence.', 'en', 0, 16000, True, 'endpoint').event()
        for _ in range(4): session.pipeline.feed(event)
        with self.assertRaisesRegex(StageError, 'queue'): session.pipeline.feed(event)
        await session.cancel(); await instance.close(); self.assertEqual(repo.users, 0)
        owner = SimpleNamespace(workers=ControlledWorkers(), profiler=Profiler(), id='fixture',
                                plan=SimpleNamespace(target_language='zh', source_language='en'))
        speech = SpeechPipeline(owner, package_wav)
        unit = TextUnit(1, 1, 'A sentence.', 'A sentence.', 'en', True,
                        (SourceRef(1, 1, 1, 0, 11, 0, 16000),), 'endpoint')
        with self.assertRaisesRegex(StageError, 'budget'):
            await speech.submit(unit, dict(translation_id='t', revision=1, text='字' * 513))
        self.assertEqual(speech.pending_chars, 0); await speech.worker.aclose()

    async def test_forced_incomplete_is_skipped_but_terminal_tail_is_spoken(self):
        emitted = []
        async def emit(row): emitted.append(row); return row
        owner = SimpleNamespace(workers=ControlledWorkers(), profiler=Profiler(), emit=emit,
                                id='fixture', plan=SimpleNamespace(target_language='zh', source_language='en'))
        speech = SpeechPipeline(owner, package_wav)
        unit = TextUnit(1, 1, 'provided by', 'provided by', 'en', True,
                        (SourceRef(1, 1, 1, 0, 11, 0, 16000),), 'forced_length', incomplete=True)
        event = dict(translation_id='t', revision=1, text='由')
        await speech.submit(unit, event)
        await speech.submit(replace(unit, incomplete=False), {**event, 'quality_issues': ['negation_requires_review']})
        self.assertEqual([e['reason'] for e in emitted], ['incomplete', 'quality_issues'])
        self.assertTrue(all(e['type'] == 'audio_skipped' for e in emitted))
        self.assertTrue(speech.queue.empty()); self.assertEqual(speech.sequence, 0)
        terminal_tail = replace(unit, unit_id=2, reason='finish')
        await speech.submit(terminal_tail, {**event, 'translation_id': 't2'})
        queued = speech.queue.get_nowait()
        self.assertEqual(queued.translation_id, 't2')
        self.assertTrue(queued.incomplete)
        self.assertEqual(queued.boundary_reason, 'finish')
        await speech.worker.aclose()

    async def test_terminal_incomplete_source_tail_reaches_audio_and_completes_session(self):
        workers = ControlledWorkers(); base_binding = workers.binding
        def binding(spec, plan):
            stage = (spec.streaming or {}).get('stage', 'tts')
            if stage == 'asr': return lambda: TerminalIncompleteASR(plan.source_language)
            if stage == 'translation': return lambda: TerminalTailTranslator(plan.target_language)
            return base_binding(spec, plan)
        workers.binding = binding
        instance, repo, _ = manager(workers)
        session = instance.open(configuration('spoken_interpretation', 'zh', 'playback'))
        events = []; consumer = asyncio.create_task(collect(session, events))
        self.addAsyncCleanup(instance.close)
        await asyncio.wait_for(session.ready.wait(), 2)
        await session.push_audio(b'\0\0' * 16000)
        await session.finish_input()
        terminal = await asyncio.wait_for(consumer, 3)

        self.assertEqual(terminal['type'], 'session_complete', terminal)
        self.assertEqual(terminal['status'], 'complete', terminal)
        source = next(e for e in events if e['type'] == 'source_final')
        source_unit = next(e for e in events if e['type'] == 'source_unit_final')
        target = next(e for e in events if e['type'] == 'target_final')
        audio = next(e for e in events if e['type'] == 'audio_segment')
        self.assertEqual(source['text'], '这项政策并不')
        self.assertEqual(target['text'], 'This policy is not.')
        self.assertTrue(source_unit['incomplete'])
        self.assertEqual(source_unit['reason'], 'finish')
        self.assertTrue(audio['source_unit_incomplete'])
        self.assertEqual(audio['translation_id'], target['translation_id'])
        self.assertEqual(audio['text'], target['text'])
        with wave.open(io.BytesIO(audio['audio']), 'rb') as wav:
            self.assertGreater(wav.getnframes(), 0)
        self.assertFalse(any(e['type'] == 'audio_skipped' for e in events))
        self.assertEqual(session.speech.last_played, 1)
        self.assertEqual(repo.users, 0); self.assertFalse(instance.active)

    async def test_silence_quality_skips_and_invalid_pcm_terminal_status(self):
        class Silence(SegmentASR):
            def push_audio(self, block): return []
        class UnsafeTranslation(Translator):
            def translate(self, text): return replace(super().translate(text), quality_issues=('fixture_quality_issue',))
        class InvalidPCM(FakeTTS):
            def synthesize(self, target): return replace(super().synthesize(target), pcm=b'\0')
        for scenario, expected in (('silence', 'no_speech'), ('quality_skip', 'no_audio'), ('invalid_pcm', 'invalid_stage_output')):
            with self.subTest(scenario=scenario):
                workers = ControlledWorkers(); base_binding = workers.binding
                def binding(spec, plan):
                    stage = (spec.streaming or {}).get('stage', 'tts')
                    if scenario == 'silence' and stage == 'asr': return lambda: Silence(plan.source_language)
                    if scenario == 'quality_skip' and stage == 'translation': return lambda: UnsafeTranslation(plan.target_language)
                    if scenario == 'invalid_pcm' and stage == 'tts': return InvalidPCM
                    return base_binding(spec, plan)
                workers.binding = binding
                instance, repo, _ = manager(workers)
                session = instance.open(configuration('spoken_interpretation')); events = []
                consumer = asyncio.create_task(collect(session, events))
                await asyncio.wait_for(session.ready.wait(), 2); await session.push_audio(b'\0\0' * 16000)
                await session.finish_input(); terminal = await asyncio.wait_for(consumer, 3)
                self.assertEqual(terminal.get('status', terminal.get('code')), expected, terminal)
                self.assertFalse(any(e['type'] == 'audio_segment' for e in events))
                if scenario == 'quality_skip': self.assertTrue(any(e['type'] == 'audio_skipped' for e in events))
                self.assertEqual(repo.users, 0); self.assertFalse(instance.active)
                self.assertTrue(all(w.closed for w in workers.created)); await instance.close()

    async def test_estimated_memory_and_missing_model_reject_before_stage_creation(self):
        instance, repo, workers = manager(streaming_memory_mib=1)
        with self.assertRaisesRegex(StageError, 'budget'): instance.open(configuration())
        self.assertFalse(workers.created); self.assertEqual(repo.users, 0); await instance.close()
        from contextlib import contextmanager
        instance, repo, workers = manager()
        @contextmanager
        def unavailable(ids): raise OSError('Missing installed assets'); yield
        repo.hold_models = unavailable
        with self.assertRaises(StageError) as caught: instance.open(configuration())
        self.assertEqual(caught.exception.code, 'model_unavailable')
        self.assertFalse(workers.created); self.assertFalse(instance.active); await instance.close()

    async def test_shared_rest_thread_waits_for_stream_compute_permit(self):
        from smartvoice.adapters.inference.runtime.compute_budget import ComputeBudget
        budget = ComputeBudget(1)
        def rest_call():
            with budget.permit(timeout=2): return budget.snapshot()['active']
        async with budget.async_permit(priority=0):
            task = asyncio.create_task(asyncio.to_thread(rest_call))
            async def waiting():
                while budget.snapshot()['waiting'] != 1: await asyncio.sleep(.001)
            await asyncio.wait_for(waiting(), 1)
            self.assertFalse(task.done()); self.assertEqual(budget.snapshot()['active'], 1)
        self.assertEqual(await asyncio.wait_for(task, 1), 1)
        self.assertEqual(budget.snapshot()['active'], 0); self.assertEqual(budget.snapshot()['waiting'], 0)

    async def test_cancel_real_owned_native_work_releases_process(self):
        from smartvoice.adapters.inference.runtime.stream_workers import ProcessAffinityWorker
        from tests.test_streaming import hang_forever
        worker = ProcessAffinityWorker('cancel-test', Profiler(), grace_seconds=.05)
        task = asyncio.create_task(worker.call(hang_forever))
        try:
            async def started():
                while not worker.owned_pids(): await asyncio.sleep(.001)
            await asyncio.wait_for(started(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await asyncio.wait_for(task, 3)
            await worker.aclose(); self.assertTrue(worker.closed); self.assertFalse(worker.owned_pids())
        finally:
            if not task.done(): task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await worker.aclose()


class SemanticBoundaryTests(unittest.TestCase):
    def test_dangling_english_endpoint_merges_next_confirmed_span(self):
        from smartvoice.services.streaming.text_units import TextCoordinator, boundary_incomplete
        from smartvoice.services.streaming.planning import CONFIG
        coordinator = TextCoordinator('en', CONFIG['text_policy'])
        first = Hypothesis('source_final', 1, 1, 'Shade provided by', 'en', 0, 16000, True, 'forced_length').event()
        first['event_sequence'] = 1
        coordinator.ingest(first, now=0)
        draft = coordinator.update('Shade provided by.', now=1)
        self.assertTrue(boundary_incomplete(first['text'], 'en'))
        self.assertFalse(any(u.committed for u in draft))
        second = Hypothesis('source_final', 2, 1, 'trees', 'en', 16000, 32000, True, 'endpoint').event()
        second['event_sequence'] = 2
        coordinator.ingest(second, now=2)
        units = coordinator.update('Shade provided by trees.', now=2)
        self.assertEqual(len(units), 1); self.assertTrue(units[0].committed)
        self.assertEqual(len(units[0].refs), 2)
        self.assertEqual([(r.start_sample, r.end_sample) for r in units[0].refs], [(0, 16000), (16000, 32000)])
        coordinator.close()

    def test_chinese_conditional_and_english_word_boundaries(self):
        from smartvoice.services.streaming.text_units import boundary_incomplete
        self.assertTrue(boundary_incomplete('如果今天下雨', 'zh'))
        self.assertFalse(boundary_incomplete('如果今天下雨那么我们就不出发', 'zh'))
        self.assertTrue(boundary_incomplete('shade provided by', 'en'))
        self.assertFalse(boundary_incomplete('we eat candy', 'en'))
