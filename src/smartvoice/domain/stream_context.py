"""Bounded, session-private confirmed context; snapshots are immutable."""
from collections import deque
from dataclasses import dataclass
import re
import time

@dataclass(frozen=True)
class ContextSnapshot:
    version: int
    recent_text: tuple[str, ...]
    terms: tuple[str, ...]
    language: str

class SessionContext:
    def __init__(self, language, terms=(), sentences=3, chars=512, max_terms=64):
        self.language = language; self.version = 0
        self.recent = deque(maxlen=sentences); self.chars = chars; self.max_terms = max_terms
        self.terms = tuple(dict.fromkeys(terms))[:max_terms]
        self.entities = {}; self.seen = set(); self.seen_order=deque()

    def commit(self, unit_id, text):
        if unit_id in self.seen or not text.strip(): return
        self.seen.add(unit_id)
        self.seen_order.append(unit_id)
        if len(self.seen_order)>128: self.seen.discard(self.seen_order.popleft())
        self.recent.append(text[-self.chars:])
        while sum(map(len,self.recent))>self.chars and len(self.recent)>1: self.recent.popleft()
        self.version += 1
        # Only repeated explicit acronyms become automatic bias candidates.
        # Arbitrary guessed names never enter the bias lexicon.
        now=time.monotonic()
        for term in set(re.findall(r'\b[A-Z][A-Z0-9]{1,15}\b',text)):
            count,_=self.entities.get(term,(0,now));self.entities[term]=(count+1,now)
        self.entities={k:v for k,v in self.entities.items() if now-v[1]<300}
        self.entities=dict(sorted(self.entities.items(),key=lambda x:x[1][1],reverse=True)[:self.max_terms])

    def snapshot(self):
        now=time.monotonic()
        auto=[k for k,(count,last) in self.entities.items() if count>=2 and now-last<300]
        return ContextSnapshot(self.version,tuple(self.recent),tuple(dict.fromkeys((*self.terms,*auto)))[:self.max_terms],self.language)
