"""Run fixed-arrival streaming probes against isolated max_sessions configurations."""
import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from .capacity import run as run_capacity
from .run import MODES, percentiles


def summarize_runs(mode, rows):
    grouped = {}
    for level in rows:
        for run in level["runs"]:
            key = (level["max_sessions"], run["case"], run["arrival_multiplier"])
            grouped.setdefault(key, []).append(json.loads(Path(run["report"]).read_text(encoding="utf-8")))
    metric = ("audio_segment" if mode == "spoken_interpretation" else
              "target_partial" if mode == "translated_subtitles" else "source_unit_partial")
    summary = []
    for (configured_limit, case, multiplier), reports in sorted(grouped.items()):
        records = [record for report in reports for record in report.get("records", [])]
        successful = [record for record in records if record.get("functional_success")]
        failed = [record for record in records if not record.get("functional_success")]
        failure_types = {}
        for record in failed:
            reason = record.get("failure") or (record.get("terminal") or {}).get("code") or \
                (record.get("terminal") or {}).get("status") or "unknown"
            failure_types[reason] = failure_types.get(reason, 0) + 1
        latency = [record["first_output_seconds"][metric] for record in successful
                   if metric in record.get("first_output_seconds", {})]
        audio = mode == "spoken_interpretation"
        audio_success = sum(bool(record.get("output_audio_success")) for record in records) if audio else None
        full_audio = sum(bool(record.get("full_audio_success")) for record in records) if audio else None
        skip_reasons = {}
        if audio:
            for record in records:
                for event in record.get("final_snapshot", {}).get("audio_skipped", []):
                    reason = event.get("reason") or "unspecified"
                    skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
        summary.append(dict(
            configured_max_sessions=configured_limit, case=case, arrival_multiplier=multiplier,
            offered_arrivals=len(records), successful_sessions=len(successful),
            functional_failures=len(failed), failure_types=failure_types,
            success_rate=(len(successful) / len(records) if records else None),
            peak_client_attempts=max((report["observed_peak_overlapping_client_attempts"] for report in reports), default=0),
            peak_successful_overlap=max((report["observed_peak_successful_session_overlap"] for report in reports), default=0),
            first_output_metric=metric, first_output_seconds=percentiles(latency),
            output_audio_success_sessions=audio_success,
            output_audio_success_rate=(audio_success / len(records) if audio and records else None),
            full_audio_success_sessions=full_audio,
            full_audio_success_rate=(full_audio / len(records) if audio and records else None),
            no_audio_sessions=(sum(not record.get("final_snapshot", {}).get("audio_segments") for record in records) if audio else None),
            audio_skip_reasons=skip_reasons,
            strict_quality_guard_passed=all((report.get("strict_quality_guard") or {}).get("passed", False)
                                             for report in reports),
            quality_compared_successful_sessions=len(successful),
            max_root_process_cpu_core_equivalents_p95=max(
                (report.get("cpu_core_equivalents_p95") for report in reports
                 if report.get("cpu_core_equivalents_p95") is not None), default=None),
            max_sampled_root_rss_bytes=max((report.get("peak_summed_rss_bytes") for report in reports
                                            if report.get("peak_summed_rss_bytes") is not None), default=None),
            process_tree_complete=all((report.get("resources") or {}).get("process_tree_complete", False)
                                      for report in reports),
            rounds=len(reports)))
    return summary


def stop_group(process):
    if process.poll() is not None:
        return True
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    try:
        process.wait(timeout=20)
        return True
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=10)
        return False


async def wait_healthy(process, port, timeout):
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Server exited before becoming healthy (exit {process.returncode})")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            pass
        await asyncio.sleep(.25)
    raise TimeoutError(f"Server did not become healthy at {url}")


