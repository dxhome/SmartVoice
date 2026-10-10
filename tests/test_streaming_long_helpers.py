"""CI contracts for full-run input generation and incremental output auditing."""
import unittest
from smartvoice.adapters.audio.streaming import package_wav
from tests.streaming_long_audio import repeated_blocks, OutputAudit, fixture, required_models


class LongReplayHelpersTests(unittest.TestCase):
    def test_cyclic_generation_preserves_samples_and_bounded_frames(self):
        pcm=b'\x01\x00\x02\x00\x03\x00'
        blocks=list(repeated_blocks(pcm,7/16000,frame_samples=2))
        self.assertEqual(b''.join(blocks),(pcm*3)[:14])
        self.assertEqual(list(map(len,blocks)),[4,4,4,2])
        count=0
        for block in repeated_blocks(pcm,3600):
            self.assertLessEqual(len(block),32000);count+=len(block)
        self.assertEqual(count,3600*32000)

    def test_pinned_fixtures_and_all_required_models(self):
        for language in ('zh','en'):
            case,pcm=fixture(language)
            self.assertEqual(case['samples']*2,len(pcm))
        models=required_models()
        self.assertEqual(len(models),8)
        self.assertIn('tts-matcha-zh-baker',models)
        self.assertIn('tts-supertonic-v3-multilingual-int8',models)

    def audit(self):
        audit=OutputAudit('spoken_interpretation')
        audit.event(dict(type='target_final',event_sequence=1,translation_id='t',text='Hello world.',revision=1,refs=[]),0)
        return audit

    def audio(self):
        return dict(type='audio_segment',event_sequence=2,audio_sequence=1,translation_id='t',
            text='Hello world.',target_text_start=0,target_text_end=12,revision=1,refs=[],
            chunk_index=0,chunk_count=1,sample_rate=16000,duration_seconds=.02,
            audio=package_wav(b'\0\0'*320,16000))

    def test_audio_text_alignment_and_completion(self):
        audit=self.audit();audit.event(self.audio(),1)
        self.assertFalse(audit.targets);self.assertEqual(audit.audio_sequence,1)
        self.assertEqual(audit.first['target_final'],0)
        self.assertEqual(audit.first['audio_segment'],1)

    def test_audit_rejects_wrong_order_skips_and_missing_prefix(self):
        for changes in ({'audio_sequence':2},{'target_text_start':1},{'revision':2},
                        {'text':'wrong'},{'chunk_index':1},{'refs':[{}]}):
            with self.subTest(changes=changes),self.assertRaises(AssertionError):
                self.audit().event({**self.audio(),**changes},1)
        with self.assertRaises(AssertionError):
            self.audit().event(dict(type='audio_skipped',event_sequence=2,reason='incomplete'),1)
