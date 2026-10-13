"""Run a transcription or speech synthesis request through the async SDK."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from smartvoice_sdk import SmartVoiceClient


async def run(args: argparse.Namespace) -> None:
    async with SmartVoiceClient(args.base_url) as client:
        capabilities = await client.capabilities(task=args.task)
        languages = capabilities.languages_for(
            args.task, model=args.model, include_auto=True,
        )
        print(f"{args.task} languages for {args.model}: {', '.join(languages) or '(none)'}")

        if args.task == "transcription":
            result = await client.transcribe(
                args.input,
                model=args.model,
                language=args.language,
                response_format="verbose_json",
                timestamps=True,
            )
            print(result.text)
            if result.segments:
                print(f"Received {len(result.segments)} timestamped segments")
            print(f"Actual model: {result.model}; language: {result.language}")
        else:
            result = await client.synthesize(
                args.input,
                model=args.model,
                language=args.language,
                response_format=args.format,
            )
            output_path = args.output or Path(f"speech.{result.format}")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with output_path.open("xb") as output:
                output.write(result.audio)
            print(f"Saved {len(result.audio)} bytes to {output_path}")
            print(f"Actual model: {result.model}; format: {result.format}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", choices=("transcription", "speech"), help="STT or TTS task")
    parser.add_argument("input", help="Audio path for STT; text for TTS")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--model", default="smartvoice-auto")
    parser.add_argument("--language", default="auto")
    parser.add_argument("--format", choices=("mp3", "wav"), default="mp3")
    parser.add_argument("--output", type=Path)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
