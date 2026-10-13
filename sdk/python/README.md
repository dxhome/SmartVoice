# SmartVoice Python async SDK prototype

An experimental asynchronous client for the supported speech APIs: STT, TTS,
and Streaming. Capability discovery reports currently installed runtime models
and usable language routes. This SDK intentionally has no model installation,
import/export, catalog-management, or router-configuration APIs.

## Install from a SmartVoice checkout

```sh
python -m pip install -e ./sdk/python
```

The service must be running. Audio paths passed to `transcribe()` can use any
format accepted by the service; Streaming accepts only 16 kHz, mono, PCM16.

## Discover language capabilities

```python
import asyncio
from smartvoice_sdk import SmartVoiceClient

async def main():
    async with SmartVoiceClient("http://127.0.0.1:8000") as client:
        capabilities = await client.capabilities()
        print(capabilities.languages_for("transcription", model="smartvoice-auto"))
        print(capabilities.languages_for("speech", model="smartvoice-auto", include_auto=True))

        streaming = await client.streaming_capabilities()
        print(streaming.language_pairs("translated_subtitles"))
        print(streaming.language_pairs("spoken_interpretation", available_only=True))

asyncio.run(main())
```

`languages_for()` derives STT/TTS languages from `/v1/capabilities`; for
`smartvoice-auto`, it includes languages with an installed candidate in the
read-only runtime router status. `include_auto=True` adds the API's `auto`
request option. Streaming mode/language pairs and their current readiness come
from `/v1/audio/stream/capabilities`.

## STT and TTS

```sh
python sdk/python/examples/stt_tts.py transcription ./speech.wav --language auto
python sdk/python/examples/stt_tts.py speech "Hello from SmartVoice" --language en --format wav --output hello.wav
```

The async methods return typed results with response text/audio, actual model,
resolved language, response headers, and (for verbose STT) timestamped segments.
The default model is `smartvoice-auto`; an installed model ID can be passed to
`model=` for direct inference.

```python
from pathlib import Path

async with SmartVoiceClient() as client:
    stt = await client.transcribe("./speech.wav", language="auto")
    print(stt.text, stt.model, stt.language)

    tts = await client.synthesize("Hello", language="en", response_format="wav")
    Path("hello.wav").write_bytes(tts.audio)
```

## Streaming

Input must be **16 kHz, mono, PCM16 WAV**. The sample checks capabilities,
starts a session, sends 20 ms frames in real time, prints events, and saves any
returned speech chunks as WAV files.

```sh
python sdk/python/examples/stream_file.py ./speech.wav
python sdk/python/examples/stream_file.py ./speech.wav --mode translated_subtitles --source zh
python sdk/python/examples/stream_file.py ./speech.wav --mode spoken_interpretation --source en
```

Use `--fast` to send the recording as quickly as the socket accepts frames.
This is useful for functional checks, but it does not represent real-time
microphone behavior and may exceed the service's input queue limit. Use real-time
pacing for long or resumable sessions.

The streaming client validates mode/language pairs against the server's live
capability response, handles PCM framing, heartbeats, event ACKs, terminal
errors, and ordered WAV chunks. `events()` ACKs an event after the caller
advances the iterator. In `output_consumption="playback"` mode, applications
must call `mark_audio_started()` and `mark_audio_played()` after actual
playback.

### Recover an interrupted stream

Set `resumable=True` to opt in to automatic WebSocket recovery. The SDK keeps
the session token and cursors in memory, reconnects within the server's recovery
grace period, replays unacknowledged input frames, and resumes application
events from the last acknowledged cursor:

```python
async with await client.start_stream(
    mode="transcription", source_language="zh", resumable=True,
) as session:
    async def send_audio():
        await session.send_audio(pcm16, realtime=True)
        await session.finish()

    async def receive_events():
        async for event in session.events():
            print(event.type, event.data)

    sender = asyncio.create_task(send_audio())
    await receive_events()
    await sender
```

Consume `events()` while sending audio in resumable mode. The SDK retains up
to the server-advertised input replay window until `input_ack` arrives; it raises
`StreamRecoveryError` rather than silently discarding audio if that window fills.
Recovery is limited to the same live SDK object and server process, and must
complete within the server's advertised grace period. It doesn't survive process
restarts. The server also bounds replayable output events and audio bytes.

The file example enables this behavior with `--resumable`.

## Prototype limits

- Audio capture, resampling, and playback-device integration remain application
  responsibilities.
- The capability objects are read-only runtime information. They do not expose
  model lifecycle or router configuration mutations.
