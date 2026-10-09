"""Replay pinned PCM16 at 1x and measure all six product streaming routes."""
import argparse
import asyncio
import hashlib
import io
import json
import math
import platform
import time
import wave
from pathlib import Path
from importlib.metadata import version
from benchmarks.metrics import error_rate

MODES={'transcription':'transcription','translated_subtitles':'translation','spoken_interpretation':'speech'}

def percentiles(values):
    values=sorted(values);n=len(values)
    return {'n':n,**{f'p{p}_seconds':values[math.ceil(n*p/100)-1] if n else None for p in (50,90,95)}}

def normalize_snapshot(row):
    snapshot=row.get('final_snapshot',{})
    return dict(finals={kind:[e.get('text','') for e in snapshot.get('final_events',[]) if e['type']==kind]
        for kind in ('source_final','source_unit_final','target_final')},
        audio=[(e.get('text'),e.get('target_text_start'),e.get('target_text_end')) for e in snapshot.get('audio_segments',[])],
        skips=[(e.get('unit_id'),e.get('reason')) for e in snapshot.get('audio_skipped',[])])

def compare(baseline, records):
    before={(r['scenario'],r['case']):r for r in baseline['records']}
    findings=[]
    for row in records:
        key=(row['scenario'],row['case']);old=before.get(key);errors=[]
        if old is None:errors.append('missing_baseline_case')
        elif normalize_snapshot(old)!=normalize_snapshot(row):errors.append('strict_final_audio_or_skip_mismatch')
        if row.get('failure'):errors.append('session_failure')
        findings.append(dict(scenario=key[0],case=key[1],errors=errors))
    return dict(scope='only selected cases; not complete baseline coverage',passed=all(not f['errors'] for f in findings),findings=findings)

