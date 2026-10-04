"""Small real canonical audio for format conversion contract tests."""
import io
import math
import struct
import wave

def wav_audio(rate=24000,seconds=.8):
    output=io.BytesIO()
    with wave.open(output,'wb') as f:
        f.setnchannels(1);f.setsampwidth(2);f.setframerate(rate)
        period=b''.join(struct.pack('<h',int(4000*math.sin(2*math.pi*440*i/rate))) for i in range(rate))
        count=int(rate*seconds)
        f.writeframes(period*(count//rate)+period[:(count%rate)*2])
    return output.getvalue()
