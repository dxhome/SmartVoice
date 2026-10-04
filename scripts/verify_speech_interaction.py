"""Fresh-process HTTP interaction probes; metadata only, no capacity certification.

Requires installed models and the project benchmark dependencies. No downloads
or persisted setting changes. Run without other model-backed workloads.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import logging
from pathlib import Path
import resource
import socket
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

ASR = 'stt-qwen3-asr-600m-int8'
TTS = 'tts-supertonic-v3-multilingual-int8'
CONFIGS = {'serial': (0, 1), 'parallel-one': (1, 1), 'parallel-two': (1, 2),
           'parallel-two-warm': (1, 2)}
MEMORY = {'sensevoice': 'stt-sensevoice-small-int8',
          'whisper': 'stt-whisper-base-multilingual-int8', 'qwen': ASR, 'supertonic': TTS}


def percentiles(values):
    """Nearest-rank summaries; six requests cannot establish a tail SLA."""
    import math
    values = sorted(values)
    return {f'p{p}': values[max(0, math.ceil(len(values)*p/100)-1)] for p in (50, 95, 99)}


def worker(label, repeat):
    import httpx
    import psutil
    import platform
    import sherpa_onnx
    import uvicorn
    from smartvoice.app import create_app
    from smartvoice.config.settings import Settings
    from tests.inference_environment import isolated_runtime
    from tests.stt_regression_audio import build_audio

    memory_model = MEMORY.get(label.removeprefix('memory-')) if label.startswith('memory-') else None
    parallel, instances = (1, 1) if memory_model else CONFIGS[label]
    source = replace(Settings.from_env(), max_concurrent_inference=parallel,
                     min_instances=1, max_instances=instances, max_queued_inference=8,
                     num_threads=2, inference_queue_timeout_seconds=120)
    process = psutil.Process()
    report = {'configuration': label, 'repeat': repeat, 'pid': process.pid,
              'platform': platform.platform(), 'native_runtime': sherpa_onnx.__version__,
              'parallel_enabled': parallel, 'max_instances': instances,
              'num_threads': 2, 'max_queued': 8, 'completed': False}
    with isolated_runtime(source) as (settings, provider):
        app = create_app(settings, provider)
        logging.getLogger('smartvoice.api').setLevel(logging.WARNING)
        sock = socket.socket(); sock.bind(('127.0.0.1', 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
        thread.start()
        try:
            until = time.monotonic()+30
            while not server.started:
                if not thread.is_alive() or time.monotonic()>until: raise RuntimeError('Server startup failed')
                time.sleep(.05)
            short_audio = (ROOT/'tests/fixtures/zh.wav').read_bytes()
            long_audio = build_audio(120, 'zh')
            with httpx.Client(base_url=f'http://127.0.0.1:{sock.getsockname()[1]}', timeout=180) as client:
                def request(kind, long=False, model=None):
                    began = time.perf_counter()
                    if kind == 'stt':
                        response = client.post('/v1/audio/transcriptions',
                            files={'file': ('probe.wav', long_audio if long else short_audio, 'audio/wav')},
                            data={'model': model or ASR, 'language': 'zh', 'response_format': 'verbose_json'})
                    else:
                        text = 'The next order is ready.'
                        response = client.post('/v1/audio/speech', json={'model': model or TTS,
                            'input': (text+' ')*40 if long else text, 'language': 'en'})
                    row = {'kind': kind, 'long': long, 'status': response.status_code,
                           'seconds': round(time.perf_counter()-began, 4)}
                    assert response.status_code == 200, row
                    if kind == 'stt':
                        result = response.json(); assert result['text'].strip()
                        row.update(chunks=result.get('chunk_count', 1), duration=result['duration'],
                                   queue_wait_seconds=result.get('queue_wait_seconds', 0),
                                   runtime_wait_seconds=result.get('runtime_wait_seconds', 0))
                    else:
                        assert response.headers['content-type'].startswith('audio/mpeg')
                        assert len(response.content)>100
                        row.update(chunks=int(response.headers['x-audio-segments']),
                                   duration=float(response.headers['x-audio-duration']),
                                   queue_wait_seconds=float(response.headers['x-queue-wait-seconds']),
                                   runtime_wait_seconds=float(response.headers['x-runtime-wait-seconds']))
                    return row

                report['rss_before_loading_mib'] = round(process.memory_info().rss/2**20, 2)
                if memory_model:
                    kind = 'tts' if memory_model == TTS else 'stt'
                    report['warmup'] = request(kind, model=memory_model)
                    report['rss_after_warmup_mib'] = round(process.memory_info().rss/2**20, 2)
                    report['long'] = request(kind, long=True, model=memory_model)
                    report['rss_after_long_mib'] = round(process.memory_info().rss/2**20, 2)
                else:
                    request('stt'); request('tts')
                    if label == 'parallel-two-warm':
                        with ThreadPoolExecutor(2) as executor:
                            warm = [executor.submit(request, 'stt') for _ in range(2)]
                            for job in warm: job.result()
                        until = time.monotonic()+30
                        while True:
                            pools = client.get('/v1/runtime').json()['backends']['sherpa-onnx']['instance_pools']
                            if pools[ASR]['ready'] == 2: break
                            if time.monotonic()>until: raise RuntimeError('Second instance was not warmed')
                            time.sleep(.05)
                    report['rss_after_warmup_mib'] = round(process.memory_info().rss/2**20, 2)
                    report['idle_short'] = [request(kind) for _ in range(3) for kind in ('stt', 'tts')]
                    with ThreadPoolExecutor(8) as executor:
                        began = time.monotonic()
                        long_job = executor.submit(request, 'stt', True)
                        # Wait for actual native admission so every short request
                        # arrives during the background operation, not startup.
                        until = time.monotonic()+10
                        while True:
                            pools = client.get('/v1/runtime').json()['backends']['sherpa-onnx']['instance_pools']
                            if pools.get(ASR, {}).get('active', 0): break
                            if long_job.done() or time.monotonic()>until: raise RuntimeError('No long native admission observed')
                            time.sleep(.01)
                        arrivals = time.monotonic()
                        jobs = []
                        for index in range(6):
                            time.sleep(max(0, arrivals+index*.3-time.monotonic()))
                            assert not long_job.done(), 'Background finished before all short arrivals'
                            jobs.append(executor.submit(request, 'stt' if index%2==0 else 'tts'))
                        report['short_during_long'] = [job.result() for job in jobs]
                        report['long'] = long_job.result()
                        report['workload_seconds'] = round(time.monotonic()-began, 3)
                    report['rss_after_workload_mib'] = round(process.memory_info().rss/2**20, 2)
                    report['short_latency'] = {kind: percentiles([r['seconds'] for r in report['short_during_long'] if r['kind']==kind])
                                               for kind in ('stt', 'tts')}
                runtime = client.get('/v1/runtime').json()
                pools = runtime['backends']['sherpa-onnx']['instance_pools']
                assert all(not p['active'] and not p['waiting'] for p in pools.values())
                assert app.state.inference_queue._reserved == 0
                report['final_pools'] = pools
                peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                report['process_peak_rss_mib'] = round(peak/(2**20 if sys.platform=='darwin' else 1024), 2)
                report['completed'] = True
        finally:
            server.should_exit = True; thread.join(30); sock.close()
            if thread.is_alive(): raise RuntimeError('Owned server did not stop')
    report['rss_after_shutdown_mib'] = round(process.memory_info().rss/2**20, 2)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--worker', choices=(*CONFIGS, *(f'memory-{key}' for key in MEMORY)))
    parser.add_argument('--repeat', type=int, default=2)
    parser.add_argument('--only', nargs='+', choices=(*CONFIGS, *(f'memory-{key}' for key in MEMORY)))
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args.worker, args.repeat)), flush=True); return
    if args.report is None: parser.error('--report is required')
    if args.repeat < 1: parser.error('--repeat must be positive')
    report = {'fixture': 'synthetic 120-second Chinese STT background, fixed short STT/English TTS arrivals',
              'arrival_interval_seconds': .3, 'short_count_per_round': 6,
              'limitations': 'Exploratory paired workloads, three observations per task per round; not a capacity or tail-latency guarantee. Memory includes the HTTP process.',
              'cases': [], 'completed': False}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    try:
        labels = [(label, repeat) for repeat in range(1, args.repeat+1) for label in CONFIGS]
        labels += [(f'memory-{key}', 1) for key in MEMORY]
        if args.only: labels = [(label, repeat) for label, repeat in labels if label in args.only]
        for label, repeat in labels:
            completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker', label, '--repeat', str(repeat)],
                                       capture_output=True, text=True, timeout=300)
            if completed.returncode:
                report['cases'].append({'configuration': label, 'repeat': repeat,
                                        'completed': False, 'worker_returncode': completed.returncode})
                raise RuntimeError(f'{label} failed: {completed.stderr[-3000:]}')
            row = json.loads(completed.stdout.strip().splitlines()[-1]); report['cases'].append(row)
            args.report.write_text(json.dumps(report, indent=2))
            print(json.dumps({'configuration': label, 'repeat': repeat, 'long_seconds': row['long']['seconds'],
                              'short_latency': row.get('short_latency'), 'peak_rss_mib': row['process_peak_rss_mib']}), flush=True)
        report['completed'] = True
    finally: args.report.write_text(json.dumps(report, indent=2))


if __name__ == '__main__': main()
