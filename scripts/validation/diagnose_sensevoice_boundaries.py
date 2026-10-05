"""Isolated SenseVoice window ablations and opt-in bounded listening material.

Default reports contain metrics and edit positions, never transcripts. A review
directory explicitly enables local audio excerpts and bounded text snippets.
These diagnostics do not promote a policy or change production configuration.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import io
import importlib.util
import json
import logging
from pathlib import Path
import re
import subprocess
import sys
import time
import wave

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from scripts.validation.compare_stt_policies import normalize, recording_spans, score
from scripts.validation.stt_validation_common import (
    TemporaryStorageTrace, TraceStore, WindowTrace, check_coverage, fingerprint, provenance,
)
from scripts.validation.verify_stt_candidates import corpus
from scripts.validation.stt_window_policies import Policy, BASE_POLICIES as POLICIES
from smartvoice.services.audio_planning import merge

MODEL = 'stt-sensevoice-small-int8'


def units(text, language):
    text = normalize(text)
    return text.split() if language == 'en' else list(re.sub(r'\s', '', text))


def edit_events(reference, hypothesis, language):
    # This optional diagnostic requires rapidfuzz; ordinary regression scoring
    # continues to offer its existing exact Python fallback.
    from rapidfuzz.distance import Levenshtein
    a, b = units(reference, language), units(hypothesis, language)
    return [{'kind':op.tag, 'reference_start':op.src_start,
             'reference_end':op.src_end, 'output_start':op.dest_start,
             'output_end':op.dest_end}
            for op in Levenshtein.opcodes(a, b) if op.tag != 'equal']


def error_positions(events):
    return set(position for event in events
               for position in range(event['reference_start'],
                                     max(event['reference_end'], event['reference_start'] + 1)))


def bounded(text, maximum=96):
    return text[:maximum] + ('…' if len(text) > maximum else '')


def export_clip(audio, destination, start, end):
    with wave.open(io.BytesIO(audio), 'rb') as source:
        rate = source.getframerate()
        total = source.getnframes()
        first, last = max(0, int(start * rate)), min(total, int(end * rate))
        if first >= last:
            raise ValueError('Empty review interval')
        source.setpos(first)
        params = source.getparams()
        data = source.readframes(last - first)
    with wave.open(str(destination), 'wb') as output:
        output.setparams(params)
        output.writeframes(data)
    return first / rate, last / rate


def stitched_references():
    path = ROOT / 'sandbox/streaming/data/real.manifest.json'
    manifest = json.loads(path.read_text())
    selected = [c for c in manifest['cases'] if c['language'] == 'zh'
                and c['variant'] == 'clean'][:10]
    spans = recording_spans(selected)
    offset = 0
    for span, case in zip(spans, selected):
        length = len(units(case['reference'], 'zh'))
        span.update(reference=case['reference'], reference_start=offset,
                    reference_end=offset + length)
        offset += length
    return spans


def review_boundaries(case, policy, windows, results, folder):
    """Acoustic cuts are exact; neighboring references are recording context.

    No word timestamps are inferred from text alignment. Continuous cases use
    annotated interval context; stitched Chinese uses source-recording spans.
    """
    folder.mkdir(parents=True, exist_ok=True)
    spans = ([{'start':a['start'],'end':a['end'],'reference':a['text'],
               'id':a.get('speaker','unknown')} for a in case['annotations']]
             if case['group']=='continuous' else stitched_references())
    rows = []
    for index in range(1, len(windows)):
        left, right = windows[index - 1], windows[index]
        cut = left['start'] + left['duration']
        path = folder / f'{policy}-boundary-{index:02d}.wav'
        begin, end = export_clip(case['audio'], path, cut - 3, cut + 3)
        neighboring = [s for s in spans if s['start'] < end and s['end'] > begin]
        rows.append({'case_id':case['id'], 'policy':policy, 'boundary':index, 'cut_seconds':cut,
                     'new_window_start':right['start'], 'overlap_seconds':right['overlap'],
                     'clip_start':begin, 'clip_end':end, 'audio':str(path.resolve()),
                     'left_output_tail':str(results[index - 1].get('text', ''))[-96:],
                     'right_output_head':bounded(str(results[index].get('text', ''))),
                     'reference_context':bounded(' / '.join(s['reference'] for s in neighboring), 160),
                     'source_recording_ids':[s['id'] for s in neighboring],
                     'alignment':'reference interval context; not native word timestamps',
                     'human_verdict':'pending'})
    (folder / f'{policy}-boundaries.json').write_text(
        json.dumps(rows, ensure_ascii=False, indent=2))
    return rows


def review_errors(case, policy, hypothesis, events, folder):
    """Export original-recording ranges; edit alignment never implies word time."""
    spans = stitched_references()
    reference, output = units(case['reference'], 'zh'), units(hypothesis, 'zh')
    rows = []
    for index, span in enumerate(spans):
        matching = [e for e in events if span['reference_start'] <=
                    min(e['reference_start'], len(reference)-1) < span['reference_end']]
        if not matching:
            continue
        path = folder / f'{policy}-recording-{index+1:02d}.wav'
        begin, end = export_clip(case['audio'], path, span['start'], span['end'])
        excerpts = []
        for event in matching:
            left, right = event['reference_start'], event['reference_end']
            a, b = event['output_start'], event['output_end']
            excerpts.append({**event,
                'reference_context':bounded(''.join(reference[max(0,left-12):min(len(reference),right+12)])),
                'output_context':bounded(''.join(output[max(0,a-12):min(len(output),b+12)])),
                'reference_error':bounded(''.join(reference[left:right]),32),
                'output_error':bounded(''.join(output[a:b]),32)})
        rows.append({'policy':policy, 'recording_id':span['id'],
                     'reference':bounded(span['reference']), 'audio':str(path.resolve()),
                     'clip_start':begin, 'clip_end':end, 'edits':excerpts,
                     'alignment':'original recording; edit positions are not word timestamps',
                     'human_verdict':'pending'})
    (folder/f'{policy}-errors.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))


def worker(policy, manifest, report_path, review_dir, full):
    from fastapi.testclient import TestClient
    from smartvoice.app import create_app
    from smartvoice.config.settings import Settings
    from smartvoice.adapters.audio.input import BoundedAudioInput
    from tests.inference_environment import isolated_runtime
    inputs = [c for c in corpus(manifest) if full or c['group'] == 'stitched'
              or c['duration'] == 75]
    settings = Settings.from_env()
    report = {'completed':False, 'model':MODEL, 'policy':policy,
              'configuration':asdict(POLICIES[policy]), 'provenance':provenance(settings),
              'scope':'all corpus' if full else '75-second continuous plus stitched controls',
              'cases':[], 'human_audit':'pending', 'production_defaults_changed':False}
    report_path.write_text(json.dumps(report, indent=2))
    try:
        with TemporaryStorageTrace() as storage, isolated_runtime(settings) as (private, provider):
            if MODEL not in {m['id'] for m in provider.installed_models()}:
                raise RuntimeError('SenseVoice is not installed')
            original = provider.transcribe
            results = []
            def capture(*args, **kwargs):
                result = original(*args, **kwargs)
                results.append(result)
                return result
            provider.transcribe = capture
            app = create_app(private, provider)
            logging.getLogger('smartvoice.api').setLevel(logging.CRITICAL)
            windows = WindowTrace(BoundedAudioInput(private.max_audio_seconds,
                                                    **asdict(POLICIES[policy])))
            app.state.transcription_service.audio_input = windows
            trace = TraceStore()
            app.add_middleware(trace.middleware())
            with TestClient(app) as client:
                warm = client.post('/v1/audio/transcriptions',
                    files={'file':('warm.wav', (ROOT/'tests/fixtures/zh.wav').read_bytes(), 'audio/wav')},
                    data={'model':MODEL, 'language':'zh'})
                assert warm.status_code == 200
                for case in inputs:
                    results.clear()
                    started = time.perf_counter()
                    response = client.post('/v1/audio/transcriptions',
                        headers={'x-request-id':case['id']},
                        files={'file':('control.wav', case['audio'], 'audio/wav')},
                        data={'model':MODEL, 'language':'auto' if case['language']=='mixed' else case['language'],
                              'response_format':'verbose_json'})
                    row = {k:case[k] for k in ('id', 'sha256', 'group', 'language', 'duration')}
                    row['reference_sha256'] = hashlib.sha256(case['reference'].encode()).hexdigest()
                    report['cases'].append(row)
                    row.update(status=response.status_code, seconds=time.perf_counter()-started)
                    if response.status_code != 200:
                        row['error_code'] = response.json().get('error', {}).get('code')
                        raise AssertionError('Diagnostic HTTP request failed')
                    body = response.json()
                    chunks = body.get('chunk_count', 1)
                    assert chunks == len(results)
                    merged = ''
                    boundaries = []
                    for window, result in zip(windows.rows, results):
                        current = str(result.get('text', ''))
                        before = len(units(merged, case['language']))
                        merged = merge(merged, current, window['overlap'])
                        after = len(units(merged, case['language']))
                        boundaries.append({**window, 'merged_unit_start':before,
                            'merged_unit_end':after,
                            'removed_units':before + len(units(current, case['language'])) - after})
                    assert merged == body['text'], 'Diagnostic replay differs from production merge'
                    row.update(chunks=chunks,
                        coverage_end=check_coverage(windows.rows, case['duration'], chunks),
                        quality=score(case['reference'], merged, case['language']),
                        raw_quality=score(case['reference'], ' '.join(str(r.get('text','')) for r in results), case['language']),
                        edits=edit_events(case['reference'], merged, case['language']),
                        windows=boundaries, stages=trace.snapshot(case['id']))
                    if review_dir and policy in ('current', 'candidate'):
                        if case['id']=='fleurs-zh':
                            review_boundaries(case, policy, windows.rows, results, review_dir)
                            review_errors(case, policy, merged, row['edits'], review_dir)
                        elif case['group']=='continuous' and case['duration']==75:
                            destination=review_dir/re.sub(r'[^a-zA-Z0-9_-]','_',case['id'])
                            review_boundaries(case, policy, windows.rows, results, destination)
                    pools = client.get('/v1/runtime').json()['backends']['sherpa-onnx']['instance_pools']
                    assert all(not p['active'] and not p['waiting'] for p in pools.values())
                    assert not app.state.inference_queue._reserved
                    report_path.write_text(json.dumps(report, indent=2))
            report['audio_spools'] = storage.assert_closed()
        report['isolated_state_removed'] = not private.data_dir.exists()
        assert report['isolated_state_removed']
        report['completed'] = True
    except BaseException as exc:
        report['failure_type'] = type(exc).__name__
        raise
    finally:
        report_path.write_text(json.dumps(report, indent=2))


def summarize(rows):
    baseline = {c['id']:c for c in rows[0]['cases']}
    comparisons = []
    for trial in rows:
        cases = []
        for row in trial['cases']:
            left = baseline[row['id']]
            new = error_positions(row['edits']) - error_positions(left['edits'])
            joins=[w['merged_unit_start'] for w in row['windows'][1:]]
            first_removal=min((w['merged_unit_start'] for w in row['windows']
                               if w['removed_units']>0),default=None)
            locations=[]
            for event in row['edits']:
                if not (error_positions([event]) & new):
                    continue
                locations.append({**event,
                    'near_output_join':any(abs(event['output_start']-p)<=2 or
                                           abs(event['output_end']-p)<=2 for p in joins),
                    'before_first_removal':first_removal is None or event['output_end']<=first_removal})
            cases.append({'id':row['id'], 'error_delta':row['quality']['errors']-left['quality']['errors'],
                          'new_reference_error_positions':sorted(new),
                          'new_error_locations':locations,
                          'first_removal_output_unit':first_removal,
                          'raw_error_delta':row['raw_quality']['errors']-left['raw_quality']['errors']})
        comparisons.append({'policy':trial['policy'], 'cases':cases})
    return comparisons


def render_review(folder):
    rows = []
    for policy in ('current', 'candidate'):
        rows.extend(json.loads((folder/f'{policy}-boundaries.json').read_text()))
        for path in sorted(folder.glob(f'*/{policy}-boundaries.json')):
            rows.extend(json.loads(path.read_text()))
    lines = ['# SenseVoice 边界试听评审', '',
             '当前与候选使用相同中文拼接输入和四份 75 秒中英文连续输入。各片段为切点前后约三秒。', '',
             '**参考文本是相邻原始录音或标注区间的上下文，不能当作片段的逐字时间对齐。**', '',
             '逐项核对：切断词句、边界漏字、重复、数字、人名、合法重复表达。人工结论均待填写。', '']
    current_path=folder/'current-errors.json'
    if current_path.exists() and (folder/'candidate-errors.json').exists():
        current=json.loads(current_path.read_text())
        known=error_positions([e for row in current for e in row['edits']])
        for row in json.loads((folder/'candidate-errors.json').read_text()):
            new=[e for e in row['edits'] if error_positions([e])-known]
            if not new:
                continue
            lines.extend([f"## 候选新增错误定位 · {row['recording_id']}", '',
                          f"![原始录音试听]({row['audio']})", '',
                          f"参考录音文本：{row['reference']}", '',
                          '以下按归一化字符编辑对齐定位，重复文字可能产生多种对齐；不是词级时间戳。', ''])
            for event in new:
                lines.extend([f"- {event['kind']}：参考 `{event['reference_error']}` → 识别 `{event['output_error']}`；参考上下文：{event['reference_context']}；识别上下文：{event['output_context']}", ''])
            lines.extend(['人工结论：待评审。', ''])
    for row in rows:
        lines.extend([f"## {row['case_id']} · {row['policy']} · 边界 {row['boundary']} · {row['cut_seconds']:.2f} 秒", '',
            f"![边界试听]({row['audio']})", '',
            f"重叠：{row['overlap_seconds']:.2f} 秒；原音频区间：{row['clip_start']:.2f}–{row['clip_end']:.2f} 秒。", '',
            f"参考上下文：{row['reference_context']}", '',
            f"前窗末尾：{row['left_output_tail']}", '',
            f"后窗开头：{row['right_output_head']}", '',
            '人工结论：待评审。', ''])
    (folder/'review.md').write_text('\n'.join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=ROOT/'sandbox/stt-continuous/manifest.json')
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--worker', choices=POLICIES)
    parser.add_argument('--review-dir', type=Path, help='Explicit bounded local audio/text review export')
    parser.add_argument('--full', action='store_true', help='Include 300/590-second continuous clips')
    args = parser.parse_args()
    if importlib.util.find_spec('rapidfuzz') is None:
        parser.error('This optional alignment diagnostic requires rapidfuzz; install it in the verification environment')
    args.report.parent.mkdir(parents=True, exist_ok=True)
    if args.worker:
        worker(args.worker, args.manifest, args.report, args.review_dir, args.full)
        return
    report = {'completed':False, 'purpose':'diagnosis, not candidate acceptance',
              'corpus_manifest_sha256':fingerprint(args.manifest),
              'fleurs_manifest_sha256':fingerprint(ROOT/'sandbox/streaming/data/real.manifest.json'),
              'rounds':[]}
    try:
        for policy in POLICIES:
            path = args.report.with_name(args.report.stem+'-'+policy+'.json')
            command = [sys.executable, __file__, '--worker', policy, '--manifest', str(args.manifest), '--report', str(path)]
            if args.review_dir: command += ['--review-dir', str(args.review_dir)]
            if args.full: command.append('--full')
            result = subprocess.run(command, capture_output=True, text=True, timeout=1800)
            if path.exists(): report['rounds'].append(json.loads(path.read_text()))
            args.report.write_text(json.dumps(report, indent=2))
            if result.returncode: raise RuntimeError(f'{policy} diagnostic failed: {result.stderr[-500:]}')
            print(json.dumps({'policy':policy, 'scores':[(c['id'],c['quality']['errors']) for c in report['rounds'][-1]['cases']]}), flush=True)
        report['comparisons'] = summarize(report['rounds'])
        if args.review_dir: render_review(args.review_dir)
        report['completed'] = True
    finally:
        args.report.write_text(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
