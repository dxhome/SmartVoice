import unittest
from scripts.validation.verify_stt_isolated_load import latency_summary, stable_windows, verify_completed


class IsolatedLoadTests(unittest.TestCase):
    def test_fast_rejections_do_not_improve_successful_latency(self):
        result=latency_summary([{'valid':False,'seconds':.01},
                                {'valid':True,'seconds':2}])
        self.assertEqual(result['success_rate'],.5)
        self.assertEqual(result['successful_latency']['p95'],2)
        self.assertEqual(result['rejected_latency']['p95'],.01)

    def test_wait_growth_is_reported_independently_of_full_success(self):
        rows=[{'valid':True,'seconds':1,'queue_wait':0} for _ in range(50)]
        rows += [{'valid':True,'seconds':1,'queue_wait':1} for _ in range(50)]
        windows,stable=stable_windows(rows)
        self.assertFalse(stable)
        self.assertEqual(windows[-1]['mean_wait'],1)

    def test_successful_window_coverage_requires_actual_completed_calls(self):
        row={'chunks':2,'duration':29}
        metadata={'adapter_completed':2,'attempts_after_cancellation':0,
                  'windows':[{'start':0,'duration':15,'overlap':0},
                             {'start':14,'duration':15,'overlap':1}]}
        self.assertEqual(verify_completed(row,metadata),29)
        metadata['adapter_completed']=1
        with self.assertRaises(AssertionError):verify_completed(row,metadata)
