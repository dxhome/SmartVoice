"""Finite committed-text TTS using installed product model assets."""
import io,re,wave
from dataclasses import replace
from smartvoice.domain.streaming import CommittedTarget,SynthesizedAudio,StageError

class SpeechAdapter:
    def __init__(self,settings,spec,plan):
        from smartvoice.adapters.inference.sherpa_onnx.provider import SherpaOnnxProvider
        self.provider=SherpaOnnxProvider(replace(settings,num_threads=plan.tts_threads,max_tts_audio_seconds=10,
            max_tts_internal_bytes=882044,max_tts_output_bytes=882044))
        self.fallback=plan.tts_fallback_model_id;self.fallback_sid=plan.tts_fallback_speaker_id
        self.spec=spec;self.language=plan.target_language;self.speed=plan.tts_speed;self.sid=plan.tts_speaker_id
        self.plan={'model_id':spec.id,'profile':spec.id,'chunk_max_chars':plan.tts_chunk_chars,'sample_rate':44100 if spec.model_type=='supertonic' else 22050,
                   'channels':1,'native_streaming':False}
        try:self._generate('Ready.' if self.language=='en' else '准备好了。')
        except BaseException:self.close();raise
    def _generate(self,text):
        return self.provider.synthesize(text,voice=str(self.sid),speed=self.speed,model_id=self.spec.id,language=self.language)
    def synthesize(self,target):
        if not isinstance(target,CommittedTarget) or not target.committed or target.language!=self.language:
            raise StageError('tts_input_not_committed','Expected committed text in the configured language')
        spoken=target.text
        mixed=False
        if self.spec.model_type=='matcha':
            spoken=spoken.replace('·','').replace('；','，').replace(';','，')
            mixed=bool(re.search(r'[A-Za-z]',spoken))
        if mixed and not self.fallback:
            raise StageError('tts_unsupported_text','No declared mixed-script voice')
        try:
            out = (self.provider.synthesize(spoken,voice=str(self.fallback_sid),speed=self.speed,
                model_id=self.fallback,language=self.language) if mixed else self._generate(spoken))
        except Exception as exc:raise StageError('tts_failure','Synthesis failed') from exc
        with wave.open(io.BytesIO(out.audio),'rb') as wav:
            rate=wav.getframerate();channels=wav.getnchannels();width=wav.getsampwidth();pcm=wav.readframes(wav.getnframes())
        if mixed and rate != 22050:
            pcm=resample_pcm16_mono(pcm,rate,22050);rate=22050
        if not pcm or (channels,width)!=(1,2) or len(pcm)>rate*2*10:
            raise StageError('tts_output_limit','Generated audio exceeds the chunk format or duration limit')
        return SynthesizedAudio(target.translation_id,target.language,pcm,rate,channels,profile=self.fallback if mixed else self.spec.id)
    def close(self):
        if self.provider:self.provider.close();self.provider=None

def resample_pcm16_mono(pcm,source_rate,target_rate):
    """Resample signed PCM16 mono through the project's PyAV dependency."""
    if not pcm or len(pcm)%2 or source_rate<=0 or target_rate<=0:
        raise StageError('tts_output_limit','Invalid PCM16 mono resampling input')
    if source_rate==target_rate:return pcm
    try:
        import av
        frame=av.AudioFrame(format='s16',layout='mono',samples=len(pcm)//2)
        frame.sample_rate=source_rate
        frame.planes[0].update(pcm)
        resampler=av.AudioResampler(format='s16',layout='mono',rate=target_rate)
        frames=resampler.resample(frame)+resampler.resample(None)
        return b''.join(bytes(out.planes[0])[:out.samples*2] for out in frames)
    except Exception as exc:
        raise StageError('tts_failure',f'pcm_resample_{type(exc).__name__}') from exc
