"""Ordered committed-text synthesis, bounded playback and delivery ownership."""
import asyncio
import time
from dataclasses import replace, asdict
from smartvoice.domain.streaming import CommittedTarget, SynthesizedAudio, StageError
from smartvoice.domain.speech_chunks import chunks


class SpeechPipeline:
    def __init__(self, owner, packager):
        self.owner = owner
        self.packager = packager
        self.worker = owner.workers.create('tts', owner.profiler)
        self.adapter = None
        self.queue = asyncio.Queue(maxsize=4)
        self.pending_chars = 0
        self.sequence = 0
        self.ledger = {}
        self.changed = asyncio.Event()
        self.finished = False
        self.task = None
        self.skipped = {}
        self.total_audio_seconds = 0.
        self.last_started = 0
        self.last_played = 0

    async def start(self):
        self.adapter = await self.worker.construct(
            self.owner.workers.binding(self.owner.specs['tts'], self.owner.plan),
            deadline=self.owner.initialization_seconds)
        self.task = asyncio.create_task(self.run())

    def check(self):
        if self.task and self.task.done(): self.task.result()

    async def submit(self, unit, event):
        if unit.incomplete or event.get('quality_issues'):
            reason = 'incomplete' if unit.incomplete else 'quality_issues'
            self.skipped[reason] = self.skipped.get(reason, 0) + 1
            await self.owner.emit(dict(type='audio_skipped', unit_id=unit.unit_id,
                translation_id=event['translation_id'], reason=reason, recoverable=False))
            return
        target = CommittedTarget(self.owner.id, event['translation_id'], unit.unit_id,
            event['revision'], self.owner.plan.target_language, event['text'], unit.refs,
            source_language=self.owner.plan.source_language, source_unit_revision=unit.revision,
            boundary_reason=unit.reason)
        if self.pending_chars + len(target.text) > 512:
            raise StageError('tts_overload', 'Pending synthesis text exceeds budget')
        self.pending_chars += len(target.text)
        try:
            await asyncio.wait_for(self.queue.put(target), 30)
        except BaseException:
            self.pending_chars -= len(target.text)
            raise

    async def wait_capacity(self, seconds):
        began = time.monotonic()
        while sum(row['duration_seconds'] for row in self.ledger.values()) + seconds > 15:
            if self.owner.stop.is_set(): raise asyncio.CancelledError()
            if time.monotonic() - began > 30:
                raise StageError('consumer_timeout', 'Audio consumer did not release playback capacity')
            self.changed.clear()
            try: await asyncio.wait_for(self.changed.wait(), .1)
            except TimeoutError: pass
        self.owner.profiler.add('tts.consumer_wait', time.monotonic() - began)

    async def run(self):
        while not self.owner.stop.is_set():
            target = await self.queue.get()
            if target is None: return
            try:
                parts = list(chunks(target.text, self.owner.plan.tts_chunk_chars, target.language))
                for index, (start, end, text) in enumerate(parts):
                    # Reserve before inference; never hold compute while waiting for playback.
                    await self.wait_capacity(10)
                    queued = time.monotonic()
                    async with self.owner.translation_gate.permit(0):
                        audio = await self.worker.call(self.adapter.synthesize, replace(target, text=text),
                            deadline=self.owner.limits.translation_job_seconds)
                    if not isinstance(audio,SynthesizedAudio) or not audio.committed or audio.translation_id!=target.translation_id or audio.language!=target.language:
                        raise StageError('invalid_stage_output','Synthesis output must match the committed target')
                    if not audio.pcm or audio.channels != 1 or audio.sample_rate <= 0 or len(audio.pcm) % 2:
                        raise StageError('invalid_stage_output', 'Expected nonempty PCM16 mono output')
                    duration = len(audio.pcm) / (audio.sample_rate * 2)
                    if duration > 10: raise StageError('tts_output_limit', 'Audio chunk exceeds duration limit')
                    payload = self.packager(audio.pcm, audio.sample_rate)
                    self.sequence += 1
                    self.total_audio_seconds += duration
                    row = dict(type='audio_segment', audio_sequence=self.sequence,
                        translation_id=target.translation_id, unit_id=target.unit_id,
                        revision=target.revision, language=target.language, text=text,
                        target_text_start=start, target_text_end=end, refs=[asdict(ref) for ref in target.refs],
                        source_unit_revision=target.source_unit_revision, chunk_index=index, chunk_count=len(parts),
                        sample_rate=audio.sample_rate, channels=1, encoding='wav',
                        duration_seconds=duration, synthesis_model_id=audio.profile,
                        audio=payload, committed=True, synthesis_seconds=time.monotonic()-queued)
                    # Register before emitting so an immediate ACK cannot race admission.
                    self.ledger[self.sequence] = dict(duration_seconds=duration, event_sequence=None)
                    emitted = await self.owner.emit(row)
                    self.ledger.get(self.sequence, {})['event_sequence'] = emitted['event_sequence']
                    self.confirm_delivery(self.owner.last_ack)
            finally:
                self.pending_chars -= len(target.text)

    def confirm_delivery(self, sequence):
        if self.owner.plan.output_consumption != 'delivery': return
        for key, row in list(self.ledger.items()):
            if row['event_sequence'] is not None and row['event_sequence'] <= sequence:
                del self.ledger[key]
        self.changed.set()

    def started(self, sequence):
        if sequence != self.last_started + 1 or sequence not in self.ledger:
            raise StageError('invalid_playback', 'Audio must start in emitted order')
        self.last_started = sequence

    def played(self, sequence):
        if sequence != self.last_played + 1 or sequence > self.last_started or sequence not in self.ledger:
            raise StageError('invalid_playback', 'Audio must finish after starting and in emitted order')
        self.last_played = sequence
        del self.ledger[sequence]
        self.changed.set()

    async def finish(self):
        self.check()
        await asyncio.wait_for(self.queue.put(None), 30)
        await self.task
        began = time.monotonic()
        while self.ledger:
            if time.monotonic()-began > 30:
                raise StageError('consumer_timeout', 'Audio completion ACK timed out')
            self.changed.clear()
            try: await asyncio.wait_for(self.changed.wait(), .1)
            except TimeoutError: pass

    async def close(self):
        if self.task and not self.task.done(): self.task.cancel()
        if self.task: await asyncio.gather(self.task, return_exceptions=True)
        try:
            if self.adapter and not self.owner.teardown_force: await self.worker.call(self.adapter.close, deadline=self.owner.limits.translation_job_seconds)
        finally: await self.worker.aclose()
