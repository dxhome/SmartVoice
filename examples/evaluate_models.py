"""Run installed STT models against a CSV reference corpus and report CER/WER."""

from __future__ import annotations

import argparse
import csv
import json
import platform
import re
import statistics
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import httpx
from jiwer import cer, wer


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", text).casefold()
    text = "".join(char for char in text if not unicodedata.category(char).startswith("P"))
    return re.sub(r"\s+", " ", text).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="CSV columns: audio_path,reference,language")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=Path("stt-evaluation.json"))
    args = parser.parse_args()

    rows: list[dict[str, object]] = []
    with args.manifest.open("r", encoding="utf-8-sig", newline="") as stream:
        for index, row in enumerate(csv.DictReader(stream), start=1):
            path = Path(row["audio_path"])
            reference = row["reference"]
            language = row.get("language", "auto") or "auto"
            started = time.perf_counter()
            with path.open("rb") as audio:
                response = httpx.post(
                    f"{args.base_url.rstrip('/')}/v1/audio/transcriptions",
                    files={"file": (path.name, audio, "application/octet-stream")},
                    data={"language": language, "response_format": "json"},
                    timeout=900,
                )
            response.raise_for_status()
            result = response.json()
            elapsed = time.perf_counter() - started
            hypothesis = str(result["text"])
            rows.append({
                "index": index,
                "audio_path": str(path),
                "language": language,
                "reference": reference,
                "hypothesis": hypothesis,
                "cer": cer(normalize(reference), normalize(hypothesis)),
                "wer": wer(normalize(reference), normalize(hypothesis)),
                "audio_duration_seconds": result.get("duration"),
                "processing_seconds": result.get("processing_seconds"),
                "request_elapsed_seconds": round(elapsed, 4),
                "device": result.get("device"),
                "model": result.get("model"),
            })

    by_language = {}
    for language in sorted({str(row["language"]) for row in rows}):
        subset = [row for row in rows if row["language"] == language]
        references = [normalize(str(row["reference"])) for row in subset]
        hypotheses = [normalize(str(row["hypothesis"])) for row in subset]
        by_language[language] = {
            "utterances": len(subset),
            "corpus_cer": cer(references, hypotheses),
            "corpus_wer": wer(references, hypotheses),
            "mean_rtf": statistics.mean(
                float(row["processing_seconds"]) / float(row["audio_duration_seconds"])
                for row in subset if row["audio_duration_seconds"] and row["processing_seconds"] is not None
            ) if any(row["audio_duration_seconds"] and row["processing_seconds"] is not None for row in subset) else None,
        }
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "runtime": httpx.get(f"{args.base_url.rstrip('/')}/v1/runtime", timeout=10).json(),
        "normalization": "Unicode NFC, casefold, remove Unicode punctuation, collapse whitespace",
        "by_language": by_language,
        "utterances": rows,
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(by_language, ensure_ascii=False, indent=2))
    print(f"Detailed report: {args.output.resolve()}")


if __name__ == "__main__":
    main()
