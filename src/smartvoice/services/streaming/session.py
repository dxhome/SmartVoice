"""Transport-independent bounded streaming session lifecycle."""
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from smartvoice.domain.streaming import AudioBlock, StageError
from .workflows import WORKFLOWS
from smartvoice.domain.stream_context import SessionContext

logger=logging.getLogger(__name__)

@dataclass(frozen=True)
class Limits:
    translation_pending_chars: int = 4096
    translation_job_seconds: float = 15.

class StreamSession:
    def __init__(self, plan, specs, settings, workers, profiler, gate, packager, release, ack_window=16):
        self.plan, self.specs, self.workers = plan, specs, workers
        self.profiler, self.translation_gate = profiler, gate
        self.limits = Limits(translation_job_seconds=settings.streaming_native_seconds)
        self.initialization_seconds = settings.streaming_initialization_seconds
        self.workflow = WORKFLOWS[plan.mode]
        self.workflow.validate(specs)
        self.context = SessionContext(plan.source_language,plan.context_terms)
        self.asr_context_version = -1
        self.id = uuid.uuid4().hex
        self.started = time.monotonic()
        self.last_activity = self.started
        self.ready = asyncio.Event(); self.stop = asyncio.Event(); self.done = asyncio.Event()
        self.input = asyncio.Queue(maxsize=50); self.output = asyncio.Queue(maxsize=32)
        self.translation_input = asyncio.Queue(maxsize=4)
        self.translation_submitted = self.translation_completed = 0
        self.pending_translation_chars = self.peak_translation_chars = self.peak_translation_segments = 0
        self.translation_seconds = 0.; self.translation_failure = None
        self.sequence = self.last_ack = self.input_sequence = self.samples = 0
        self.ack_window = ack_window; self.ack_changed = asyncio.Event()
        self.input_finished = False; self.terminal = False
        self.run_started = asyncio.Event(); self.protocol_failure = None; self.cancel_requested = False; self.tearing_down = False
        self.cancel_reason = None; self.first_input = None; self.first = {}; self.counts = {}
        self.settings = settings; self.release = release
        self.asr_worker = workers.create('asr', profiler); self.asr = None
        self.mt_worker = workers.create('translation', profiler) if plan.translation_model_id else None
        self.translator = None; self.pipeline = None
        self.speech = self.workflow.speech_pipeline(self, packager)
        self.task = asyncio.create_task(self.run())

    async def emit(self, row, terminal=False):
        if self.terminal: return row
        if not terminal:
            began = time.monotonic()
            while self.sequence-self.last_ack >= self.ack_window:
                if self.stop.is_set(): raise asyncio.CancelledError()
                if time.monotonic()-began > 30: raise StageError('consumer_timeout','Event ACK timed out')
                self.ack_changed.clear()
                try: await asyncio.wait_for(self.ack_changed.wait(), .1)
                except TimeoutError: pass
        now = time.monotonic(); self.sequence += 1
        event = dict(row, protocol='smartvoice.stream.v1', session_id=self.id,
            event_sequence=self.sequence, elapsed_seconds=round(now-self.started,6))
        kind = row['type']; self.counts[kind] = self.counts.get(kind,0)+1
        if self.first_input is not None and row.get('text','').strip() and not row.get('retracted'):
            self.first.setdefault(kind, round(now-self.first_input,6))
        if kind == 'audio_segment' and self.first_input is not None:
            self.first.setdefault(kind,round(now-self.first_input,6))
        if terminal: self.terminal = True
        try: await asyncio.wait_for(self.output.put(event), 5 if terminal else 30)
        except TimeoutError: raise StageError('consumer_timeout', 'Output queue did not drain')
        return event

    def acknowledge(self, sequence):
        if type(sequence) is not int or not self.last_ack <= sequence <= self.sequence:
            raise StageError('invalid_ack', 'ACK must reference emitted events in increasing order')
        self.last_ack = sequence; self.ack_changed.set()
        if self.speech: self.speech.confirm_delivery(sequence)

    async def push_audio(self, pcm):
        if not self.ready.is_set() or self.input_finished or self.stop.is_set():
            raise StageError('invalid_state','Audio requires a ready, open session')
        if not pcm or len(pcm)%2 or len(pcm)>32000:
            raise StageError('invalid_audio','PCM frames must contain 1–16000 complete samples')
        if self.settings.streaming_max_audio_seconds and self.samples+len(pcm)//2 > int(16000*self.settings.streaming_max_audio_seconds):
            raise StageError('input_limit','Audio duration limit exceeded')
        if self.input.full(): raise StageError('input_overload','Input queue is full; pace audio in real time')
        now = time.monotonic()
        self.last_activity = now
        if self.first_input is None: self.first_input = now
        self.input_sequence += 1
        self.input.put_nowait(AudioBlock(self.input_sequence,pcm,self.samples,now))
        self.samples += len(pcm)//2

    def heartbeat(self):
        self.last_activity = time.monotonic()

    async def finish_input(self):
        if not self.ready.is_set() or self.input_finished or self.stop.is_set():
            raise StageError('invalid_state','Finish requires an open session')
        self.input_finished = True
        # Never block receive/ACK behind a full input queue.
        if self.input.full(): raise StageError('input_overload','Finish cannot enter a full input queue')
        self.input.put_nowait(None)

    async def translation_native(self, function, *args, **kwargs):
        return await self.mt_worker.call(function,*args,**kwargs)

    async def hypotheses(self, rows):
        for hypothesis in rows:
            event = await self.emit(hypothesis.event())
            self.pipeline.feed(event)

    async def process(self):
        self.asr = await self.asr_worker.construct(self.workers.binding(self.specs['asr'],self.plan), deadline=self.initialization_seconds)
        if self.mt_worker:
            self.translator = await self.mt_worker.construct(self.workers.binding(self.specs['translation'],self.plan),deadline=self.initialization_seconds)
        self.pipeline = self.workflow.text_pipeline(self,self.translator,self.plan)
        await self.pipeline.start()
        if self.speech: await self.speech.start()
        self.profiler.add('session.initialize',time.monotonic()-self.started)
        self.last_frame=time.monotonic()
        self.ready.set()
        await self.emit(dict(type='session_ready', plan=self.plan.public(), input_queue_frames=50, ack_window=self.ack_window,
            heartbeat_interval_seconds=max(.01,self.settings.streaming_idle_seconds/3),
            context_capabilities=getattr(self.asr,'context_support',{})))
        while not self.stop.is_set():
            self.pipeline.check()
            if self.speech: self.speech.check()
            try: block=await asyncio.wait_for(self.input.get(),min(.1,self.settings.streaming_idle_seconds))
            except TimeoutError:
                if time.monotonic()-self.last_activity > self.settings.streaming_idle_seconds:
                    raise StageError('input_timeout','No audio or heartbeat received within connection idle deadline')
                continue
            if block is None: break
            self.last_frame=time.monotonic()
            self.profiler.add('input.queue_wait',time.monotonic()-block.received_at)
            snapshot=self.context.snapshot()
            if hasattr(self.asr,'context_support') and snapshot.version!=self.asr_context_version:
                await self.asr_worker.call(self.asr.update_context,snapshot,deadline=self.limits.translation_job_seconds)
                self.asr_context_version=snapshot.version
            await self.hypotheses(await self.asr_worker.call(self.asr.push_audio,block,deadline=self.limits.translation_job_seconds))
        await self.hypotheses(await self.asr_worker.call(self.asr.finish,deadline=self.limits.translation_job_seconds))
        await self.pipeline.finish()
        if self.speech: await self.speech.finish()

    async def run(self):
        self.run_started.set()
        failure=None;cleanup_errors=[];safe_to_release=True
        try:
            async with asyncio.timeout(self.settings.streaming_lifetime_seconds or None): await self.process()
        except asyncio.CancelledError:
            self.cancel_reason=self.cancel_reason or 'canceled'
        except TimeoutError: failure=StageError('session_timeout','Session lifetime exceeded')
        except StageError as exc:
            failure=exc
            logger.warning('Streaming session %s failed at %s (%s): %s',self.id,exc.stage or 'session',exc.code,exc.message)
        except Exception as exc:
            failure=StageError('stage_failure','Streaming stage failed')
            logger.exception('Unexpected streaming session %s failure',self.id)
        finally:
            self.tearing_down=True
            self.teardown_force=bool(failure or self.cancel_reason or self.protocol_failure)
            self.stop.set();self.ack_changed.set()
            # Release only this session's stage handles; shared native work settles before release.
            for component in (self.pipeline,self.speech):
                if component:
                    try: await component.close()
                    except Exception as exc:
                        cleanup_errors.append(type(exc).__name__)
                        safe_to_release=safe_to_release and component.worker.closed
            for worker,stage in ((self.mt_worker,self.translator),(self.asr_worker,self.asr)):
                if worker:
                    try:
                        if stage and not self.teardown_force: await worker.call(stage.close,deadline=self.limits.translation_job_seconds)
                    except Exception as exc: cleanup_errors.append(type(exc).__name__)
                    finally:
                        try: await worker.aclose()
                        except Exception as exc:
                            cleanup_errors.append(type(exc).__name__)
                            safe_to_release=False
            if safe_to_release:self.release()
            try:
                failure=self.protocol_failure or failure
                if cleanup_errors: failure=StageError('cleanup_failure','Stage resource cleanup failed')
                if failure:
                    await self.emit(dict(type='error',code=failure.code,message=failure.message,
                        stage=failure.stage or 'session',retryable=failure.code.endswith(('timeout','overload')),
                        profiling=self.profiler.summary(), first_output_seconds=self.first.copy(),
                        counts=self.counts.copy(),resources=self.resource_snapshot()),terminal=True)
                else:
                    status=('canceled' if self.cancel_reason else 'no_speech' if not self.counts.get('source_unit_final') else
                        'no_audio' if self.speech and not self.speech.sequence else
                        'partial' if self.speech and self.speech.skipped else 'complete')
                    await self.emit(dict(type='session_complete',status=status,reason=self.cancel_reason,
                        counts=self.counts.copy(),first_output_seconds=self.first.copy(),
                        audio_skips=self.speech.skipped.copy() if self.speech else {},
                        profiling=self.profiler.summary(),resources=self.resource_snapshot(),
                        metrics_clock='server monotonic; first received PCM; not browser/onset TTFO'),terminal=True)
            finally: self.done.set()

    def resource_snapshot(self):
        snapshot=self.context.snapshot()
        return dict(scope='bounded logical session state; model pool is global, not RSS',
            input_frames=self.input.qsize(),recent_context_units=len(snapshot.recent_text),
            recent_context_chars=sum(map(len,snapshot.recent_text)),context_terms=len(snapshot.terms),
            context_version=snapshot.version,
            retained_text_units=len(self.pipeline.latest) if self.pipeline else 0,
            model_pool=self.workers.metrics() if hasattr(self.workers,'metrics') else {})

    async def cancel(self, reason='canceled'):
        if self.done.is_set(): return
        self.cancel_reason=reason;self.stop.set();self.ack_changed.set()
        if self.cancel_requested or self.tearing_down:
            await asyncio.shield(self.task);return
        self.cancel_requested=True
        await self.run_started.wait()
        if not self.task.done(): self.task.cancel()
        await asyncio.shield(self.task)
