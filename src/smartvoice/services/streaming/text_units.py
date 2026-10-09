"""Bounded semantic units; source drafts stay revisable until acoustic confirmation."""
from dataclasses import replace
import re
import time
import unicodedata
from smartvoice.domain.streaming import SourceRef, TextUnit, StageError
from smartvoice.domain.stream_text import lexical,source_offsets

OPEN_END=re.compile(r'(尽管|虽然|如果|因为|但是|但|而且|以及|当|例如|包括|不|没|未|无|非|whether|although|because|if|and|but|not|never|no|don\x27t|doesn\x27t|didn\x27t|isn\x27t|aren\x27t|wasn\x27t|weren\x27t|haven\x27t|hasn\x27t|hadn\x27t|cannot|can\x27t|won\x27t|wouldn\x27t|shouldn\x27t|couldn\x27t|such as)$',re.I)
SENTENCE_BOUNDARY=re.compile(r'[。！？!?]|(?<!\d)\.(?!\d)')

def boundary_incomplete(raw,language):
    if language=='en':
        # Word boundaries matter: "candy" is not an unfinished "and".
        value=raw.strip().rstrip('.!? ')
        return bool(re.search(r'\b(?:and|but|or|because|although|if|whether|by|with|of|to|from|for|a|an|the|not|never|no|don\x27t|doesn\x27t|didn\x27t|isn\x27t|aren\x27t|wasn\x27t|weren\x27t|haven\x27t|hasn\x27t|hadn\x27t|cannot|can\x27t|won\x27t|wouldn\x27t|shouldn\x27t|couldn\x27t|provided by|such as)\s*$',value,re.I))
    value=lexical(raw)
    paired=bool((value.startswith(('虽然','尽管')) and not re.search(r'但是|但|可是|仍|还是|却',value[2:])) or (value.startswith(('如果','假如','假使')) and not re.search(r'那么|则|就|将|会',value[2:])))
    return bool(paired or OPEN_END.search(value) or re.search(r'(不是|并不是|不能|不会|没有|直到|才)$',value))

