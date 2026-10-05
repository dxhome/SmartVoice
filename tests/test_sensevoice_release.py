import unittest

from scripts.validation.verify_sensevoice_release import MODEL, quality_ready, reusable, same_environment


class SenseVoiceReleaseTests(unittest.TestCase):
    def environment(self):
        return {'settings': {'max_instances': 2}, 'dependencies': {}, 'models': {},
                'platform': 'test', 'python_files_sha256': {'src/core.py': 'abc',
                'scripts/validation/stt_isolated_http.py': 'def'}}

    def test_product_and_measurement_changes_prevent_reuse(self):
        current = self.environment()
        previous = {**current, 'python_files_sha256': {'src/core.py': 'abc'}}
        self.assertTrue(same_environment(previous, current))
        self.assertFalse(same_environment(previous, current, ('scripts/validation/stt_isolated_http.py',)))
        previous['python_files_sha256']['src/core.py'] = 'changed'
        self.assertFalse(same_environment(previous, current))
        previous['python_files_sha256'] = {**current['python_files_sha256'], 'src/removed.py': 'old'}
        self.assertFalse(same_environment(previous, current))

    def test_quality_requires_three_complete_pairs_on_current_product(self):
        environment = self.environment()
        quality = {'completed': True, 'candidate_policy': 'relative-minimum8',
                   'evaluations': [{'quality_non_regression': True}],
                   'rounds': [{'model': MODEL, 'completed': True, 'policy': p, 'repeat': n,
                               'candidate_policy': 'relative-minimum8',
                               'policy_configuration': {
                                   'window_seconds': 15.0,
                                   'minimum_seconds': 8 if p == 'candidate' else None,
                                   'relative_quiet': p == 'candidate',
                                   'overlap_on_silence': True},
                               'cases': [{'status': 200, 'chunks': 1, 'adapter_calls': 1,
                                          'coverage_end': 1, 'duration': 1} for _ in range(15)],
                               'provenance': environment}
                              for p in ('current', 'candidate') for n in (1, 2, 3)]}
        self.assertTrue(quality_ready(quality, environment))
        quality['rounds'][0]['cases'][0]['coverage_end'] = 0
        self.assertFalse(quality_ready(quality, environment))

    def test_completed_report_requires_process_exit_and_matching_policy(self):
        environment = self.environment()
        spec = {'policy': 'current', 'scenario': 'same', 'count': 500, 'interval_seconds': .4}
        row = {**spec, 'completed': True, 'server_process_exited': True,
               'configuration': {'relative_quiet': False, 'minimum_window_seconds': None,
                                 'overlap_on_silence': True}, 'provenance': environment}
        self.assertTrue(reusable(row, spec, environment))
        row['server_process_exited'] = False
        self.assertFalse(reusable(row, spec, environment))
