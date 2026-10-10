"""CI guard for portable committed reports and the native-hour exporter."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.streaming.report_paths import (
    REPO_ROOT, portable_location, evidence_reference,
    validate_relative_path, validate_report_paths,
)
from benchmarks.streaming.summarize_full_hour import MODES, summarize


class ReportPathTests(unittest.TestCase):
    def test_internal_paths_are_repository_relative(self):
        self.assertEqual(portable_location(REPO_ROOT/'benchmarks/streaming/run.py'),
                         'benchmarks/streaming/run.py')

    def test_external_files_expose_identity_and_basename_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'evidence.json';path.write_text('{}')
            reference=evidence_reference(path)
            self.assertEqual(reference['raw_report']['name'],'evidence.json')
            checksum=hashlib.sha256(b'{}').hexdigest()
            self.assertEqual(reference['raw_report']['artifact_id'],'sha256:'+checksum)
            self.assertEqual(reference['raw_report_sha256'],checksum)
            self.assertNotIn(directory,json.dumps(reference))
            with self.assertRaises(ValueError):portable_location(path)
            validate_report_paths(reference)

    def test_unsafe_paths_rejected_independently_of_host_platform(self):
        for path in ('/Users/person/audio.wav','/tmp/a.json',r'C:\Users\person\a.json',
                     'C:/audio.wav','C:audio.wav',r'\\server\share\a.json',
                     r'\rooted\a.json','file:///tmp/a.json','~/a.json',
                     '../a.json','safe/../../a.json',r'safe\..\a.json'):
            with self.subTest(path=path),self.assertRaises(ValueError):
                validate_relative_path(path)
        self.assertEqual(validate_relative_path(r'benchmarks\streaming\report.json'),
                         'benchmarks/streaming/report.json')

    def test_nested_fields_lists_and_checksum_map_keys(self):
        for report in ({'routes':[{'raw_report':'/tmp/private.json'}]},
                       {'config':{'audio_root':'C:/secret'}},
                       {'evidence_paths':['safe.json','../outside.json']},
                       {'source_full_report_local_path':r'\\host\share\private'},
                       {'code_sha256':{'/Users/person/code.py':'abc'}}):
            with self.subTest(report=report),self.assertRaises(ValueError):
                validate_report_paths(report)
        # Recognized artifact references and free-form natural language survive.
        report={'raw_report':{'name':'a.json','artifact_id':'sha256:abc'},
                'text':'/this is spoken text', 'audio_root':'benchmarks/cache'}
        self.assertIs(validate_report_paths(report),report)

    def test_committable_streaming_reports_have_portable_paths(self):
        paths=sorted((REPO_ROOT/'benchmarks/streaming/reports').glob('*.json'))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(report=path.name):
                validate_report_paths(json.loads(path.read_text()))


class HourSummaryExportTests(unittest.TestCase):
    def prepare(self,root):
        for mode in MODES:
            for language in ('zh','en'):
                report={'schema':'smartvoice.stream.full-hour.v1','mode':mode,'language':language,
                        'input_audio_seconds':3600,'failure':None,'output':{'counts':{}},
                        'owned_workers_still_alive':[]}
                (root/(mode+'-'+language+'.json')).write_text(json.dumps(report))

    def test_six_routes_export_retains_failures_without_local_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.prepare(root)
            path=root/'translated_subtitles-en.json'
            report=json.loads(path.read_text());report.update(failure='audio failure',input_audio_seconds=42)
            path.write_text(json.dumps(report))
            result=summarize(root,[path])
            self.assertEqual(len(result['routes']),6)
            failed=[row for row in result['routes'] if row['failure']]
            self.assertEqual(failed[0]['input_audio_seconds'],42)
            self.assertEqual(result['supplemental_runs'][0]['failure'],'audio failure')
            self.assertNotIn(directory,json.dumps(result))
            validate_report_paths(result)

    def test_missing_or_misidentified_route_fails_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with self.assertRaises(FileNotFoundError):summarize(root)
            self.prepare(root)
            path=root/'transcription-zh.json';report=json.loads(path.read_text())
            report['language']='en';path.write_text(json.dumps(report))
            with self.assertRaises(ValueError):summarize(root)
