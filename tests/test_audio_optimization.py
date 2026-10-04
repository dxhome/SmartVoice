"""Production integration contracts for the independently validated audio path."""
import asyncio
import gc
import io
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
import httpx
from fastapi.testclient import TestClient
import av
from tests.audio_fixtures import wav_audio
from smartvoice.app import create_app
from smartvoice.config.settings import Settings
from smartvoice.domain.contracts import SynthesizedSpeech
from smartvoice.domain.errors import InferenceTimeoutError, SpeechOutputTooLargeError, InferenceError
from smartvoice.adapters.audio.encoding import AudioEncoder, AudioAssembler
from smartvoice.adapters.audio.input import BoundedAudioInput
from smartvoice.ports.inference_context import request_cancelled, request_deadline
from smartvoice.services.audio_planning import append_timed_segments, split_text, merge

class Provider:
    def __init__(self):self.speech=[];self.stt=[];self.lid=[];self.cancel=None
    def installed_models(self):
        return [{'id':'tts-supertonic-v3-multilingual-int8','task':'speech','languages':['en'],'backend':'fixture'},
                {'id':'stt-sensevoice-small-int8','task':'transcription','languages':['auto','en','zh'],'backend':'fixture'}]
    def segment_limits(self,model):return {'characters':200} if model.startswith('tts') else {'audio_seconds':15}
    def runtime(self):return {'backend':'fixture','actual_device':'cpu','provider_status':'available'}
    def capabilities(self):return {'api_version':'v1','tasks':[]}
    def synthesize(self,text,voice='default',speed=1,model_id=None,language='auto'):
        self.speech.append(text)
        if self.cancel:self.cancel.set()
        return SynthesizedSpeech(wav_audio(),24000,.8,.012)
    def transcribe(self,audio,language='auto',model_id=None):
        with wave.open(io.BytesIO(audio),'rb') as f:duration=f.getnframes()/f.getframerate()
        self.stt.append(duration)
        return {'text':'boundary speech','duration':duration,'language':'en','model':model_id,'device':'cpu',
                'processing_seconds':.01,'runtime_wait_seconds':.002,'segments':[{'text':'word','start':1.,'end':1.1}]}
    def identify_language(self,audio):
        with wave.open(io.BytesIO(audio),'rb') as f:self.lid.append(f.getnframes()/f.getframerate())
        return {'language':'en','runtime_wait_seconds':.003}
    def language_identification_available(self):return True

class AudioOptimizationTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.provider=Provider();self.app=create_app(Settings(data_dir=Path(self.directory.name),inference_execution_timeout_seconds=2),self.provider)
        self.client=TestClient(self.app);self.client.__enter__();self.addCleanup(self.client.__exit__,None,None,None)
    def speech(self,**args):
        return self.client.post('/v1/audio/speech',json={'model':'tts-supertonic-v3-multilingual-int8','input':'Hello','language':'en',**args})
    def test_default_mp3_and_decode_tail(self):
        result=self.speech();self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.headers['content-type'],'audio/mpeg');self.assertEqual(result.headers['x-audio-format'],'mp3')
        with av.open(io.BytesIO(result.content)) as source:duration=sum(f.samples/f.sample_rate for f in source.decode(audio=0))
        self.assertAlmostEqual(duration,.8,delta=.03)
        self.assertAlmostEqual(float(result.headers['x-runtime-wait-seconds']),.012,places=3)
    def test_explicit_wav_identity(self):
        result=self.speech(response_format='wav');self.assertEqual(result.content,wav_audio())
        self.assertEqual(result.headers['content-type'],'audio/wav')
    def test_missing_codec_fails_before_model(self):
        self.app.state.audio_encoder.reason='missing';result=self.speech()
        self.assertEqual(result.status_code,501);self.assertEqual(self.provider.speech,[])
        self.assertEqual(self.speech(response_format='wav').status_code,200)
    def test_capabilities_separate_model_and_output(self):
        result=self.client.get('/v1/capabilities').json()
        self.assertEqual(result['speech_output']['default_format'],'mp3')
        self.assertEqual(result['limits']['transcription']['audio_seconds'],600)
        self.assertEqual(result['limits']['speech']['json_bytes'],65536)
    def test_json_bound_before_model(self):
        self.assertEqual(self.speech(input='a'*65536).status_code,413)
        self.assertEqual(self.provider.speech,[])
    def test_unsupported_format_before_model(self):
        self.assertEqual(self.speech(response_format='ogg').status_code,501);self.assertEqual(self.provider.speech,[])
    def test_long_text_single_application_plan_and_cumulative_audio(self):
        text='Hello everyone. Please check 12.5 kilograms. '*20
        result=self.speech(input=text,response_format='wav');self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(''.join(self.provider.speech).rstrip(),text.rstrip());self.assertGreater(len(self.provider.speech),1)
        self.assertTrue(all(len(s)<=200 for s in self.provider.speech))
        self.assertAlmostEqual(float(result.headers['x-audio-duration']),len(self.provider.speech)*.8,places=3)
        self.assertEqual(list((Path(self.directory.name)/'tmp/speech').iterdir()),[])
    def test_long_tts_does_not_send_whitespace_only_chunks(self):
        original=self.provider.synthesize
        def synthesize(text,*args,**kwargs):
            self.assertTrue(text.strip(), 'Whitespace must not reach inference')
            return original(text,*args,**kwargs)
        self.provider.synthesize=synthesize
        text='The next order is ready. '*40
        self.assertFalse(split_text(text,200)[-1].strip())
        result=self.speech(input=text)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(len(self.provider.speech),5)
        self.assertEqual(''.join(self.provider.speech),text.rstrip())
    def test_cancellation_stops_future_chunks(self):
        event=threading.Event();self.provider.cancel=event;token=request_cancelled.set(event)
        try:
            with self.assertRaises(InferenceTimeoutError):
                self.app.state.speech_service.execute('Hello world. '*40,'tts-supertonic-v3-multilingual-int8','en','default',1,response_format='mp3')
        finally:request_cancelled.reset(token)
        self.assertEqual(len(self.provider.speech),1)
    def test_expired_total_deadline_preflight(self):
        token=request_deadline.set(time.monotonic()-1)
        try:
            with self.assertRaises(InferenceTimeoutError):self.app.state.speech_service.execute('Hello','tts-supertonic-v3-multilingual-int8','en','default',1)
        finally:request_deadline.reset(token)
        self.assertEqual(self.provider.speech,[])
    def test_long_stt_native_offsets_and_duration(self):
        result=self.client.post('/v1/audio/transcriptions',files={'file':('long.wav',wav_audio(16000,70),'audio/wav')},data={'model':'stt-sensevoice-small-int8','language':'en','response_format':'verbose_json','timestamps':'true'})
        self.assertEqual(result.status_code,200,result.text);data=result.json()
        self.assertEqual(data['duration'],70);self.assertEqual(data['chunk_count'],5)
        self.assertEqual(self.provider.stt,[15.,15.,15.,15.,14.])
        self.assertEqual([s['start'] for s in data['segments']],[1.,15.,29.,43.,57.])
        self.assertTrue(all(s<=15 for s in self.provider.stt))
    def test_routed_lid_uses_bounded_decoded_sample(self):
        result=self.client.post('/v1/audio/transcriptions',files={'file':('long.wav',wav_audio(16000,70),'audio/wav')},data={'model':'smartvoice-auto'})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(self.provider.lid,[29.]);self.assertEqual(result.json()['duration'],70)
        self.assertEqual(self.provider.stt,[15.,15.,15.,15.,14.])
    def test_lid_timeout_does_not_fall_back_to_transcription(self):
        def identify(audio):raise InferenceTimeoutError('LID deadline expired')
        self.provider.identify_language=identify
        result=self.client.post('/v1/audio/transcriptions',files={'file':('short.wav',wav_audio(),'audio/wav')},data={'model':'smartvoice-auto'})
        self.assertEqual(result.status_code,504,result.text)
        self.assertEqual(self.provider.stt,[])
    def test_lid_queue_overload_does_not_fall_back(self):
        from smartvoice.domain.errors import InferenceOverloadedError
        def identify(audio):raise InferenceOverloadedError('LID queue full')
        self.provider.identify_language=identify
        result=self.client.post('/v1/audio/transcriptions',files={'file':('long.wav',wav_audio(16000,70),'audio/wav')},data={'model':'smartvoice-auto','language':'auto'})
        self.assertEqual(result.status_code,503)
        self.assertEqual(self.provider.stt,[])

    def test_unexpected_lid_error_is_not_silently_recovered(self):
        def identify(audio):raise RuntimeError('programming error')
        self.provider.identify_language=identify
        with self.assertRaises(RuntimeError):
            self.app.state.transcription_service.execute(wav_audio(16000,70),'smartvoice-auto','auto')
        self.assertEqual(self.provider.stt,[])

    def test_lid_cancellation_does_not_start_transcription(self):
        event=threading.Event()
        def identify(audio):
            event.set()
            raise InferenceError('Native LID failed after cancellation')
        self.provider.identify_language=identify
        token=request_cancelled.set(event)
        try:
            with self.assertRaises(InferenceTimeoutError):
                self.app.state.transcription_service.execute(wav_audio(),'smartvoice-auto','auto')
        finally:request_cancelled.reset(token)
        self.assertEqual(self.provider.stt,[])
    def test_stt_uses_declared_window_policy(self):
        self.provider.segment_limits=lambda model: {'audio_seconds':25}
        result=self.client.post('/v1/audio/transcriptions',files={'file':('long.wav',wav_audio(16000,70),'audio/wav')},data={'model':'stt-sensevoice-small-int8','language':'en','response_format':'verbose_json'})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['chunk_count'],3)
        self.assertEqual(self.provider.stt,[25.,25.,22.])
    def test_direct_auto_stays_auto_for_each_window(self):
        original=self.provider.transcribe;languages=[]
        def transcribe(audio,language,model):
            languages.append(language)
            result=original(audio,language,model)
            result['language']='zh' if len(languages)==1 else 'en'
            return result
        self.provider.transcribe=transcribe
        result=self.client.post('/v1/audio/transcriptions',files={'file':('mixed.wav',wav_audio(16000,40),'audio/wav')},data={'model':'stt-sensevoice-small-int8','language':'auto'})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(languages,['auto']*3)
        self.assertIsNone(result.json()['language'])
        self.assertEqual(self.provider.lid,[])
    def test_auto_does_not_label_unknown_windows_with_first_language(self):
        original=self.provider.transcribe
        def transcribe(*args):
            result=original(*args)
            result['language']='en' if len(self.provider.stt)==1 else None
            return result
        self.provider.transcribe=transcribe
        outcome=self.app.state.transcription_service.execute(wav_audio(16000,40),'stt-sensevoice-small-int8','auto')
        self.assertIsNone(outcome.result['language'])
    def test_crossing_timestamp_interval_is_preserved(self):
        original=self.provider.transcribe
        def transcribe(*args):
            result=original(*args)
            result['segments']=[{'text':'crossing','start':.8,'end':1.3},
                                {'text':'duplicate','start':.2,'end':.6}]
            return result
        self.provider.transcribe=transcribe
        result=self.app.state.transcription_service.execute(wav_audio(16000,29),'stt-sensevoice-small-int8','en').result
        self.assertIn({'text':'crossing','start':14.8,'end':15.3},result['segments'])
        self.assertNotIn({'text':'duplicate','start':14.2,'end':14.6},result['segments'])
    def test_routed_auto_uses_one_lid_language_for_all_windows(self):
        original=self.provider.transcribe;languages=[]
        def transcribe(audio,language,model):
            languages.append(language)
            return original(audio,language,model)
        self.provider.transcribe=transcribe
        outcome=self.app.state.transcription_service.execute(wav_audio(16000,40),'smartvoice-auto','auto')
        self.assertEqual(languages,['en']*3)
        self.assertEqual(self.provider.lid,[29.])
        self.assertEqual(outcome.result['language'],'en')
    def test_start_only_overlap_is_retained_and_sorted(self):
        original=self.provider.transcribe
        def transcribe(*args):
            result=original(*args)
            result['segments']=([{'text':'tail','start':14.9}] if len(self.provider.stt)==1
                                else [{'text':'crossing','start':.8}, {'text':'tail','start':.9}])
            return result
        self.provider.transcribe=transcribe
        result=self.app.state.transcription_service.execute(wav_audio(16000,29),'stt-sensevoice-small-int8','en').result
        self.assertEqual(result['segments'],[{'text':'crossing','start':14.8},{'text':'tail','start':14.9}])
        self.assertTrue(all('end' not in item for item in result['segments']))
    def test_start_only_alignment_preserves_uncertain_and_repeated_tokens(self):
        previous=[{'text':'again','start':14.5}]
        append_timed_segments(previous,[{'text':'again','start':.5},
                                       {'text':'again','start':.5},
                                       {'text':'again','start':.6},
                                       {'text':'different','start':.5}],14,1)
        self.assertEqual(len(previous),4)
        self.assertEqual(sum(item['text']=='again' for item in previous),3)
    def test_start_only_alignment_requires_overlap_and_text_evidence(self):
        previous=[{'text':'word','start':14}, {'start':14.2}]
        append_timed_segments(previous,[{'text':'word','start':0}],14,0)
        append_timed_segments(previous,[{'start':.2}],14,1)
        self.assertEqual(len(previous),4)
    def test_stt_cancellation_stops_after_current_window(self):
        event=threading.Event();original=self.provider.transcribe
        def transcribe(*args):
            result=original(*args);event.set();return result
        self.provider.transcribe=transcribe;token=request_cancelled.set(event)
        try:
            with self.assertRaises(InferenceTimeoutError):
                self.app.state.transcription_service.execute(wav_audio(16000,70),'stt-sensevoice-small-int8','en')
        finally:request_cancelled.reset(token)
        self.assertEqual(self.provider.stt,[15.])
    def test_http_client_disconnect_cancels_future_stt_windows(self):
        from smartvoice.api.v1.routes import _run_request_inference
        entered=threading.Event();release=threading.Event();calls=[]
        original=self.provider.transcribe
        def blocked_transcribe(*args,**kwargs):
            calls.append(len(calls)+1)
            if len(calls)==1:
                entered.set()
                release.wait(timeout=5)
            return original(*args,**kwargs)
        self.provider.transcribe=blocked_transcribe
        class DisconnectingRequest:
            def __init__(inner,app): inner.app=app;inner.checks=0
            async def is_disconnected(inner):
                inner.checks+=1
                return inner.checks>=2
        request=DisconnectingRequest(self.app)
        async def run_request():
            return await _run_request_inference(request,lambda: self.app.state.transcription_service.execute(
                wav_audio(16000,70),'stt-sensevoice-small-int8','en'))
        outcome=[]
        def serve():
            try: asyncio.run(run_request())
            except asyncio.CancelledError: outcome.append('cancelled')
        try:
            server=threading.Thread(target=serve,daemon=True);server.start()
            self.assertTrue(entered.wait(timeout=2),'STT did not begin before client disconnect')
            server.join(timeout=2)
            self.assertFalse(server.is_alive(),'request inference did not cancel after disconnect')
            self.assertEqual(outcome,['cancelled'])
            self.assertEqual(calls,[1])
        finally:
            release.set()
        server.join(timeout=3)
        deadline=time.monotonic()+2
        while self.app.state.inference_queue._reserved and time.monotonic()<deadline:
            time.sleep(.01)
        self.assertEqual(self.app.state.inference_queue._reserved,0)
        self.assertEqual(calls,[1])

    def test_real_asgi_disconnect_through_middleware_cancels_stt(self):
        """A real ASGI http.disconnect must pass through the app middleware."""
        entered=threading.Event();release=threading.Event();disconnect_sent=threading.Event();calls=[];observed=[]
        original=self.provider.transcribe
        def blocked_transcribe(*args,**kwargs):
            calls.append(len(calls)+1)
            if len(calls)==1:
                # Released by the test's finally block. A timeout here can
                # expire during gc.collect(), invalidating lease assertions.
                entered.set();release.wait()
            return original(*args,**kwargs)
        self.provider.transcribe=blocked_transcribe
        from starlette.requests import Request
        original_check=Request.is_disconnected
        async def observe(request):
            result=await original_check(request);observed.append(result);return result
        request=httpx.Request('POST','http://test/v1/audio/transcriptions',
            files={'file':('long.wav',wav_audio(16000,70),'audio/wav')},
            data={'model':'stt-sensevoice-small-int8','language':'en','response_format':'verbose_json'})
        body=request.read();messages=[{'type':'http.request','body':body,'more_body':False}]
        scope={'type':'http','asgi':{'version':'3.0','spec_version':'2.3'},'http_version':'1.1',
            'method':'POST','scheme':'http','path':'/v1/audio/transcriptions','raw_path':b'/v1/audio/transcriptions',
            'query_string':b'','headers':[(k.lower().encode(),v.encode()) for k,v in request.headers.items()],
            'server':('test',80),'client':('127.0.0.1',12345),'root_path':'','app':self.app}
        sent=[]
        async def receive():
            if messages:return messages.pop(0)
            if entered.is_set():
                disconnect_sent.set();return {'type':'http.disconnect'}
            # Starlette's BaseHTTPMiddleware starts a receive waiter concurrently;
            # yielding here lets the initial body reach the endpoint first.
            await asyncio.sleep(0)
            return {'type':'http.request','body':b'','more_body':True}
        async def send(message):sent.append(message)
        error=[];finished=threading.Event()
        async def invoke():
            try:
                with patch.object(Request,'is_disconnected',observe):
                    await self.app(scope,receive,send)
            except asyncio.CancelledError:pass
            except BaseException as exc:error.append(exc)
            finally:finished.set()
        server=threading.Thread(target=lambda:asyncio.run(invoke()),daemon=True);server.start()
        try:
            self.assertTrue(entered.wait(2),'native STT did not enter')
            self.assertTrue(disconnect_sent.wait(2),'the ASGI receive channel did not deliver http.disconnect')
            self.assertTrue(finished.wait(1),'request did not cancel promptly after disconnect')
            self.assertTrue(any(observed),'Request.is_disconnected did not observe the ASGI disconnect')
            self.assertEqual(calls,[1],'disconnect must prevent later STT windows from starting')
            self.assertFalse(any(m.get('type')=='http.response.start' for m in sent),
                             'a disconnected client must not receive a synthetic error response')
            # A cancelled request detaches the native worker from its caller.
            # Force collection to verify the queue keeps that worker alive.
            gc.collect()
            self.assertEqual(self.app.state.inference_queue._reserved,1,
                             'active native work must retain its queue slot until it returns')
        finally:release.set()
        server.join(3)
        self.assertFalse(server.is_alive(),'request did not finish after native work returned')
        self.assertEqual(error,[])
        deadline=time.monotonic()+2
        while self.app.state.inference_queue._reserved and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual(self.app.state.inference_queue._reserved,0)
    def test_duration_limit_before_native(self):
        result=self.client.post('/v1/audio/transcriptions',files={'file':('long.wav',wav_audio(16000,601),'audio/wav')},data={'model':'stt-sensevoice-small-int8','language':'en'})
        self.assertEqual(result.status_code,413);self.assertEqual(self.provider.stt,[])
    def test_assembler_cumulative_cap_and_cleanup(self):
        work=Path(self.directory.name)/'assembly'
        assembler=AudioAssembler(work,100000,1)
        with self.assertRaises(SpeechOutputTooLargeError):
            with assembler.session() as (append,finish):
                append(SynthesizedSpeech(wav_audio(),24000,.8));append(SynthesizedSpeech(wav_audio(),24000,.8))
        self.assertEqual(list(work.iterdir()),[])
    def test_assembler_rejects_changed_rate(self):
        with self.assertRaises(InferenceError):
            with AudioAssembler(Path(self.directory.name),100000,3).session() as (append,finish):
                append(SynthesizedSpeech(wav_audio(),24000,.8));append(SynthesizedSpeech(wav_audio(16000),16000,.8))
    def test_input_pcm_is_exact_and_storage_closes(self):
        raw=wav_audio(16000,.8)
        with BoundedAudioInput().prepare(raw) as prepared:self.assertEqual(prepared.sample(30),raw)
    def test_no_fake_word_timestamps(self):
        original=self.provider.transcribe
        def transcribe(*args):
            result=original(*args);result.pop('segments');return result
        self.provider.transcribe=transcribe
        outcome=self.app.state.transcription_service.execute(wav_audio(16000,40),'stt-sensevoice-small-int8','en')
        self.assertNotIn('segments',outcome.result)
    def test_planning_preserves_numbers_and_repetitions(self):
        text='Order 12.5 kilograms. '*30;self.assertEqual(''.join(split_text(text,200)),text)
        self.assertEqual(merge('again again','again again',0),'again again again again')
        self.assertEqual(merge('İZMİR hello','İZMİR hello there',1),'İZMİR hello there')
    def test_settings_validate_encoding(self):
        with self.assertRaises(ValueError):Settings(data_dir=Path(self.directory.name),tts_mp3_bitrate=1)
        with self.assertRaises(ValueError):Settings(data_dir=Path(self.directory.name),max_tts_internal_bytes=0)
    def test_unavailable_model_fails_before_decode(self):
        self.provider.installed_models=lambda: []
        result=self.client.post('/v1/audio/transcriptions',files={'file':('invalid.wav',b'broken','audio/wav')},data={'model':'stt-sensevoice-small-int8','language':'en'})
        self.assertEqual(result.status_code,503)
        self.assertEqual(self.provider.stt,[])
    def test_wav_without_av_import(self):
        with patch.dict('sys.modules',{'av':None}):
            encoder=AudioEncoder()
        audio=SynthesizedSpeech(wav_audio(),24000,.8)
        self.assertFalse(encoder.capabilities()['formats']['mp3']['available'])
        self.assertIs(encoder.encode(audio,'wav'),audio)
    def test_encoder_model_sample_rates_and_bitrates(self):
        for rate in [22050,24000,44100]:
            audio=SynthesizedSpeech(wav_audio(rate),rate,.8)
            for bitrate in [64000,96000,128000]:
                output=AudioEncoder(bitrate).encode(audio,'mp3')
                with av.open(io.BytesIO(output.audio)) as source:
                    duration=sum(f.samples/f.sample_rate for f in source.decode(audio=0))
                self.assertAlmostEqual(duration,.8,delta=.04)
    def test_legacy_config_budget_is_preserved_without_rewrite(self):
        import json
        config=Path(self.directory.name)/'settings.json'
        original='{"max_tts_output_bytes": 1048576}'
        config.write_text(original)
        with patch.dict('os.environ',{},clear=True):
            settings=Settings.from_env(config_path=config,data_dir_override=Path(self.directory.name))
        self.assertEqual(settings.max_tts_internal_bytes,1048576)
        self.assertEqual(config.read_text(),original)
if __name__=='__main__':unittest.main()
