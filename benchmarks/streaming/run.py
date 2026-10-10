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
from urllib.request import urlopen
from urllib.parse import urlsplit, urlunsplit
from pathlib import Path
from importlib.metadata import version
from benchmarks.metrics import error_rate
from .report_paths import portable_location, validate_report_paths

MODES={'transcription':'transcription','translated_subtitles':'translation','spoken_interpretation':'speech'}

def percentiles(values):
    values=sorted(values);n=len(values)
    return {'n':n,**{f'p{p}_seconds':values[math.ceil(n*p/100)-1] if n else None for p in (50,90,95)}}

def normalize_snapshot(row):
    snapshot=row.get('final_snapshot',{})
    def normalize_audio(event):
        refs=[{key:value for key,value in ref.items() if key!='event_sequence'}
            for ref in event.get('refs',[])]
        translation_id=event.get('translation_id','')
        if ':' in translation_id:translation_id=translation_id.split(':',1)[1]
        return (event.get('audio_sequence'),translation_id,event.get('unit_id'),event.get('text'),
            event.get('target_text_start'),event.get('target_text_end'),event.get('chunk_index'),
            event.get('chunk_count'),refs)
    return dict(finals={kind:[e.get('text','') for e in snapshot.get('final_events',[]) if e['type']==kind]
        for kind in ('source_final','source_unit_final','target_final')},
        audio=[normalize_audio(e) for e in snapshot.get('audio_segments',[])],
        skips=[(e.get('unit_id'),e.get('reason')) for e in snapshot.get('audio_skipped',[])])

def compare(baseline, records):
    before={(r['scenario'],r['case']):r for r in baseline['records']}
    findings=[]
    for row in records:
        key=(row['scenario'],row['case']);old=before.get(key);errors=[]
        if old is None:errors.append('missing_baseline_case')
        elif normalize_snapshot(old)!=normalize_snapshot(row):errors.append('strict_final_audio_or_skip_mismatch')
        if row.get('failure'):errors.append('session_failure')
        if old is not None and (row.get('terminal_type')!=old.get('terminal_type') or row.get('terminal_status')!=old.get('terminal_status')):
            errors.append('terminal_outcome_mismatch')
        if not row.get('functional_success'):errors.append('functional_failure')
        if row.get('audio_integrity_errors'):errors.append('audio_integrity_failure')
        findings.append(dict(scenario=key[0],case=key[1],errors=errors))
    return dict(scope='only selected cases; not complete baseline coverage',passed=all(not f['errors'] for f in findings),findings=findings)

def production_audio_gate(rows, acceptance):
    """Evaluate the 300-session numerical floor without mislabeling replay as telemetry."""
    policy=acceptance['speech_audio_success']
    eligible=[r for r in rows if not r.get('expect_no_speech')]
    total=len(eligible);successes=sum(bool(r.get('full_audio_success')) for r in eligible)
    rate=successes/total if total else None
    minimum=policy['production_minimum_sessions_per_direction']
    floor=policy['production_minimum_rate']
    required=math.ceil(minimum*floor)
    if total<minimum:status='insufficient_samples'
    else:status='pass' if rate>=floor else 'fail'
    return dict(metric='full_audio_success_rate',sample_scope='local_fixed_corpus_replay',
        production_telemetry_status='not_available_from_local_benchmark',eligible_sessions=total,
        full_audio_success_sessions=successes,full_audio_success_rate=rate,
        minimum_sessions=minimum,minimum_successes_at_minimum_sample=required,
        required_rate=floor,status=status,
        denominator_rule='Every non-silence speech session; no-audio, invalid, incomplete, and skipped output count as failures')

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
        terminal=None,reference=case.get('reference',''),target_reference=case.get('target_reference'),
        partition=case.get('partition'),variant=case.get('variant'),expect_no_speech=case.get('expect_no_speech',False),
        human_translation_review_status='pending' if case.get('target_reference') else None,
        profiling={},audio_integrity_errors=[])
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
                            if wav.getframerate()!=event.get('sample_rate'):row['audio_integrity_errors'].append('sample_rate')
                            if wav.getcomptype()!='NONE':row['audio_integrity_errors'].append('wav_compression')
                            actual_duration=wav.getnframes()/wav.getframerate() if wav.getframerate() else 0
                            if abs(actual_duration-event.get('duration_seconds',0))>0.02:row['audio_integrity_errors'].append('duration')
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
                        row['terminal_type']=kind;row['terminal_status']=event.get('status')
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
    completed=row['terminal_type']=='session_complete' and row['terminal_status'] in ('complete','partial') and not row['failure']
    row['output_audio_success']=bool(row['final_snapshot']['audio_segments']) and completed and not row['audio_integrity_errors']
    row['full_audio_success']=row['output_audio_success'] and row['terminal_status']=='complete' and not row['final_snapshot']['audio_skipped']
    required_kind='source_unit_final' if mode=='transcription' else 'target_final'
    expected_silence=(row['terminal_type']=='session_complete' and row['terminal_status']=='no_speech'
        and not row['final_snapshot']['final_events'] and not row['final_snapshot']['audio_segments'])
    row['functional_success']=(expected_silence if row['expect_no_speech'] else completed and
        (row['output_audio_success'] if mode=='spoken_interpretation' else any(e['type']==required_kind and e.get('text','').strip() for e in row['final_snapshot']['final_events'])))
    row['onset_first_output_seconds']={k:v-onset for k,v in row['first_output_seconds'].items() if v>=onset} if onset is not None else {}
    row['invalid_onset_metrics']=[k for k,v in row['first_output_seconds'].items() if onset is not None and v<onset]
    hypothesis=(' ' if source=='en' else '').join(e['text'] for e in row['final_snapshot']['final_events'] if e['type']=='source_final')
    row['asr_error_rate']=error_rate([row['reference']],[hypothesis],'latin_wer' if source=='en' else 'han_cer')
    return row

