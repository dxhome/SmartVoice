"""Export six native hour reports as a portable, reviewable summary."""
import argparse
import json
from pathlib import Path
from .report_paths import evidence_reference, validate_report_paths

MODES = ('transcription', 'translated_subtitles', 'spoken_interpretation')


def summarize(report_dir, supplements=()):
    rows = []
    for mode in MODES:
        for language in ('zh', 'en'):
            path = Path(report_dir)/(mode+'-'+language+'.json')
            report = json.loads(path.read_text())
            if (report.get('schema') != 'smartvoice.stream.full-hour.v1'
                    or report.get('mode') != mode or report.get('language') != language):
                raise ValueError('Unexpected native hour report identity: '+path.name)
            # Preserve failures and incomplete input; an export is not a pass assertion.
            fields = ('mode', 'language', 'fixture_id', 'fixture_sha256', 'manifest_sha256',
                      'model_manifests', 'versions', 'input_audio_seconds', 'wall_seconds',
                      'effective_audio_speedup', 'failure', 'peak_state', 'before_finish',
                      'output', 'pool_after_close', 'owned_workers_still_alive')
            row = {key: report[key] for key in fields if key in report}
            # Keep the established curated summary fields for downstream readers.
            row['source_language'] = language
            row['counts'] = report.get('output', {}).get('counts', {})
            for key in ('audio_chunks', 'final_text_sha256', 'first_output_wall_seconds'):
                if key in report.get('output', {}):
                    row[key] = report['output'][key]
            if 'effective_audio_speedup' in report:
                row['speedup'] = round(report['effective_audio_speedup'], 3)
            row.update(evidence_reference(path))
            rows.append(row)
    extra = []
    for path in supplements:
        report = json.loads(Path(path).read_text())
        if report.get('schema') != 'smartvoice.stream.full-hour.v1':
            raise ValueError('Supplement must be a native hour report')
        extra.append({'mode': report['mode'], 'language': report['language'],
                      'failure': report['failure'], 'before_finish': report.get('before_finish'),
                      'output': report['output'], **evidence_reference(path)})
    return validate_report_paths({
        'schema': 'smartvoice.stream.full-hour-summary.v1', 'routes': rows,
        'supplemental_runs': extra,
        'raw_report_location_note': 'Locations are repository-relative run-local ignored artifacts, '
            'or external artifact identities. Original evidence is not distributed with this summary.',
        'scope': 'One hour cyclic pinned audio per route, accelerated native session inference; '
            'not a wall-hour soak, natural meeting quality, latency SLA, browser playback or concurrency test.'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reports-dir', type=Path, required=True)
    parser.add_argument('--supplement', type=Path, action='append', default=[])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = summarize(args.reports_dir, args.supplement)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Preserve prior measurements; require a fresh output filename.
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False)
        stream.write('\n')


if __name__ == '__main__':
    main()
