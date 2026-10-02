"""Fixed-arrival-rate benchmark of the actual production FastAPI endpoints."""
import argparse, concurrent.futures, hashlib, io, json, math, os, platform, socket, statistics, subprocess, sys, threading, time, wave
from pathlib import Path
import httpx, psutil
from smartvoice.config.settings import default_data_dir
SCRIPT_ROOT = Path(__file__).resolve().parent
ROOT = Path('.smartvoice-dev/benchmark-concurrency').resolve()
ROOT.mkdir(parents=True, exist_ok=True)
BENCHMARK_CONFIG = json.loads((SCRIPT_ROOT / 'scenarios.json').read_text())
CONFIG = BENCHMARK_CONFIG['scenarios']
SUCCESS_RATE_REQUIRED = float(BENCHMARK_CONFIG.get('success_rate_required', 0.999))
P90_RATIO_LIMIT = float(BENCHMARK_CONFIG.get('p90_ratio_limit', 1.2))
WINDOW_SECONDS = float(BENCHMARK_CONFIG.get('window_seconds', 10))
SCREENING_SECONDS = float(BENCHMARK_CONFIG.get('screening_seconds', 30))
CONFIRMATION_REQUESTS = int(BENCHMARK_CONFIG.get('confirmation_requests', 3000))
CLIENT_WORKERS = int(BENCHMARK_CONFIG.get('client_workers', 64))
P = lambda xs, q=0.9: sorted(xs)[min(len(xs) - 1, math.ceil(q * len(xs)) - 1)] if xs else None