def summarize(records):
    result={};acceptance=json.loads(Path('benchmarks/streaming/acceptance.json').read_text())
    minimum_n=acceptance['sample_scope']['minimum_sessions_per_route']
    for scenario in sorted({r['scenario'] for r in records}):
        rows=[r for r in records if r['scenario']==scenario];speech=scenario.startswith('speech_')
        latency_rows=[r for r in rows if r.get('variant')=='clean']
        first_kind='audio_segment' if speech else 'target_partial' if scenario.startswith('translation_') else 'source_unit_partial'
        usable=[r for r in latency_rows if r['functional_success']]
        language=scenario.split('_')[-1 if scenario.startswith('transcription_') else -2]
        profile='latin_wer' if language=='en' else 'han_cer'
        quality_rows=[r for r in rows if r.get('reference','').strip() and not r.get('expect_no_speech')]
        hypotheses=[(' ' if language=='en' else '').join(e['text'] for e in r['final_snapshot'].get('final_events',[]) if e['type']=='source_final') for r in quality_rows]
        result[scenario]=dict(total=len(rows),failed=sum(not r['functional_success'] for r in rows),protocol_failures=sum(bool(r['failure']) for r in rows),
            first_pcm_latency=percentiles([r['first_output_seconds'][first_kind] for r in usable if first_kind in r['first_output_seconds']]),
            onset_latency=percentiles([r['onset_first_output_seconds'][first_kind] for r in usable if first_kind in r['onset_first_output_seconds']]),
            latency_sample_count=sum(first_kind in r['first_output_seconds'] for r in usable),
            latency_missing_count=sum(first_kind not in r['first_output_seconds'] for r in latency_rows),
            onset_ground_truth=bool(latency_rows) and all(r['speech_onset_method']=='human' for r in latency_rows),
            output_audio_success_rate=sum(r['output_audio_success'] for r in rows if not r.get('expect_no_speech'))/sum(not r.get('expect_no_speech') for r in rows) if speech and any(not r.get('expect_no_speech') for r in rows) else None,
            full_audio_success_rate=sum(r['full_audio_success'] for r in rows if not r.get('expect_no_speech'))/sum(not r.get('expect_no_speech') for r in rows) if speech and any(not r.get('expect_no_speech') for r in rows) else None,
            no_audio_rate=sum(not r['final_snapshot']['audio_segments'] for r in rows if not r.get('expect_no_speech'))/sum(not r.get('expect_no_speech') for r in rows) if speech and any(not r.get('expect_no_speech') for r in rows) else None,
            expected_no_speech=sum(bool(r.get('expect_no_speech')) for r in rows),
            skip_reasons={reason:sum(e.get('reason')==reason for r in rows for e in r['final_snapshot']['audio_skipped'])
                for reason in {e.get('reason') for r in rows for e in r['final_snapshot']['audio_skipped']}},
            source_quality=dict(metric='WER' if language=='en' else 'CER',normalization_profile=profile,
                value=error_rate([r.get('reference','') for r in quality_rows],hypotheses,profile) if quality_rows else None,n=len(quality_rows),
                scope='all selected sessions including failed/no-output sessions'),
            target_quality_review=(dict(status='pending_human_review',cases=sum(bool(r.get('target_reference')) for r in rows),
                automated_semantic_score=None) if scenario.startswith('translation_') else dict(status='not_applicable',cases=0)),
            resource_scope='See report resource_metrics if enabled; requested concurrency is not accepted capacity')
        measured=result[scenario]
        if speech:
            audio_gate=acceptance['speech_audio_success']['clean_fixture_minimum_rate']
            audio_rows=[r for r in rows if not r.get('expect_no_speech')]
            if len(audio_rows)<minimum_n:audio_status='insufficient_samples'
            elif measured['full_audio_success_rate']<audio_gate:audio_status='fail'
            else:audio_status='pass'
            measured['clean_fixture_audio_gate']=dict(metric='full_audio_success_rate',required_rate=audio_gate,
                value=measured['full_audio_success_rate'],status=audio_status,minimum_sessions=minimum_n)
            measured['production_audio_gate_replay_assessment']=production_audio_gate(rows,acceptance)
            latency=measured['onset_latency'];human=measured['onset_ground_truth'];missing=measured['latency_missing_count']
            if not human:status='not_evaluable_human_onset_required'
            elif len(latency_rows)<minimum_n:status='insufficient_samples'
            elif missing or latency['n']!=len(rows):status='fail_missing_output'
            else:status='pass' if latency['p90_seconds']<acceptance['latency_targets']['speech_ttfo']['limit_seconds'] else 'fail'
            measured['latency_target_assessment']=dict(metric='speech_ttfo',clock='human_annotated_speech_onset',
                limit_seconds=acceptance['latency_targets']['speech_ttfo']['limit_seconds'],value_p90_seconds=latency['p90_seconds'],n=latency['n'],status=status)
        else:
            latency=measured['first_pcm_latency'];missing=measured['latency_missing_count']
            target_key='source_partial' if scenario.startswith('transcription_') else 'target_partial'
            limit=acceptance['latency_targets'][target_key]['limit_seconds']
            if len(latency_rows)<minimum_n:status='insufficient_samples'
            elif missing or latency['n']!=len(latency_rows):status='fail_missing_output'
            else:status='pass' if latency['p90_seconds']<limit else 'fail'
            measured['latency_target_assessment']=dict(metric='source_partial' if scenario.startswith('transcription_') else 'target_partial',
                clock='first_pcm_send',limit_seconds=limit,value_p90_seconds=latency['p90_seconds'],n=latency['n'],status=status)
        slices={}
        for dimension in ('partition','variant'):
            values=sorted({r.get(dimension) or 'unspecified' for r in rows})
            slices[dimension]={}
            for value in values:
                group=[r for r in rows if (r.get(dimension) or 'unspecified')==value]
                references=[r for r in group if r.get('reference','').strip() and not r.get('expect_no_speech')]
                group_hypotheses=[(' ' if language=='en' else '').join(e['text'] for e in r['final_snapshot'].get('final_events',[]) if e['type']=='source_final') for r in references]
                translated=sum(any(e['type']=='target_final' and e.get('text','').strip() for e in r['final_snapshot']['final_events']) for r in group)
                slices[dimension][value]=dict(total=len(group),functional_failures=sum(not r['functional_success'] for r in group),
                    source_quality=dict(metric='WER' if language=='en' else 'CER',value=error_rate([r['reference'] for r in references],group_hypotheses,profile) if references else None,n=len(references)),
                    target_reference_count=sum(bool(r.get('target_reference')) for r in group),target_final_count=translated,
                    target_semantic_quality_status='pending_human_review' if scenario.startswith('translation_') else None)
        measured['quality_slices']=slices
    return result

