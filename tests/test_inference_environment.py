"""Test-state isolation does not rewrite installed model assets or routing."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from smartvoice.config.settings import Settings
from tests.inference_environment import isolated_runtime, language_id_ready


class InferenceEnvironmentTests(unittest.TestCase):
    def test_lid_preflight_cache_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Settings(data_dir=Path(directory));source.models_dir.mkdir()
            cache=source.models_dir/'.integrity-cache.json';cache.write_text('{}')
            def verify(settings):
                self.assertNotEqual(settings.data_dir,source.data_dir)
                (settings.models_dir/cache.name).write_text('private')
                return settings.models_dir
            with patch('smartvoice.services.spoken_language_identifier.installed_language_id_model_dir',side_effect=verify):
                self.assertTrue(language_id_ready(source))
            self.assertEqual(cache.read_text(),'{}')

    def test_scanner_repairs_only_probe_state(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Settings(data_dir=Path(directory))
            source.models_dir.mkdir()
            # An incomplete model forces the real scanner to update state.
            (source.models_dir/'stt-sensevoice-small-int8').mkdir()
            before=list(source.models_dir.iterdir())
            with patch('tests.inference_environment.create_inference_provider',return_value=object()):
                with isolated_runtime(source) as (settings,_):
                    self.assertTrue(any(p.is_file() for p in settings.models_dir.iterdir()))
            self.assertEqual(list(source.models_dir.iterdir()),before)

    def test_assets_and_configuration_are_read_from_isolated_state(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Settings(data_dir=Path(directory))
            model = "stt-sensevoice-small-int8"
            asset = source.models_dir / model
            asset.mkdir(parents=True)
            (asset / "sentinel").write_text("unchanged")
            (source.models_dir / ".smartvoice").mkdir()
            cache = source.models_dir / ".integrity-cache.json"
            cache.write_text("{}")
            router = source.data_dir / "router.json"
            router.write_text("{}")
            installed = [{"id": model, "task": "transcription"}]
            def factory(settings, repository):
                self.assertEqual(repository.model_directory(model), asset)
                snapshot = repository.installed_models()
                snapshot.clear()
                self.assertEqual(repository.refresh_installed_models(), installed)
                return object()
            with patch("tests.inference_environment.CatalogModelRepository.installed_models", return_value=installed), \
                 patch("tests.inference_environment.create_inference_provider", side_effect=factory):
                with isolated_runtime(source) as (settings, provider):
                    temporary = settings.data_dir
                    self.assertNotEqual(temporary, source.data_dir)
                    self.assertFalse((settings.models_dir / model).exists())
                    self.assertTrue((settings.models_dir / ".smartvoice").is_symlink())
                    self.assertEqual((temporary / "router.json").read_text(), "{}")
                    (temporary / "router.json").write_text("local change")
                    (settings.models_dir / cache.name).write_text("local cache")
                self.assertFalse(temporary.exists())
            self.assertEqual(router.read_text(), "{}")
            self.assertEqual(cache.read_text(), "{}")
            self.assertEqual((asset / "sentinel").read_text(), "unchanged")

    def test_runtime_is_closed_when_probe_fails_before_app_startup(self):
        from unittest.mock import Mock
        runtime = Mock()
        with tempfile.TemporaryDirectory() as directory:
            source = Settings(data_dir=Path(directory))
            with patch("tests.inference_environment.CatalogModelRepository.installed_models", return_value=[]), patch("tests.inference_environment.create_inference_provider", return_value=runtime):
                with self.assertRaises(RuntimeError):
                    with isolated_runtime(source) as (settings, _):
                        temporary = settings.data_dir
                        raise RuntimeError('probe prerequisite failed')
                runtime.close.assert_called_once()
                self.assertFalse(temporary.exists())
