"""Reference extraction and scoring invariants; no downloads or native models."""
import io
import unittest
import zipfile
from scripts.prepare_stt_continuous import textgrid, ami_words, overlap_seconds
from scripts.compare_stt_policies import score

class SttValidationToolTests(unittest.TestCase):
    def test_interrupt_preserves_partial_worker_evidence(self):
        import importlib
        import json
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        for name,worker_arg in [('verify_stt_load','same'),
                                ('verify_stt_candidates','stt-sensevoice-small-int8')]:
            module=importlib.import_module('scripts.'+name)
            with self.subTest(tool=name),tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/'partial.json'
                def interrupted(*args):
                    path.write_text(json.dumps({'completed':False,'cases':[{'id':'completed-before-interrupt'}]}))
                    raise KeyboardInterrupt()
                with patch.object(module,'worker',side_effect=interrupted),patch('sys.argv',[name,'--worker',worker_arg,'--report',str(path)]):
                    with self.assertRaises(KeyboardInterrupt):module.main()
                report=json.loads(path.read_text())
                self.assertEqual(report['cases'][0]['id'],'completed-before-interrupt')
                self.assertEqual(report['failure_type'],'KeyboardInterrupt')
                self.assertFalse(report['completed'])

    def test_load_gate_rejects_failed_confirmation_despite_fast_percentiles(self):
        import copy
        from scripts.verify_stt_load import evaluate_pair
        current={'completed':True,'success_rate':1,'queue_stable':True,
                 'max_scheduling_lag':0,'interval_seconds':.8,
                 'latency':{'p95':1},'background':[{'seconds':10}],
                 'peak_rss_mib':100}
        candidate=copy.deepcopy(current)
        self.assertTrue(evaluate_pair(current,candidate,500)['resource_latency_gate'])
        current['success_rate']=.994
        self.assertFalse(evaluate_pair(current,candidate,500)['stable_load_gate'])
        current['success_rate']=1
        candidate['max_scheduling_lag']=.9
        self.assertFalse(evaluate_pair(current,candidate,500)['resource_latency_gate'])
        candidate['max_scheduling_lag']=0
        self.assertFalse(evaluate_pair(current,candidate,20)['sample_gate'])
        candidate['peak_rss_mib']=111
        self.assertFalse(evaluate_pair(current,candidate,500)['resource_latency_gate'])

    def test_textgrid_preserves_lexical_repetition_and_ignores_markup(self):
        source='''item [1]:
 name = "speaker1"
 intervals [1]:
  xmin = 0
  xmax = 1.2
  text = "谢谢谢谢<sil>"
 intervals [2]:
  xmin = 1.2
  xmax = 2
  text = "<%>"
'''
        rows=textgrid(source)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['text'],'谢谢谢谢')
        self.assertEqual(rows[0]['end'],1.2)

    def test_ami_merges_speakers_by_source_time_and_excludes_punctuation(self):
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w') as archive:
            archive.writestr('words/EN2001a.A.words.xml','<root><w starttime="1" endtime="2">yes</w><w starttime="2" endtime="2" punc="true">.</w></root>')
            archive.writestr('words/EN2001a.B.words.xml','<root><w starttime="0" endtime="1">hello</w></root>')
        with zipfile.ZipFile(buffer) as archive:rows=ami_words(archive,'EN2001a')
        self.assertEqual([r['text'] for r in rows],['hello','yes'])
        self.assertEqual([r['speaker'] for r in rows],['B','A'])

    def test_overlap_intervals_are_clipped_and_not_double_counted(self):
        rows=[{'start':0,'end':3},{'start':1,'end':4},{'start':2,'end':5}]
        self.assertEqual(overlap_seconds(rows,3.5),2.5)
        self.assertEqual(overlap_seconds(rows,1),0)

    def test_overlapping_words_from_one_speaker_are_not_overlapping_speech(self):
        rows=[{'start':0,'end':2,'speaker':'A'},
              {'start':1,'end':3,'speaker':'A'},
              {'start':1.5,'end':2.5,'speaker':'B'}]
        self.assertEqual(overlap_seconds(rows,3),1)

    def test_scoring_keeps_repeats_and_numeric_differences(self):
        self.assertEqual(score('谢谢谢谢','谢谢','zh')['errors'],2)
        self.assertEqual(score('Hello, WORLD!','hello world','en')['errors'],0)
        self.assertGreater(score('零幺三','013','zh')['errors'],0)

    def test_audio_spool_observer_detects_spill_and_explicit_cleanup(self):
        import tempfile
        from scripts.stt_validation_common import TemporaryStorageTrace
        with TemporaryStorageTrace() as trace:
            with tempfile.SpooledTemporaryFile(max_size=1) as file:
                file.write(b'abc')
                self.assertEqual(trace.snapshot()['spilled'],1)
                with self.assertRaises(AssertionError):trace.assert_closed()
            self.assertEqual(trace.assert_closed(),{'created':1,'spilled':1,'open':0})

    def test_resume_rejects_changed_source_or_incomplete_evidence(self):
        import copy
        from unittest.mock import patch
        from scripts.verify_stt_candidates import reusable_trial
        current={'settings':{'num_threads':4},'dependencies':{'native':'1'},
                 'models':{'asr':'digest'},'python_files_sha256':{'src/service.py':'new'}}
        previous={'completed':True,'model':'asr','policy':'current','repeat':1,
                  'provenance':copy.deepcopy(current),
                  'cases':[{'id':'clip','sha256':'audio','status':200,'raw_concat_quality':{}}]}
        inputs=[{'id':'clip','sha256':'audio'}]
        with patch('scripts.verify_stt_candidates.provenance',return_value=current):
            self.assertTrue(reusable_trial(previous,'asr','current',1,inputs,None))
            previous['provenance']['python_files_sha256']['src/service.py']='old'
            self.assertFalse(reusable_trial(previous,'asr','current',1,inputs,None))
            previous['provenance']=copy.deepcopy(current)
            previous['cases'][0].pop('raw_concat_quality')
            self.assertFalse(reusable_trial(previous,'asr','current',1,inputs,None))
