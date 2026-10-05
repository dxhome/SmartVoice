import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import unittest
from unittest.mock import patch
from smartvoice.ports.diagnostics import StageTimings, stage, stage_timings
from scripts.validation.stt_validation_common import check_coverage

class StageDiagnosticsTests(unittest.TestCase):
    def test_nested_scopes_are_exclusive(self):
        collector = StageTimings(); token = stage_timings.set(collector)
        try:
            with patch('smartvoice.ports.diagnostics.time.perf_counter', side_effect=[0, 1, 4, 5]):
                with stage('parent'):
                    with stage('child'): pass
            self.assertEqual(collector.snapshot()['parent']['seconds'], 2)
            self.assertEqual(collector.snapshot()['child']['seconds'], 3)
        finally: stage_timings.reset(token)

    def test_failure_is_recorded_and_propagated(self):
        collector = StageTimings(); token = stage_timings.set(collector)
        try:
            with self.assertRaises(ValueError):
                with stage('native'): raise ValueError('failure')
            self.assertEqual(collector.snapshot()['native']['failures'], 1)
        finally: stage_timings.reset(token)

    def test_context_survives_worker_thread_and_reset(self):
        collector = StageTimings(); token = stage_timings.set(collector)
        def work():
            with stage('native'): pass
        try:
            with ThreadPoolExecutor(1) as executor: executor.submit(copy_context().run, work).result()
            self.assertEqual(collector.snapshot()['native']['calls'], 1)
        finally: stage_timings.reset(token)
        with stage('disabled'): pass
        self.assertNotIn('disabled', collector.snapshot())

    def test_coverage_checks_tail_progress_and_gaps(self):
        rows = [{'start':0, 'duration':15}, {'start':14, 'duration':1.1}]
        self.assertEqual(check_coverage(rows, 15.1, 2), 15.1)
        for broken in ([{'start':0,'duration':15},{'start':14,'duration':1}],
                       [{'start':0,'duration':15},{'start':16,'duration':1}]):
            with self.assertRaises(AssertionError): check_coverage(broken,17,2)
