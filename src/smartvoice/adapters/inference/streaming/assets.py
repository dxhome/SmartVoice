"""Resolve and verify installed model files, never download at runtime."""
import json
from smartvoice.domain.streaming import StageError
from smartvoice.services.model_storage import model_directory
from smartvoice.services.file_integrity import load_hash_cache, sha256

def model_files(settings,spec):
    root=model_directory(settings,spec.id)
    try:
        manifest=json.loads((root/'smartvoice-model.json').read_text())
        if manifest.get('id') not in (spec.id,*spec.legacy_ids) or manifest.get('task')!=spec.task or manifest.get('archive_sha256')!=spec.archive_sha256 or root.is_symlink():
            raise ValueError('manifest mismatch')
        cache=load_hash_cache(settings.models_dir/'.integrity-cache.json');paths={}
        for name in spec.required_files:
            path=(root/manifest['files'][name]).resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file():raise ValueError('unsafe asset')
            expected=(spec.file_sha256 or {}).get(name,manifest['file_sha256'].get(name))
            if not expected or expected!=manifest['file_sha256'].get(name):raise ValueError('missing pinned digest')
            stat=path.stat();cached=cache.get(str(path),{})
            if not (cached.get('size')==stat.st_size and cached.get('mtime_ns')==stat.st_mtime_ns and cached.get('sha256')==expected):
                if sha256(path)!=expected:raise ValueError('asset digest mismatch')
            paths[name]=path
        return paths
    except Exception as exc:
        raise StageError('model_unavailable','Required installed model files are missing or invalid') from exc
