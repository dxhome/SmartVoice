"""Evidence guards must reject missing speech and preserve link pipelining."""
import asyncio
import time
import unittest
import hashlib
import tempfile
import wave
from pathlib import Path
from benchmarks.streaming.browser_proxy import ShapedLink
from benchmarks.streaming.analyze_soak import audio_guard
from benchmarks.streaming.analyze_browser import analyze as analyze_browser, classify_expected_negative, summarize as summarize_browser

class EvidenceTests(unittest.IsolatedAsyncioTestCase):
    def test_strict_quality_guard_rejects_changed_final_text(self):
        from benchmarks.streaming.run import compare
        def row(text):
            return dict(scenario='translation_en_zh',case='en_00_clean',failure=None,
                terminal_type='session_complete',terminal_status='complete',functional_success=True,
                audio_integrity_errors=[],final_snapshot=dict(
                    final_events=[dict(type='source_final',text='source'),dict(type='target_final',text=text)],
                    audio_segments=[],audio_skipped=[]))
        result=compare(dict(records=[row('original translation')]),[row('changed translation')])
        self.assertFalse(result['passed'])
        self.assertIn('strict_source_or_target_final_mismatch',result['findings'][0]['errors'])

    def test_strict_guard_accepts_complete_audio_when_baseline_was_partial(self):
        from benchmarks.streaming.run import compare
        def row(status,audio,skips,full):
            return dict(scenario='speech_zh_en',case='zh_05_clean',failure=None,
                terminal_type='session_complete',terminal_status=status,functional_success=True,
                full_audio_success=full,audio_integrity_errors=[],final_snapshot=dict(
                    final_events=[dict(type='source_final',text='原文'),dict(type='target_final',text='translation')],
                    audio_segments=audio,audio_skipped=skips))
        before=row('partial',[dict(audio_sequence=1,text='part')],[dict(unit_id=1,reason='incomplete')],False)
        after=row('complete',[dict(audio_sequence=1,text='part'),dict(audio_sequence=2,text='tail')],[],True)
        result=compare(dict(records=[before]),[after])
        self.assertTrue(result['passed'])
        self.assertTrue(result['findings'][0]['baseline_audio_behavior_changed'])
        self.assertTrue(result['findings'][0]['baseline_terminal_changed'])

    def test_stale_browser_diagnostics_do_not_mask_failed_new_run(self):
        report=dict(browser=dict(events=[],audio=[],playback=[],mode='transcription',language='en',
            fixture_name='fixture.wav',user_agent='test',diagnostics=dict(status='complete'),
            final_dom=dict(source=[],target=[]),input_pending=0,dom_updates=[],samples=[]))
        result=analyze_browser(report)
        self.assertFalse(result['passed']);self.assertIn('missing_current_complete_event',result['errors'])

    def test_declared_browser_timeout_control_keeps_failure_and_marks_expected_outcome(self):
        row=dict(passed=False,errors=['missing_current_complete_event','not_complete',
            'server_error:input_timeout','final_text_differs_from_browser_baseline'])
        result=classify_expected_negative(row,'input_timeout')
        self.assertFalse(result['passed'])
        self.assertIn('server_error:input_timeout',result['errors'])
        self.assertEqual(result['classification'],'expected_negative')
        self.assertTrue(result['expectation_met'])
        self.assertFalse(result['counted_as_product_failure'])

    def test_declared_browser_timeout_control_does_not_hide_unexpected_error(self):
        row=dict(passed=False,errors=['missing_current_complete_event','not_complete',
            'server_error:worker_lost'])
        result=classify_expected_negative(row,'input_timeout')
        self.assertFalse(result['expectation_met'])
        self.assertTrue(result['counted_as_product_failure'])

    def test_browser_matrix_summary_separates_expected_timeout_from_product_failures(self):
        expected=classify_expected_negative(dict(passed=False,errors=['server_error:input_timeout',
            'missing_current_complete_event','not_complete']),'input_timeout')
        result=summarize_browser([dict(passed=True),expected])
        self.assertEqual(result,dict(positive_cases=1,positive_passed=1,positive_failed=0,
            expected_negative_cases=1,expected_negative_matched=1,unexpected_failures=0))

    async def test_connection_failure_remains_in_benchmark_denominator(self):
        from benchmarks.streaming.run import replay
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'short.wav'
            with wave.open(str(path),'wb') as wav:
                wav.setparams((1,2,16000,0,'NONE','not compressed'));wav.writeframes(b'\0'*640)
            case=dict(id='failure',path=path.name,language='en',sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            row=await replay('ws://127.0.0.1:1/v1/audio/stream',case,root,'spoken_interpretation',None)
        self.assertIsNotNone(row['failure']);self.assertFalse(row['output_audio_success'])
        self.assertFalse(row['functional_success']);self.assertIsNone(row['terminal_status'])

    async def test_latency_is_pipelined_not_added_for_every_message(self):
        link=ShapedLink(rate=1000,latency=.05);received=[];began=time.monotonic()
        async def source():
            for value in ('a'*10,'b'*10,'c'*10):yield value
        async def send(value):received.append(value)
        await asyncio.gather(link.collect(source()),link.deliver(send))
        self.assertEqual(received,['a'*10,'b'*10,'c'*10])
        self.assertEqual(link.bytes,0);self.assertEqual(link.delivered_bytes,30)
        self.assertLess(time.monotonic()-began,.14)  # 50ms once + 30ms serialization

    def test_missing_final_audio_tail_is_failure(self):
        row=dict(final_snapshot=dict(final_events=[dict(type='target_final',translation_id='t',text='hello world')],
            audio_segments=[dict(translation_id='t',audio_sequence=1,target_text_start=0,target_text_end=5,
                text='hello',duration_seconds=1)],audio_skipped=[]),audio_integrity_errors=[],
            input_duration_seconds=.02,input_pacing=dict(frame_count=1),terminal_status='complete',failure=None)
        self.assertIn('unspoken_target_tail',audio_guard(row)['errors'])
        row['final_snapshot']['audio_segments'].append(dict(translation_id='t',audio_sequence=2,target_text_start=5,
            target_text_end=11,text=' world',duration_seconds=1))
        self.assertTrue(audio_guard(row)['passed'])

    def test_soak_analyzer_reports_sampled_queue_depth_and_high_water(self):
        from benchmarks.streaming.analyze_soak import input_queue_metrics
        result=input_queue_metrics([dict(input_queues=dict(current_frames=0,peak_frames=12)),
            dict(input_queues=dict(current_frames=3,peak_frames=24)),dict(input_queues=dict(current_frames=0,peak_frames=24))])
        self.assertEqual(result['current_frames']['maximum'],3)
        self.assertEqual(result['session_peak_frames']['maximum'],24)

if __name__=='__main__':unittest.main()