async def main_async(args):
    manifest=json.loads(args.manifest.read_text());annotations={}
    parsed=urlsplit(args.url)
    capability_url=urlunsplit(('https' if parsed.scheme=='wss' else 'http',parsed.netloc,
        '/v1/audio/stream/capabilities','',''))
    try:
        with urlopen(capability_url,timeout=5) as response:capabilities=json.load(response)
    except Exception as exc:
        capabilities={'unavailable':type(exc).__name__}
    if args.annotations:annotations={r['case']:r for r in json.loads(args.annotations.read_text())['cases']}
    selected=[c for c in manifest['cases'] if args.all_cases or c.get('variant')=='clean']
    if args.cases:
        requested=set(args.cases.split(','))
        selected=[c for c in selected if c['id'] in requested or c.get('legacy_case') in requested]
    modes=args.modes.split(',') if args.modes else list(MODES)
    if not modes or any(mode not in MODES for mode in modes):raise ValueError('Unknown mode; choose '+','.join(MODES))
    if not selected:raise ValueError('No selected samples')
    records=[];resource_sampler=None
    if args.server_pid:
        from .resources import ResourceSampler
        resource_sampler=ResourceSampler(args.server_pid,args.cpu_cores);resource_sampler.start()
    try:
        for round_index in range(args.rounds):
            for mode in modes:
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
        corpus_sha256=hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        corpus_revision=manifest.get('revision',manifest.get('corpus_revision')),
        catalog_sha256=hashlib.sha256(Path('catalog/models.json').read_bytes()).hexdigest(),
        code_sha256={str(path):hashlib.sha256(path.read_bytes()).hexdigest() for root in ('src/smartvoice','benchmarks/streaming') for path in sorted(Path(root).rglob('*.py'))},
        configuration_sha256=hashlib.sha256(Path('catalog/streaming.json').read_bytes()).hexdigest(),
        effective_capabilities=capabilities,
        effective_capabilities_sha256=hashlib.sha256(json.dumps(capabilities,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
        acceptance_revision=json.loads(Path('benchmarks/streaming/acceptance.json').read_text())['revision'],
        acceptance_sha256=hashlib.sha256(Path('benchmarks/streaming/acceptance.json').read_bytes()).hexdigest(),
        environment=dict(platform=platform.platform(),python=platform.python_version(),
            versions={name:version(name) for name in ('sherpa-onnx','ctranslate2','sentencepiece','websockets')}),
        records=records,summary=summarize(records),resource_metrics=resource_metrics,
        evaluation_inputs=dict(manifest_sha256=hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
            annotations_sha256=hashlib.sha256(args.annotations.read_bytes()).hexdigest() if args.annotations else None,
            strict_baseline_sha256=hashlib.sha256(args.baseline.read_bytes()).hexdigest() if args.baseline else None,
            selected_case_ids=[case['id'] for case in selected],mode_order=modes,
            rounds=args.rounds,concurrency=args.concurrency,
            audio_root=portable_location(args.audio_root,
                artifact_id='corpus-sha256:'+hashlib.sha256(args.manifest.read_bytes()).hexdigest())),
        requested_concurrency=args.concurrency,
        requested_sessions_per_cpu_core=args.concurrency/args.cpu_cores if args.cpu_cores else None,
        passing_concurrency_capacity=None,capacity_status='not accepted: requires repeated quality/latency/success-rate confirmation')
    if args.baseline:report['strict_quality_guard']=compare(json.loads(args.baseline.read_text()),records)
    validate_report_paths(report)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as stream:json.dump(report,stream,indent=2,ensure_ascii=False)
    print(args.output,flush=True)

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--url',default='ws://127.0.0.1:8766/v1/audio/stream')
    parser.add_argument('--manifest',type=Path,required=True);parser.add_argument('--audio-root',type=Path,required=True)
    parser.add_argument('--annotations',type=Path);parser.add_argument('--baseline',type=Path)
    parser.add_argument('--server-pid',type=int);parser.add_argument('--cpu-cores',type=int)
    parser.add_argument('--concurrency',type=int,choices=range(1,5),default=1)
    parser.add_argument('--cases');parser.add_argument('--rounds',type=int,choices=range(1,31),default=1,
        help='Repeat the selected fixtures (up to 30 rounds; 30 rounds over 10 clean cases gives 300 sessions per direction)')
    parser.add_argument('--all-cases',action='store_true',help='Include non-clean variants and expected-silence controls')
    parser.add_argument('--modes',help='Comma-separated subset: transcription,translated_subtitles,spoken_interpretation')
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    if args.cpu_cores is not None and args.cpu_cores<=0:parser.error('cpu-cores must be positive')
    if args.output.exists():parser.error('Output exists; preserve previous runs with a fresh report path')
    asyncio.run(main_async(args))
if __name__=='__main__':main()
