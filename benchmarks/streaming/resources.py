"""Optional complete-server-process-tree CPU/RSS sampling, outside product runtime."""
import asyncio
import os
import time
import math

def numeric_quantiles(values):
    values=sorted(values);n=len(values)
    return dict(n=n,mean=sum(values)/n if n else None,maximum=max(values) if n else None,
        **{f'p{p}':values[math.ceil(n*p/100)-1] if n else None for p in (50,90,95)})

class ResourceSampler:
    def __init__(self,pid,cores=None):
        import psutil
        self.psutil=psutil;self.root=psutil.Process(pid);self.cores=cores or os.cpu_count() or 1
        self.processes={};self.rows=[];self.stop=asyncio.Event();self.task=None
        self.child_discovery_error=None;self.process_tree_complete=True
    async def run(self):
        while not self.stop.is_set():
            cpu=rss=0.;pids=[]
            try:current=[self.root,*self.root.children(recursive=True)]
            except (self.psutil.AccessDenied, PermissionError) as exc:
                current=[self.root]
                self.process_tree_complete=False
                self.child_discovery_error=type(exc).__name__
            except self.psutil.NoSuchProcess:break
            except self.psutil.Error as exc:
                current=[self.root]
                self.process_tree_complete=False
                self.child_discovery_error=type(exc).__name__
            for item in current:
                try:
                    process=self.processes.setdefault(item.pid,item)
                    cpu+=process.cpu_percent(None);rss+=process.memory_info().rss;pids.append(item.pid)
                except self.psutil.Error:pass
            self.processes={pid:p for pid,p in self.processes.items() if pid in pids}
            self.rows.append(dict(at=time.monotonic(),cpu_core_equivalents=cpu/100,rss_bytes=int(rss),process_count=len(pids)))
            try:await asyncio.wait_for(self.stop.wait(),.1)
            except TimeoutError:pass
    def start(self):self.task=asyncio.create_task(self.run())
    async def finish(self):
        self.stop.set()
        if self.task:await self.task
        samples=self.rows[1:]
        rss_values=[row['rss_bytes'] for row in samples]
        process_values=[row['process_count'] for row in samples]
        return dict(scope=('API process plus all observed descendants; summed RSS can double-count shared pages'
                           if self.process_tree_complete else 'API root process only; descendant enumeration was blocked'),
            process_tree_complete=self.process_tree_complete,child_discovery_error=self.child_discovery_error,
            cpu_cores=self.cores,sample_interval_seconds=.1,samples=len(samples),
            cpu_core_equivalents=numeric_quantiles([r['cpu_core_equivalents'] for r in samples]),
            cpu_capacity_fraction_p90=(numeric_quantiles([r['cpu_core_equivalents']/self.cores for r in samples])['p90']),
            rss_bytes=numeric_quantiles(rss_values),rss_start_bytes=rss_values[0] if rss_values else None,
            rss_end_bytes=rss_values[-1] if rss_values else None,
            rss_delta_bytes=(rss_values[-1]-rss_values[0] if rss_values else None),
            peak_summed_rss_bytes=max((r['rss_bytes'] for r in samples),default=None),
            process_count_start=process_values[0] if process_values else None,
            process_count_end=process_values[-1] if process_values else None,
            peak_descendant_process_count=max((r['process_count'] for r in samples),default=None),
            unit_note='CPU is core equivalents, capacity fraction is 0–1, RSS is bytes')
