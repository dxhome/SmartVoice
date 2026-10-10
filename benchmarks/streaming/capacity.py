"""Open-loop fixed-arrival streaming capacity probe (one mode/direction per run)."""
import argparse
import asyncio
import hashlib
import json
import math
import platform
import time
from pathlib import Path
from importlib.metadata import version
from urllib.request import urlopen
from urllib.parse import urlsplit, urlunsplit

from .run import MODES, compare, percentiles, replay
from .resources import ResourceSampler


async def run(args):
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    case_by_id = {case["id"]: case for case in manifest["cases"]}
    if args.case not in case_by_id:
        raise ValueError(f"Unknown case {args.case!r}; choose from {', '.join(case_by_id)}")
    case = case_by_id[args.case]
    if case.get("variant") != "clean":
        raise ValueError("Capacity probe requires a pinned clean fixture")

    sampler = ResourceSampler(args.server_pid, args.cpu_cores) if args.server_pid else None
    parsed = urlsplit(args.url)
    capabilities_url = urlunsplit(("https" if parsed.scheme == "wss" else "http", parsed.netloc,
                                   "/v1/audio/stream/capabilities", "", ""))
    try:
        with urlopen(capabilities_url, timeout=5) as response:
            capabilities = json.load(response)
    except Exception as exc:
        capabilities = {"unavailable": type(exc).__name__}
    if sampler:
        sampler.start()
    start = time.perf_counter()
    active = 0
    peak_active = 0
    concurrency_samples = []
    arrivals = []
    async def session(index, scheduled):
        nonlocal active, peak_active
        actual = time.perf_counter()
        arrival = dict(index=index, scheduled_seconds=scheduled-start,
                       actual_seconds=actual-start, schedule_lag_seconds=actual-scheduled)
        arrivals.append(arrival)
        active += 1
        peak_active = max(peak_active, active)
        concurrency_samples.append(dict(at=actual-start, active=active))
        try:
            result = await replay(args.url, case, args.audio_root, args.mode, None)
            result["session_wall_seconds"] = time.perf_counter() - actual
            result["arrival_index"] = index
            result["actual_start_seconds"] = arrival["actual_seconds"]
            return result
        finally:
            active -= 1
            concurrency_samples.append(dict(at=time.perf_counter()-start, active=active))

    jobs = []
    try:
        count = math.ceil(args.arrival_rate * args.duration_seconds)
        for index in range(count):
            scheduled = start + index / args.arrival_rate
            delay = scheduled - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            jobs.append(asyncio.create_task(session(index, scheduled)))
        records = await asyncio.gather(*jobs, return_exceptions=True)
    finally:
        resource_metrics = await sampler.finish() if sampler else None

    normalized = []
    errors = []
    for i, record in enumerate(records):
        if isinstance(record, BaseException):
            normalized.append(dict(arrival_index=i, failure=type(record).__name__, functional_success=False,
                                   output_audio_success=False, full_audio_success=False))
            errors.append(dict(arrival_index=i, error=repr(record)))
        else:
            normalized.append(record)
    elapsed_values = [r["session_wall_seconds"] for r in normalized if "session_wall_seconds" in r]
    failed = sum(not row.get("functional_success", False) for row in normalized)
    overlap_events = []
    for row in normalized:
        if row.get("functional_success") and row.get("session_wall_seconds") is not None:
            at = row.get("actual_start_seconds", 0)
            overlap_events.extend(((at, 1), (at + row["session_wall_seconds"], -1)))
    successful_overlap = 0
    peak_successful_overlap = 0
    for _, delta in sorted(overlap_events):
        successful_overlap += delta
        peak_successful_overlap = max(peak_successful_overlap, successful_overlap)
    cpu_equiv = (resource_metrics or {}).get("cpu_core_equivalents", {}).get("p90")
    first_kind = ("audio_segment" if args.mode == "spoken_interpretation" else
                  "target_partial" if args.mode == "translated_subtitles" else "source_unit_partial")
    latency_values = [row["first_output_seconds"][first_kind] for row in normalized
                      if row.get("functional_success") and first_kind in row.get("first_output_seconds", {})]
    report = dict(
        schema="smartvoice.stream.capacity.v1",
        measurement="open_loop_fixed_arrival_rate; one clean audio fixture, one route per run; 1x PCM pacing",
        mode=args.mode, case=args.case, source_language=case["language"],
        offered_arrival_rate_sessions_per_second=args.arrival_rate,
        requested_duration_seconds=args.duration_seconds,
        scheduled_sessions=len(normalized), actual_test_window_seconds=time.perf_counter()-start,
        observed_peak_overlapping_client_attempts=peak_active,
        observed_peak_successful_session_overlap=peak_successful_overlap,
        active_session_samples=concurrency_samples,
        arrival_schedule=arrivals,
        functional_failures=failed,
        failure_rate=failed/len(normalized) if normalized else None,
        successful_sessions_per_second=sum(bool(r.get("functional_success")) for r in normalized)/max(time.perf_counter()-start, 1e-9),
        first_output_metric=first_kind,
        first_output_latency_seconds=percentiles(latency_values),
        first_output_missing_count=sum(first_kind not in row.get("first_output_seconds", {}) for row in normalized),
        output_audio_success_rate=(sum(bool(r.get("output_audio_success")) for r in normalized)/len(normalized)
                                   if args.mode == "spoken_interpretation" and normalized else None),
        full_audio_success_rate=(sum(bool(r.get("full_audio_success")) for r in normalized)/len(normalized)
                                 if args.mode == "spoken_interpretation" and normalized else None),
        session_duration_seconds=percentiles(elapsed_values),
        cpu_core_equivalents_p90=cpu_equiv,
        cpu_core_equivalents_p95=(resource_metrics or {}).get("cpu_core_equivalents", {}).get("p95"),
        peak_summed_rss_bytes=(resource_metrics or {}).get("peak_summed_rss_bytes"),
        peak_successful_sessions_per_physical_core=(peak_successful_overlap/args.cpu_cores if args.cpu_cores else None),
        peak_successful_sessions_per_observed_cpu_core_equivalent=(peak_successful_overlap/cpu_equiv if cpu_equiv and cpu_equiv > 0 else None),
        cores=args.cpu_cores,resources=resource_metrics,
        advertised_max_sessions=capabilities.get("limits", {}).get("max_sessions"),
        effective_capabilities=capabilities,
        capacity_status="measurement only; acceptance requires quality/latency gates and repeated runs",
        manifest_sha256=hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        fixture_sha256=case["sha256"],
        environment=dict(platform=platform.platform(), python=platform.python_version(),
                         versions={name: version(name) for name in ("websockets", "psutil")}),
        records=normalized, runner_errors=errors)
    if args.baseline:
        baseline_data = json.loads(args.baseline.read_text(encoding="utf-8"))
        successful_records = [row for row in normalized if row.get("functional_success")]
        report["strict_quality_guard"] = compare(baseline_data, successful_records)
        report["quality_guard_scope"] = "successful sessions only; rejected/failed attempts are counted separately"
        report["quality_guard_failed_attempts_excluded"] = len(normalized) - len(successful_records)
        report["baseline_sha256"] = hashlib.sha256(args.baseline.read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(args.output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://127.0.0.1:8766/v1/audio/stream")
    parser.add_argument("--manifest", type=Path, default=Path("benchmarks/streaming/data/fleurs/clean.manifest.json"))
    parser.add_argument("--audio-root", type=Path, default=Path("benchmarks/streaming"))
    parser.add_argument("--case", default="zh_02_clean")
    parser.add_argument("--mode", choices=MODES, default="translated_subtitles")
    parser.add_argument("--arrival-rate", type=float, required=True, help="Offered sessions per second")
    parser.add_argument("--duration-seconds", type=float, default=60)
    parser.add_argument("--server-pid", type=int)
    parser.add_argument("--cpu-cores", type=int)
    parser.add_argument("--baseline", type=Path, help="Frozen product snapshot for strict final/audio comparison")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.arrival_rate <= 0 or args.duration_seconds <= 0:
        parser.error("arrival-rate and duration-seconds must be positive")
    if args.server_pid is not None and args.server_pid <= 0:
        parser.error("server-pid must be positive")
    if args.cpu_cores is not None and args.cpu_cores <= 0:
        parser.error("cpu-cores must be positive")
    if args.output.exists():
        parser.error("Output exists; use a new report path")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
