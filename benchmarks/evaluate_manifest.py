"""Run installed STT models against a CSV reference corpus and report CER/WER."""

from __future__ import annotations

import argparse
import csv
import json
import platform
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from benchmarks.metrics import error_rate
from smartvoice.config.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="CSV columns: audio_path,reference,language")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--model", required=True, help="Installed model ID to evaluate")
    parser.add_argument("--output", type=Path, help="Aggregate JSON output; defaults outside the repository")
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
                    data={"model": args.model, "language": language, "response_format": "json"},
                    timeout=900,
                )
            response.raise_for_status()
            result = response.json()
            elapsed = time.perf_counter() - started
            hypothesis = str(result["text"])
            rows.append({
                "index": index,
                "language": language,
                "reference": reference,
                "hypothesis": hypothesis,
                "audio_duration_seconds": result.get("duration"),
                "processing_seconds": result.get("request_processing_seconds"),
                "request_elapsed_seconds": round(elapsed, 4),
            })

    by_language = {}
    for language in sorted({str(row["language"]) for row in rows}):
        subset = [row for row in rows if row["language"] == language]
        profile = "han_cer" if language.startswith("zh") else "latin_wer"
        references = [str(row["reference"]) for row in subset]
        hypotheses = [str(row["hypothesis"]) for row in subset]
        rtf_values = [
            float(row["processing_seconds"]) / float(row["audio_duration_seconds"])
            for row in subset if row["audio_duration_seconds"] and row["processing_seconds"] is not None
        ]
        by_language[language] = {
            "utterances": len(subset),
            "metric": "cer" if profile == "han_cer" else "wer",
            "error_rate": error_rate(references, hypotheses, profile),
            "mean_rtf": statistics.mean(rtf_values) if rtf_values else None,
        }
    report = {
        "schema_version": "1.0",
        "category": "quality",
        "model_id": args.model,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "runtime": {key: httpx.get(f"{args.base_url.rstrip('/')}/v1/runtime", timeout=10).json().get(key) for key in ("backend", "actual_device", "runtime_version", "host")},
        "by_language": by_language,
        "evaluated_utterances": len(rows),
    }
    output = args.output or Settings.from_env().data_dir / "benchmark-runs" / "custom-corpus-quality.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(by_language, ensure_ascii=False, indent=2))
    print(f"Aggregate report: {output.resolve()}")


if __name__ == "__main__":
    main()
