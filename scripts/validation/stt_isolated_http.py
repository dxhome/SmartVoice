"""Owned validation server process and private control pipe, never product API."""
from __future__ import annotations

from contextvars import ContextVar
import gc
import logging
from collections import Counter
import multiprocessing
import socket
import threading
import time
import weakref

request_key = ContextVar('validation_request_key', default=None)


def serve(connection, source, policy, profile_python):
    import psutil
    import uvicorn
    from dataclasses import replace
    from smartvoice.app import create_app
    from smartvoice.adapters.audio.input import BoundedAudioInput
    from scripts.validation.stt_validation_common import TemporaryStorageTrace, TraceStore
    from tests.inference_environment import isolated_runtime
    if profile_python:
        import tracemalloc
        tracemalloc.start(5)
    process = psutil.Process()
    try:
        while True:
            heap_before = tracemalloc.take_snapshot() if profile_python else None
            types_before = Counter(
                f'{type(obj).__module__}.{type(obj).__qualname__}' for obj in gc.get_objects()
            ) if profile_python else None
            rows, attempts, completed, after_cancel, operation_times = {}, {}, {}, {}, {}
            lock = threading.Lock()
            with TemporaryStorageTrace() as storage, isolated_runtime(source) as (settings, provider):
                app = create_app(settings, provider)
                logging.getLogger('smartvoice.api').setLevel(logging.CRITICAL)
                original_run = app.state.inference_queue.run
                async def timed_run(operation, *args, **kwargs):
                    key = request_key.get()
                    def timed_operation():
                        started = time.perf_counter()
                        try:return operation()
                        finally:
                            with lock:operation_times[key] = time.perf_counter()-started
                    return await original_run(timed_operation, *args, **kwargs)
                app.state.inference_queue.run = timed_run
                inner = BoundedAudioInput(settings.max_audio_seconds, **policy)

                class AudioTrace:
                    def prepare(self, audio):
                        from contextlib import contextmanager
                        @contextmanager
                        def prepared_context():
                            key = request_key.get()
                            with inner.prepare(audio) as prepared:
                                class Prepared:
                                    duration = prepared.duration
                                    def sample(self, seconds): return prepared.sample(seconds)
                                    def windows(self, seconds):
                                        import io, wave
                                        for window in prepared.windows(seconds):
                                            with wave.open(io.BytesIO(window.audio)) as wav:
                                                duration = wav.getnframes()/wav.getframerate()
                                            with lock:
                                                rows.setdefault(key, []).append({'start':window.start_seconds,
                                                    'duration':duration, 'overlap':window.overlap_seconds})
                                            yield window
                                yield Prepared()
                        return prepared_context()
                app.state.transcription_service.audio_input = AudioTrace()
                original = provider.transcribe
                def captured(*args, **kwargs):
                    from smartvoice.ports.inference_context import request_cancelled
                    key = request_key.get()
                    with lock:
                        attempts[key] = attempts.get(key, 0) + 1
                        cancelled = request_cancelled.get()
                        if cancelled is not None and cancelled.is_set():
                            after_cancel[key] = after_cancel.get(key, 0) + 1
                    result = original(*args, **kwargs)
                    with lock: completed[key] = completed.get(key, 0) + 1
                    return result
                provider.transcribe = captured
                timings = TraceStore()
                app.add_middleware(timings.middleware())
                class RequestContext:
                    def __init__(self, app): self.app = app
                    async def __call__(self, scope, receive, send):
                        if scope['type'] != 'http':
                            return await self.app(scope, receive, send)
                        key = dict(scope.get('headers', [])).get(b'x-request-id', b'').decode('ascii')
                        token = request_key.set(key)
                        try: return await self.app(scope, receive, send)
                        finally: request_key.reset(token)
                app.add_middleware(RequestContext)
                sock = socket.socket(); sock.bind(('127.0.0.1', 0))
                server = uvicorn.Server(uvicorn.Config(app, log_level='critical'))
                thread = threading.Thread(target=server.run, kwargs={'sockets':[sock]}, daemon=True)
                thread.start()
                action = 'close'
                try:
                    deadline = time.monotonic() + 30
                    while not server.started:
                        if not thread.is_alive() or time.monotonic() > deadline:
                            raise RuntimeError('Owned server did not start')
                        time.sleep(.02)
                    connection.send({'ready':True, 'url':f'http://127.0.0.1:{sock.getsockname()[1]}',
                                     'pid':process.pid, 'rss_mib':process.memory_info().rss/2**20})
                    while True:
                        command = connection.recv()
                        action = command['action']
                        if action in ('close','restart'):
                            pool = provider.providers['sherpa-onnx'].pool
                            refs = [weakref.ref(i.runtime) for group in pool._groups.values() for i in group.instances]
                            break
                        if action == 'timeout':
                            app.state.settings = settings if command['seconds'] is None else replace(
                                settings, inference_execution_timeout_seconds=command['seconds'])
                            connection.send({'updated':True})
                        elif action == 'snapshot':
                            with lock:
                                snapshot = {key:{'windows':list(rows.get(key, [])),
                                    'adapter_attempts':attempts.get(key, 0),
                                    'adapter_completed':completed.get(key, 0),
                                    'attempts_after_cancellation':after_cancel.get(key, 0),
                                    'operation_wall_seconds':operation_times.get(key),
                                    'stages':timings.snapshot(key)} for key in command['keys']}
                            connection.send({'requests':snapshot, 'reserved':app.state.inference_queue._reserved,
                                             'audio_spools':storage.snapshot()})
                        elif action == 'memory':
                            reply = {'rss_mib':process.memory_info().rss/2**20}
                            if profile_python:
                                reply['python_traced_bytes'], reply['python_peak_bytes'] = tracemalloc.get_traced_memory()
                            connection.send(reply)
                        else:
                            raise ValueError('Unknown private validation command')
                finally:
                    server.should_exit = True
                    thread.join(30)
                    sock.close()
                    if thread.is_alive(): raise RuntimeError('Owned server did not stop')
                spools = storage.assert_closed()
            gc.collect()
            remaining = sum(ref() is not None for ref in refs)
            tracked_growth = []
            if profile_python:
                types_after = Counter(
                    f'{type(obj).__module__}.{type(obj).__qualname__}' for obj in gc.get_objects()
                )
                tracked_growth = [{'type':kind, 'count_diff':count}
                                  for kind, count in (types_after-types_before).most_common(12)]
                del types_after, types_before
            heap_growth = []
            if profile_python:
                heap_after = tracemalloc.take_snapshot()
                heap_diffs = heap_after.compare_to(heap_before, 'lineno')
                stat = frame = None
                for stat in heap_diffs:
                    if stat.size_diff > 0:
                        frame = stat.traceback[0]
                        heap_growth.append({'site': f'{frame.filename}:{frame.lineno}',
                                            'size_diff_bytes': stat.size_diff,
                                            'count_diff': stat.count_diff,
                                            'traceback': [f'{item.filename}:{item.lineno}'
                                                          for item in stat.traceback]})
                    if len(heap_growth) == 8: break
                del heap_diffs, heap_before, heap_after, stat, frame
                gc.collect()
            assert remaining == 0
            assert not settings.data_dir.exists()
            cleanup = {'stopped':True, 'adapter_objects_remaining':remaining,
                       'isolated_state_removed':True, 'audio_spools':spools,
                       'rss_after_shutdown_mib':process.memory_info().rss/2**20}
            if profile_python:
                cleanup['python_traced_bytes'], cleanup['python_peak_bytes'] = tracemalloc.get_traced_memory()
                cleanup['python_allocation_growth'] = heap_growth
                cleanup['python_gc_tracked_growth'] = tracked_growth
            connection.send(cleanup)
            if action == 'close': break
    except BaseException as exc:
        try: connection.send({'fatal':type(exc).__name__, 'message':str(exc)[:200]})
        except (BrokenPipeError, EOFError): pass
        raise
    finally:
        connection.close()


