"""Read installed model assets while isolating all test runtime state."""
from contextlib import contextmanager
import copy
from dataclasses import replace
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch
from smartvoice.adapters.storage.catalog_model_repository import CatalogModelRepository
from smartvoice.adapters.inference.factory import create_inference_provider


def language_id_ready(source):
    """Verify LID prerequisites without writing its derived cache to user state."""
    from smartvoice.services.spoken_language_identifier import installed_language_id_model_dir
    with tempfile.TemporaryDirectory(prefix='smartvoice-lid-preflight-') as directory:
        work=Path(directory);models=work/'models';models.mkdir()
        assets=source.models_dir/'.smartvoice'
        if assets.is_dir():(models/assets.name).symlink_to(assets,target_is_directory=True)
        cache=source.models_dir/'.integrity-cache.json'
        if cache.is_file():shutil.copyfile(cache,models/cache.name)
        return installed_language_id_model_dir(replace(source,data_dir=work)) is not None


@contextmanager
def isolated_runtime(source):
    with tempfile.TemporaryDirectory(prefix="smartvoice-inference-") as directory:
        work = Path(directory)
        models = work / "models"
        models.mkdir()
        if source.models_dir.is_dir():
            lid = source.models_dir / ".smartvoice"
            if lid.is_dir():
                (models / lid.name).symlink_to(lid, target_is_directory=True)
            cache = source.models_dir / ".integrity-cache.json"
            if cache.is_file():
                shutil.copyfile(cache, models / cache.name)
        router = source.data_dir / "router.json"
        if router.is_file():
            shutil.copyfile(router, work / router.name)
        # The normal scanner may repair derived integrity/local-model caches.
        # Verify source assets, but redirect those writes to this probe's state.
        from smartvoice.services.model_storage import save_hash_cache
        def save_probe_cache(path, contents):
            target = models / Path(path).name
            save_hash_cache(target, contents)
        if source.models_dir.is_dir():
            with patch('smartvoice.services.model_storage.save_hash_cache', save_probe_cache):
                installed = CatalogModelRepository(source).installed_models()
        else:
            installed = []
        settings = replace(source, data_dir=work)
        class AssetRepository(CatalogModelRepository):
            def model_directory(self, model_id):
                return source.models_dir / self.get_spec(model_id).id
            def installed_models(self):
                return copy.deepcopy(installed)
            def refresh_installed_models(self, **kwargs):
                return self.installed_models()
        provider = create_inference_provider(settings, AssetRepository(settings))
        try:
            yield settings, provider
        finally:
            close = getattr(provider, "close", None)
            if callable(close):
                close()
