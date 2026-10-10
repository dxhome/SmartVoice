"""Bounded source formatting and revision-aware draft/final translation."""
import asyncio
import time
from smartvoice.domain.streaming import StageError
from smartvoice.domain.stream_text import lexical
from smartvoice.services.streaming.text_units import TextCoordinator

def select_draft_preview(coordinator,unit,*,mode,profile,now,interval):
    """Select the source snapshot and admission decision for a target draft."""
    eager=bool(mode=='translation' and profile.get('eager_partial_preview'))
    if eager:
        # Translation subtitles may speculate on the current formatted source
        # once it reaches the existing minimum length. Speech and finals never
        # use this path; the draft remains replaceable as ASR revises its input.
        snapshot=unit if len(lexical(unit.text))>=coordinator.policy['min_draft_chars'] else None
        ready=bool(snapshot is not None and now-coordinator.last_draft>=interval)
        if ready:coordinator.last_draft=now
        return snapshot,ready,eager
    snapshot=coordinator.draft_snapshot(unit)
    ready=bool(snapshot is not None and coordinator.draft_ready(snapshot,now=now,interval=interval))
    return snapshot,ready,eager

class IncrementalPipeline:
    def __init__(self,owner,translator,plan):
        self.owner,self.translator,self.plan=owner,translator,plan
        self.coordinator=TextCoordinator(plan.source_language,dict(plan.text_policy))
        self.draft_interval=plan.draft_interval_seconds if translator else None
        self.source_preview=plan.source_preview if translator else False
        self.worker=owner.workers.create('formatting',owner.profiler);self.formatter=None
        self.finals=asyncio.Queue(maxsize=4);self.partial=None;self.wake=asyncio.Event();self.input_finished=False
        self.mt_wake=asyncio.Event();self.draft=None;self.inflight=None;self.text_done=False
        self.latest={};self.target_revisions={};self.committed=set();self.last_format=0.;self.discarded_jobs=0;self.draft_calls=0
        # Causal provenance for the one-item coalesced draft mailbox. Kept
                    # alongside the existing three-field job tuple for compatibility.
        self.draft_context=None
        self.draft_gate_history=[]
        self.source_task=None;self.target_task=None;self.target_partial_count=0
    async def start(self):
        self.formatter=await self.worker.construct(self.owner.workers.binding(self.owner.specs['formatting'],self.plan),deadline=self.owner.initialization_seconds)
        self.source_task=asyncio.create_task(self.text_loop())
        if self.translator:self.target_task=asyncio.create_task(self.target_loop());self.owner.translation_task=self.target_task
    def check(self):
        for task in (self.source_task,self.target_task):
            if task and task.done():task.result()
    def feed(self,event):
        self.check()
        if event['type']=='source_final':
            if self.finals.full():raise StageError('text_overload','Source-final text queue exceeded bound')
            self.finals.put_nowait(event);self.partial=None
        elif event['type']=='source_partial':self.partial=event
        self.wake.set()
    async def emit_unit(self,unit):
        previous=self.latest.get(unit.unit_id)
        self.latest[unit.unit_id]=unit
        if previous and (unit.committed or not lexical(unit.raw_text).startswith(lexical(previous.raw_text))) and unit.unit_id in self.target_revisions:
            await self.target_event(unit,'',retracted=True)
        if len(self.latest)>128:
            for key in sorted(self.latest)[:-128]:
                if key in self.committed or (self.translator is None and self.latest[key].committed):self.latest.pop(key,None);self.target_revisions.pop(key,None);self.committed.discard(key)
        if unit.committed:self.owner.context.commit(unit.unit_id,unit.text)
        event=await self.owner.emit(unit.event())
        if not self.translator:return
        if unit.committed:
            # Invalidate matching draft before final enqueue; native work is not
            # interrupted/released. The result guard suppresses its late output.
            if self.draft and self.draft[0].unit_id==unit.unit_id:self.draft=None;self.discarded_jobs+=1
            began=time.monotonic()
            while self.owner.translation_input.full():
                self.check()
                if self.owner.stop.is_set():return
                if time.monotonic()-began>30:raise StageError('translation_overload','Final MT queue did not drain within deadline')
                await asyncio.sleep(.02)
            self.owner.profiler.add('translation.final_backpressure_wait',time.monotonic()-began)
            self.owner.translation_input.put_nowait((unit,time.monotonic(),event['elapsed_seconds']))
            self.owner.translation_submitted+=1
        elif self.source_preview:
            decision_now=time.monotonic()
            stable_prefix=self.coordinator.stable_prefix(decision_now)
            profile=dict(self.plan.translation_policy)
            snapshot,draft_ready,eager_preview=select_draft_preview(self.coordinator,unit,
                mode=self.plan.mode,profile=profile,now=decision_now,interval=self.draft_interval)
            ref=unit.refs[-1] if unit.refs else None
            gate_row={
                'source_preview_event_sequence':ref.event_sequence if ref else None,
                'source_preview_revision':ref.revision if ref else None,
                'source_unit_event_sequence':event.get('event_sequence'),
                'source_unit_revision':unit.revision,
                'source_unit_elapsed_seconds':event.get('elapsed_seconds'),
                'unit_lexical_chars':len(lexical(unit.text)),
                'stable_prefix_lexical_chars':len(lexical(stable_prefix)),
                'minimum_draft_chars':self.coordinator.policy['min_draft_chars'],
                'stable_seconds_required':self.coordinator.policy['stable_seconds'],
                'stable_updates_required':self.coordinator.policy['stable_updates'],
                'format_interval_seconds':self.coordinator.policy['format_interval_seconds'],
                'draft_interval_seconds':self.draft_interval,
                'snapshot_lexical_chars':len(lexical(snapshot.text)) if snapshot is not None else 0,
                'decision':'ready' if draft_ready else 'minimum_length' if eager_preview and snapshot is None else 'eager_draft_interval' if eager_preview else 'stable_prefix_or_length' if snapshot is None else 'draft_interval',
            }
            self.draft_gate_history.append(gate_row)
            self.draft_gate_history=self.draft_gate_history[-32:]
            if draft_ready:
                if self.draft:self.discarded_jobs+=1
                self.draft=(snapshot,time.monotonic(),event['elapsed_seconds'])
                ready_at=time.monotonic()
                self.draft_context={
                    'source_preview_event_sequence':ref.event_sequence if ref else None,
                    'source_preview_revision':ref.revision if ref else None,
                    'source_unit_event_sequence':event.get('event_sequence'),
                    'draft_source_unit_revision':unit.revision,
                    'source_unit_emitted_elapsed_seconds':event.get('elapsed_seconds'),
                    'draft_ready_elapsed_seconds':round(ready_at-self.owner.started,6),
                    'draft_ready_monotonic':ready_at,
                    'draft_text_chars':len(snapshot.text),
                    'draft_gate_history':list(self.draft_gate_history),
                }
        self.account();self.mt_wake.set()
    def account(self):
        rows=list(self.owner.translation_input._queue)
        chars=sum(len(j[0].text) for j in rows)
        if self.draft:chars+=len(self.draft[0].text)
        if self.inflight:chars+=len(self.inflight.text)
        self.owner.pending_translation_chars=chars
        self.owner.peak_translation_chars=max(self.owner.peak_translation_chars,chars)
        self.owner.peak_translation_segments=max(self.owner.peak_translation_segments,len(rows))
        if chars>self.owner.limits.translation_pending_chars:raise StageError('translation_overload','MT pending character budget exceeded')
    def format_input(self,raw):
        context=self.coordinator.context
        available=max(0,512-len(raw)-(1 if context and self.plan.source_language=='en' else 0))
        context=context[-available:] if available else ''
        combined=context+(' ' if context and self.plan.source_language=='en' else '')+raw
        return combined,context
    def trim_context(self,formatted,context):
        if not context:return formatted
        need=len(lexical(context));seen=0;cut=0
        for i,c in enumerate(formatted):
            if lexical(c):seen+=len(lexical(c))
            if seen==need:cut=i+1;break
        return formatted[cut:].lstrip('，。,.!?！？;；:： ')
    async def format_native(self,raw,final):
        start=time.monotonic()
        async with self.owner.translation_gate.permit(0 if final else 2):
            self.owner.profiler.add('formatting.admission_wait',time.monotonic()-start)
            if self.owner.stop.is_set():return ''
            combined,context=self.format_input(raw)
            formatted=await self.worker.call(self.formatter.format,combined,self.plan.source_language,final,deadline=self.owner.limits.translation_job_seconds)
            return self.trim_context(formatted,context)

    async def text_loop(self):
        try:
            while not self.owner.stop.is_set():
                self.wake.clear()
                event=None
                if not self.finals.empty():event=self.finals.get_nowait()
                elif self.partial is not None:event=self.partial;self.partial=None
                if event:self.coordinator.ingest(event,now=self.owner.started+event['elapsed_seconds'])
                raw=self.coordinator.format_input();now=time.monotonic()
                final=self.coordinator.current is None
                changed=bool(event);due=now-self.last_format>=self.coordinator.policy['format_interval_seconds']
                pending_due=self.coordinator.pending_since is not None and now-self.coordinator.pending_since>=self.coordinator.policy['max_wait_seconds']
                if raw and ((changed and (final or due)) or ((final or pending_due) and due) or self.input_finished):
                    self.last_format=now
                    formatted=await self.format_native(raw,final)
                    if self.owner.stop.is_set():break
                    # TTS may hold the shared text permit for several seconds.
                    # An acoustic continuation received during that wait must
                    # be ingested before deciding the old forced tail timed out.
                    if not self.finals.empty():continue
                    for unit in self.coordinator.update(formatted,finish=self.input_finished and self.finals.empty() and self.partial is None):
                        await self.emit_unit(unit)
                if self.input_finished and self.finals.empty() and self.partial is None:
                    if self.coordinator.raw_text():
                        raw=self.coordinator.raw_text();formatted=await self.format_native(raw,True)
                        for unit in self.coordinator.update(formatted,finish=True):await self.emit_unit(unit)
                    break
                if not self.finals.empty() or self.partial is not None:continue
                try:await asyncio.wait_for(self.wake.wait(),.1)
                except TimeoutError:pass
        finally:self.text_done=True;self.mt_wake.set()
    def valid(self,unit):
        latest=self.latest.get(unit.unit_id)
        if self.owner.stop.is_set() or unit.unit_id in self.committed or latest is None:return False
        if unit.committed:return latest.committed and latest.revision==unit.revision
        return not latest.committed and lexical(latest.raw_text).startswith(lexical(unit.raw_text))
    async def target_event(self,unit,text,final=False,**metrics):
        if not self.valid(unit):return
        if not text.strip() and not metrics.get('retracted'):return
        rev=self.target_revisions.get(unit.unit_id,0)+1;self.target_revisions[unit.unit_id]=rev
        e=unit.event();event={k:v for k,v in e.items() if k not in ('raw_text','reason','language','revision','text','type')}
        event.update(type='target_final' if final else 'target_partial',translation_id=f'{self.owner.id}:{unit.unit_id}:{self.plan.target_language}',
                     revision=rev,source_unit_revision=unit.revision,source_language=self.plan.source_language,
                     language=self.plan.target_language,text=text,committed=final,final=final,**metrics)
        emitted=await self.owner.emit(event)
        if final and getattr(self.owner,"speech",None):await self.owner.speech.submit(unit,emitted)
        if final:self.committed.add(unit.unit_id);self.owner.translation_completed+=1
        else:
            self.target_partial_count+=1
            if self.target_partial_count==1:self.draft_gate_history=[]
    async def target_loop(self):
        try:
            while not self.owner.stop.is_set():
                self.mt_wake.clear()
                if not self.owner.translation_input.empty():job=self.owner.translation_input.get_nowait()
                elif self.draft:job=self.draft;self.draft=None
                else:
                    if self.text_done:return
                    try:await asyncio.wait_for(self.mt_wake.wait(),.1)
                    except TimeoutError:pass
                    continue
                unit,submitted,source_elapsed=job;self.inflight=unit;self.account()
                draft_context=None
                if not unit.committed and self.draft_context is not None:
                    draft_context=self.draft_context
                    self.draft_context=None
                try:
                    async with self.owner.translation_gate.permit(0 if unit.committed else 1):
                        if not self.valid(unit):self.discarded_jobs+=1;continue
                        admitted=time.monotonic();queue_seconds=admitted-submitted
                        self.owner.profiler.add('translation.admission_wait',queue_seconds)
                        admitted=time.monotonic()
                        result=await self.owner.translation_native(self.translator.translate,unit.text,deadline=self.owner.limits.translation_job_seconds,context={'unit_id':unit.unit_id,'source_unit_revision':unit.revision,'committed':unit.committed})
                        finished=time.monotonic();self.owner.translation_seconds+=finished-admitted
                        if not unit.committed:self.draft_calls+=1
                    if not self.valid(unit):self.discarded_jobs+=1;continue
                    causal={}
                    if draft_context:
                        causal={k:v for k,v in draft_context.items() if k not in ('draft_ready_monotonic','draft_gate_history')}
                        causal.update(
                            draft_queue_wait_seconds=round(max(0,admitted-submitted),6),
                            draft_ready_to_translation_start_seconds=round(max(0,admitted-draft_context['draft_ready_monotonic']),6),
                            translation_admitted_elapsed_seconds=round(admitted-self.owner.started,6),
                            translation_finished_elapsed_seconds=round(finished-self.owner.started,6),
                            draft_gate_history=draft_context['draft_gate_history'],
                        )
                    await self.target_event(unit,result.text,final=unit.committed,queue_seconds=round(queue_seconds,6),
                        inference_seconds=round(finished-admitted,6),source_commit_to_target_ready_seconds=round(finished-self.owner.started-source_elapsed,6),
                        source_tokens=result.source_tokens,target_tokens=result.target_tokens,**causal,
                        normalizations=result.normalizations,quality_issues=result.quality_issues)
                finally:self.inflight=None;self.account()
        except Exception as exc:
            self.owner.translation_failure=exc;self.owner.stop.set();raise
    async def finish(self):
        self.input_finished=True;self.wake.set()
        while not self.source_task.done() and not self.owner.stop.is_set():self.check();await asyncio.sleep(.02)
        self.check()
        if self.target_task:
            while not self.target_task.done() and not self.owner.stop.is_set():self.check();await asyncio.sleep(.02)
            self.check()
    async def close(self):
        for t in (self.source_task,self.target_task):
            if t and not t.done():t.cancel()
        await asyncio.gather(*(t for t in (self.source_task,self.target_task) if t),return_exceptions=True)
        try:
            if self.formatter and not self.owner.teardown_force:await self.worker.call(self.formatter.close,deadline=self.owner.limits.translation_job_seconds)
        finally:
            try:await self.worker.aclose()
            finally:self.coordinator.close()
