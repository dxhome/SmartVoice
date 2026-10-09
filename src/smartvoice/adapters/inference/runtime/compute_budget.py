"""Shared finite-job compute admission for REST and streaming native calls."""
import asyncio
import threading
import time
from contextlib import contextmanager, asynccontextmanager
from smartvoice.domain.errors import InferenceOverloadedError

class ComputeBudget:
    def __init__(self, slots=2, max_waiting=64):
        self.slots=slots;self.max_waiting=max_waiting;self.active=0
        self.burst=0;self.condition=threading.Condition();self.waiters=[];self.serial=0
    def _register(self,priority):
        with self.condition:
            if len(self.waiters)>=self.max_waiting:raise InferenceOverloadedError('Shared compute admission is full.')
            self.serial+=1;entry=(priority,self.serial);self.waiters.append(entry);return entry
    def _take(self,entry):
        with self.condition:
            if not self.waiters:return False
            normal=[row for row in self.waiters if row[0]>0]
            selected=min(normal,key=lambda row:row[1]) if normal and self.burst>=8 else min(self.waiters)
            if self.active<self.slots and entry==selected:
                self.waiters.remove(entry);self.active+=1;self.burst=self.burst+1 if entry[0]==0 else 0;return True
            return False
    def _remove(self,entry):
        with self.condition:
            if entry in self.waiters:self.waiters.remove(entry)
            self.condition.notify_all()
    def _release(self):
        with self.condition:self.active-=1;self.condition.notify_all()
    @contextmanager
    def permit(self,priority=1,timeout=60):
        entry=self._register(priority);owned=False;end=time.monotonic()+timeout
        try:
            with self.condition:
                while not self._take(entry):
                    from smartvoice.ports.inference_context import check_execution
                    check_execution()
                    if time.monotonic()>=end:raise InferenceOverloadedError('Shared compute admission timed out.')
                    self.condition.wait(min(.05,max(0,end-time.monotonic())))
                owned=True
            yield
        finally:
            if owned:self._release()
            else:self._remove(entry)
    @asynccontextmanager
    async def async_permit(self,priority=1,timeout=30):
        entry=self._register(priority);owned=False;end=time.monotonic()+timeout
        try:
            while not self._take(entry):
                if time.monotonic()>=end:raise InferenceOverloadedError('Shared compute admission timed out.')
                await asyncio.sleep(.01)
            owned=True;yield
        finally:
            if owned:self._release()
            else:self._remove(entry)
    def snapshot(self):
        with self.condition:return {'slots':self.slots,'active':self.active,'waiting':len(self.waiters),'max_waiting':self.max_waiting}
