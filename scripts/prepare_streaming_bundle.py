"""Package already acquired pinned stage files for explicit SmartVoice model import.

No downloads or conversion. Sources must match the product catalog exactly.
Usage: python scripts/prepare_streaming_bundle.py MODEL --root /prepared/tree --output model.zip
MT tree: tokenizer/* and ct2/*. --file name=/absolute/path overrides individual sources.
"""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile
from smartvoice.services.model_registry import get_model_spec


def package(model_id, root, destination, overrides=None):
    spec=get_model_spec(model_id)
    if not spec.file_sha256:raise ValueError('Use existing models export for archive-installed models')
    overrides=overrides or {}
    if set(overrides)-set(spec.required_files):raise ValueError('Unknown file override')
    files={};hashes={}
    for name in spec.required_files:
        path=Path(overrides.get(name,root/name))
        if not path.is_file() or path.is_symlink():raise ValueError(f'Missing regular asset: {name}')
        with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        if digest!=spec.file_sha256[name]:raise ValueError(f'Pinned asset mismatch: {name}')
        files[name]=path;hashes[name]=digest
    manifest=dict(schema_version="1.0",id=spec.id,task=spec.task,archive_sha256=spec.archive_sha256,
        files={name:name for name in files},file_sha256=hashes,source=spec.source,
        license_note=spec.license_note,provenance='Product catalog pinned source and converted-file hashes')
    if destination.exists():raise ValueError('Output already exists; use a new bundle path')
    destination.parent.mkdir(parents=True,exist_ok=True)
    try:
        with zipfile.ZipFile(destination,'x',compression=zipfile.ZIP_STORED) as archive:
            archive.writestr('model/smartvoice-model.json',json.dumps(manifest,ensure_ascii=False))
            for name,path in files.items():archive.write(path,'model/'+name)
    except BaseException:
        destination.unlink(missing_ok=True);raise
    return destination


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('model');parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--file',action='append',default=[])
    args=parser.parse_args();mapping={}
    for item in args.file:
        key,separator,value=item.partition('=')
        if not separator or key in mapping:parser.error('Expected unique file-name=/path overrides')
        mapping[key]=Path(value)
    print(package(args.model,args.root,args.output,mapping))
if __name__=='__main__':main()
