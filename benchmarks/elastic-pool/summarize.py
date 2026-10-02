"""Aggregate saved measurements without loading models or running inference."""
import argparse
import json
import math
import statistics
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent
SCENARIOS = json.loads((SCRIPT_ROOT / "scenarios.json").read_text())["scenarios"]


def percentile(values, q=0.9):
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * q) - 1] if ordered else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, default=Path(".smartvoice-dev/benchmark-concurrency"))
    args = parser.parse_args()
    results = []
    for path in sorted(args.directory.glob("*-confirmation-*.json")):
        result = json.loads(path.read_text())
        if not result["label"].startswith("confirmation-"):
            continue
        result.setdefault("baseline_p90", result["p90"] / result["p90_ratio"])
        results.append({key: value for key, value in result.items() if key != "rows"} | {"raw_file": str(path.resolve())})
    warmups = {}
    for scenario in SCENARIOS:
        path = args.directory / f"{scenario}-warm-trials.json"
        if not path.is_file():
            continue
        trials = json.loads(path.read_text())
        first = [trial["first_request"]["latency"] for trial in trials]
        expansion = [max(row["latency"] for row in trial["expansion_requests"]) for trial in trials]
        initialization = [trial["warmups"][-1]["seconds"] for trial in trials]
        warm = [row["latency"] for trial in trials for row in trial["warm_requests"]]
        warmups[scenario] = {
            "metadata": trials[-1].get("metadata") if trials else None,
            "trials": len(trials), "first_request_p90": percentile(first),
            "expansion_request_p90": percentile(expansion), "expansion_initialization_p90": percentile(initialization),
            "faster_request_during_expansion_p90": percentile([min(row["latency"] for row in trial["expansion_requests"]) for trial in trials]),
            "warm_request_p90": percentile(warm),
            "baseline_rss_mib_median": statistics.median(trial["baseline_rss_mib"] for trial in trials),
            "one_instance_rss_mib_median": statistics.median(trial["one_rss_mib"] for trial in trials),
            "two_instance_rss_mib_median": statistics.median(trial["two_rss_mib"] for trial in trials),
            "first_warmup_peak_rss_mib": max(trial["first_resources"]["rss_peak_mib"] for trial in trials),
            "expansion_peak_rss_mib": max(trial["expansion_resources"]["rss_peak_mib"] for trial in trials),
            "two_fully_warm_peak_rss_mib": max(trial["two_warm_resources"]["rss_peak_mib"] for trial in trials),
            "first_warmup_cpu_mean_cores_median": statistics.median(trial["first_resources"]["cpu_mean_cores"] for trial in trials),
            "expansion_cpu_mean_cores_median": statistics.median(trial["expansion_resources"]["cpu_mean_cores"] for trial in trials),
            "one_warm_cpu_mean_cores_median": statistics.median(trial["one_warm_resources"]["cpu_mean_cores"] for trial in trials),
            "two_warm_cpu_mean_cores_median": statistics.median(trial["two_warm_resources"]["cpu_mean_cores"] for trial in trials),
            "raw_file": str(path.resolve()),
        }
    output = {"confirmed_cases": results, "warmups": warmups}
    (args.directory / "summary.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