class TextCoordinator:
    def __init__(self,language,policy=None):
        self.language=language;self.policy=dict(policy);self.unit_id=0;self.revision=0
        self.pending=[];self.current=None;self.previous='';self.common='';self.agreements=0
        self.stability=[];self.stable_since=time.monotonic();self.pending_since=None;self.last_draft=0.;self.last_text=''
        self.context='';self.closed=False;self.consumed_lexical={};self.early_commit_count=0;self.alignment_conflicts=0
    def _strip_consumed_prefix(self,event):
        """Remove a previously committed partial prefix from later ASR revisions."""
        expected=self.consumed_lexical.get(event.get('utterance_id'),'')
        if not expected:return event
        text=event.get('text','');provided=lexical(text)
        if len(provided)<len(expected) and expected.startswith(provided) and event.get('type')=='source_partial':
            # Some decoders briefly retract the tail of a hypothesis. Keep the
            # last usable suffix until the new partial again covers the commit.
            return None
        if not provided.startswith(expected):
            self.alignment_conflicts+=1
            raise StageError('early_commit_alignment','ASR revised a source prefix after it had been spoken')
        seen=0;cut=0
        for index,char in enumerate(text):
            seen+=len(lexical(char))
            if seen>=len(expected):
                cut=index+1
                while cut<len(text) and (text[cut].isspace() or unicodedata.category(text[cut]).startswith('P')):cut+=1
                break
        if seen<len(expected):
            self.alignment_conflicts+=1
            raise StageError('early_commit_alignment','ASR final did not cover the already spoken source prefix')
        return {**event,'text':text[cut:],'text_start':event.get('text_start',0)+cut}
    def ingest(self,event,now=None):
        now=time.monotonic() if now is None else now
        if self.closed:raise StageError('stage_closed','Text coordinator closed')
        event=self._strip_consumed_prefix(event)
        if event is None:return
        if event['type']=='source_partial':
            text=event['text'];common=''
            for a,b in zip(text,self.previous):
                if a!=b:break
                common+=a
            if not common or not common.startswith(self.common):self.stable_since=now;self.agreements=1
            else:self.agreements+=1
            old=self.stability
            self.stability=[(old[i][0]+1,old[i][1]) if i<len(common) and i<len(old) else (1,now) for i in range(len(text))]
            self.common=common;self.previous=text;self.current=event
        elif event['type']=='source_final':
            self.pending.append(event);self.current=None;self.stability=[];self.previous=self.common='';self.agreements=0
            if self.pending_since is None:self.pending_since=now
        if len(self.raw_text())>self.policy['max_total_chars']:
            raise StageError('text_overload','Semantic buffer exceeds character budget')
    def raw_text(self):
        chunks=[e['text'] for e in self.pending]
        if self.current:chunks.append(self.current['text'])
        return ('' if self.language=='zh' else ' ').join(chunks)
    def format_input(self):
        return self.raw_text()
    def refs(self):
        rows=self.pending+([self.current] if self.current else [])
        return tuple(SourceRef(e['utterance_id'],e['revision'],e['event_sequence'],e.get('text_start',0),e.get('text_start',0)+len(e['text']),e['start_sample'],e['end_sample']) for e in rows)
    def forced_boundaries(self):
        """Raw-text offsets at decoder resets currently buffered in pending finals."""
        separator=0 if self.language=='zh' else 1
        offset=0;boundaries=[]
        for index,event in enumerate(self.pending):
            offset+=len(event['text'])
            if event.get('reason')=='forced_length':boundaries.append(offset)
            if index<len(self.pending)-1:offset+=separator
        return boundaries
    def complete_prefix(self,formatted,confirmed_chars,forced_boundaries=()):
        """Return complete, acoustically confirmed sentence prefix before an open tail.

        Forced decoder cuts are not sentence boundaries even when punctuation
        restoration inserts a period there. A later real sentence boundary may
        confirm the full phrase across that cut.
        """
        offsets=source_offsets(self.raw_text(),formatted);end=0;begin=0
        for match in SENTENCE_BOUNDARY.finditer(formatted):
            raw_end=offsets[match.end()-1]
            if raw_end>confirmed_chars:break
            # Ignore punctuation projected exactly onto a decoder reset. It may
            # be an artifact of formatting a fragment ending mid-sentence.
            if raw_end in forced_boundaries:
                begin=match.end()
                continue
            piece=formatted[begin:match.end()]
            if not boundary_incomplete(piece,self.language):end=match.end()
            begin=match.end()
        return end
    def consume_confirmed(self,count):
        separator=0 if self.language=='zh' else 1
        while self.pending and count:
            e=self.pending[0];size=len(e['text'])
            if count<size:
                self.pending[0]={**e,'text':e['text'][count:],'text_start':e.get('text_start',0)+count}
                count=0
            else:
                self.pending.pop(0);count-=size
                if self.pending and count:count=max(0,count-separator)
        if count:raise StageError('text_alignment','Cannot consume revisable source words')
        if not self.pending:self.pending_since=None
    def _refs_for_prefix(self,count):
        rows=self.pending+([self.current] if self.current else [])
        refs=[];left=count
        for index,event in enumerate(rows):
            size=len(event['text']);take=min(size,left)
            if take:
                start=event.get('text_start',0)
                refs.append(SourceRef(event['utterance_id'],event['revision'],event['event_sequence'],
                    start,start+take,event['start_sample'],event['end_sample']))
            left-=take
            if left<=0:break
            if index<len(rows)-1 and self.language=='en':left=max(0,left-1)
        if left:raise StageError('text_alignment','Early sentence prefix exceeds source coverage')
        return tuple(refs)
    def _starts_continuation(self,text):
        if self.language=='en':
            return bool(re.match(r"\s*(?:so|and|but|or|because|which|that)\b",text,re.I))
        return bool(re.match(r'\s*(?:所以|因此|但是|不过|然而|但|而且|并且|因为)',text))
    def _early_piece_incomplete(self,text):
        if boundary_incomplete(text,self.language):return True
        if self.language=='en':
            return bool(re.match(r'^\s*(?:if|unless|whether|because|although)\b',text,re.I)
                and not re.search(r'[,，]\s*\w',text))
        value=lexical(text)
        return bool((value.startswith(('虽然','尽管')) and not re.search(r'但是|但|可是|仍|还是|却',value[2:]))
            or (value.startswith(('如果','假如','假使')) and not re.search(r'那么|则|就|将|会',value[2:]))
            or (value.startswith('因为') and not re.search(r'所以|因此|于是|就|才',value[2:])))
    def early_sentence_prefix(self,formatted,now=None,*,stable_seconds=.5,
                              min_following_zh_chars=6,min_following_en_words=4,
                              min_prefix_zh_chars=8,min_prefix_en_words=4):
        """Commit a stable complete sentence inside a live partial, retaining its tail."""
        now=time.monotonic() if now is None else now
        if self.current is None or not formatted:return None
        raw=self.raw_text();pending_raw=('' if self.language=='zh' else ' ').join(e['text'] for e in self.pending)
        current_start=len(pending_raw)+(1 if pending_raw and self.language=='en' else 0)
        offsets=source_offsets(raw,formatted)
        boundary=SENTENCE_BOUNDARY
        sentence_start=0
        for match in boundary.finditer(formatted):
            end=match.end()
            if end>=len(formatted):
                sentence_start=end
                continue
            piece=formatted[sentence_start:end].strip()
            following=formatted[end:]
            following_raw=raw[offsets[end-1]:]
            # A formatter can mistake a clause break for a sentence boundary.
            # Keep cause, contrast, condition and coordination attached when
            # the lookahead begins with a discourse connector.
            if self._starts_continuation(following):
                sentence_start=end
                continue
            if self.language=='zh':
                enough_prefix=len(lexical(piece))>=min_prefix_zh_chars
                enough_following=len(lexical(following_raw))>=min_following_zh_chars
            else:
                enough_prefix=len(re.findall(r"\b[\w']+\b",piece))>=min_prefix_en_words
                enough_following=len(re.findall(r"\b[\w']+\b",following_raw))>=min_following_en_words
            cut=offsets[end-1]
            while cut<len(raw) and (raw[cut].isspace() or unicodedata.category(raw[cut]).startswith('P')):cut+=1
            current_cut=cut-current_start
            if not (enough_prefix and enough_following and cut<len(raw) and current_cut>0):
                sentence_start=end
                continue
            if self._early_piece_incomplete(piece):
                sentence_start=end
                continue
            if current_cut>len(self.stability) or any(
                    count<self.policy['stable_updates'] or now-since<stable_seconds
                    for count,since in self.stability[:current_cut]):
                sentence_start=end
                continue
            text=formatted[:end].strip()
            source_prefix=raw[:cut]
            refs=self._refs_for_prefix(cut)
            unit=TextUnit(self.unit_id,1,source_prefix,text,self.language,True,refs,
                          'early_sentence_prefix',False)
            # Keep per-utterance source offsets so later cumulative partials and
            # decoder finals contribute only text not already sent to TTS.
            cursor=cut;rows=self.pending+([self.current] if self.current else [])
            for index,event in enumerate(rows):
                take=min(len(event['text']),cursor)
                if take:
                    uid=event['utterance_id']
                    self.consumed_lexical[uid]=self.consumed_lexical.get(uid,'')+lexical(event['text'][:take])
                cursor-=take
                if cursor<=0:break
                if index<len(rows)-1 and self.language=='en':cursor=max(0,cursor-1)
            self._consume_live_prefix(cut,now)
            self.context=(self.context+text)[-self.policy['context_chars']:]
            self.unit_id+=1;self.revision=0;self.last_text='';self.early_commit_count+=1
            return unit
        return None
    def _consume_live_prefix(self,count,now):
        rows=self.pending+([self.current] if self.current else []);left=count;new_pending=[];new_current=None;current_consumed=0
        for index,event in enumerate(rows):
            size=len(event['text']);take=min(size,left)
            left-=take
            tail=event['text'][take:]
            updated={**event,'text':tail,'text_start':event.get('text_start',0)+take} if take else event
            if index<len(self.pending):
                if tail:new_pending.append(updated)
            else:
                current_consumed=take
                if tail:new_current=updated
            if left<=0:
                new_pending.extend(rows[index+1:len(self.pending)])
                if len(rows)>len(self.pending):
                    if index<len(self.pending):new_current=rows[-1]
                    else:new_current=updated if tail else None
                break
            if index<len(rows)-1 and self.language=='en':left=max(0,left-1)
        if left:raise StageError('text_alignment','Cannot consume early sentence prefix')
        self.pending=new_pending
        self.current=new_current
        self.pending_since=now if self.pending else None
        # The source partial is now represented only by its unconsumed suffix.
        self.previous=self.current['text'] if self.current else ''
        self.stability=self.stability[current_consumed:] if self.current else []
        self.common='';self.agreements=0
    def update(self,formatted,now=None,finish=False):
        now=time.monotonic() if now is None else now
        raw=self.raw_text()
        if not raw.strip():return []
        confirmed=self.current is None and bool(self.pending)
        forced=bool(self.pending and self.pending[-1].get('reason')=='forced_length')
        has_forced_boundary=any(e.get('reason')=='forced_length' for e in self.pending)
        incomplete=boundary_incomplete(raw,self.language)
        age=now-(self.pending_since if self.pending_since is not None else now)
        # A new utterance's revisable tail must not postpone the already
        # confirmed prefix forever. Apply the cap to confirmed text separately.
        pending_raw=('' if self.language=='zh' else ' ').join(e['text'] for e in self.pending)
        forced_open=forced and boundary_incomplete(pending_raw,self.language)
        # Commit confirmed complete sentences immediately, retaining a forced
        # tail across decoder resets. Previously every sentence in the reset
        # fragment inherited incomplete=True and was silently unspoken.
        if self.pending and not finish and (has_forced_boundary or self.current is not None):
            end=self.complete_prefix(formatted,len(pending_raw),self.forced_boundaries())
            if end:
                offsets=source_offsets(raw,formatted);cut=offsets[end-1]
                active=self.current;pending=self.pending;pending_since=self.pending_since
                prefix=[];left=cut
                for e in pending:
                    if left<=0:break
                    size=min(left,len(e['text']));prefix.append({**e,'text':e['text'][:size],'reason':'semantic_prefix'})
                    left-=size+(1 if self.language=='en' else 0)
                self.current=None;self.pending=prefix
                accepted=self.update(formatted[:end],now=now)
                self.pending=pending;self.current=active;self.pending_since=pending_since;self.consume_confirmed(cut)
                if self.pending:self.pending_since=now
                return accepted+self.update(formatted[end:].lstrip(),now=now)
        # A decoder fragment needs at least the next 10-second acoustic final
        # to complete a word/clause. The ordinary 4-second wait discarded it
        # before that continuation arrived. Character limits remain unchanged.
        wait=self.policy.get('forced_wait_seconds',14) if forced else self.policy['max_wait_seconds']
        if self.current is not None and self.pending and not forced_open and (age>=wait or len(pending_raw)>=self.policy['max_unit_chars']):
            offsets=source_offsets(raw,formatted)
            split=next((i for i,offset in enumerate(offsets) if offset>len(pending_raw)),len(formatted))
            active=self.current;self.current=None
            accepted=self.update(formatted[:split].strip(),now=now)
            self.current=active
            return accepted+self.update(formatted[split:].strip(),now=now,finish=finish)
        # A forced decoder window may end in the middle of a sentence. Do not
        # turn the unit-size soft cap into an incomplete commit: keep carrying
        # that tail until the next decoder fragment supplies its continuation.
        # The coordinator's separate max_total_chars hard bound still limits
        # retained text; explicit input finish may safely close as incomplete.
        must_flush=finish or (confirmed and ((age>=wait or len(raw)>=self.policy['max_unit_chars']) and not forced_open))
        commit=confirmed and ((not forced and not incomplete) or must_flush)
        if finish:commit=True
        self.revision+=1
        unit=TextUnit(self.unit_id,self.revision,raw,formatted,self.language,commit,self.refs(),
                      'finish' if finish else ('forced_incomplete' if must_flush and (forced or incomplete) else 'semantic_final' if commit else 'draft'),
                      bool(commit and (forced or incomplete)))
        if commit:
            grouped_prefix=bool(self.pending and all(e.get('reason')=='semantic_prefix' for e in self.pending))
            self.pending=[];self.current=None;self.pending_since=None
            # Split confirmed units on real sentence punctuation; don't use a
            # comma alone as proof of complete meaning. Preserve source spans.
            if grouped_prefix:
                # Translate the confirmed multi-sentence prefix as one unit;
                # its clauses retain context and share a single source scope.
                pieces=[formatted]
            else:
                pieces=[];cursor=0
                for match in SENTENCE_BOUNDARY.finditer(formatted):
                    end=match.end()
                    while end<len(formatted) and formatted[end].isspace():end+=1
                    pieces.append(formatted[cursor:end]);cursor=end
                if cursor<len(formatted):pieces.append(formatted[cursor:])
                pieces=[piece for piece in pieces if piece.strip()]
            if not pieces:pieces=[formatted]
            units=[];offsets=source_offsets(raw,formatted);cursor=0
            for piece in pieces:
                end=cursor+len(piece);a=offsets[cursor-1] if cursor else 0;b=offsets[end-1]
                refs=[];base=0
                for ref in unit.refs:
                    size=ref.text_end-ref.text_start
                    lo=max(a,base);hi=min(b,base+size)
                    if lo<hi:refs.append(replace(ref,text_start=ref.text_start+lo-base,text_end=ref.text_start+hi-base))
                    base+=size+(1 if self.language=='en' else 0)
                if refs:
                    # A final forced fragment can contain complete earlier
                    # sentences; only the actual forced tail is uncertain.
                    # A terminal forced-length marker only says that the
                    # recognizer reset its decoder window. At end of input it
                    # must not make an otherwise complete final sentence
                    # unspoken; semantic dangling-boundary checks still apply.
                    piece_incomplete=boundary_incomplete(raw[a:b],self.language) or (forced and end==len(formatted) and not finish)
                    units.append(replace(unit,unit_id=self.unit_id,raw_text=raw[a:b],text=piece.strip(),refs=tuple(refs),incomplete=piece_incomplete))
                    self.unit_id+=1
                cursor=end
            self.context=(self.context+formatted)[-self.policy['context_chars']:]
            self.revision=0;self.last_text='';return units
        if formatted==self.last_text:return []
        self.last_text=formatted
        return [unit]
    def draft_ready(self,unit,now=None,interval=None):
        now=time.monotonic() if now is None else now
        stable=bool(self.stable_prefix(now))
        enough=len(lexical(unit.text))>=self.policy['min_draft_chars']
        # Confirmed forced fragments can be previewed while waiting for continuation.
        if self.current is None:stable=True
        if stable and enough and now-self.last_draft>=(interval or self.policy['draft_interval_seconds']):
            self.last_draft=now;return True
        return False
    def stable_prefix(self,now=None):
        now=time.monotonic() if now is None else now
        length=0
        for count,since in self.stability:
            if count<self.policy['stable_updates'] or now-since<self.policy['stable_seconds']:break
            length+=1
        return self.previous[:length]
    def draft_snapshot(self,unit):
        if self.current is None:return unit
        prefix=('' if self.language=='zh' else ' ').join([e['text'] for e in self.pending]+[self.stable_prefix()])
        if len(lexical(prefix))<self.policy['min_draft_chars']:return None
        offsets=source_offsets(unit.raw_text,unit.text);end=len(prefix)
        text=''.join(c for c,offset in zip(unit.text,offsets) if offset<=end).rstrip('，。,.!?！？ ')
        refs=list(unit.refs)
        refs[-1]=replace(refs[-1],text_end=refs[-1].text_start+len(self.stable_prefix()))
        return replace(unit,raw_text=prefix,text=text,refs=tuple(refs),reason='stable_prefix_preview')

    def close(self):self.pending.clear();self.current=None;self.closed=True
