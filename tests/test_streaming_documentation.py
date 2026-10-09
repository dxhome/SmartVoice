"""Execute the documented client against a deterministic protocol peer."""
import asyncio
import contextlib
import io
import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from smartvoice.adapters.audio.streaming import package_wav
from smartvoice.services.model_registry import get_model_spec

ROOT = Path(__file__).resolve().parents[1]


def client_namespace():
    text = (ROOT / 'doc/streaming.md').read_text()
    examples = re.findall(r'```python\n(.*?)```', text, flags=re.S)
    if len(examples) != 1: raise AssertionError('Expected one documented Python client')
    namespace = {'__name__': 'documented_example'}
    exec(compile(examples[0], 'doc/streaming.md:client', 'exec'), namespace)
    return namespace


class ProtocolPeer:
    def __init__(self, mode, source):
        self.mode = mode; self.source = source; self.sent = []; self.finished = asyncio.Event()
        self.index = 0; self.closed = False
        self.wav = package_wav(b'\0\0' * 320, 16000)
        self.events = [json.dumps({'type': 'source_unit_final', 'event_sequence': 2, 'text': 'fixture'})]
        if mode == 'spoken_interpretation':
            self.events += [json.dumps({'type': 'audio_segment', 'event_sequence': 3,
                                        'audio_sequence': 1, 'audio_bytes': len(self.wav)}), self.wav]
        self.events += [json.dumps({'type': 'session_complete', 'status': 'complete', 'event_sequence': 4})]
    async def __aenter__(self): return self
    async def __aexit__(self, *args): self.closed = True
    async def recv(self): return json.dumps({'type': 'session_ready', 'event_sequence': 1})
    async def send(self, message):
        self.sent.append(message)
        if isinstance(message, str) and json.loads(message)['type'] == 'finish': self.finished.set()
    def __aiter__(self): return self
    async def __anext__(self):
        await asyncio.wait_for(self.finished.wait(), 1)
        if self.index == len(self.events): raise StopAsyncIteration
        message = self.events[self.index]; self.index += 1; return message


class StreamingDocumentationTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(importlib.util.find_spec("websockets"), "Install streaming extra to execute the documented client")
    async def test_documented_client_six_modes_and_directions(self):
        for mode in ('transcription', 'translated_subtitles', 'spoken_interpretation'):
            for source in ('zh', 'en'):
                with self.subTest(mode=mode, source=source), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary); audio = root / 'input.wav'
                    audio.write_bytes(package_wav(b'\0\0' * 640, 16000))
                    namespace = client_namespace()
                    namespace['Path'] = lambda name: root / name
                    peer = ProtocolPeer(mode, source)
                    with patch('websockets.connect', return_value=peer), contextlib.redirect_stdout(io.StringIO()):
                        await asyncio.wait_for(namespace['main'](str(audio), mode, source), 2)
                    messages = [json.loads(m) for m in peer.sent if isinstance(m, str)]
                    config = messages[0]
                    self.assertEqual(config['source_language'], source)
                    self.assertEqual(config['mode'], mode)
                    if mode == 'transcription': self.assertNotIn('target_language', config)
                    else: self.assertEqual(config['target_language'], 'en' if source == 'zh' else 'zh')
                    self.assertEqual([len(m) for m in peer.sent if isinstance(m, bytes)], [640, 640])
                    self.assertTrue(peer.finished.is_set()); self.assertTrue(peer.closed)
                    acknowledgements = [m['event_sequence'] for m in messages if m['type'] == 'ack']
                    self.assertEqual(acknowledgements, [1, 2, 3] if mode == 'spoken_interpretation' else [1, 2])
                    output = root / 'target-0001.wav'
                    if mode == 'spoken_interpretation': self.assertEqual(output.read_bytes(), peer.wav)
                    else: self.assertFalse(output.exists())

    @unittest.skipUnless(importlib.util.find_spec("websockets"), "Install streaming extra to execute the documented client")
    async def test_documented_client_rejects_wrong_input_format_before_connecting(self):
        with tempfile.TemporaryDirectory() as temporary:
            audio = Path(temporary) / 'input.wav'; audio.write_bytes(package_wav(b'\0\0' * 320, 22050))
            with patch('websockets.connect') as connect, self.assertRaises(ValueError):
                await client_namespace()['main'](str(audio), 'transcription', 'en')
            connect.assert_not_called()

    @unittest.skipUnless(importlib.util.find_spec("websockets"), "Install streaming extra to execute the documented client")
    async def test_documented_client_preserves_existing_audio_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); audio = root / 'input.wav'; audio.write_bytes(package_wav(b'\0\0' * 320, 16000))
            output = root / 'target-0001.wav'; output.write_bytes(b'previous-output')
            namespace = client_namespace(); namespace['Path'] = lambda name: root / name
            peer = ProtocolPeer('spoken_interpretation', 'en')
            with patch('websockets.connect', return_value=peer), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(FileExistsError):
                await namespace['main'](str(audio), 'spoken_interpretation', 'en')
            self.assertEqual(output.read_bytes(), b'previous-output'); self.assertTrue(peer.closed)

    def test_documented_install_commands_reference_real_catalog_models(self):
        text = (ROOT / 'doc/streaming.md').read_text()
        self.assertNotIn('models install --all', text)
        model_ids = re.findall(r'python -m smartvoice models install ([a-z0-9-]+)', text)
        self.assertGreaterEqual(len(model_ids), 6)
        for model_id in (item for item in model_ids if item != "all"): self.assertEqual(get_model_spec(model_id).id, model_id)
