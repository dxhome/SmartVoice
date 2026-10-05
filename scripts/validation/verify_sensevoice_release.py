"""Sequential, resumable SenseVoice acceptance; keeps production defaults intact."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT/'src'))
from scripts.validation.stt_validation_common import provenance
from scripts.validation.stt_window_policies import POLICIES
from scripts.validation.verify_stt_load import evaluate_pair

MODEL = 'stt-sensevoice-small-int8'
TOOLS = ('scripts/validation/stt_isolated_http.py', 'scripts/validation/stt_window_policies.py',
         'scripts/validation/verify_stt_isolated_load.py', 'scripts/validation/stt_validation_common.py')


def same_environment(recorded, current, tools=()):
    if any(recorded.get(k) != current.get(k) for k in ('settings', 'dependencies', 'models', 'platform')):
        return False
    files = current['python_files_sha256']
    relevant = {p: h for p, h in files.items() if p.startswith('src/') or p in tools}
    previous = {p: h for p, h in recorded.get('python_files_sha256', {}).items()
                if p.startswith('src/') or p in tools}
    return previous == relevant


def quality_ready(quality, current):
    rounds = quality.get('rounds', [])
    expected = {
        'current': {'window_seconds': 15.0, 'minimum_seconds': None,
                    'relative_quiet': False, 'overlap_on_silence': True},
        'candidate': {'window_seconds': 15.0, 'minimum_seconds': 8,
                      'relative_quiet': True, 'overlap_on_silence': True},
    }
    return (quality.get('completed') and quality.get('candidate_policy') == 'relative-minimum8'
            and len(rounds) == 6
            and {(r['policy'], r['repeat']) for r in rounds}
                == {(p, n) for p in ('current', 'candidate') for n in (1, 2, 3)}
            and all(r.get('model') == MODEL and r.get('completed')
                    and r.get('candidate_policy') == 'relative-minimum8'
                    and r.get('policy_configuration') == expected[r['policy']]
                    and len(r.get('cases', [])) == 15
                    and all(c.get('status') == 200 and c.get('chunks') == c.get('adapter_calls')
                            and c.get('coverage_end') == c.get('duration') for c in r['cases'])
                    and same_environment(r.get('provenance', {}), current) for r in rounds)
            and all(r.get('quality_non_regression') for r in quality.get('evaluations', []))
            and len(quality.get('evaluations', [])) == 1)


def reusable(row, spec, current):
    return (row.get('completed') and row.get('server_process_exited')
            and same_environment(row.get('provenance', {}), current, TOOLS)
            and all(row.get(k) == v for k, v in spec.items())
            and row.get('configuration') == vars(POLICIES[spec['policy']]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--quality-report', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    from smartvoice.config.settings import Settings
    current = provenance(Settings.from_env())
    quality = json.loads(args.quality_report.read_text())
    if not quality_ready(quality, current):
        raise RuntimeError('Three paired quality rounds must pass on the current product code and environment')
    args.report.parent.mkdir(parents=True, exist_ok=True)
    result = {'completed': False, 'provenance': current, 'quality_report': str(args.quality_report),
              'human_audit': 'pending; assumed passing only for experiment sequencing',
              'candidate_policy': 'relative-minimum8', 'cases': [], 'evaluations': [],
              'regression': [], 'production_defaults_changed': False}
    def save(): args.report.write_text(json.dumps(result, indent=2))
    def worker(name, scenario, policy, interval=.6, instances=None, cycles=5):
        path = args.report.with_name(f'{args.report.stem}-{name}.json')
        spec = {'scenario': scenario, 'policy': policy, 'count': 500, 'interval_seconds': interval}
        command = [sys.executable, str(ROOT/'scripts/validation/verify_stt_isolated_load.py'),
                   '--scenario', scenario, '--policy', policy, '--interval', str(interval),
                   '--cycles', str(cycles), '--report', str(path)]
        if instances is not None: command += ['--instances', str(instances)]
        previous = json.loads(path.read_text()) if path.exists() else {}
        expected_max = instances or current['settings']['max_instances']
        can_reuse = (args.resume and reusable(previous, spec, current)
                     and previous['provenance']['settings']['max_instances'] == expected_max
                     and (scenario != 'lifecycle' or len(previous['cycles']) == cycles))
        # Instance trials deliberately change only isolated max_instances.
        if instances is not None:
            adjusted = dict(current, settings=dict(current['settings'], max_instances=instances, min_instances=1))
            can_reuse = (args.resume and reusable(previous, spec, adjusted))
        if can_reuse:
            print(json.dumps({'reuse': name}), flush=True)
        else:
            if path.exists():
                from time import time
                path.rename(path.with_name(path.stem+f'-previous-{int(time())}.json'))
            log = path.with_suffix('.log')
            result['running'] = name; save()
            print(json.dumps({'start': name, 'report': str(path)}), flush=True)
            with log.open('w') as output:
                finished = subprocess.run(command, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT, timeout=1800)
            row = json.loads(path.read_text()) if path.exists() else {'completed': False}
            if finished.returncode or not row.get('completed'):
                result['cases'].append({'name': name, 'report': str(path), 'completed': False})
                save(); raise RuntimeError(f'Acceptance worker failed: {name}; see {log}')
        row = json.loads(path.read_text())
        result['cases'].append({'name': name, 'report': str(path), 'completed': row['completed']})
        save(); print(json.dumps({'finish': name, 'success_rate': row.get('success_rate')}), flush=True)
        return row
    save()
    try:
        pairs = {}
        for index, (scenario, interval) in enumerate((('same', .4), ('different', 1.6), ('tts', .8))):
            order = ('current', 'relative-minimum8') if index % 2 == 0 else ('relative-minimum8', 'current')
            rows = {p: worker(f'{scenario}-{p}', scenario, p, interval) for p in order}
            count = min(len(r['short']) for r in rows.values())
            evaluation = {'scenario': scenario, **evaluate_pair(rows['current'], rows['relative-minimum8'], count)}
            pairs[scenario] = rows; result['evaluations'].append(evaluation); save()
        single = worker('same-one-instance', 'same', 'relative-minimum8', .4, instances=1)
        result['instance_comparison'] = {'scope': 'same-model workload only; max_instances 1 versus 2',
            **evaluate_pair(pairs['same']['relative-minimum8'], single, len(single['short']))}
        for policy in ('current', 'relative-minimum8'):
            worker(f'lifecycle-{policy}', 'lifecycle', policy)
        for mode in ('ci', 'regression', 'full'):
            log = args.report.with_name(f'{args.report.stem}-{mode}.log')
            result['running'] = mode; save()
            command = [sys.executable, str(ROOT/'scripts/test.py'), mode, '--stt-models', MODEL]
            with log.open('w') as output:
                finished = subprocess.run(command, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT, timeout=1800)
            result['regression'].append({'mode': mode, 'exit_code': finished.returncode, 'log': str(log)})
            save()
            if finished.returncode: raise RuntimeError(f'Final {mode} regression failed; see {log}')
        result['all_load_gates_passed'] = all(e['resource_latency_gate'] for e in result['evaluations'])
        result['recommendation'] = 'retain production defaults; candidate requires completed human audit and all resource/latency gates'
        result['final_provenance'] = provenance(Settings.from_env())
        result['product_code_unchanged'] = same_environment(current, result['final_provenance'])
        result.pop('running', None); result['completed'] = True
    except BaseException as exc:
        result['failure_type'] = type(exc).__name__
        raise
    finally: save()


if __name__ == '__main__': main()
