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
CONFIRMATION_MIN_REQUESTS = int(BENCHMARK_CONFIG.get('confirmation_min_requests', 500))
CONFIRMATION_MAX_REQUESTS = int(BENCHMARK_CONFIG.get('confirmation_max_requests', 2000))
CONFIRMATION_MAX_ROUND_SECONDS = float(BENCHMARK_CONFIG.get('confirmation_max_round_seconds', 1800))
CONFIRMATION_OVERHEAD_RESERVE_SECONDS = float(BENCHMARK_CONFIG.get('confirmation_overhead_reserve_seconds', 240))
CLIENT_WORKERS = int(BENCHMARK_CONFIG.get('client_workers', 64))
P = lambda xs, q=0.9: sorted(xs)[min(len(xs) - 1, math.ceil(q * len(xs)) - 1)] if xs else None

class Server:

    def __init__(self, scenario, idle_seconds=300, *, num_steps=None,
                 threads_per_instance=None, instances=None, artifact_tag=None):
        self.spec = CONFIG[scenario]
        self.model = self.spec['model_id']
        self.scenario = scenario
        self.artifact_tag = artifact_tag
        self.backend = self.spec.get('backend', 'sherpa-onnx')
        self._request_state_lock = threading.Lock()
        self._successful_request_count = 0
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        self.log = (ROOT / (scenario + '-server.log')).open('a')
        server_args = [sys.executable, str(SCRIPT_ROOT / 'server.py'), str(port), scenario, str(idle_seconds)]
        for flag, value in (('--num-steps', num_steps),
                            ('--threads-per-instance', threads_per_instance),
                            ('--instances', instances)):
            if value is not None:
                server_args.extend([flag, str(value)])
        self.child = subprocess.Popen(server_args, stdout=self.log, stderr=self.log)
        self.process = psutil.Process(self.child.pid)
        self.url = f'http://127.0.0.1:{port}'
        self.client = httpx.Client(base_url=self.url, timeout=float(self.spec.get('client_timeout_seconds', 120)), limits=httpx.Limits(max_connections=64, max_keepalive_connections=64))
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
        runtime_backend = (runtime.get('backends') or {}).get(self.backend, {})
        self.runtime_backend = runtime_backend
        self.metadata = {
            'model_id': self.model,
            'task': self.spec['task'],
            'language': self.spec['language'],
            'backend': self.backend,
            'model_manifest_sha256': manifest_digest,
            'platform': {'system': platform.platform(), 'machine': platform.machine(),
                         'logical_cpu_count': psutil.cpu_count(),
                         'physical_memory_bytes': psutil.virtual_memory().total},
            'service_pool_limits': runtime_backend.get('pool_limits') or (
                None if self.spec.get('execution_model') == 'single_native_engine_serialized' else {
                    'min_instances': int(self.spec.get('min_instances', 1)),
                    'max_instances': int(self.spec.get('instances', 1)),
                    'max_waiting': int(self.spec.get('max_waiting', 0)),
                    'threads_per_instance': int(self.spec.get('threads_per_instance', 1)),
                }
            ),
            'execution_model': self.spec.get('execution_model', 'elastic_instance_pool'),
            'runtime_execution_limits': ({
                'native_engine_processes': 1,
                'serialized_inference': True,
                'native_threads': int(self.spec.get('threads_per_instance', 1)),
                'max_waiting_inferences': int(self.spec.get('max_waiting', 0)),
            } if self.spec.get('execution_model') == 'single_native_engine_serialized' else None),
            'resource_measurement_scope': (
                'server process plus native engine child when process-tree access permits; verify process_tree_complete'
                if self.spec.get('execution_model') == 'single_native_engine_serialized'
                else 'server process tree when accessible; model inference runs in the server process'
            ),
            'benchmark_settings': {'success_rate_required': SUCCESS_RATE_REQUIRED,
                                   'p90_ratio_limit': P90_RATIO_LIMIT,
                                   'window_seconds': WINDOW_SECONDS,
                                   'screening_seconds': SCREENING_SECONDS,
                                   'confirmation_request_range': [CONFIRMATION_MIN_REQUESTS, CONFIRMATION_MAX_REQUESTS],
                                   'confirmation_max_round_seconds': CONFIRMATION_MAX_ROUND_SECONDS,
                                   'client_workers': CLIENT_WORKERS,
                                   'experimental_overrides': {
                                       key: value for key, value in {
                                           'supertonic_num_steps': num_steps,
                                           'threads_per_instance': threads_per_instance,
                                           'max_instances': instances,
                                       }.items() if value is not None
                                   }},
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
        backend = self.client.get('/v1/runtime').json()['backends'][self.backend]
        if 'instance_pools' in backend:
            return backend['instance_pools']
        if self.backend == 'qwen-tts':
            children = []
            try:
                children = self.process.children(recursive=True)
            except (psutil.Error, OSError):
                pass
            native_running = False
            for child in children:
                try:
                    native_running = native_running or (
                        child.is_running()
                        and 'qwen_tts' in (child.name().lower() + ' ' + ' '.join(child.cmdline()).lower())
                    )
                except (psutil.Error, OSError):
                    continue
            with self._request_state_lock:
                engine_started = self._successful_request_count > 0
            engine_ready = native_running or engine_started
            return {self.model: {
                'instances': int(engine_ready), 'ready': int(engine_ready),
                'active': 0, 'waiting': 0, 'peak_active': int(engine_ready),
                'native_engine_process_observed': native_running, 'serialized_execution': True,
                'warmups': [],
            }}
        return {self.model: {'instances': 0, 'ready': 0, 'active': 0, 'waiting': 0,
                             'peak_active': 0, 'warmups': []}}

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
                    if valid:
                        with self._request_state_lock:
                            self._successful_request_count += 1
                if save:
                    tag = f'-{self.artifact_tag}' if self.artifact_tag else ''
                    path = ROOT / f"{self.scenario}{tag}-sample-{i}.{('json' if self.spec['task'] == 'asr' else 'wav')}"
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
        early_stop_reason = None
        latency_overload_streak = 0
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
                        early_stop_reason = 'invalid_responses_exceeded_success_budget'
                        print('Early stop:', failed, 'invalid responses exceed the allowed count.', flush=True)
                        break
                    scheduled_elapsed = i / rate
                    baseline_p90 = getattr(self, 'baseline_p90', None)
                    if baseline_p90 and scheduled_elapsed >= 30:
                        recent = [f.result() for f in futures if f.done()
                                  and f.result().get('latency') is not None
                                  and f.result()['i'] / rate >= scheduled_elapsed - 30]
                        if len(recent) >= max(10, math.ceil(rate * 20)):
                            recent_p90 = P([row['latency'] for row in recent])
                            if recent_p90 > baseline_p90 * P90_RATIO_LIMIT * 1.10:
                                latency_overload_streak += 1
                            else:
                                latency_overload_streak = 0
                            if latency_overload_streak >= 10:
                                early_stop_reason = 'sustained_30_second_p90_exceeded_110_percent_of_limit'
                                print('Early stop:', len(recent), 'recent responses repeatedly exceed the latency limit; latest 30-second P90',
                                      round(recent_p90, 3), 'sustained for ten checks.', flush=True)
                                break
            rows = [f.result() for f in futures]
        end = time.monotonic()
        ok = [r for r in rows if r['status'] == 200 and r['valid']]
        from collections import Counter
        delivered_rps = len(ok) / (end - start)
        resources = self.resources(start, end)
        mean_cpu_cores = resources.get('cpu_mean_cores')
        cpu_sample_valid = bool(mean_cpu_cores) and (
            resources.get('process_tree_complete', True)
            or not self.spec.get('cpu_metric_requires_complete_tree', False)
        )
        out = {'metadata': self.metadata, 'label': label, 'offered_rps': rate, 'requests': len(rows), 'planned_requests': count, 'success': len(ok), 'success_rate': len(ok) / len(rows), 'status_counts': dict(Counter((r['status'] for r in rows))), 'elapsed': end - start, 'delivered_rps': delivered_rps, 'delivered_rps_per_cpu_core': delivered_rps / mean_cpu_cores if cpu_sample_valid else None, 'p90': P([r['latency'] for r in ok]), 'p90_all': P([r['latency'] for r in rows]), 'wait_p90': P([r['wait'] for r in ok]), 'client_lag_p90': P([r.get('client_lag', 0) for r in rows]), 'resources': resources, 'pool': self.snapshot(), 'rows': rows}
        if early_stop_reason:
            out['early_stop_reason'] = early_stop_reason
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
    rate_group = ap.add_mutually_exclusive_group()
    rate_group.add_argument('--rates', type=float, nargs='+')
    rate_group.add_argument('--start-rate', type=float,
                            help='Screen from this offered rate down toward --minimum-rate.')
    ap.add_argument('--decrement', type=float,
                    help='Positive step used with --start-rate.')
    ap.add_argument('--minimum-rate', type=float,
                    help='Lowest offered rate used with --start-rate.')
    ap.add_argument('--stop-on-pass', action='store_true',
                    help='Stop a descending screen at its first stable passing rate.')
    ap.add_argument('--count', type=int)
    ap.add_argument('--confirmation', action='store_true',
                    help='Choose 500–2,000 requests from the offered rate and 30-minute round budget.')
    ap.add_argument('--warm-trials', type=int, default=0)
    ap.add_argument('--num-steps', type=int,
                    help='Benchmark-only override for Supertonic generation steps; product defaults are unchanged.')
    ap.add_argument('--threads-per-instance', type=int,
                    help='Benchmark-only override for runtime threads per instance.')
    ap.add_argument('--instances', type=int,
                    help='Benchmark-only override for the maximum runtime pool instances.')
    ap.add_argument('--label', default='screen')
    ap.add_argument('--abort-overload', action='store_true')
    ap.add_argument('--idle-trial', action='store_true')
    args = ap.parse_args()
    if args.num_steps is not None and (args.scenario != 'supertonic' or args.num_steps < 1):
        ap.error('--num-steps requires the supertonic scenario and a positive integer')
    if args.threads_per_instance is not None and args.threads_per_instance < 1:
        ap.error('--threads-per-instance must be positive')
    if args.instances is not None and args.instances < 1:
        ap.error('--instances must be positive')
    overrides = {'num_steps': args.num_steps,
                 'threads_per_instance': args.threads_per_instance,
                 'instances': args.instances,
                 'artifact_tag': args.label if args.warm_trials else None}
    if args.count is not None and args.count < 1:
        ap.error('--count must be positive')
    if args.confirmation and (args.count is not None or not args.rates or len(args.rates) != 1):
        ap.error('--confirmation requires exactly one --rates value and cannot be combined with --count')
    if args.start_rate is not None:
        if args.decrement is None or args.minimum_rate is None:
            ap.error('--start-rate requires --decrement and --minimum-rate')
        if (not math.isfinite(args.start_rate) or args.start_rate <= 0
                or not math.isfinite(args.decrement) or args.decrement <= 0
                or not math.isfinite(args.minimum_rate) or args.minimum_rate <= 0
                or args.minimum_rate > args.start_rate):
            ap.error('descending rate values must be finite and positive, with minimum <= start')
        rates = []
        rate = args.start_rate
        while rate >= args.minimum_rate - 1e-9:
            rates.append(round(rate, 8))
            rate -= args.decrement
        args.stop_on_pass = True
    else:
        if any(value is not None for value in (args.decrement, args.minimum_rate)):
            ap.error('--decrement and --minimum-rate require --start-rate')
        rates = args.rates or []
    if any(not math.isfinite(rate) or rate <= 0 for rate in rates):
        ap.error('--rates must be finite and positive')
    confirmation_count = None
    if args.confirmation:
        rate = rates[0]
        workload_budget = CONFIRMATION_MAX_ROUND_SECONDS - CONFIRMATION_OVERHEAD_RESERVE_SECONDS
        confirmation_count = min(CONFIRMATION_MAX_REQUESTS, math.floor(rate * workload_budget))
        if confirmation_count < CONFIRMATION_MIN_REQUESTS:
            minimum_rate = CONFIRMATION_MIN_REQUESTS / workload_budget
            ap.error(f'Rate {rate:g} req/s cannot fit the {CONFIRMATION_MIN_REQUESTS}-request minimum within the '
                     f'{CONFIRMATION_MAX_ROUND_SECONDS:g}s round budget; minimum eligible rate is {minimum_rate:.3f} req/s')
    if args.idle_trial:
        server = Server(args.scenario, idle_seconds=1, **overrides)
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
            server = Server(args.scenario, **overrides)
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
                expected_instances = args.instances or int(server.spec.get('instances', 2))
                if group['instances'] != expected_instances:
                    raise RuntimeError(f'did not expand to {expected_instances} instances')
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
                warm_tag = f'-{args.label}' if args.label != 'screen' else ''
                (ROOT / f'{args.scenario}{warm_tag}-warm-trials.json').write_text(json.dumps(trials, ensure_ascii=False, indent=2) + '\n')
            finally:
                server.close()
        return
    server = Server(args.scenario, **overrides)
    try:
        server.request(0)
        with concurrent.futures.ThreadPoolExecutor(2) as ex:
            [f.result() for f in [ex.submit(server.request, i) for i in range(2)]]
        with concurrent.futures.ThreadPoolExecutor(2) as ex:
            for i in range(8):
                [f.result() for f in [ex.submit(server.request, i), ex.submit(server.request, i)]]
        baseline_label = 'baseline-' + args.label + '-' + '_'.join(f'{rate:g}' for rate in rates)
        server.metadata['benchmark_settings']['rate_search'] = {
            'strategy': 'descending' if args.start_rate is not None else 'explicit_order',
            'offered_rate_schedule': rates,
            'stop_on_first_pass': bool(args.stop_on_pass),
        }
        baseline = server.run_rate(float(server.spec.get('baseline_rps', 0.5 if args.scenario == 'supertonic' else 2)), int(server.spec.get('baseline_requests', BENCHMARK_CONFIG.get('baseline_requests', 40))), baseline_label)
        if server.spec['task'] == 'asr':
            server.reference_texts = {row['i'] % len(server.audios): row['signature'] for row in baseline['rows'] if row['valid']}
        server.baseline_p90 = baseline['p90']
        for rate in rates:
            count = confirmation_count or args.count or max(60, math.ceil(rate * SCREENING_SECONDS))
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
                and (not args.confirmation or result['requests'] == result['planned_requests'])
            )
            if args.confirmation:
                result['confirmation_complete'] = result['requests'] == result['planned_requests']
            (ROOT / f'{args.scenario}-{args.label}-{rate:g}.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
            print('stable=', result['stable'], 'p90_ratio=', round(result['p90_ratio'], 3) if result['p90_ratio'] is not None else None, flush=True)
            if args.stop_on_pass and result['stable']:
                print('Descending screen stopped at its first stable passing rate:', rate,
                      '(rate resolution:', args.decrement if args.start_rate is not None else 'explicit schedule', ')',
                      flush=True)
                break
    finally:
        server.close()
if __name__ == '__main__':
    main()
