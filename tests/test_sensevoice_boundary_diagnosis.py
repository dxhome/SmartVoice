"""Diagnostic invariants without downloading data or running inference."""
import io
from pathlib import Path
import tempfile
import unittest
import wave

from scripts.validation.diagnose_sensevoice_boundaries import (
    POLICIES, bounded, error_positions, export_clip, review_boundaries, summarize, units,
)


class BoundaryDiagnosisTests(unittest.TestCase):
    def test_full_factorial_keeps_three_controls_independent(self):
        choices={(p.relative_quiet,p.minimum_window_seconds,p.overlap_on_silence)
                 for p in POLICIES.values()}
        self.assertEqual(len(choices),8)
        self.assertEqual(POLICIES['current'].minimum_window_seconds,None)
        self.assertEqual(POLICIES['candidate'].minimum_window_seconds,8)
        self.assertFalse(POLICIES['candidate'].overlap_on_silence)

    def test_units_match_existing_scoring_without_collapsing_repetition(self):
        self.assertEqual(units('谢谢，谢谢 013!','zh'),list('谢谢谢谢013'))
        self.assertEqual(units('Hello, WORLD!','en'),['hello','world'])
        self.assertEqual(units('谢谢 Hello','mixed'),list('谢谢hello'))

    def test_insertions_keep_the_reference_anchor(self):
        events=[{'reference_start':2,'reference_end':2},
                {'reference_start':4,'reference_end':6}]
        self.assertEqual(error_positions(events),{2,4,5})

    def test_review_text_is_bounded(self):
        self.assertEqual(bounded('abc',3),'abc')
        self.assertEqual(bounded('abcdef',3),'abc…')

    def test_join_localization_does_not_invent_audio_timestamps(self):
        current={'id':'clip','quality':{'errors':0},'raw_quality':{'errors':0},
                 'edits':[],'windows':[]}
        candidate={**current,'quality':{'errors':1},'raw_quality':{'errors':1},
                   'edits':[{'kind':'delete','reference_start':5,'reference_end':6,
                             'output_start':5,'output_end':5}],
                   'windows':[{'merged_unit_start':0,'removed_units':0},
                              {'merged_unit_start':5,'removed_units':0},
                              {'merged_unit_start':15,'removed_units':2}]}
        rows=[{'policy':'current','cases':[current]},
              {'policy':'candidate','cases':[candidate]}]
        result=summarize(rows)[1]['cases'][0]
        self.assertEqual(result['new_reference_error_positions'],[5])
        location=result['new_error_locations'][0]
        self.assertTrue(location['near_output_join'])
        self.assertTrue(location['before_first_removal'])
        self.assertNotIn('start_seconds',location)

    def test_audio_excerpt_is_clipped_to_source_endpoints(self):
        audio=io.BytesIO()
        with wave.open(audio,'wb') as output:
            output.setnchannels(1);output.setsampwidth(2);output.setframerate(16000)
            output.writeframes(b'\0\0'*32000)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'clip.wav'
            self.assertEqual(export_clip(audio.getvalue(),path,-1,3),(0,2))
            with wave.open(str(path)) as source:
                self.assertEqual(source.getnframes(),32000)
            with self.assertRaises(ValueError):export_clip(audio.getvalue(),path,3,4)

    def test_continuous_review_uses_annotation_context_and_bounded_outputs(self):
        audio=io.BytesIO()
        with wave.open(audio,'wb') as output:
            output.setnchannels(1);output.setsampwidth(2);output.setframerate(16000)
            output.writeframes(b'\0\0'*32000)
        case={'id':'meeting','group':'continuous','audio':audio.getvalue(),
              'annotations':[{'start':.5,'end':1.5,'speaker':'A','text':'yes'}]}
        windows=[{'start':0,'duration':1,'overlap':0},
                 {'start':1,'duration':1,'overlap':0}]
        results=[{'text':'x'*200},{'text':'y'*200}]
        with tempfile.TemporaryDirectory() as directory:
            rows=review_boundaries(case,'current',windows,results,Path(directory))
        self.assertEqual(rows[0]['case_id'],'meeting')
        self.assertEqual(rows[0]['reference_context'],'yes')
        self.assertLessEqual(len(rows[0]['left_output_tail']),96)
        self.assertLessEqual(len(rows[0]['right_output_head']),97)
        self.assertEqual(rows[0]['human_verdict'],'pending')
