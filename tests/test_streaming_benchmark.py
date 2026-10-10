import unittest

from benchmarks.streaming.run import production_audio_gate


class ProductionAudioGateTests(unittest.TestCase):
    POLICY = {'speech_audio_success': {
        'production_minimum_sessions_per_direction': 300,
        'production_minimum_rate': 0.99,
    }}

    def test_replay_gate_requires_300_sessions_and_counts_every_failure(self):
        rows=[{'full_audio_success': True, 'expect_no_speech': False} for _ in range(300)]
        rows[0]['full_audio_success']=False
        rows[1]['full_audio_success']=False
        rows[2]['full_audio_success']=False
        report=production_audio_gate(rows,self.POLICY)
        self.assertEqual(report['eligible_sessions'],300)
        self.assertEqual(report['full_audio_success_sessions'],297)
        self.assertEqual(report['status'],'pass')
        self.assertEqual(report['production_telemetry_status'],'not_available_from_local_benchmark')

    def test_four_failures_at_300_sessions_fail_the_99_percent_floor(self):
        rows=[{'full_audio_success': True, 'expect_no_speech': False} for _ in range(300)]
        # The gate consumes the replay's strict full-audio-success result. Each
        # failure mode below must remain in the eligible-session denominator.
        failure_modes = ('no_audio', 'partial_terminal', 'audio_skipped', 'wav_integrity_error')
        for row, mode in zip(rows, failure_modes):
            row.update(full_audio_success=False, failure_mode=mode)
        report=production_audio_gate(rows,self.POLICY)
        self.assertEqual(report['eligible_sessions'],300)
        self.assertEqual(report['full_audio_success_sessions'],296)
        self.assertEqual(report['status'],'fail')

    def test_fewer_than_300_sessions_are_insufficient_even_at_100_percent(self):
        rows=[{'full_audio_success': True, 'expect_no_speech': False} for _ in range(299)]
        self.assertEqual(production_audio_gate(rows,self.POLICY)['status'],'insufficient_samples')

    def test_expected_silence_is_not_an_eligible_speech_session(self):
        rows=[{'full_audio_success': True, 'expect_no_speech': False} for _ in range(300)]
        rows.append({'full_audio_success': False, 'expect_no_speech': True})
        report=production_audio_gate(rows,self.POLICY)
        self.assertEqual(report['eligible_sessions'],300)
        self.assertEqual(report['status'],'pass')


if __name__=='__main__':
    unittest.main()
