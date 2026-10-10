"""Explicit test scope must not weaken the unfiltered full-suite contract."""
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import test as runner


class TestRunnerScopeTests(unittest.TestCase):
    def run_empty_suite(self, arguments):
        result=Mock();result.wasSuccessful.return_value=True
        with patch('sys.argv',['scripts/test.py',*arguments]), \
             patch.object(runner.unittest.defaultTestLoader,'discover',return_value=unittest.TestSuite()), \
             patch.object(runner.unittest.TextTestRunner,'run',return_value=result), \
             patch('sys.stdout',new_callable=io.StringIO):
            return runner.main()

    def test_default_scope_discards_an_inherited_test_filter(self):
        with patch.dict(os.environ,{'SMARTVOICE_TEST_STT_MODELS':'stt-sensevoice-small-int8'}):
            self.assertEqual(self.run_empty_suite(['ci']),0)
            self.assertNotIn('SMARTVOICE_TEST_STT_MODELS',os.environ)

    def test_scoped_full_does_not_require_deferred_whisper_but_default_does(self):
        model='stt-sensevoice-small-int8'
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ), \
             patch.dict('sys.modules',{'sherpa_onnx':SimpleNamespace(__version__='1.13.8')}), \
             patch.object(runner,'_regression_prerequisites',return_value=[]), \
             patch('tests.streaming_long_audio.required_models',return_value=[]), \
             patch('tests.streaming_long_audio.fixture',return_value=({},b'\0\0')), \
             patch.object(runner.importlib.util,'find_spec',return_value=True), \
             patch('smartvoice.services.model_storage.model_directory',side_effect=lambda _,name:Path(directory)/name), \
             patch('sys.stderr',new_callable=io.StringIO):
            asset=Path(directory)/model;asset.mkdir();(asset/'smartvoice-model.json').write_text('{}')
            self.assertEqual(self.run_empty_suite(['full','--stt-models',model]),0)
            self.assertEqual(self.run_empty_suite(['full']),2)
            for other in ('stt-qwen3-asr-600m-int8','stt-whisper-base-multilingual-int8'):
                asset=Path(directory)/other;asset.mkdir();(asset/'smartvoice-model.json').write_text('{}')
            # Even installed manifests do not make an unpatched Whisper wheel
            # eligible for the unfiltered full suite.
            self.assertEqual(self.run_empty_suite(['full']),2)

    def test_streaming_only_full_requires_streaming_assets(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ), \
             patch.dict('sys.modules',{'sherpa_onnx':SimpleNamespace(__version__='1.13.8')}), \
             patch.object(runner,'_regression_prerequisites',return_value=[]), \
             patch.object(runner.importlib.util,'find_spec',return_value=True), \
             patch('tests.streaming_long_audio.required_models',return_value=['required-streaming-model']), \
             patch('tests.streaming_long_audio.fixture',return_value=({},b'\0\0')), \
             patch('smartvoice.services.model_storage.model_directory',side_effect=lambda _,name:Path(directory)/name), \
             patch('sys.stderr',new_callable=io.StringIO):
            self.assertEqual(self.run_empty_suite(['full','--streaming-only']),2)
            asset=Path(directory)/'required-streaming-model';asset.mkdir()
            (asset/'smartvoice-model.json').write_text('{}')
            self.assertEqual(self.run_empty_suite(['full','--streaming-only']),0)
