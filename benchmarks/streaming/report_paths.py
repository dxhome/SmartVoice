"""Portable locations for reports that may be checked into the repository."""
import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath

REPO_ROOT = Path(__file__).resolve().parents[2]
PATH_KEYS = {'path', 'file', 'filename', 'audio', 'raw_report', 'full_session_report'}


def validate_relative_path(value):
    """Reject absolute, drive-relative, home-relative and escaping paths on any OS."""
    windows = PureWindowsPath(value)
    if (not value or PurePosixPath(value).is_absolute() or windows.drive or windows.root
            or value.startswith(('~', 'file:'))
            or '..' in PurePosixPath(value.replace('\\', '/')).parts):
        raise ValueError('Report path must be repository-relative without traversal')
    return value.replace('\\', '/')


def portable_location(path, repo_root=REPO_ROOT, *, artifact_id=None):
    """External locations expose an artifact identity, never their local directory."""
    if os.name != 'nt' and (PureWindowsPath(str(path)).drive or str(path).startswith('\\')):
        raise ValueError('Foreign Windows paths cannot be resolved on this platform')
    path = Path(path).resolve()
    try:
        return validate_relative_path(path.relative_to(Path(repo_root).resolve()).as_posix())
    except ValueError:
        if artifact_id is None:
            raise ValueError('External report location requires an artifact ID') from None
        return {'artifact_id': artifact_id, 'name': path.name, 'availability': 'external'}


def evidence_reference(path, repo_root=REPO_ROOT):
    path = Path(path)
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    return {'raw_report': portable_location(path, repo_root, artifact_id='sha256:'+checksum),
            'raw_report_sha256': checksum}


def _path_key(key):
    return key in PATH_KEYS or key.endswith(('_path', '_root', '_file', '_report', '_paths'))


def validate_report_paths(report):
    """Validate nested path fields and path-keyed checksum maps without changing text."""
    def visit(value, location='$', is_path=False):
        if isinstance(value, dict):
            for key, child in value.items():
                if location.endswith('.code_sha256'):
                    validate_relative_path(key)
                visit(child, location+'.'+key, _path_key(key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f'{location}[{index}]', is_path)
        elif isinstance(value, str) and is_path:
            try:
                validate_relative_path(value)
            except ValueError as exc:
                # Do not echo the sensitive path in validation diagnostics.
                raise ValueError('Nonportable report path at '+location) from exc
    visit(report)
    return report
