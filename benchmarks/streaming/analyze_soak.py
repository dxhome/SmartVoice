"""Analyze a duplex wall-clock soak without treating partial trees as complete."""
import argparse
import json
import math
import os
from pathlib import Path


def quantiles(values):
    values=sorted(values)
    return dict(n=len(values),maximum=max(values,default=None),
        **{f'p{p}':values[math.ceil(len(values)*p/100)-1] if values else None for p in (50,90,95)})


def audio_guard(row):
    snapshot=row['final_snapshot'];segments=snapshot['audio_segments']
    finals={e['translation_id']:e for e in snapshot['final_events'] if e['type']=='target_final'}
    errors=list(row['audio_integrity_errors'])
    if not segments:errors.append('no_audio')
    if not finals:errors.append('no_target_final')
    if [e['audio_sequence'] for e in segments]!=list(range(1,len(segments)+1)):errors.append('audio_sequence')
    grouped={}
    for segment in segments:grouped.setdefault(segment['translation_id'],[]).append(segment)
    for key,target in finals.items():
        chunks=grouped.get(key,[]);end=0
        for chunk in chunks:
            if chunk['target_text_start']!=end:errors.append('noncontiguous_target_span')
            if target['text'][chunk['target_text_start']:chunk['target_text_end']]!=chunk['text']:
                errors.append('audio_text_differs_from_final')
            if chunk.get('refs')!=target.get('refs'):errors.append('audio_source_span_differs_from_final')
            end=chunk['target_text_end']
        if end!=len(target['text']):errors.append('unspoken_target_tail')
    if set(grouped)-set(finals):errors.append('audio_without_target_final')
    if snapshot['audio_skipped']:errors.append('audio_skipped')
    expected=math.ceil(row['input_duration_seconds']/.02)
    if row['input_pacing']['frame_count']!=expected:errors.append('input_not_fully_sent')
    if row.get('terminal_status')!='complete' or row.get('failure'):errors.append('session_not_complete')
    return dict(passed=not errors,errors=sorted(set(errors)),target_finals=len(finals),audio_chunks=len(segments),
        audio_duration_seconds=sum(e['duration_seconds'] for e in segments),
        scope='complete input, WAV validation, exact target-final spans and chunk order; no semantic quality claim')


def input_queue_metrics(rows):
    sampled=[row.get('input_queues') or {} for row in rows]
    return dict(current_frames=quantiles([r['current_frames'] for r in sampled if r.get('current_frames') is not None]),
        session_peak_frames=quantiles([r['peak_frames'] for r in sampled if r.get('peak_frames') is not None]),
        scope='sampled aggregate current queue and max per-session high-water; not per-frame occupancy')


def analyze(report,steady_after=120,baseline=None):
    rows=report.get('process_samples',[])
    samples=[]
    for row in rows:
        resource=row.get('process_resource_sample') or {}
        if resource.get('available'):samples.append((row['elapsed_seconds'],resource,row.get('worker_pool') or {}))
    # Discard cold startup for steady-state trend, but keep its RSS in the peak.
    stable=[r for r in samples if r[0]>=steady_after and r[1].get('active_sessions')==2
        and r[1].get('owned_workers_complete') and r[1].get('known_owned_worker_count',0)>=7]
    cpu=[];consumed_cpu=0.
    for before,after in zip(stable,stable[1:]):
        a,b=before[1],after[1];dt=after[0]-before[0]
        delta=(b['cpu_user_seconds']+b['cpu_system_seconds'])-(a['cpu_user_seconds']+a['cpu_system_seconds'])
        if dt>0 and delta>=0 and a['process_count']==b['process_count']:
            cpu.append(delta/dt);consumed_cpu+=delta
    rss=[row[1]['rss_bytes'] for row in samples]
    xs=[r[0] for r in stable];ys=[r[1]['rss_bytes'] for r in stable]
    slope=None
    if len(xs)>1:
        mx=sum(xs)/len(xs);my=sum(ys)/len(ys)
        slope=sum((x-mx)*(y-my) for x,y in zip(xs,ys))/sum((x-mx)**2 for x in xs)*3600
    route_metrics={}
    for row in report['direction_results']:
        profile=row['profiling']
        route_metrics[row['scenario']]=dict(guard=audio_guard(row),terminal_status=row.get('terminal_status'),
            first_output_seconds=row['first_output_seconds'],input_pacing=row['input_pacing'],
            peak_input_queue_frames=(row.get('terminal') or {}).get('resources',{}).get('input_queue_peak_frames'),
            input_overload_count=(row.get('terminal') or {}).get('resources',{}).get('input_overload_count'),
            stage_waits={key:profile.get(key) for key in ('input.queue_wait','asr.pool_queue_wait',
                'asr.compute_admission_wait','translation.pool_queue_wait','translation.translate.wall','tts.synthesize.wall')})
        if baseline is not None:
            old=next((r for r in baseline['direction_results'] if r['scenario']==row['scenario']),None)
            def finals(r,kind):return [e['text'] for e in r['final_snapshot']['final_events'] if e['type']==kind]
            same_input=bool(old and old['source_audio_sha256']==row['source_audio_sha256'])
            route_metrics[row['scenario']]['strict_baseline_guard']=dict(same_input=same_input,
                source_finals_match=bool(same_input and finals(old,'source_final')==finals(row,'source_final')),
                target_finals_match=bool(same_input and finals(old,'target_final')==finals(row,'target_final')))
    return dict(schema='smartvoice.stream.soak-analysis.v1',wall_seconds=report['wall_seconds'],routes=route_metrics,
        resources=dict(sample_count=len(samples),steady_samples=len(stable),steady_after_seconds=steady_after,logical_cpu_count=os.cpu_count(),
            owned_workers_complete=all(r[1].get('owned_workers_complete') for r in stable) if stable else False,
            process_tree_complete=all(r[1].get('process_tree_complete') for r in stable) if stable else False,
            cpu_core_equivalents=quantiles(cpu),rss_bytes=quantiles(rss),
            steady_rss_start_bytes=ys[0] if ys else None,steady_rss_end_bytes=ys[-1] if ys else None,
            steady_rss_slope_bytes_per_hour=slope,
            worker_rss_bytes_per_active_session=quantiles([r[1]['worker_rss_bytes_per_active_session'] for r in stable
                if r[1].get('worker_rss_bytes_per_active_session') is not None]),
            audio_seconds_per_cpu_second=(2*(xs[-1]-xs[0])/consumed_cpu if consumed_cpu and len(xs)>1 else None),
            pending_jobs_peak=max((r[2].get('pending_jobs',0) for r in samples),default=None),
            input_queue=input_queue_metrics(report.get('process_samples',[])),
            marginal_session_memory_status='not_measured; amortized model memory is not marginal session cost',
            scope='API plus all scheduler-known native workers; descendant traversal may be restricted; summed RSS includes shared pages'))


def cli():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('report',type=Path);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--steady-after',type=float,default=120);p.add_argument('--baseline',type=Path)
    args=p.parse_args();baseline=json.loads(args.baseline.read_text()) if args.baseline else None
    args.output.write_text(json.dumps(analyze(json.loads(args.report.read_text()),args.steady_after,baseline),ensure_ascii=False,indent=2))

if __name__=='__main__':cli()