async def replay(url,case,audio_root,mode,annotation):
    import websockets
    path=audio_root/case['path']
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    if digest!=case['sha256']:raise ValueError('Pinned corpus checksum mismatch: '+case['id'])
    with wave.open(str(path),'rb') as wav:
        if (wav.getframerate(),wav.getnchannels(),wav.getsampwidth())!=(16000,1,2):raise ValueError('Expected PCM16 mono 16kHz')
        pcm=wav.readframes(wav.getnframes())
    source=case['language'];target='en' if source=='zh' else 'zh'
    scenario=MODES[mode]+'_'+source+('_'+target if mode!='transcription' else '')
    row=dict(scenario=scenario,case=case['id'],source_audio_sha256=digest,input_duration_seconds=len(pcm)/32000,
        first_output_seconds={},final_snapshot=dict(final_events=[],audio_segments=[],audio_skipped=[]),failure=None,
        terminal=None,reference=case.get('reference',''),profiling={},audio_integrity_errors=[])
    onset=None;method=None
    if annotation:
        if annotation.get('audio_sha256')!=digest:raise ValueError('Onset annotation checksum mismatch')
        onset=annotation.get('human_onset_seconds')
        method='human' if onset is not None else 'vad_provisional'
        if onset is None:onset=annotation.get('vad_onset_seconds')
    row['speech_onset_seconds']=onset;row['speech_onset_method']=method
    began=time.perf_counter();first_input=[None];ready=asyncio.Event();terminal=asyncio.Event()
    descriptor=[None];expected_sequence=[1]
    try:
        async with websockets.connect(url,max_size=1000000,open_timeout=10,close_timeout=3) as socket:
            config=dict(type='configure',protocol='smartvoice.stream.v1',mode=mode,source_language=source,
                audio=dict(encoding='pcm_s16le',sample_rate=16000,channels=1),output_consumption='delivery')
            if mode!='transcription':config['target_language']=target
            await socket.send(json.dumps(config))
            async def receive():
                async for message in socket:
                    now=time.perf_counter()
                    if isinstance(message,bytes):
                        event=descriptor[0];descriptor[0]=None
                        if event is None:raise ValueError('Audio without descriptor')
                        if len(message)!=event['audio_bytes']:row['audio_integrity_errors'].append('payload_size')
                        if event['audio_sequence']!=expected_sequence[0]:row['audio_integrity_errors'].append('sequence')
                        expected_sequence[0]+=1
                        with wave.open(io.BytesIO(message),'rb') as wav:
                            if wav.getnframes()<=0 or wav.getnchannels()!=1 or wav.getsampwidth()!=2:row['audio_integrity_errors'].append('wav_format')
                        if first_input[0] is not None:row['first_output_seconds'].setdefault('audio_segment',now-first_input[0])
                        row['final_snapshot']['audio_segments'].append(event)
                        await socket.send(json.dumps(dict(type='ack',event_sequence=event['event_sequence'])))
                        continue
                    event=json.loads(message);kind=event['type']
                    if kind=='session_ready':row['session_ready_seconds']=now-began;row['plan']=event['plan'];ready.set()
                    if kind=='audio_segment':
                        if descriptor[0] is not None:raise ValueError('Missing audio binary')
                        descriptor[0]=event;continue
                    if first_input[0] is not None and event.get('text','').strip() and not event.get('retracted'):
                        row['first_output_seconds'].setdefault(kind,now-first_input[0])
                    if kind in ('source_final','source_unit_final','target_final'):row['final_snapshot']['final_events'].append(event)
                    if kind=='audio_skipped':row['final_snapshot']['audio_skipped'].append(event)
                    if kind in ('error','session_complete'):
                        row['terminal']=event;row['profiling']=event.get('profiling',{})
                        if kind=='error':row['failure']=event['code']
                        if descriptor[0] is not None:row['audio_integrity_errors'].append('missing_payload')
                        terminal.set();ready.set();return
                    await socket.send(json.dumps(dict(type='ack',event_sequence=event['event_sequence'])))
            reader=asyncio.create_task(receive())
            try:
                await asyncio.wait_for(ready.wait(),150)
                if not terminal.is_set():
                    pacing=time.perf_counter()
                    for offset in range(0,len(pcm),640):
                        if reader.done():reader.result();break
                        if terminal.is_set():break
                        if first_input[0] is None:first_input[0]=time.perf_counter()
                        await socket.send(pcm[offset:offset+640])
                        delay=pacing+min(len(pcm),offset+640)/32000-time.perf_counter()
                        if delay>0:await asyncio.sleep(delay)
                    if not terminal.is_set():await socket.send(json.dumps(dict(type='finish')))
                await asyncio.wait_for(reader,90)
            finally:
                if not reader.done():reader.cancel()
                await asyncio.gather(reader,return_exceptions=True)
    except Exception as exc:row['failure']=type(exc).__name__
    if row['terminal'] is None and row['failure'] is None:row['failure']='missing_terminal'
    row['output_audio_success']=bool(row['final_snapshot']['audio_segments']) and not row['failure'] and not row['audio_integrity_errors']
    row['full_audio_success']=row['output_audio_success'] and not row['final_snapshot']['audio_skipped']
    required_kind='source_unit_final' if mode=='transcription' else 'target_final'
    row['functional_success']=not row['failure'] and (row['output_audio_success'] if mode=='spoken_interpretation' else any(e['type']==required_kind and e.get('text','').strip() for e in row['final_snapshot']['final_events']))
    row['onset_first_output_seconds']={k:v-onset for k,v in row['first_output_seconds'].items() if v>=onset} if onset is not None else {}
    row['invalid_onset_metrics']=[k for k,v in row['first_output_seconds'].items() if onset is not None and v<onset]
    hypothesis=(' ' if source=='en' else '').join(e['text'] for e in row['final_snapshot']['final_events'] if e['type']=='source_final')
    row['asr_error_rate']=error_rate([row['reference']],[hypothesis],'latin_wer' if source=='en' else 'han_cer')
    return row

def summarize(records):
    result={}
    for scenario in sorted({r['scenario'] for r in records}):
        rows=[r for r in records if r['scenario']==scenario];speech=scenario.startswith('speech_')
        first_kind='audio_segment' if speech else 'target_partial' if scenario.startswith('translation_') else 'source_unit_partial'
        usable=[r for r in rows if r['functional_success']]
        language=scenario.split('_')[-1 if scenario.startswith('transcription_') else -2]
        profile='latin_wer' if language=='en' else 'han_cer'
        hypotheses=[(' ' if language=='en' else '').join(e['text'] for e in r['final_snapshot'].get('final_events',[]) if e['type']=='source_final') for r in rows]
        result[scenario]=dict(total=len(rows),failed=sum(not r['functional_success'] for r in rows),protocol_failures=sum(bool(r['failure']) for r in rows),
            first_pcm_latency=percentiles([r['first_output_seconds'][first_kind] for r in usable if first_kind in r['first_output_seconds']]),
            onset_latency=percentiles([r['onset_first_output_seconds'][first_kind] for r in usable if first_kind in r['onset_first_output_seconds']]),
            latency_missing_count=sum(first_kind not in r['first_output_seconds'] for r in rows),
            onset_ground_truth=all(r['speech_onset_method']=='human' for r in rows),
            output_audio_success_rate=sum(r['output_audio_success'] for r in rows)/len(rows) if speech else None,
            full_audio_success_rate=sum(r['full_audio_success'] for r in rows)/len(rows) if speech else None,
            no_audio_rate=sum(not r['final_snapshot']['audio_segments'] for r in rows)/len(rows) if speech else None,
            skip_reasons={reason:sum(e.get('reason')==reason for r in rows for e in r['final_snapshot']['audio_skipped'])
                for reason in {e.get('reason') for r in rows for e in r['final_snapshot']['audio_skipped']}},
            source_quality=dict(metric='WER' if language=='en' else 'CER',normalization_profile=profile,
                value=error_rate([r.get('reference','') for r in rows],hypotheses,profile),n=len(rows),
                scope='all selected sessions including failed/no-output sessions'),
            resource_scope='See report resource_metrics if enabled; requested concurrency is not accepted capacity')
    return result

