"""Run quality, serial performance, and concurrent-load comparisons."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import os
import platform
import random
import socket
import statistics
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from benchmarks.datasets import SpeechSample, load_fleurs_samples
from benchmarks.metrics import numeric_summary, quality_summary
from smartvoice import __version__
from smartvoice.config.settings import Settings


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read benchmark config {path}: {exc}") from exc
    if not isinstance(config, dict) or config.get("schema_version") != "1.0":
        raise ValueError("Benchmark config must be an object with schema_version '1.0'.")
    if not isinstance(config.get("models"), list) or not config["models"]:
        raise ValueError("Benchmark config must declare at least one model.")
    if not isinstance(config.get("languages"), dict) or not config["languages"]:
        raise ValueError("Benchmark config must declare language profiles.")
    return config


def _server_port(host: str) -> int:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


class LocalServer:
    def __init__(self, host: str, port: int, log_path: Path, timeout: float, source_root: Path | None = None):
        self.host = host
        self.port = port or _server_port(host)
        self.base_url = f"http://{'[' + host + ']' if ':' in host else host}:{self.port}"
        self.log_path = log_path
        self.timeout = timeout
        self.source_root = source_root or REPO_ROOT
        self.process: subprocess.Popen | None = None
        self.log_stream = None

    def __enter__(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_stream = self.log_path.open("a", encoding="utf-8")
        env = os.environ.copy()
        source_python = str(self.source_root / "src")
        env["PYTHONPATH"] = os.pathsep.join(
            [source_python, env["PYTHONPATH"]] if env.get("PYTHONPATH") else [source_python]
        )
        self.process = subprocess.Popen(
            [sys.executable, "-m", "smartvoice", "--host", self.host, "--port", str(self.port)],
            cwd=self.source_root, stdout=self.log_stream, stderr=subprocess.STDOUT,
            env=env,
        )
        deadline = time.monotonic() + self.timeout
        last_error = None
        try:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError(f"SmartVoice exited during startup. See local log: {self.log_path}")
                try:
                    with httpx.Client(timeout=2) as client:
                        response = client.get(f"{self.base_url}/health")
                        if response.is_success:
                            return self
                except httpx.HTTPError as exc:
                    last_error = exc
                time.sleep(0.2)
            raise RuntimeError(f"SmartVoice did not start within {self.timeout}s: {last_error}. See {self.log_path}")
        except Exception:
            self.__exit__()
            raise

    def __exit__(self, *_exc):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.log_stream:
            self.log_stream.close()


def _send_with_client(client: httpx.Client, model: dict[str, Any], language: str, sample: SpeechSample) -> tuple[httpx.Response, float]:
    started = time.perf_counter()
    if model["task"] == "transcription":
        with sample.audio_path.open("rb") as audio:
            response = client.post(
                "/v1/audio/transcriptions",
                files={"file": (sample.audio_path.name, audio, "audio/wav")},
                data={"model": model["id"], "language": language, "response_format": "json"},
            )
    else:
        response = client.post(
            "/v1/audio/speech",
            json={"model": model["id"], "input": sample.text, "language": language},
        )
    elapsed = time.perf_counter() - started
    response.raise_for_status()
    return response, elapsed


def _send(base_url: str, model: dict[str, Any], language: str, sample: SpeechSample) -> tuple[httpx.Response, float]:
    with httpx.Client(base_url=base_url, timeout=900) as client:
        return _send_with_client(client, model, language, sample)


def _response_metrics(response: httpx.Response, elapsed: float, task: str) -> dict[str, float | int | None]:
    if task == "transcription":
        payload = response.json()
        inference = float(payload.get("request_processing_seconds", 0))
        queue_wait = float(payload.get("queue_wait_seconds", 0))
        runtime_wait = float(payload.get("runtime_wait_seconds", 0))
        audio_duration = float(payload.get("duration", 0))
    else:
        inference = float(response.headers.get("x-inference-time-seconds", 0))
        queue_wait = float(response.headers.get("x-queue-wait-seconds", 0))
        runtime_wait = float(response.headers.get("x-runtime-wait-seconds", 0))
        audio_duration = float(response.headers.get("x-audio-duration", 0))
    cpu = response.headers.get("x-process-cpu-time-seconds")
    rss = response.headers.get("x-process-working-set-bytes")
    peak_rss = response.headers.get("x-process-peak-working-set-bytes")
    return {
        "request_wall_seconds": elapsed,
        "inference_seconds": inference,
        "queue_wait_seconds": queue_wait,
        "runtime_wait_seconds": runtime_wait,
        "audio_duration_seconds": audio_duration,
        "real_time_factor": inference / audio_duration if audio_duration else None,
        "process_cpu_seconds": float(cpu) if cpu else None,
        "working_set_bytes": int(rss) if rss else None,
        "process_peak_working_set_bytes": int(peak_rss) if peak_rss else None,
    }


def _memory_growth_per_minute(records: list[dict[str, Any]]) -> float | None:
    points = [
        (float(row["elapsed_since_start_seconds"]), float(row["working_set_bytes"]))
        for row in records
        if row.get("ok") and row.get("working_set_bytes") is not None
    ]
    if len(points) < 2:
        return None
    mean_x = sum(point[0] for point in points) / len(points)
    mean_y = sum(point[1] for point in points) / len(points)
    denominator = sum((x - mean_x) ** 2 for x, _ in points)
    if not denominator:
        return None
    slope_per_second = sum((x - mean_x) * (y - mean_y) for x, y in points) / denominator
    return round(slope_per_second * 60, 2)


def _select_samples(samples: list[SpeechSample], limit: int) -> list[SpeechSample]:
    if len(samples) <= limit:
        return samples
    ordered = sorted(samples, key=lambda sample: sample.duration_seconds)
    return [ordered[round(i * (len(ordered) - 1) / max(1, limit - 1))] for i in range(limit)]


def _run_performance(base_url: str, model: dict[str, Any], language: str, samples: list[SpeechSample], config: dict[str, Any], logical_cpus: int) -> dict[str, Any]:
    sample = samples[0]
    started = time.perf_counter()
    warmups = int(config["warmup_iterations"])
    iterations = int(config["iterations"])
    measurements = []
    with httpx.Client(base_url=base_url, timeout=900) as client:
        cold_response, cold_elapsed = _send_with_client(client, model, language, sample)
        cold = _response_metrics(cold_response, cold_elapsed, model["task"])
        for _ in range(warmups):
            _send_with_client(client, model, language, sample)
        for _ in range(iterations):
            response, elapsed = _send_with_client(client, model, language, sample)
            measurements.append(_response_metrics(response, elapsed, model["task"]))
    rss_values = [float(row["working_set_bytes"]) for row in measurements if row["working_set_bytes"] is not None]
    peak_values = [float(row["process_peak_working_set_bytes"]) for row in measurements if row["process_peak_working_set_bytes"] is not None]
    rss_median = int(sorted(rss_values)[len(rss_values) // 2]) if rss_values else None
    baseline_rss = None
    return {
        "case": {"task": model["task"], "model_id": model["id"], "language": language, "sample_id": sample.sample_id},
        "cold_request": cold,
        "cold_request_over_inference_seconds": round(max(0.0, float(cold["request_wall_seconds"]) - float(cold["inference_seconds"]) - float(cold["queue_wait_seconds"])), 6),
        "warm": {
            metric: numeric_summary([float(row[metric]) for row in measurements if row[metric] is not None])
            for metric in ("request_wall_seconds", "inference_seconds", "queue_wait_seconds", "runtime_wait_seconds", "real_time_factor", "process_cpu_seconds")
        },
        "serial_throughput_requests_per_second": numeric_summary([1 / float(row["request_wall_seconds"]) for row in measurements if row["request_wall_seconds"]]),
        "logical_cpu_utilization_percent": numeric_summary([
            100 * float(row["process_cpu_seconds"]) / float(row["request_wall_seconds"]) / max(1, logical_cpus)
            for row in measurements if row["process_cpu_seconds"] is not None and row["request_wall_seconds"]
        ]),
        "characters_per_inference_second": (
            round(len(sample.text) / statistics.mean(float(row["inference_seconds"]) for row in measurements), 6)
            if model["task"] == "speech" and measurements and statistics.mean(float(row["inference_seconds"]) for row in measurements) > 0
            else None
        ),
        "resources": {
            "baseline_process_rss_bytes": baseline_rss,
            "steady_rss_median_bytes": rss_median,
            "model_loaded_rss_delta_bytes": rss_median - baseline_rss if rss_median is not None and baseline_rss is not None else None,
            "peak_rss_bytes": max(peak_values, default=None),
        },
        "warmup_iterations": warmups,
        "measured_iterations": iterations,
        "elapsed_seconds": round(time.perf_counter() - started, 6),
    }


def _run_stt_quality(base_url: str, model: dict[str, Any], language: str, normalization: str, samples: list[SpeechSample], bootstrap_samples: int) -> dict[str, Any]:
    records = []
    failures = 0
    with httpx.Client(base_url=base_url, timeout=900) as client:
        for sample in samples:
            try:
                response, _elapsed = _send_with_client(client, model, language, sample)
                records.append({"reference": sample.text, "hypothesis": str(response.json().get("text", ""))})
            except (httpx.HTTPError, OSError, ValueError):
                failures += 1
    summary = quality_summary(records, normalization, bootstrap_samples=bootstrap_samples)
    summary["failed_samples"] = failures
    summary["dataset"] = samples[0].dataset if samples else None
    return summary


def _run_tts_quality(
    base_url: str, model: dict[str, Any], language: str, samples: list[SpeechSample],
    audio_dir: Path, blind_map: dict[str, Any], ratings_rows: list[dict[str, str]], judge_model_id: str, normalization: str,
) -> dict[str, Any]:
    generated = failures = judge_failures = 0
    judge_records = []
    judge_model = {"id": judge_model_id, "task": "transcription"}
    with httpx.Client(base_url=base_url, timeout=900) as client:
        for sample in samples:
            key = uuid.uuid4().hex[:12]
            try:
                response, _elapsed = _send_with_client(client, model, language, sample)
                suffix = ".wav" if "wav" in response.headers.get("content-type", "") else ".audio"
                audio_dir.mkdir(parents=True, exist_ok=True)
                file_name = f"{key}{suffix}"
                (audio_dir / file_name).write_bytes(response.content)
                blind_map[key] = {
                    "model_id": model["id"], "language": language,
                    "dataset_sample_id": sample.sample_id, "audio_file": file_name,
                    "prompt": sample.text,
                }
                ratings_rows.append({
                    "sample_key": key, "audio_file": f"tts-audio/{file_name}",
                    "prompt": sample.text,
                    "naturalness_1_to_5": "", "intelligibility_1_to_5": "",
                    "pronunciation_prosody_1_to_5": "",
                })
                generated += 1
                judge_sample = SpeechSample(
                    sample_id=sample.sample_id, language=language, text=sample.text,
                    audio_path=audio_dir / file_name, duration_seconds=sample.duration_seconds,
                    dataset=sample.dataset,
                )
                try:
                    judged, _ = _send_with_client(client, judge_model, language, judge_sample)
                    judge_records.append({"reference": sample.text, "hypothesis": str(judged.json().get("text", ""))})
                except (httpx.HTTPError, OSError, ValueError):
                    judge_failures += 1
            except (httpx.HTTPError, OSError, ValueError):
                failures += 1
    judged_quality = quality_summary(judge_records, normalization, bootstrap_samples=500)
    return {
        "evaluation": "human_blind_listening",
        "generated_samples": generated,
        "failed_samples": failures,
        "human_mos": None,
        "round_trip_asr": {
            "judge_model_id": judge_model_id,
            "metric": judged_quality["metric"],
            "value": judged_quality["value"],
            "confidence_interval_95": judged_quality["confidence_interval_95"],
            "sample_count": judged_quality["sample_count"],
            "failed_samples": judge_failures,
        },
        "note": "Fill the local ratings template, then run aggregate_tts_ratings.py. Audio and rating data stay outside Git.",
        "source_dataset": samples[0].dataset if samples else None,
    }


def _run_concurrent(base_url: str, model: dict[str, Any], language: str, sample: SpeechSample, config: dict[str, Any]) -> dict[str, Any]:
    def request_once(client: httpx.Client) -> dict[str, Any]:
        try:
            response, elapsed = _send_with_client(client, model, language, sample)
            return {"ok": True, **_response_metrics(response, elapsed, model["task"])}
        except (httpx.HTTPError, OSError, ValueError) as exc:
            response = getattr(exc, "response", None)
            return {
                "ok": False,
                "error": type(exc).__name__,
                "status_code": response.status_code if response is not None else None,
            }

    levels = []
    for workers in config["concurrency_levels"]:
        count_per_worker = int(config["requests_per_worker"])
        started = time.perf_counter()
        def worker(_index: int):
            with httpx.Client(base_url=base_url, timeout=900) as client:
                return [request_once(client) for _ in range(count_per_worker)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=int(workers)) as executor:
            measurements = [row for group in executor.map(worker, range(int(workers))) for row in group]
        elapsed = time.perf_counter() - started
        good = [row for row in measurements if row["ok"]]
        levels.append({
            "workers": int(workers), "requests_per_worker": count_per_worker,
            "request_count": len(measurements), "success_count": len(good),
            "failure_count": len(measurements) - len(good),
            "failure_rate": round((len(measurements) - len(good)) / len(measurements), 6) if measurements else None,
            "requests_per_second": round(len(good) / elapsed, 6) if elapsed else None,
            "request_wall_seconds": numeric_summary([float(row["request_wall_seconds"]) for row in good]),
            "queue_wait_seconds": numeric_summary([float(row["queue_wait_seconds"]) for row in good]),
            "runtime_wait_seconds": numeric_summary([float(row["runtime_wait_seconds"]) for row in good]),
            "inference_seconds": numeric_summary([float(row["inference_seconds"]) for row in good]),
            "process_cpu_seconds": numeric_summary([float(row["process_cpu_seconds"]) for row in good if row["process_cpu_seconds"] is not None]),
            "working_set_peak_bytes": max((int(row["process_peak_working_set_bytes"] or row["working_set_bytes"]) for row in good if row["process_peak_working_set_bytes"] is not None or row["working_set_bytes"] is not None), default=None),
            "errors_by_status": {
                str(status): sum(1 for row in measurements if not row["ok"] and row.get("status_code") == status)
                for status in sorted({row.get("status_code") for row in measurements if not row["ok"]}, key=lambda value: str(value))
            },
            "duration_seconds": round(elapsed, 6),
        })

    soak_seconds = int(config["soak_seconds"])
    soak: dict[str, Any] | None = None
    if soak_seconds > 0:
        workers = int(config["soak_workers"])
        stop_at = time.monotonic() + soak_seconds
        records: list[dict[str, Any]] = []
        lock = threading.Lock()
        soak_started = time.monotonic()

        def loop():
            local = []
            with httpx.Client(base_url=base_url, timeout=900) as client:
                while time.monotonic() < stop_at:
                    measurement = request_once(client)
                    measurement["elapsed_since_start_seconds"] = time.monotonic() - soak_started
                    local.append(measurement)
            with lock:
                records.extend(local)

        started = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(lambda _n: loop(), range(workers)))
        elapsed = time.perf_counter() - started
        good = [row for row in records if row["ok"]]
        chronological = sorted(good, key=lambda row: float(row["elapsed_since_start_seconds"]))
        soak = {
            "workers": workers, "target_seconds": soak_seconds,
            "actual_seconds": round(elapsed, 6), "request_count": len(records),
            "success_count": len(good), "failure_count": len(records) - len(good),
            "failure_rate": round((len(records) - len(good)) / len(records), 6) if records else None,
            "requests_per_second": round(len(good) / elapsed, 6) if elapsed else None,
            "request_wall_seconds": numeric_summary([float(row["request_wall_seconds"]) for row in good]),
            "queue_wait_seconds": numeric_summary([float(row["queue_wait_seconds"]) for row in good]),
            "runtime_wait_seconds": numeric_summary([float(row["runtime_wait_seconds"]) for row in good]),
            "inference_seconds": numeric_summary([float(row["inference_seconds"]) for row in good]),
            "process_cpu_seconds": numeric_summary([float(row["process_cpu_seconds"]) for row in good if row["process_cpu_seconds"] is not None]),
            "real_time_factor": numeric_summary([float(row["real_time_factor"]) for row in good if row["real_time_factor"] is not None]),
            "working_set_peak_bytes": max((int(row["process_peak_working_set_bytes"] or row["working_set_bytes"]) for row in good if row["process_peak_working_set_bytes"] is not None or row["working_set_bytes"] is not None), default=None),
            "working_set_start_bytes": next((int(row["working_set_bytes"]) for row in chronological if row["working_set_bytes"] is not None), None),
            "working_set_end_bytes": next((int(row["working_set_bytes"]) for row in reversed(chronological) if row["working_set_bytes"] is not None), None),
            "estimated_working_set_growth_bytes_per_minute": _memory_growth_per_minute(records),
            "errors_by_status": {
                str(status): sum(1 for row in records if not row["ok"] and row.get("status_code") == status)
                for status in sorted({row.get("status_code") for row in records if not row["ok"]}, key=lambda value: str(value))
            },
        }
    return {
        "case": {"task": model["task"], "model_id": model["id"], "language": language},
        "workload_model": "closed_loop_fixed_concurrency",
        "levels": levels,
        "soak": soak,
    }


def _run_mixed_concurrent(
    base_url: str, scenario_id: str, cases: list[tuple[dict[str, Any], str, SpeechSample]], config: dict[str, Any]
) -> dict[str, Any]:
    levels = []
    for workers in config["concurrency_levels"]:
        count_per_worker = int(config["requests_per_worker"])
        started = time.perf_counter()

        def worker(worker_index: int):
            rows = []
            with httpx.Client(base_url=base_url, timeout=900) as client:
                for request_index in range(count_per_worker):
                    case_index = (worker_index * count_per_worker + request_index) % len(cases)
                    model, language, sample = cases[case_index]
                    request_started = time.perf_counter()
                    try:
                        response, _elapsed = _send_with_client(client, model, language, sample)
                        row = _response_metrics(response, time.perf_counter() - request_started, model["task"])
                        row.update({"ok": True, "case_id": model["id"]})
                    except Exception as exc:
                        row = {
                            "request_wall_seconds": time.perf_counter() - request_started,
                            "queue_wait_seconds": 0.0,
                            "runtime_wait_seconds": 0.0,
                            "ok": False,
                            "case_id": model["id"],
                            "error_type": type(exc).__name__,
                            "status_code": getattr(getattr(exc, "response", None), "status_code", None),
                        }
                    rows.append(row)
            return rows

        with concurrent.futures.ThreadPoolExecutor(max_workers=int(workers)) as executor:
            measurements = [row for group in executor.map(worker, range(int(workers))) for row in group]
        elapsed = time.perf_counter() - started
        good = [row for row in measurements if row["ok"]]
        by_case = {}
        for model, _language, _sample in cases:
            selected = [row for row in good if row["case_id"] == model["id"]]
            by_case[model["id"]] = {
                "request_count": len(selected),
                "request_wall_seconds": numeric_summary([float(row["request_wall_seconds"]) for row in selected]),
                "queue_wait_seconds": numeric_summary([float(row["queue_wait_seconds"]) for row in selected]),
                "runtime_wait_seconds": numeric_summary([float(row["runtime_wait_seconds"]) for row in selected]),
                "inference_seconds": numeric_summary([float(row["inference_seconds"]) for row in selected]),
                "process_cpu_seconds": numeric_summary([float(row["process_cpu_seconds"]) for row in selected if row["process_cpu_seconds"] is not None]),
                "process_peak_working_set_bytes": max((int(row["process_peak_working_set_bytes"] or row["working_set_bytes"]) for row in selected if row["process_peak_working_set_bytes"] is not None or row["working_set_bytes"] is not None), default=None),
            }
        levels.append({
            "workers": int(workers),
            "requests_per_worker": count_per_worker,
            "request_count": len(measurements),
            "success_count": len(good),
            "failure_count": len(measurements) - len(good),
            "failure_rate": round((len(measurements) - len(good)) / len(measurements), 6) if measurements else None,
            "requests_per_second": round(len(good) / elapsed, 6) if elapsed else None,
            "request_wall_seconds": numeric_summary([float(row["request_wall_seconds"]) for row in good]),
            "queue_wait_seconds": numeric_summary([float(row["queue_wait_seconds"]) for row in good]),
            "runtime_wait_seconds": numeric_summary([float(row["runtime_wait_seconds"]) for row in good]),
            "inference_seconds": numeric_summary([float(row["inference_seconds"]) for row in good]),
            "process_cpu_seconds": numeric_summary([float(row["process_cpu_seconds"]) for row in good if row["process_cpu_seconds"] is not None]),
            "working_set_peak_bytes": max((int(row["process_peak_working_set_bytes"] or row["working_set_bytes"]) for row in good if row["process_peak_working_set_bytes"] is not None or row["working_set_bytes"] is not None), default=None),
            "per_case": by_case,
            "errors_by_status": {
                str(status): sum(1 for row in measurements if not row["ok"] and row.get("status_code") == status)
                for status in sorted({row.get("status_code") for row in measurements if not row["ok"]}, key=lambda value: str(value))
            },
            "duration_seconds": round(elapsed, 6),
        })
    return {
        "case": {"scenario_id": scenario_id, "models": [model["id"] for model, _, _ in cases]},
        "workload_model": "closed_loop_mixed_model_fixed_concurrency",
        "levels": levels,
    }


def _fingerprint_models(settings: Settings, model_ids: set[str]) -> list[dict[str, Any]]:
    rows = []
    for model_dir in settings.models_dir.iterdir() if settings.models_dir.exists() else []:
        if model_dir.name not in model_ids:
            continue
        manifest = model_dir / "smartvoice-model.json"
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        model_files = data.get("files", {})
        model_bytes = 0
        for relative in model_files.values():
            candidate = (model_dir / str(relative)).resolve()
            if candidate.is_relative_to(model_dir.resolve()) and candidate.is_file():
                model_bytes += candidate.stat().st_size
        rows.append({
            "id": data.get("id", model_dir.name),
            "archive_sha256": data.get("archive_sha256"),
            "file_sha256": data.get("file_sha256", {}),
            "installed_model_files_bytes": model_bytes,
        })
    return sorted(rows, key=lambda row: row["id"])


def _report_runtime(runtime: dict[str, Any]) -> dict[str, Any]:
    process = runtime.get("process") or {}
    return {
        key: runtime.get(key)
        for key in ("backend", "requested_device", "actual_device", "provider_status", "runtime_version", "host", "system_memory")
        if key in runtime
    } | {
        "process_resources": {
            key: process[key] for key in ("cpu_time_seconds", "working_set_bytes", "peak_working_set_bytes", "private_bytes")
            if key in process
        }
    }


def run_benchmark(
    config: dict[str, Any], config_path: Path, *, profile_name: str,
    selected_models: set[str] | None = None, selected_categories: set[str] | None = None,
    output_path: Path | None = None, server_source_root: Path | None = None,
) -> Path:
    settings = Settings.from_env()
    profiles = config["profiles"]
    if profile_name not in profiles:
        raise ValueError(f"Unknown profile {profile_name!r}; choose from {', '.join(profiles)}")
    profile = profiles[profile_name]
    available_categories = {"quality", "performance", "concurrency"}
    categories = available_categories if selected_categories is None else selected_categories
    if not categories or categories - available_categories:
        raise ValueError(f"Categories must be selected from {', '.join(sorted(available_categories))}.")
    models = [model for model in config["models"] if selected_models is None or model["id"] in selected_models]
    if selected_models and selected_models - {model["id"] for model in models}:
        raise ValueError(f"Unknown model IDs: {', '.join(sorted(selected_models - {model['id'] for model in models}))}")
    if not models:
        raise ValueError("No benchmark models selected.")
    for model in models:
        if model["task"] not in {"transcription", "speech"}:
            raise ValueError(f"Unsupported task for {model['id']}: {model['task']}")
    needed_languages = sorted({
        language for model in models for language in model["languages"]
        if language in config["languages"]
    })
    if not needed_languages:
        raise ValueError("Selected models do not match any configured language profile.")
    cache_root = settings.data_dir / "benchmark-cache"
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    run_dir = settings.data_dir / "benchmark-runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log_dir = run_dir / "logs"
    dataset_meta: dict[str, Any] = {}
    samples_by_language = {}
    dataset_config = config["dataset"]
    for language in needed_languages:
        language_profile = config["languages"][language]
        samples, metadata = load_fleurs_samples(
            repo_id=dataset_config["repo_id"], revision=dataset_config["revision"],
            split=dataset_config["split"], dataset_config=language_profile["dataset_config"],
            language=language, cache_dir=cache_root,
            limit=int(profile["quality_sample_limit"]) if profile["quality_sample_limit"] else None,
            seed=int(config.get("seed", 20260928)),
        )
        samples_by_language[language] = samples
        dataset_meta[language] = metadata

    tts_judge_id = config.get("tts_quality_judge_model_id")
    if any(model["task"] == "speech" for model in models) and not tts_judge_id:
        raise ValueError("Configure tts_quality_judge_model_id to enable TTS intelligibility scoring.")

    quality_results: dict[str, Any] = {"stt": [], "tts": []}
    performance_results = []
    concurrency_results = []
    blind_map: dict[str, Any] = {}
    ratings_rows: list[dict[str, str]] = []
    audio_dir = run_dir / "tts-audio"
    random.Random(config.get("seed", 20260928)).shuffle(models)
    host = str(config["server"].get("host", "127.0.0.1"))
    for model in models:
        category = "stt" if model["task"] == "transcription" else "tts"
        for language in model["languages"]:
            if language not in samples_by_language:
                continue
            samples = samples_by_language[language]
            if not samples:
                continue
            startup_started = time.perf_counter()
            with LocalServer(
                host, 0, log_dir / f"{model['id']}-{language}.log",
                float(config["server"]["startup_timeout_seconds"]), source_root=server_source_root,
            ) as server:
                with httpx.Client(base_url=server.base_url, timeout=20) as client:
                    runtime = client.get("/v1/runtime")
                    runtime.raise_for_status()
                    available = {item["id"] for item in client.get("/v1/models").json().get("data", [])}
                startup_seconds = time.perf_counter() - startup_started
                if model["id"] not in available:
                    raise RuntimeError(f"Model {model['id']} is not installed or not available in the active provider.")
                if model["task"] == "speech" and tts_judge_id not in available:
                    raise RuntimeError(f"Configured TTS quality judge {tts_judge_id} is not installed or available.")
                runtime_data = runtime.json()
                if "performance" in categories or "concurrency" in categories:
                    perf_samples = _select_samples(samples, int(profile["performance_sample_limit"]))
                if "performance" in categories:
                    logical_cpus = int((runtime_data.get("host") or {}).get("logical_cpu_count") or 1)
                    perf = _run_performance(server.base_url, model, language, perf_samples, profile["performance"], logical_cpus)
                    perf["service_startup_seconds"] = round(startup_seconds, 6)
                    perf["host_runtime"] = _report_runtime(runtime_data)
                    baseline_process = runtime_data.get("process") or {}
                    perf["resources"]["baseline_process_rss_bytes"] = baseline_process.get("working_set_bytes")
                    if perf["resources"]["steady_rss_median_bytes"] is not None and baseline_process.get("working_set_bytes") is not None:
                        perf["resources"]["model_loaded_rss_delta_bytes"] = perf["resources"]["steady_rss_median_bytes"] - int(baseline_process["working_set_bytes"])
                    performance_results.append(perf)
                if "concurrency" in categories:
                    conc = _run_concurrent(server.base_url, model, language, perf_samples[0], profile["concurrency"])
                    conc["host_runtime"] = _report_runtime(runtime_data)
                    concurrency_results.append(conc)
                if "quality" in categories and category == "stt":
                    quality_results["stt"].append({
                        "model_id": model["id"], "language": language,
                        **_run_stt_quality(server.base_url, model, language, config["languages"][language]["normalization"], samples, int(profile["bootstrap_samples"])),
                    })
                elif "quality" in categories:
                    quality_results["tts"].append({
                        "model_id": model["id"], "language": language,
                        **_run_tts_quality(
                            server.base_url, model, language, samples, audio_dir, blind_map,
                            ratings_rows, str(tts_judge_id), config["languages"][language]["normalization"],
                        ),
                    })

    if "concurrency" in categories and profile.get("mixed_concurrency"):
        selected_by_id = {str(model["id"]): model for model in models}
        selected_scenarios = []
        for scenario in config.get("mixed_concurrency_scenarios", []):
            model_ids = [str(item["model_id"]) for item in scenario["cases"]]
            if not set(model_ids) <= selected_by_id.keys():
                continue
            cases = []
            for item in scenario["cases"]:
                model = selected_by_id[str(item["model_id"])]
                language = str(item["language"])
                samples = samples_by_language.get(language, [])
                if language not in model["languages"] or not samples:
                    raise ValueError(f"Mixed concurrency case {model['id']} has no sample for language {language!r}.")
                cases.append((model, language, _select_samples(samples, 1)[0]))
            selected_scenarios.append((str(scenario["id"]), cases))
        if selected_scenarios:
            timeout = float(config["server"]["startup_timeout_seconds"])
            with LocalServer(host, 0, log_dir / "mixed-concurrency.log", timeout, source_root=server_source_root) as server:
                with httpx.Client(base_url=server.base_url, timeout=20) as client:
                    available = {item["id"] for item in client.get("/v1/models").json().get("data", [])}
                for scenario_id, cases in selected_scenarios:
                    missing = sorted({str(model["id"]) for model, _, _ in cases} - available)
                    if missing:
                        raise RuntimeError(f"Mixed concurrency models unavailable: {', '.join(missing)}")
                    # Warm model instances serially so measurements focus on contention, not construction.
                    for model, language, sample in cases:
                        _send(server.base_url, model, language, sample)
                    concurrency_results.append(_run_mixed_concurrent(
                        server.base_url, scenario_id, cases, profile["mixed_concurrency"],
                    ))

    if ratings_rows:
        with (run_dir / "tts-ratings-template.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(ratings_rows[0]))
            writer.writeheader()
            writer.writerows(ratings_rows)
        (run_dir / "blind-mapping.json").write_text(json.dumps({
            "run_id": run_id,
            "benchmark_suite": config["suite_id"],
            "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
            "dataset": {"repo_id": dataset_config["repo_id"], "revision": dataset_config["revision"], "split": dataset_config["split"]},
            "samples": blind_map,
        }, indent=2, ensure_ascii=False), encoding="utf-8")

    report = {
        "schema_version": "2.0",
        "benchmark_suite": config["suite_id"],
        "run_id": run_id,
        "profile": profile_name,
        "selected_categories": sorted(categories),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "software": {"smartvoice": __version__, "python": platform.python_version()},
        "platform": {"system": platform.system(), "release": platform.release(), "machine": platform.machine(), "processor": platform.processor(), "python_implementation": platform.python_implementation()},
        "dataset": {"repo_id": dataset_config["repo_id"], "revision": dataset_config["revision"], "split": dataset_config["split"], "license": dataset_config["license"], "languages": dataset_meta},
        "models": _fingerprint_models(settings, {model["id"] for model in models} | ({str(tts_judge_id)} if tts_judge_id and any(model["task"] == "speech" for model in models) else set())),
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "profile_settings": profile,
        "service_settings": {
            "provider": settings.provider,
            "inference_threads": settings.num_threads,
            "max_concurrent_inference": settings.max_concurrent_inference,
            "max_queued_inference": settings.max_queued_inference,
            "inference_queue_timeout_seconds": settings.inference_queue_timeout_seconds,
            "inference_execution_timeout_seconds": settings.inference_execution_timeout_seconds,
        },
        "categories": {
            "quality": quality_results,
            "performance": performance_results,
            "concurrency": concurrency_results,
        },
        "local_artifacts": {"tts_listening_files_generated": bool(ratings_rows)},
        "interpretation": "Observed comparison results only; not an acceptance threshold or cross-platform ranking.",
    }
    destination = output_path or run_dir / "aggregate-result.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare SmartVoice models by quality, performance, and concurrent load")
    parser.add_argument("--config", type=Path, default=Path(__file__).parent / "config" / "model-comparison.json")
    parser.add_argument("--profile", choices=("smoke", "standard", "full"), default="smoke")
    parser.add_argument("--category", action="append", choices=("quality", "performance", "concurrency"), help="Category to run; repeat to select multiple (defaults to all)")
    parser.add_argument("--model", action="append", dest="models", help="Model ID to include; repeat to select multiple")
    parser.add_argument("--server-source", type=Path, help="Source checkout used by benchmark server processes (for controlled code comparisons)")
    parser.add_argument("--output", type=Path, help="Aggregate JSON report path; defaults outside the repository")
    args = parser.parse_args()
    try:
        config = _read_config(args.config)
        output = run_benchmark(
            config, args.config, profile_name=args.profile,
            selected_models=set(args.models) if args.models else None,
            selected_categories=set(args.category) if args.category else None,
            output_path=args.output,
            server_source_root=args.server_source.resolve() if args.server_source else None,
        )
    except (RuntimeError, ValueError, OSError, httpx.HTTPError) as exc:
        parser.error(str(exc))
    print(f"Aggregate result: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
