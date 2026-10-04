"""Bounded regressions for complete, finite transcription window plans."""
import itertools
import unittest

import numpy as np

from smartvoice.adapters.audio.input import BoundedAudioInput, RATE, pcm_wav, plan


class AudioWindowPlanningTests(unittest.TestCase):
    def windows(self, total, value=.2, **policy):
        def read(start, end):
            self.assertGreaterEqual(start, 0)
            self.assertLessEqual(start, end)
            self.assertLessEqual(end, total)
            return np.full(end-start, value, dtype=np.float32)

        # A regressed iterator must fail promptly instead of hanging the suite.
        windows = list(itertools.islice(plan(total, read, **policy), 100))
        self.assertLess(len(windows), 100, 'Window plan did not terminate')
        if not total:
            self.assertEqual(windows, [])
            return windows
        self.assertEqual(windows[0].start, 0)
        self.assertEqual(windows[-1].end, total)
        self.assertEqual(sum(w.end == total for w in windows), 1)
        for index, window in enumerate(windows):
            self.assertLess(window.start, window.end)
            self.assertLessEqual(window.end, total)
            self.assertLessEqual(window.end-window.start, int(policy.get('seconds', 15)*RATE))
            if index:
                previous = windows[index-1]
                self.assertGreater(window.start, previous.start)
                self.assertGreater(window.end, previous.end)
                self.assertLessEqual(window.start, previous.end)
                self.assertEqual(window.overlap, previous.end-window.start)
            else:
                self.assertEqual(window.overlap, 0)
        return windows

    def test_forced_boundaries_stop_at_final_window(self):
        windows = self.windows(70*RATE)
        self.assertEqual([(w.start/RATE, w.end/RATE) for w in windows],
                         [(0, 15), (14, 29), (28, 43), (42, 57), (56, 70)])

    def test_empty_short_exact_and_tiny_tail_inputs(self):
        for total in [0, 1, RATE, 15*RATE, 15*RATE+1, 29*RATE, 29*RATE+1, 30*RATE]:
            with self.subTest(total=total):
                self.windows(total)

    def test_silence_selected_cuts_and_long_limit(self):
        for value in [0., .2]:
            for seconds in [70, 600]:
                with self.subTest(value=value, seconds=seconds):
                    self.windows(seconds*RATE, value=value)

    def test_zero_overlap_and_alternative_window(self):
        for seconds, overlap in [(15, 0), (25, 1), (15, 11)]:
            with self.subTest(seconds=seconds, overlap=overlap):
                self.windows(70*RATE, seconds=seconds, overlap_seconds=overlap)
    def test_forced_only_overlap_distinguishes_quiet_and_forced_cuts(self):
        quiet=self.windows(70*RATE,value=0.,overlap_on_silence=False)
        self.assertTrue(all(w.overlap==0 for w in quiet))
        forced=self.windows(70*RATE,value=.2,overlap_on_silence=False)
        self.assertTrue(all(w.overlap==RATE for w in forced[1:]))

    def test_invalid_policies_are_rejected(self):
        for policy in [{'seconds':float('nan')}, {'seconds':float('inf')},
                       {'seconds':1}, {'overlap_seconds':-1},
                       {'overlap_seconds':15}, {'overlap_seconds':float('nan')},
                       {'minimum_seconds':-1}, {'minimum_seconds':15},
                       {'minimum_seconds':float('inf')}, {'overlap_on_silence':'false'},
                       {'relative_quiet':'false'}]:
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                next(plan(70*RATE, lambda a,b: np.zeros(b-a), **policy))

    def test_quiet_cut_cannot_move_next_window_backwards(self):
        with self.assertRaises(ValueError):
            next(plan(70*RATE, lambda a,b: np.zeros(b-a), overlap_seconds=14))

    def test_early_quiet_candidate_is_finite_and_covers_tail(self):
        default=self.windows(70*RATE,value=0.)
        early=self.windows(70*RATE,value=0.,minimum_seconds=2)
        self.assertLess(early[0].end,default[0].end)
        self.assertGreater(len(early),len(default))

    def test_invalid_minimum_constructor_policy_is_rejected(self):
        for minimum in (-1,float('nan'),float('inf')):
            with self.subTest(minimum=minimum),self.assertRaises(ValueError):
                BoundedAudioInput(minimum_window_seconds=minimum)

    def test_prepared_audio_passes_experimental_minimum_to_planner(self):
        samples=np.full(20*RATE,.2,dtype=np.float32)
        samples[int(2.4*RATE):int(2.8*RATE)]=0
        raw=pcm_wav(samples,RATE).audio
        with BoundedAudioInput(minimum_window_seconds=2).prepare(raw) as prepared:
            windows=list(prepared.windows(15))
        self.assertEqual(windows[1].start_seconds,1.6)
        self.assertEqual(windows[1].overlap_seconds,1)
        self.assertEqual(len(windows),3)

    def test_relative_quiet_keeps_low_amplitude_constant_signal(self):
        absolute=self.windows(70*RATE,value=.001)
        relative=self.windows(70*RATE,value=.001,relative_quiet=True)
        self.assertEqual(relative[0].end,15*RATE)
        self.assertLess(absolute[0].end,relative[0].end)

    def test_relative_quiet_still_recognizes_exact_silence(self):
        self.assertEqual(self.windows(70*RATE,value=0.),
                         self.windows(70*RATE,value=0.,relative_quiet=True))
        with self.assertRaises(ValueError):BoundedAudioInput(relative_quiet='false')

    def test_prepared_audio_passes_relative_policy_to_planner(self):
        samples=np.full(20*RATE,.001,dtype=np.float32)
        samples[int(2.4*RATE):int(2.8*RATE)]=0
        with BoundedAudioInput(minimum_window_seconds=2,relative_quiet=True).prepare(pcm_wav(samples,RATE).audio) as prepared:
            windows=list(prepared.windows(15))
        self.assertEqual(windows[1].start_seconds,1.6)


if __name__ == '__main__':
    unittest.main()
