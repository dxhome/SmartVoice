"""Aggregate local TTS blind-listening ratings without copying raw ratings to Git."""

from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RATING_COLUMNS = {
    "naturalness_1_to_5": "naturalness_mos",
    "intelligibility_1_to_5": "intelligibility_mos",
    "pronunciation_prosody_1_to_5": "pronunciation_prosody_mos",
}


def _mean_interval(values: list[float], seed: int = 271828) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "lower_95": None, "upper_95": None}
    rng = random.Random(seed)
    means = sorted(statistics.mean(rng.choices(values, k=len(values))) for _ in range(2000))
    return {
        "mean": round(statistics.mean(values), 4),
        "lower_95": round(means[49], 4),
        "upper_95": round(means[1949], 4),
    }


def aggregate(ratings_path: Path, mapping_path: Path) -> dict[str, Any]:
    mapping_document = json.loads(mapping_path.read_text(encoding="utf-8"))
    mapping = mapping_document.get("samples", mapping_document)
    grouped: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    with ratings_path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            sample = mapping.get(row.get("sample_key", ""))
            if not sample:
                raise ValueError(f"Unknown sample_key in ratings: {row.get('sample_key')!r}")
            for column, metric in RATING_COLUMNS.items():
                raw = (row.get(column) or "").strip()
                if not raw:
                    continue
                score = float(raw)
                if not 1 <= score <= 5:
                    raise ValueError(f"{column} must be a score from 1 to 5")
                grouped[(sample["model_id"], sample["language"])][metric].append(score)
    results = []
    for (model_id, language), metrics in sorted(grouped.items()):
        results.append({
            "model_id": model_id,
            "language": language,
            "rating_count": max((len(values) for values in metrics.values()), default=0),
            "metrics": {name: _mean_interval(values) for name, values in sorted(metrics.items())},
        })
    return {
        "schema_version": "1.0",
        "category": "quality",
        "evaluation": "tts_human_blind_listening",
        "run_id": mapping_document.get("run_id"),
        "benchmark_suite": mapping_document.get("benchmark_suite"),
        "config_sha256": mapping_document.get("config_sha256"),
        "dataset": mapping_document.get("dataset"),
        "categories": {"quality": {"tts": results}},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
        "note": "Aggregated MOS results only; raw ratings and model mapping remain local.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ratings", required=True, type=Path, help="Completed local tts-ratings-template.csv")
    parser.add_argument("--mapping", required=True, type=Path, help="Private local blind-mapping.json")
    parser.add_argument("--output", required=True, type=Path, help="Aggregate result JSON; choose a path under benchmarks/result to commit it")
    args = parser.parse_args()
    try:
        report = aggregate(args.ratings, args.mapping)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Aggregated TTS quality result: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
