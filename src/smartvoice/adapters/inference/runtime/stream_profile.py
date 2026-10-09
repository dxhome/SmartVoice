import math
"""Bounded sampled wall spans; aggregate counts include every invocation."""
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
import threading
import time
RECORDER=ContextVar('stream_recorder',default=None)
CONTEXT=ContextVar('stream_span_context',default=None)

class Profiler:
    def __init__(self,capacity=2048):
        self.lock=threading.Lock();self.rows={};self.capacity=capacity;self.trace=deque(maxlen=512);self.started=time.monotonic()
    def add(self,name,seconds):
        with self.lock:
            row=self.rows.setdefault(name,{'count':0,'total':0.,'max':0.,'recent':deque(maxlen=self.capacity)})
            row['count']+=1;row['total']+=seconds;row['max']=max(row['max'],seconds);row['recent'].append(seconds)
            if row['count']<=4 or row['count']%32==0:
                self.trace.append({'name':name,'end_seconds':time.monotonic()-self.started,'duration_seconds':seconds,'context':CONTEXT.get(),'thread':threading.current_thread().name})
    def sampled_trace(self):
        with self.lock:return list(self.trace)
    def export(self):
        with self.lock:
            return {'origin':self.started,'rows':{k:{'count':r['count'],'total':r['total'],'max':r['max'],'recent':list(r['recent'])} for k,r in self.rows.items()},'trace':list(self.trace)}
    def merge(self,data):
        """Merge finite child job samples; keep parent distributions/traces bounded."""
        with self.lock:
            for name,value in data['rows'].items():
                r=self.rows.setdefault(name,{'count':0,'total':0.,'max':0.,'recent':deque(maxlen=self.capacity)});before=r['count']
                r['count']+=value['count'];r['total']+=value['total'];r['max']=max(r['max'],value['max']);r['recent'].extend(value['recent'])
                if r['count']<=4 or before//32!=r['count']//32:
                    last=next((t for t in reversed(data['trace']) if t['name']==name),None)
                    if last:self.trace.append({**last,'end_seconds':last['end_seconds']+data['origin']-self.started})
    def summary(self):
        with self.lock:
            result={}
            for name,r in self.rows.items():
                samples=sorted(r['recent']);n=len(samples)
                result[name]={'count':r['count'],'total_seconds':r['total'],'max_seconds':r['max'],
                    'sample_count':n,'p50_seconds':samples[max(0,math.ceil(n*.5)-1)],'p90_seconds':samples[max(0,math.ceil(n*.9)-1)],'p95_seconds':samples[max(0,math.ceil(n*.95)-1)],
                    'percentile_scope':'last bounded samples; aggregate count/total/max include all calls'}
            return result

@contextmanager
def span(name):
    recorder=RECORDER.get()
    if recorder is None:yield;return
    start=time.monotonic()
    try:yield
    finally:recorder.add(name,time.monotonic()-start)
