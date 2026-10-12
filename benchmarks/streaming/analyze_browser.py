"""Check real DOM, finalized text and completed playback from QA evidence."""
import argparse
import json
from pathlib import Path


def analyze(report):
    browser=report['browser'];events=browser['events'];errors=[]
    finals={};unique_audio={}
    for event in events:
        if event['type'] in ('source_unit_final','target_final'):
            finals[(event['type'],event['unit_id'])]=event['text']
        if event['type']=='error':errors.append('server_error:'+str(event.get('code')))
    source=[text for (kind,_),text in finals.items() if kind=='source_unit_final']
    target=[text for (kind,_),text in finals.items() if kind=='target_final']
    if not source:errors.append('missing_source_final')
    if browser['mode']!='transcription' and not target:errors.append('missing_target_final')
    if not any(e['type']=='session_complete' and e.get('status')=='complete' for e in events):
        errors.append('missing_current_complete_event')
    if browser['final_dom']['source']!=source:errors.append('source_dom_final_mismatch')
    if browser['final_dom']['target']!=target:errors.append('target_dom_final_mismatch')
    diagnostics=browser.get('diagnostics') or {}
    if not isinstance(diagnostics,dict) or diagnostics.get('status')!='complete':errors.append('not_complete')
    for audio in browser['audio']:
        seq=audio['audio_sequence']
        if seq in unique_audio and unique_audio[seq]['text']!=audio['text']:errors.append('replayed_audio_changed')
        unique_audio[seq]=audio
    target_by_id={e['translation_id']:e['text'] for e in events if e['type']=='target_final'}
    grouped={}
    for audio in unique_audio.values():grouped.setdefault(audio['translation_id'],[]).append(audio)
    for identity,chunks in grouped.items():
        text=target_by_id.get(identity);end=0
        if text is None:errors.append('audio_without_final');continue
        for chunk in chunks:
            if chunk['target_text_start']!=end:errors.append('audio_span_gap')
            if text[chunk['target_text_start']:chunk['target_text_end']]!=chunk['text']:errors.append('audio_final_text_mismatch')
            end=chunk['target_text_end']
        if end!=len(text):errors.append('audio_final_tail_missing')
    played=[row['audio_sequence'] for row in browser['playback'] if row['type']=='audio_played']
    if browser['mode']=='spoken_interpretation':
        if not unique_audio:errors.append('no_audio')
        expected=list(range(1,len(unique_audio)+1))
        if sorted(unique_audio)!=expected:errors.append('audio_delivery_gap')
        if played!=expected:errors.append('playback_gap_duplicate_or_order')
        if diagnostics.get('counts',{}).get('audio_segment')!=len(unique_audio):errors.append('audio_count_mismatch')
        if diagnostics.get('audio_skips'):errors.append('audio_skipped')
    if browser.get('input_pending')!=0:errors.append('unacknowledged_input')
    revisions={}
    for event in events:
        if event['type'] in ('source_unit_partial','target_partial') and event.get('text'):
            revisions.setdefault((event['type'],event['unit_id']),set()).add(event['revision'])
    profiles=report.get('proxy_connections',[])
    return dict(passed=not errors,errors=sorted(set(errors)),mode=browser['mode'],language=browser['language'],
        fixture=browser['fixture_name'],browser=browser['user_agent'],
        dom_updates=len(browser['dom_updates']),draft_revision_units=sum(len(v)>1 for v in revisions.values()),
        source_finals=source,target_finals=target,delivered_audio_chunks=len(unique_audio),completed_audio_sequences=played,
        resumed=sum(e['type']=='session_resumed' for e in events),
        interrupted_audio_attempts=sum(r['type']=='audio_started' for r in browser['playback'])-len(played),
        visibility_states=sorted({r['visibility'] for r in browser['samples']}),
        max_input_pending=max((r['pending_input'] for r in browser['samples']),default=0),
        browser_metrics=diagnostics.get('browser_metrics'),
        shaped_network_connections=[r for r in profiles if r.get('profile',{}).get('name')=='slow'],
        scope='real browser DOM, transport and Web Audio completion; physical audibility unverified')


def classify_expected_negative(row, expected_error_code):
    """Classify a declared negative control without erasing its raw failure."""
    result=dict(row)
    errors=set(result.get('errors',()))
    expected_error='server_error:'+expected_error_code
    matched=(expected_error in errors and 'not_complete' in errors
             and 'missing_current_complete_event' in errors)
    result.update(classification='expected_negative',
        expected_outcome=dict(error_code=expected_error_code,terminal='not_complete'),
        expectation_met=matched,
        counted_as_product_failure=not matched)
    return result


def summarize(rows):
    positive=[r for r in rows if r.get('classification')!='expected_negative']
    negative=[r for r in rows if r.get('classification')=='expected_negative']
    return dict(positive_cases=len(positive),positive_passed=sum(bool(r['passed']) for r in positive),
        positive_failed=sum(not bool(r['passed']) for r in positive),
        expected_negative_cases=len(negative),expected_negative_matched=sum(bool(r['expectation_met']) for r in negative),
        unexpected_failures=sum(bool(r.get('counted_as_product_failure',not r['passed'])) for r in rows))


def cli():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('directory',type=Path);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--expect-negative',action='append',default=[],metavar='EVIDENCE:ERROR_CODE',
        help='Declare one named evidence file as a negative control with its expected server error; raw errors remain in the report')
    p.add_argument('--product-default-idle-seconds',type=float,
        help='Record the shipped idle timeout alongside the negative-control summary')
    args=p.parse_args();rows=[];baselines={}
    expectations={}
    for value in args.expect_negative:
        evidence,separator,error_code=value.rpartition(':')
        if not separator or not evidence or not error_code:
            p.error('--expect-negative must be EVIDENCE:ERROR_CODE')
        if evidence in expectations:p.error('duplicate --expect-negative evidence: '+evidence)
        expectations[evidence]=error_code
    for path in sorted(args.directory.glob('browser-*.json')):
        row=dict(evidence=path.name,**analyze(json.loads(path.read_text())))
        key=(row['mode'],row['language'],row['fixture'])
        signature=(row['source_finals'],row['target_finals'])
        if key in baselines:
            row['strict_finals_match_baseline']=signature==baselines[key]
            if not row['strict_finals_match_baseline']:
                row['passed']=False;row['errors'].append('final_text_differs_from_browser_baseline')
        elif row['passed']:baselines[key]=signature;row['strict_finals_match_baseline']=True
        if path.name in expectations:
            row=classify_expected_negative(row,expectations[path.name])
        rows.append(row)
    report=dict(schema='smartvoice.stream.browser-matrix.v1',cases=rows,
        summary=summarize(rows))
    if args.product_default_idle_seconds is not None:
        report['product_default_idle_seconds']=args.product_default_idle_seconds
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':cli()