class OwnedServer:
    def __init__(self, settings, policy, profile_python=False):
        context = multiprocessing.get_context('spawn')
        self.connection, child = context.Pipe()
        self.process = context.Process(target=serve, args=(child, settings, policy, profile_python))
        self.process.start()
        child.close()
        try:
            self.ready = self.receive()
        except BaseException:
            self.process.terminate(); self.process.join(10); self.connection.close()
            raise
        self.url = self.ready['url']
        self.closed = False

    def receive(self):
        if not self.connection.poll(120): raise TimeoutError('Validation server control timed out')
        row = self.connection.recv()
        if 'fatal' in row: raise RuntimeError(f"Validation server failed: {row['fatal']}: {row['message']}")
        return row

    def command(self, action, **payload):
        self.connection.send({'action':action, **payload})
        return self.receive()

    def restart(self):
        cleanup = self.command('restart')
        self.ready = self.receive()
        self.url = self.ready['url']
        return cleanup

    def close(self):
        if self.closed: return {}
        try:
            cleanup = self.command('close')
            self.process.join(30)
            if self.process.is_alive(): raise RuntimeError('Validation process did not exit')
            if self.process.exitcode: raise RuntimeError('Validation process failed')
            return cleanup
        finally:
            self.closed = True
            if self.process.is_alive():
                self.process.terminate(); self.process.join(10)
            self.connection.close()