async def main_async(args):
    manifest=json.loads(args.manifest.read_text());annotations={}
    if args.annotations:annotations={r['case']:r for r in json.loads(args.annotations.read_text())['cases']}
    selected=[c for c in manifest['cases'] if c.get('variant')=='clean']
    if args.cases:selected=[c for c in selected if c['id'] in args.cases.split(',')]
    if not selected:raise ValueError('No selected clean samples')
    records=[];resource_sampler=None
    if args.server_pid:
        from .resources import ResourceSampler
        resource_sampler=ResourceSampler(args.server_pid,args.cpu_cores);resource_sampler.start()
    try:
        for round_index in range(args.rounds):
            for mode in MODES:
                for offset in range(0,len(selected),args.concurrency):
                    batch=selected[offset:offset+args.concurrency]
                    results=await asyncio.gather(*(replay(args.url,case,args.audio_root,mode,annotations.get(case['id'])) for case in batch))
                    for row in results:
                        row['round']=round_index+1;records.append(row)
                        print(row['scenario'],row['case'],row['failure'] or row['terminal']['status'],flush=True)
    finally:
        resource_metrics=await resource_sampler.finish() if resource_sampler else None
    report=dict(schema='smartvoice.stream.benchmark.v1',clock='client perf_counter; PCM send / JSON event receipt / complete WAV receipt',
        normalization_revision='product-v1 NFKC casefold punctuation deletion; historical sandbox punctuation-to-space scores are not comparable',
        percentile_method='nearest rank',measurement_scope='loopback delivery, not browser paint or physical audibility',
        corpus_sha256=hashlib.sha256(args.manifest.read_bytes()).hexdigest(),corpus_revision=manifest.get('revision'),
        catalog_sha256=hashlib.sha256(Path('catalog/models.json').read_bytes()).hexdigest(),
        code_sha256={str(path):hashlib.sha256(path.read_bytes()).hexdigest() for root in ('src/smartvoice','benchmarks/streaming') for path in sorted(Path(root).rglob('*.py'))},
        configuration_sha256=hashlib.sha256(Path('catalog/streaming.json').read_bytes()).hexdigest(),
        environment=dict(platform=platform.platform(),python=platform.python_version(),
            versions={name:version(name) for name in ('sherpa-onnx','ctranslate2','sentencepiece','websockets')}),
        records=records,summary=summarize(records),resource_metrics=resource_metrics,
        requested_concurrency=args.concurrency,
        requested_sessions_per_cpu_core=args.concurrency/args.cpu_cores if args.cpu_cores else None,
        passing_concurrency_capacity=None,capacity_status='not accepted: requires repeated quality/latency/success-rate confirmation')
    if args.baseline:report['strict_quality_guard']=compare(json.loads(args.baseline.read_text()),records)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as stream:json.dump(report,stream,indent=2,ensure_ascii=False)
    print(args.output,flush=True)

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--url',default='ws://127.0.0.1:8766/v1/audio/stream')
    parser.add_argument('--manifest',type=Path,required=True);parser.add_argument('--audio-root',type=Path,required=True)
    parser.add_argument('--annotations',type=Path);parser.add_argument('--baseline',type=Path)
    parser.add_argument('--server-pid',type=int);parser.add_argument('--cpu-cores',type=int)
    parser.add_argument('--concurrency',type=int,choices=range(1,5),default=1)
    parser.add_argument('--cases');parser.add_argument('--rounds',type=int,choices=range(1,4),default=1)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    if args.cpu_cores is not None and args.cpu_cores<=0:parser.error('cpu-cores must be positive')
    if args.output.exists():parser.error('Output exists; preserve previous runs with a fresh report path')
    asyncio.run(main_async(args))
if __name__=='__main__':main()
