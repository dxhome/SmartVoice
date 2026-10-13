"""Stream a normalized PCM16 WAV through SmartVoice and print live events."""

from __future__ import annotations

import argparse
import asyncio
import json
import wave
from pathlib import Path

from smartvoice_sdk import SmartVoiceClient


def load_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as source:
        actual = (source.getframerate(), source.getnchannels(), source.getsampwidth())
        if actual != (16_000, 1, 2):
            raise ValueError(f"Expected 16 kHz mono PCM16 WAV; got {actual}")
        return source.readframes(source.getnframes())


async def run(args: argparse.Namespace) -> None:
    pcm = load_pcm(args.wav)
    target = None if args.mode == "transcription" else ("en" if args.source == "zh" else "zh")
    args.output.mkdir(parents=True, exist_ok=True)
    async with SmartVoiceClient(args.base_url) as client:
        capabilities = await client.streaming_capabilities()
        print("Streaming capability response:")
        print(json.dumps(capabilities.raw, ensure_ascii=False, indent=2))
        chain = capabilities.chain(args.mode, args.source, target)
        if not chain or not chain.get("available"):
            reason = chain.get("reason", "chain_not_reported") if chain else "chain_not_reported"
            raise RuntimeError(f"Streaming chain is unavailable: {reason}; see capability output above")
        async with await client.start_stream(
            mode=args.mode,
            source_language=args.source,
            target_language=target,
            output_consumption="delivery",
            resumable=args.resumable,
        ) as session:
            ready_data = dict(session.ready.data)
            recovery = ready_data.get("recovery")
            if isinstance(recovery, dict) and "resume_token" in recovery:
                ready_data["recovery"] = {**recovery, "resume_token": "<redacted>"}
            print("Session ready:", json.dumps(ready_data, ensure_ascii=False))

            async def send_audio() -> None:
                await session.send_audio(pcm, realtime=not args.fast)
                await session.finish()

            async def receive_events() -> None:
                async for event in session.events():
                    if event.audio:
                        path = args.output / f"audio-{event.audio.sequence:04d}.wav"
                        with path.open("xb") as output:
                            output.write(event.audio.audio_bytes)
                        print(f"audio chunk {event.audio.sequence}: {path}")
                    else:
                        print(json.dumps(event.data, ensure_ascii=False))

            sender = asyncio.create_task(send_audio())
            try:
                await receive_events()
                await sender
            finally:
                if not sender.done():
                    sender.cancel()
                    await asyncio.gather(sender, return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", type=Path, help="16 kHz mono PCM16 WAV")
    parser.add_argument(
        "--mode",
        choices=("transcription", "translated_subtitles", "spoken_interpretation"),
        default="transcription",
    )
    parser.add_argument("--source", choices=("zh", "en"), default="zh")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=Path("smartvoice-audio"))
    parser.add_argument(
        "--fast", action="store_true",
        help="Send frames without real-time pacing (may exceed the server input queue)",
    )
    parser.add_argument(
        "--resumable", action="store_true",
        help="Enable automatic same-process recovery after a WebSocket interruption",
    )
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