async def run(args):
    output_dir = args.output.parent / (args.output.stem + "-runs")
    output_dir.mkdir(parents=True, exist_ok=True)
    case_ids = args.cases.split(",")
    cases = []
    for case_id in case_ids:
        cases.append(case_id.strip())
    baseline = args.baseline
    rows = []
    for level_index, max_sessions in enumerate(args.levels):
        port = args.base_port + level_index
        config_path = output_dir / f"max-sessions-{max_sessions}.config.json"
        log_path = output_dir / f"max-sessions-{max_sessions}.server.log"
        config_path.write_text(json.dumps({
            "server_host": "127.0.0.1", "server_port": port,
            "log_level": "INFO", "num_threads": args.num_threads,
            "streaming_max_sessions": max_sessions,
            "streaming_memory_mib": args.memory_mib,
            "streaming_compute_slots": args.compute_slots,
        }, indent=2), encoding="utf-8")
        log = log_path.open("w", encoding="utf-8")
        command = [sys.executable, "-m", "smartvoice", "--config", str(config_path),
                   "--data-dir", str(args.data_dir), "--port", str(port)]
        process = subprocess.Popen(command, cwd=Path.cwd(), stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        config_row = dict(max_sessions=max_sessions, memory_mib=args.memory_mib,
                          compute_slots=args.compute_slots, port=port,
                          server_pid=process.pid, log=str(log_path), runs=[])
        rows.append(config_row)
        try:
            await wait_healthy(process, port, args.startup_timeout_seconds)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/audio/stream/capabilities", timeout=5) as response:
                capabilities = json.load(response)
            config_row["advertised_max_sessions"] = capabilities.get("limits", {}).get("max_sessions")
            config_row["estimated_model_memory_mib"] = capabilities.get("limits", {}).get("estimated_model_memory_mib")
            if config_row["advertised_max_sessions"] != max_sessions:
                raise RuntimeError("Started service did not advertise requested max_sessions")
            for case_id in cases:
                for multiplier in args.arrival_multipliers:
                    rate = max_sessions * multiplier / args.arrival_window_seconds
                    for round_index in range(1, args.rounds + 1):
                        report_path = output_dir / f"max{max_sessions}-{args.mode}-{case_id}-x{multiplier}-r{round_index}.json"
                        await run_capacity(SimpleNamespace(
                            url=f"ws://127.0.0.1:{port}/v1/audio/stream",
                            manifest=args.manifest, audio_root=args.audio_root, case=case_id,
                            mode=args.mode, arrival_rate=rate,
                            duration_seconds=args.arrival_window_seconds,
                            server_pid=process.pid, cpu_cores=args.cpu_cores,
                            baseline=baseline, output=report_path))
                        report = json.loads(report_path.read_text(encoding="utf-8"))
                        config_row["runs"].append(dict(round=round_index, case=case_id, arrival_multiplier=multiplier, report=str(report_path),
                            offered_rate=rate, sessions=report["scheduled_sessions"],
                            successful=report["scheduled_sessions"]-report["functional_failures"],
                            failures=report["functional_failures"], failure_types={
                                kind: sum(row.get("failure") == kind or (row.get("terminal") or {}).get("code") == kind
                                          for row in report["records"])
                                for kind in sorted({row.get("failure") or (row.get("terminal") or {}).get("code")
                                                    for row in report["records"]
                                                    if row.get("failure") or (row.get("terminal") or {}).get("code")})},
                            peak_client_attempts=report["observed_peak_overlapping_client_attempts"],
                            peak_successful_overlap=report["observed_peak_successful_session_overlap"],
                            first_output=report["first_output_latency_seconds"],
                            cpu_core_p95=report.get("cpu_core_equivalents_p95"),
                            peak_rss_bytes=report.get("peak_summed_rss_bytes"),
                            process_tree_complete=(report.get("resources") or {}).get("process_tree_complete"),
                            strict_quality_guard_successful_only=report.get("strict_quality_guard"),
                            quality_guard_excluded_failed_attempts=report.get("quality_guard_excluded_failed_attempts")))
        except Exception as exc:
            config_row["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            config_row["graceful_process_exit"] = stop_group(process)
            log.close()
            config_path.unlink(missing_ok=True)
        print(f"max_sessions={max_sessions} complete", flush=True)

    result = dict(schema="smartvoice.stream.capacity-matrix.v1",
        measurement="Isolated server per max_sessions value; open-loop arrival ramp with 1x PCM pacing; memory budget held constant",
        route=dict(mode=args.mode, cases=cases), levels=rows,
        offered_rate_rule="arrival rate = max_sessions * arrival_multiplier / arrival_window_seconds; each round schedules max_sessions * arrival_multiplier requests per case",
        rounds_per_level=args.rounds,
        arrival_multipliers=args.arrival_multipliers,
        arrival_window_seconds=args.arrival_window_seconds,
        scenario_summary=summarize_runs(args.mode, rows),
        configured_memory_mib=args.memory_mib, configured_compute_slots=args.compute_slots,
        limitation="Per-session model-memory admission may cap effective concurrency below max_sessions; process-tree sampling can be permission-limited.")
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False)
    print(args.output, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--levels", type=lambda value: [int(item) for item in value.split(",")], default=[2,4,8,16])
    parser.add_argument("--mode", choices=MODES, default="transcription")
    parser.add_argument("--cases", default="zh_02_clean,en_02_clean")
    parser.add_argument("--manifest", type=Path, default=Path("benchmarks/streaming/data/fleurs/clean.manifest.json"))
    parser.add_argument("--audio-root", type=Path, default=Path("benchmarks/streaming"))
    parser.add_argument("--baseline", type=Path, default=Path("benchmarks/streaming/baselines/product-p0-2026-10-09-r1.json"))
    parser.add_argument("--data-dir", type=Path, required=True, help="Existing model data directory; no downloads are performed")
    parser.add_argument("--memory-mib", type=int, default=2800, help="Fixed safety budget for every level")
    parser.add_argument("--compute-slots", type=int, default=2)
    parser.add_argument("--num-threads", type=int, default=2)
    parser.add_argument("--arrival-window-seconds", type=float, default=10)
    parser.add_argument("--rounds", type=int, default=3,
                        help="Independent fixed-arrival repetitions for each level/case (default: 3)")
    parser.add_argument("--arrival-multipliers", type=lambda value: [int(item) for item in value.split(",")],
                        default=[1], help="Offered arrivals as multiples of configured max_sessions (e.g. 1,2,4,8)")
    parser.add_argument("--startup-timeout-seconds", type=float, default=90)
    parser.add_argument("--base-port", type=int, default=8872)
    parser.add_argument("--cpu-cores", type=int, default=8)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.levels or any(level < 1 for level in args.levels):
        parser.error("levels must be positive integers")
    if not args.arrival_multipliers or any(multiplier < 1 for multiplier in args.arrival_multipliers):
        parser.error("arrival multipliers must be positive integers")
    if args.memory_mib <= 0 or args.compute_slots <= 0 or args.num_threads <= 0:
        parser.error("memory, compute slots and thread count must be positive")
    if args.arrival_window_seconds <= 0 or args.rounds <= 0 or args.base_port < 1024 or args.base_port + len(args.levels) > 65535:
        parser.error("invalid arrival window or port range")
    if args.output.exists():
        parser.error("Output exists; use a fresh report path")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
