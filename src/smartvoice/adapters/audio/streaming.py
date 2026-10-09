"""WAV packaging kept at the audio-format boundary."""
import io
import wave

def package_wav(pcm, sample_rate):
    output=io.BytesIO()
    with wave.open(output,'wb') as wav:
        wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(sample_rate);wav.writeframes(pcm)
    return output.getvalue()
