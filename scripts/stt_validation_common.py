"""Shared isolated validation metadata and opt-in ASGI stage collection."""
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import io
import json
from pathlib import Path
import platform
import subprocess
import threading
import time
import wave

from smartvoice.ports.diagnostics import StageTimings, stage_timings

ROOT = Path(__file__).resolve().parents[1]


def fingerprint(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def provenance(settings):
    diff = subprocess.check_output(['git', 'diff', 'HEAD'], cwd=ROOT)
    status = subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT).decode()
    paths = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard'], cwd=ROOT).decode().splitlines()
    code = {p: fingerprint(ROOT/p) for p in paths if p.endswith('.py') and (ROOT/p).is_file()}
    configuration = asdict(settings)
    # Absolute personal directories are not needed to reproduce limits.
    configuration.pop('data_dir', None)
    return {'recorded_at_utc':datetime.now(timezone.utc).isoformat(),'git_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT).decode().strip(),
            'diff_sha256': hashlib.sha256(diff).hexdigest(), 'diff_summary': subprocess.check_output(['git','diff','HEAD','--stat'],cwd=ROOT).decode(), 'worktree_status': status.splitlines(),
            'python_files_sha256': code, 'platform': platform.platform(),
            'dependencies': {p: version(p) for p in ('sherpa-onnx', 'av', 'numpy', 'httpx', 'fastapi', 'psutil')},
            'edit_distance': 'rapidfuzz '+version('rapidfuzz') if __import__('importlib.util',fromlist=['find_spec']).find_spec('rapidfuzz') else 'Python exact fallback',
            'settings': {k: str(v) if isinstance(v, Path) else v for k,v in configuration.items()},
            'models': {p.parent.name: {'manifest_sha256': fingerprint(p), 'expected_files_sha256': json.loads(p.read_text()).get('file_sha256', {})} for p in sorted(settings.models_dir.glob('*/smartvoice-model.json'))}}


class TraceStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._rows = {}

    def middleware(self):
        store = self
        class TimingMiddleware:
            def __init__(self, app): self.app = app
            async def __call__(self, scope, receive, send):
                if scope['type'] != 'http':
                    return await self.app(scope, receive, send)
                headers = dict(scope.get('headers', []))
                key = headers.get(b'x-request-id', b'').decode('ascii', errors='replace')
                if not key:
                    return await self.app(scope, receive, send)
                timings = StageTimings(); token = stage_timings.set(timings)
                with store._lock: store._rows[key] = timings
                try:
                    return await self.app(scope, receive, send)
                finally:
                    # Keep collector objects: cancelled native workers may still
                    # finish after the HTTP response. Snapshot only after drain.
                    with store._lock: store._rows[key] = timings
                    stage_timings.reset(token)
        return TimingMiddleware

    def snapshot(self, key):
        with self._lock: collector = self._rows.get(key)
        return collector.snapshot() if collector else {}


class WindowTrace:
    def __init__(self, inner): self.inner = inner; self.rows = []
    @contextmanager
    def prepare(self, audio):
        self.rows.clear()
        with self.inner.prepare(audio) as prepared:
            trace = self.rows
            class Prepared:
                duration = prepared.duration
                def sample(self, seconds): return prepared.sample(seconds)
                def windows(self, seconds):
                    for window in prepared.windows(seconds):
                        with wave.open(io.BytesIO(window.audio), 'rb') as source:
                            duration = source.getnframes()/source.getframerate()
                        trace.append({'start': window.start_seconds, 'duration': duration,
                                      'overlap': window.overlap_seconds})
                        yield window
            yield Prepared()


def check_coverage(windows, duration, chunks):
    if chunks == 1 and not windows: return duration
    if len(windows) != chunks: raise AssertionError('Native window/response count mismatch')
    end = 0
    for window in windows:
        start, new_end = window['start'], window['start']+window['duration']
        if start > end+1e-5 or new_end <= end: raise AssertionError('Gap or non-progressing coverage')
        end = new_end
    if abs(end-duration) > .001: raise AssertionError('Final coverage does not reach input end')
    return end


class TemporaryStorageTrace:
    """Observe explicit closure of validation-owned audio spools, including spill files."""
    def __init__(self):
        self._lock = threading.Lock()
        self._files = []

    def __enter__(self):
        import tempfile
        from unittest.mock import patch
        original = tempfile.SpooledTemporaryFile
        def tracked(*args, **kwargs):
            result = original(*args, **kwargs)
            with self._lock: self._files.append(result)
            return result
        self._patch = patch('tempfile.SpooledTemporaryFile', tracked)
        self._upload_patch = patch('starlette.formparsers.SpooledTemporaryFile', tracked)
        self._patch.start(); self._upload_patch.start()
        return self

    def snapshot(self):
        with self._lock:
            return {'created': len(self._files), 'spilled': sum(bool(f._rolled) for f in self._files),
                    'open': sum(not f.closed for f in self._files)}

    def assert_closed(self):
        snapshot = self.snapshot()
        if snapshot['open']: raise AssertionError('Audio spool still open after native drain')
        return snapshot

    def __exit__(self, *exception):
        self._upload_patch.stop(); self._patch.stop()
        for file in self._files: file.close()