class Server:

    def __init__(self, scenario, idle_seconds=300):
        self.spec = CONFIG[scenario]
        self.model = self.spec['model_id']
        self.scenario = scenario
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        self.log = (ROOT / (scenario + '-server.log')).open('a')
        self.child = subprocess.Popen([sys.executable, str(SCRIPT_ROOT / 'server.py'), str(port), scenario, str(idle_seconds)], stdout=self.log, stderr=self.log)
        self.process = psutil.Process(self.child.pid)
        self.url = f'http://127.0.0.1:{port}'
        self.client = httpx.Client(base_url=self.url, timeout=120, limits=httpx.Limits(max_connections=64, max_keepalive_connections=64))
        self.samples = []
        self.stop = threading.Event()
        self.sampler = threading.Thread(target=self.sample, daemon=True)
        self.sampler.start()
        deadline = time.monotonic() + 60
        while True:
            try:
                if self.client.get('/health').status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline or self.child.poll() is not None:
                raise RuntimeError('server startup failed')
            time.sleep(0.05)
        runtime_response = self.client.get('/v1/runtime')
        runtime_response.raise_for_status()
        runtime = runtime_response.json()
        manifest_path = default_data_dir() / 'models' / self.model / 'smartvoice-model.json'
        try:
            manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        except OSError:
            manifest_digest = None
        runtime_backend = (runtime.get('backends') or {}).get('sherpa-onnx', {})
        self.metadata = {
            'model_id': self.model,
            'task': self.spec['task'],
            'language': self.spec['language'],
            'model_manifest_sha256': manifest_digest,
            'platform': {'system': platform.platform(), 'machine': platform.machine(),
                         'logical_cpu_count': psutil.cpu_count(),
                         'physical_memory_bytes': psutil.virtual_memory().total},
            'service_pool_limits': runtime_backend.get('pool_limits'),
            'resource_measurement_scope': 'server_process_tree_when_accessible; Sherpa inference runs in the server process',
            'benchmark_settings': {'success_rate_required': SUCCESS_RATE_REQUIRED,
                                   'p90_ratio_limit': P90_RATIO_LIMIT,
                                   'window_seconds': WINDOW_SECONDS,
                                   'screening_seconds': SCREENING_SECONDS,
                                   'confirmation_requests': CONFIRMATION_REQUESTS,
                                   'client_workers': CLIENT_WORKERS},
        }
        self.idle_rss = self.process_tree_snapshot()['rss_bytes'] / 2 ** 20
        self.texts = self.spec.get('texts', CONFIG['tts'].get('texts') or ['你好，欢迎使用本地语音服务。', '今天的天气很好，我们一起出去走走吧。', '语音识别和语音合成可以在本地完成。', '这是一次并发性能测试，请稍等片刻。'])
        self.reference_texts = None
        audio_files = self.spec.get('audio_files') or [f'samples/multilingual-stt/zh_0{i}.wav' for i in range(1, 4)]
        self.audios = [Path(path).read_bytes() for path in audio_files]
        fixture_hashes = [hashlib.sha256(audio).hexdigest() for audio in self.audios]
        if self.spec['task'] == 'tts':
            fixture_hashes.extend(hashlib.sha256(text.encode('utf-8')).hexdigest() for text in self.texts)
        self.metadata['fixture_sha256'] = fixture_hashes

    def process_tree_snapshot(self):
        tree_complete = True
        try:
            processes = [self.process, *self.process.children(recursive=True)]
        except (psutil.Error, OSError):
            processes = [self.process]
            tree_complete = False
        cpu_seconds = rss_bytes = 0
        for process in processes:
            try:
                cpu = process.cpu_times()
                cpu_seconds += cpu.user + cpu.system
                rss_bytes += process.memory_info().rss
            except psutil.Error:
                continue
        return {'cpu_seconds': cpu_seconds, 'rss_bytes': rss_bytes, 'process_count': len(processes),
                'process_tree_complete': tree_complete}

    def sample(self):
        while not self.stop.wait(0.1):
            try:
                snapshot = self.process_tree_snapshot()
                self.samples.append((time.monotonic(), snapshot['rss_bytes'] / 2 ** 20,
                                     snapshot['cpu_seconds'], snapshot['process_tree_complete']))
            except (psutil.Error, OSError):
                pass

    def snapshot(self):
        return self.client.get('/v1/runtime').json()['backends']['sherpa-onnx']['instance_pools']

    def request(self, i, scheduled=None, save=False):
        start = time.monotonic()
        scheduled = start if scheduled is None else scheduled
        try:
            if self.spec['task'] == 'asr':
                response = self.client.post('/v1/audio/transcriptions', data={'model': self.model, 'language': self.spec['language']}, files={'file': ('sample.wav', self.audios[i % len(self.audios)], 'audio/wav')})
            else:
                response = self.client.post('/v1/audio/speech', json={'model': self.model, 'language': self.spec['language'], 'input': self.texts[i % len(self.texts)]})
            ended = time.monotonic()
            status = response.status_code
            wait = None
            valid = False
            if status == 200:
                if self.spec['task'] == 'asr':
                    obj = response.json()
                    valid = bool(obj.get('text')) and (self.reference_texts is None or obj['text'] == self.reference_texts[i % len(self.audios)])
                    wait = obj.get('runtime_wait_seconds')
                    signature = obj['text']
                else:
                    with wave.open(io.BytesIO(response.content), 'rb') as wav:
                        frames = wav.getnframes()
                        pcm = wav.readframes(frames)
                        valid = frames > 0 and wav.getsampwidth() == 2 and wav.getnchannels() == 1 and len(pcm) == frames * 2 and any(pcm)
                        signature = [wav.getframerate(), wav.getnframes()]
                    wait = float(response.headers['x-runtime-wait-seconds'])
                if save:
                    path = ROOT / f"{self.scenario}-sample-{i}.{('json' if self.spec['task'] == 'asr' else 'wav')}"
                    path.write_bytes(response.content)
            else:
                signature = response.text[:300]
            return {'i': i, 'status': status, 'valid': valid, 'latency': ended - scheduled, 'http_latency': ended - start, 'client_lag': start - scheduled, 'wait': wait, 'signature': signature}
        except Exception as exc:
            return {'i': i, 'status': 0, 'valid': False, 'latency': time.monotonic() - scheduled, 'error': repr(exc)}

    def resources(self, start, end):
        samples = [s for s in self.samples if start <= s[0] <= end]
        if len(samples) < 2:
            return None
        cpu = (samples[-1][2] - samples[0][2]) / (samples[-1][0] - samples[0][0])
        deltas = [(b[2] - a[2]) / (b[0] - a[0]) for a, b in zip(samples, samples[1:])]
        return {'cpu_mean_cores': cpu, 'cpu_p90_cores': P(deltas),
                'rss_mean_mib': statistics.mean((s[1] for s in samples)),
                'rss_peak_mib': max((s[1] for s in samples)),
                'process_tree_complete': all(s[3] for s in samples)}

    def run_rate(self, rate, count, label, abort_overload=False):
        start = time.monotonic()
        rows = []
        with concurrent.futures.ThreadPoolExecutor(CLIENT_WORKERS) as executor:
            futures = []
            for i in range(count):
                target = start + i / rate
                time.sleep(max(0, target - time.monotonic()))
                futures.append(executor.submit(self.request, i, target, i < 4))
                if label.startswith('confirmation') and i % 100 == 99:
                    completed = [f.result() for f in futures if f.done()]
                    successful = [r for r in completed if r['status'] == 200 and r['valid']]
                    print('Progress', self.scenario, len(completed), '/', count,
                          'failures', len(completed) - len(successful),
                          'p90', P([r['latency'] for r in successful]), flush=True)
                if abort_overload and i % 10 == 0:
                    failed = sum(f.done() and not (f.result()['status'] == 200 and f.result()['valid']) for f in futures)
                    if failed > math.floor(count * (1 - SUCCESS_RATE_REQUIRED) + 1e-9):
                        break
            rows = [f.result() for f in futures]
        end = time.monotonic()
        ok = [r for r in rows if r['status'] == 200 and r['valid']]
        from collections import Counter
        out = {'metadata': self.metadata, 'label': label, 'offered_rps': rate, 'requests': len(rows), 'planned_requests': count, 'success': len(ok), 'success_rate': len(ok) / len(rows), 'status_counts': dict(Counter((r['status'] for r in rows))), 'elapsed': end - start, 'delivered_rps': len(ok) / (end - start), 'p90': P([r['latency'] for r in ok]), 'p90_all': P([r['latency'] for r in rows]), 'wait_p90': P([r['wait'] for r in ok]), 'client_lag_p90': P([r.get('client_lag', 0) for r in rows]), 'resources': self.resources(start, end), 'pool': self.snapshot(), 'rows': rows}
        out['windows'] = []
        for offset in range(0, math.ceil(count / rate / WINDOW_SECONDS) * int(WINDOW_SECONDS), int(WINDOW_SECONDS)):
            window = [r for r in rows if offset <= r['i'] / rate < offset + WINDOW_SECONDS]
            good = [r for r in window if r['status'] == 200 and r['valid']]
            out['windows'].append({'start_seconds': offset, 'requests': len(window),
                                   'success': len(good), 'p90': P([r['latency'] for r in good]),
                                   'wait_p90': P([r['wait'] for r in good])})
        (ROOT / f'{self.scenario}-{label}.json').write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({k: v for k, v in out.items() if k not in ['rows', 'pool', 'windows']}, ensure_ascii=False), flush=True)
        return out

    def close(self):
        self.child.terminate()
        try:
            self.child.wait(60)
        except subprocess.TimeoutExpired:
            self.child.kill()
            self.child.wait()
        self.stop.set()
        self.sampler.join()
        self.client.close()
        self.log.close()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('scenario', choices=CONFIG)
    ap.add_argument('--rates', type=float, nargs='+')
    ap.add_argument('--count', type=int)
    ap.add_argument('--warm-trials', type=int, default=0)
    ap.add_argument('--label', default='screen')
    ap.add_argument('--abort-overload', action='store_true')
    ap.add_argument('--idle-trial', action='store_true')
    args = ap.parse_args()
    if args.count is not None and args.count < 1:
        ap.error('--count must be positive')
    if any(not math.isfinite(rate) or rate <= 0 for rate in args.rates or []):
        ap.error('--rates must be finite and positive')
    if args.idle_trial:
        server = Server(args.scenario, idle_seconds=1)
        try:
            server.request(0)
            with concurrent.futures.ThreadPoolExecutor(2) as ex:
                [f.result() for f in [ex.submit(server.request, i) for i in range(2)]]
            before = server.snapshot()
            before_rss = server.process_tree_snapshot()['rss_bytes'] / 2**20
            deadline = time.monotonic() + 10
            after = before
            while after[server.model]['instances'] != 1 and time.monotonic() < deadline:
                time.sleep(0.1)
                after = server.snapshot()
            if before[server.model]['instances'] != 2 or after[server.model]['instances'] != 1:
                raise RuntimeError('idle reclamation did not occur')
            after_rss = server.process_tree_snapshot()['rss_bytes'] / 2**20
            request = server.request(1)
            result = {'idle_seconds': 1, 'before': before, 'after': after,
                      'before_rss_mib': before_rss, 'after_rss_mib': after_rss,
                      'resident_request': request, 'final': server.snapshot()}
            (ROOT / f'{args.scenario}-idle.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
            print(json.dumps(result, ensure_ascii=False), flush=True)
        finally:
            server.close()
        return
    if args.warm_trials:
        trials = []
        for trial in range(args.warm_trials):
            server = Server(args.scenario)
            try:
                start = time.monotonic()
                first = server.request(trial, save=trial < 4)
                if first['status'] != 200 or not first['valid']:
                    raise RuntimeError(f'cold request failed: {first}')
                first_end = time.monotonic()
                for i in range(8):
                    server.request(i)
                one_rss = server.process_tree_snapshot()['rss_bytes'] / 2 ** 20
                expansion_start = time.monotonic()
                with concurrent.futures.ThreadPoolExecutor(2) as ex:
                    burst = [f.result() for f in [ex.submit(server.request, i, save=trial == 0) for i in range(2)]]
                if not all(row['status'] == 200 and row['valid'] for row in burst):
                    raise RuntimeError('expansion request failed')
                expanded_end = time.monotonic()
                snapshot = server.snapshot()
                group = snapshot[server.model]
                if group['instances'] != 2:
                    raise RuntimeError('did not expand')
                warm = []
                with concurrent.futures.ThreadPoolExecutor(2) as ex:
                    for i in range(12):
                        pair = [f.result() for f in [ex.submit(server.request, i), ex.submit(server.request, i)]]
                        if i == 3:
                            fully_warm_start = time.monotonic()
                        if i >= 4:
                            warm.extend(pair)
                trial_out = {'metadata': server.metadata, 'first_request': first, 'first_resources': server.resources(start, first_end), 'one_rss_mib': one_rss, 'baseline_rss_mib': server.idle_rss, 'expansion_requests': burst, 'two_rss_mib': server.process_tree_snapshot()['rss_bytes'] / 2 ** 20, 'expansion_resources': server.resources(expansion_start, expanded_end), 'one_warm_resources': server.resources(first_end, expansion_start), 'two_warm_resources': server.resources(fully_warm_start, time.monotonic()), 'warm_requests': warm, 'warmups': group['warmups']}
                trials.append(trial_out)
                print(args.scenario, 'warm trial', trial + 1, 'cold', round(first['latency'], 3), 'expansion', round(group['warmups'][-1]['seconds'], 3), flush=True)
                (ROOT / f'{args.scenario}-warm-trials.json').write_text(json.dumps(trials, ensure_ascii=False, indent=2) + '\n')
            finally:
                server.close()
        return
    server = Server(args.scenario)
    try:
        server.request(0)
        with concurrent.futures.ThreadPoolExecutor(2) as ex:
            [f.result() for f in [ex.submit(server.request, i) for i in range(2)]]
        with concurrent.futures.ThreadPoolExecutor(2) as ex:
            for i in range(8):
                [f.result() for f in [ex.submit(server.request, i), ex.submit(server.request, i)]]
        baseline_label = 'baseline-' + args.label + '-' + '_'.join(f'{rate:g}' for rate in args.rates or [])
        baseline = server.run_rate(float(server.spec.get('baseline_rps', 0.5 if args.scenario == 'supertonic' else 2)), int(BENCHMARK_CONFIG.get('baseline_requests', 40)), baseline_label)
        if server.spec['task'] == 'asr':
            server.reference_texts = {row['i'] % len(server.audios): row['signature'] for row in baseline['rows'] if row['valid']}
        for rate in args.rates or []:
            count = args.count or max(60, math.ceil(rate * SCREENING_SECONDS))
            result = server.run_rate(rate, count, f'{args.label}-{rate:g}', args.abort_overload)
            result['baseline_p90'] = baseline['p90']
            result['p90_ratio'] = result['p90'] / baseline['p90'] if baseline['p90'] else None
            windows = [window for window in result['windows'] if window['requests']]
            first_window = windows[0] if windows else None
            last_window = windows[-1] if windows else None
            result['window_trend_ok'] = bool(first_window and last_window) and (
                len(windows) == 1 or (
                    last_window['p90'] is not None and first_window['p90'] is not None
                    and last_window['p90'] <= max(first_window['p90'] * P90_RATIO_LIMIT, baseline['p90'] * P90_RATIO_LIMIT)
                    and last_window['wait_p90'] is not None and first_window['wait_p90'] is not None
                    and last_window['wait_p90'] <= max(first_window['wait_p90'] * P90_RATIO_LIMIT, 0.05)
                )
            )
            final_pool = server.snapshot()
            result['pool_drained'] = all(group['waiting'] == 0 and group['active'] == 0 for group in final_pool.values())
            result['stable'] = bool(
                result['success_rate'] >= SUCCESS_RATE_REQUIRED and result['p90_ratio'] is not None
                and result['p90_ratio'] <= P90_RATIO_LIMIT and result['window_trend_ok'] and result['pool_drained']
            )
            (ROOT / f'{args.scenario}-{args.label}-{rate:g}.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
            print('stable=', result['stable'], 'p90_ratio=', round(result['p90_ratio'], 3) if result['p90_ratio'] is not None else None, flush=True)
    finally:
        server.close()
if __name__ == '__main__':
    main()
