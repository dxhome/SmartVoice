"""Measure cold and warm local inference latency and Windows process resources."""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from smartvoice import __version__
from smartvoice.config.settings import Settings


class ProcessSampler:
    def __init__(self, base_url: str, interval_seconds: float = 0.1):
        self.base_url = base_url
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._samples: list[dict[str, Any]] = []

    def __enter__(self):
        self._thread = threading.Thread(target=self._sample, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _sample(self):
        with httpx.Client(timeout=2) as client:
            while not self._stop.is_set():
                try:
                    response = client.get(f"{self.base_url}/v1/runtime")
                    response.raise_for_status()
                    process = response.json().get("process", {})
                    if process:
                        self._samples.append(process)
                except httpx.HTTPError:
                    pass
                self._stop.wait(self.interval_seconds)

    @property
    def samples(self) -> list[dict[str, Any]]:
        return list(self._samples)


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * percentile))))
    return round(ordered[index], 4)


def _summary(values: list[float]) -> dict[str, float | None]:
    return {
        "median": round(statistics.median(values), 4) if values else None,
        "p95": _percentile(values, 0.95),
        "min": round(min(values), 4) if values else None,
        "max": round(max(values), 4) if values else None,
    }


def _measure_request(base_url: str, operation) -> dict[str, Any]:
    sampler = ProcessSampler(base_url)
    with sampler:
        started = time.perf_counter()
        response = operation()
        wall_seconds = time.perf_counter() - started
    response.raise_for_status()
    return {
        "response": response,
        "wall_seconds": wall_seconds,
        "resource_samples": sampler.samples,
    }


def _stt_operation(client: httpx.Client, sample_path: Path, language: str):
    def request():
        with sample_path.open("rb") as audio:
            return client.post(
                "/v1/audio/transcriptions",
                files={"file": (sample_path.name, audio, "audio/wav")},
                data={"language": language, "response_format": "json"},
            )

    return request


def _tts_operation(client: httpx.Client, text: str, language: str):
    return lambda: client.post("/v1/audio/speech", json={"input": text, "language": language})


def _installed_model_fingerprints(settings: Settings) -> list[dict[str, Any]]:
    fingerprints = []
    if not settings.models_dir.is_dir():
        return fingerprints
    for model_dir in settings.models_dir.iterdir():
        manifest_path = model_dir / "smartvoice-model.json"
        if not model_dir.is_dir() or not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        model_bytes = 0
        for relative in manifest.get("files", {}).values():
            file_path = (model_dir / relative).resolve()
            if file_path.is_relative_to(model_dir.resolve()) and file_path.is_file():
                model_bytes += file_path.stat().st_size
        fingerprints.append({
            "id": manifest.get("id", model_dir.name),
            "archive_sha256": manifest.get("archive_sha256"),
            "installed_model_files_bytes": model_bytes,
        })
    return sorted(fingerprints, key=lambda model: str(model["id"]))


def _run_case(
    client: httpx.Client,
    base_url: str,
    case_id: str,
    task: str,
    language: str,
    operation,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    cold = _measure_request(base_url, operation)
    cold_response = cold["response"]
    cold_payload = cold_response.json() if task == "stt" else None
    cold_audio_duration = (
        float(cold_payload.get("duration", 0)) if cold_payload else float(cold_response.headers.get("x-audio-duration", 0))
    )
    logical_cpus = int(client.get("/v1/runtime").json().get("host", {}).get("logical_cpu_count") or 1)

    for _ in range(warmup_iterations):
        operation().raise_for_status()

    warm_results = [_measure_request(base_url, operation) for _ in range(iterations)]
    wall_times = [float(result["wall_seconds"]) for result in warm_results]
    cpu_deltas: list[float] = []
    core_percent: list[float] = []
    current_memory: list[int] = []
    peak_memory: list[int] = []
    real_time_factors: list[float] = []
    model_times: list[float] = []

    for result in warm_results:
        response = result["response"]
        samples = result["resource_samples"]
        header_cpu = float(response.headers.get("x-process-cpu-time-seconds", 0))
        if header_cpu:
            cpu_deltas.append(header_cpu)
            core_percent.append(100 * header_cpu / float(result["wall_seconds"]) / logical_cpus)
        header_working_set = response.headers.get("x-process-working-set-bytes")
        header_peak_set = response.headers.get("x-process-peak-working-set-bytes")
        if header_working_set:
            current_memory.append(int(header_working_set))
        if header_peak_set:
            peak_memory.append(int(header_peak_set))
        if samples:
            cpu_times = [float(sample["cpu_time_seconds"]) for sample in samples if "cpu_time_seconds" in sample]
            if not header_cpu and len(cpu_times) > 1:
                cpu_delta = max(0.0, cpu_times[-1] - cpu_times[0])
                cpu_deltas.append(cpu_delta)
                core_percent.append(100 * cpu_delta / float(result["wall_seconds"]) / logical_cpus)
            current_memory.extend(int(sample["working_set_bytes"]) for sample in samples if "working_set_bytes" in sample)
            peak_memory.extend(int(sample["peak_working_set_bytes"]) for sample in samples if "peak_working_set_bytes" in sample)

        if task == "stt":
            payload = response.json()
            model_seconds = float(payload.get("request_processing_seconds", payload.get("processing_seconds", 0)))
            audio_seconds = float(payload.get("duration", 0))
        else:
            model_seconds = float(response.headers.get("x-inference-time-seconds", 0))
            audio_seconds = float(response.headers.get("x-audio-duration", 0))
        model_times.append(model_seconds)
        if audio_seconds:
            real_time_factors.append(model_seconds / audio_seconds)

    return {
        "case_id": case_id,
        "task": task,
        "language": language,
        "cold_start": {
            "wall_seconds": round(float(cold["wall_seconds"]), 4),
            "audio_seconds": round(cold_audio_duration, 4),
            "process_working_set_bytes": int(cold_response.headers["x-process-working-set-bytes"])
            if "x-process-working-set-bytes" in cold_response.headers else None,
            "process_peak_working_set_bytes": int(cold_response.headers["x-process-peak-working-set-bytes"])
            if "x-process-peak-working-set-bytes" in cold_response.headers else None,
        },
        "warm": {
            "request_wall_seconds": _summary(wall_times),
            "inference_seconds": _summary(model_times),
            "real_time_factor": _summary(real_time_factors),
            "process_cpu_seconds": _summary(cpu_deltas),
            "logical_cpu_utilization_percent": _summary(core_percent),
            "working_set_bytes": {
                "median": int(statistics.median(current_memory)) if current_memory else None,
                "peak_observed": max(current_memory, default=None),
            },
            "process_peak_working_set_bytes": max(peak_memory, default=None),
        },
    }


def main() -> int:
    benchmark_dir = Path(__file__).resolve().parent
    default_config = benchmark_dir / "config" / "windows-cpu.json"
    parser = argparse.ArgumentParser(description="Benchmark SmartVoice Windows CPU inference and process resources")
    parser.add_argument("--config", type=Path, default=default_config, help="Benchmark run configuration JSON")
    parser.add_argument("--host", default=None, help="Override the host in the config")
    parser.add_argument("--port", type=int, default=None, help="Override the port in the config")
    parser.add_argument("--iterations", type=int, default=None, help="Override warm measurements per task/language")
    parser.add_argument("--output", type=Path, default=None, help="JSON report path")
    args = parser.parse_args()

    try:
        run_config = json.loads(args.config.read_text(encoding="utf-8"))
    except OSError as exc:
        parser.error(f"cannot read config {args.config}: {exc}")
    except json.JSONDecodeError as exc:
        parser.error(f"invalid config JSON: {exc}")
    if not isinstance(run_config, dict):
        parser.error("config root must be a JSON object")

    host = args.host or run_config.get("host", "127.0.0.1")
    port = args.port if args.port is not None else run_config.get("port", 8765)
    iterations = args.iterations if args.iterations is not None else run_config.get("iterations", 5)
    warmup_iterations = run_config.get("warmup_iterations", 1)
    if not isinstance(host, str) or not host:
        parser.error("config host must be a non-empty string")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        parser.error("config port must be between 1 and 65535")
    if not isinstance(iterations, int) or iterations < 1:
        parser.error("iterations must be a positive integer")
    if not isinstance(warmup_iterations, int) or warmup_iterations < 0:
        parser.error("warmup_iterations must be a non-negative integer")

    settings = Settings.from_env()
    sample_dir = settings.models_dir / "sensevoice-small-local"
    samples = {language: next(sample_dir.rglob(f"{language}.wav"), None) for language in ("zh", "en")}
    missing = [language for language, path in samples.items() if path is None]
    if missing:
        raise SystemExit(f"Missing bundled STT samples for: {', '.join(missing)}. Install the SenseVoice model first.")

    base_url = f"http://{host}:{port}"
    result_dir = benchmark_dir / "result"
    output = args.output or result_dir / f"windows-cpu-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    output.parent.mkdir(parents=True, exist_ok=True)

    log_dir = benchmark_dir / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    server_log_path = log_dir / f"{output.stem}.log"
    env = os.environ.copy()
    tts_texts = {
        "zh": "你好，SmartVoice。这是本地中文语音合成性能测试。",
        "en": "Hello, SmartVoice. This is a local English speech synthesis benchmark.",
    }
    jobs = [
        (f"stt_{language}", "stt", language, _stt_operation, sample_path)
        for language, sample_path in samples.items()
    ] + [
        (f"tts_{language}", "tts", language, _tts_operation, text)
        for language, text in tts_texts.items()
    ]
    cases = []
    runtime_info = None
    startup_times = {}
    for case_id, task, language, operation_factory, input_value in jobs:
        server_log = server_log_path.open("a", encoding="utf-8")
        server = subprocess.Popen(
            [sys.executable, "-m", "smartvoice", "--host", host, "--port", str(port)],
            cwd=benchmark_dir.parent,
            env=env,
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        try:
            startup_started = time.perf_counter()
            with httpx.Client(base_url=base_url, timeout=300) as client:
                deadline = time.monotonic() + 90
                while True:
                    if server.poll() is not None:
                        raise SystemExit(f"SmartVoice server exited early. See {server_log_path}")
                    try:
                        health = client.get("/health", timeout=3)
                        if health.is_success:
                            break
                    except httpx.HTTPError:
                        pass
                    if time.monotonic() >= deadline:
                        raise SystemExit(f"SmartVoice server did not start. See {server_log_path}")
                    time.sleep(0.2)
                startup_times[case_id] = round(time.perf_counter() - startup_started, 4)
                ready = client.get("/ready")
                ready.raise_for_status()
                runtime = client.get("/v1/runtime")
                runtime.raise_for_status()
                runtime_info = runtime.json()
                if task == "stt":
                    operation = operation_factory(client, input_value, language)
                else:
                    operation = operation_factory(client, input_value, language)
                case = _run_case(client, base_url, case_id, task, language, operation, iterations, warmup_iterations)
                case["server_startup_seconds"] = startup_times[case_id]
                cases.append(case)
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
            server_log.close()

    assert runtime_info is not None
    combined_log = server_log_path.open("a", encoding="utf-8")
    combined_server = subprocess.Popen(
        [sys.executable, "-m", "smartvoice", "--host", host, "--port", str(port)],
        cwd=benchmark_dir.parent, env=env,
        stdout=combined_log, stderr=subprocess.STDOUT,
    )
    try:
        with httpx.Client(base_url=base_url, timeout=300) as client:
            combined_deadline = time.monotonic() + 90
            while True:
                if combined_server.poll() is not None:
                    raise SystemExit(f"SmartVoice server exited early. See {server_log_path}")
                try:
                    if client.get("/health", timeout=3).is_success:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() >= combined_deadline:
                    raise SystemExit(f"SmartVoice server did not start. See {server_log_path}")
                time.sleep(0.2)
            client.get("/ready").raise_for_status()
            before_process = client.get("/v1/runtime").json()["process"]
            residency_steps = []
            combined_jobs = [
                (f"stt_{language}", _stt_operation(client, sample_path, language))
                for language, sample_path in samples.items()
            ] + [
                (f"tts_{language}", _tts_operation(client, text, language))
                for language, text in tts_texts.items()
            ]
            for case_id, operation in combined_jobs:
                measured = _measure_request(base_url, operation)
                measured["response"].raise_for_status()
                process = client.get("/v1/runtime").json()["process"]
                residency_steps.append({
                    "case_id": case_id,
                    "first_request_wall_seconds": round(float(measured["wall_seconds"]), 4),
                    "working_set_bytes_after_request": process.get("working_set_bytes"),
                    "peak_working_set_bytes": process.get("peak_working_set_bytes"),
                })
            after_all = client.get("/v1/runtime").json()["process"]
            combined_residency = {
                "description": "Load and run both STT languages and both TTS language samples in one service process.",
                "before_inference_working_set_bytes": before_process.get("working_set_bytes"),
                "after_all_working_set_bytes": after_all.get("working_set_bytes"),
                "after_all_peak_working_set_bytes": after_all.get("peak_working_set_bytes"),
                "steps": residency_steps,
            }
    finally:
        combined_server.terminate()
        try:
            combined_server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            combined_server.kill()
            combined_server.wait(timeout=5)
        combined_log.close()

    report = {
        "schema_version": "1.0",
        "benchmark_type": "observed_benchmark_not_acceptance_threshold",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "software": {"smartvoice": __version__, "python": platform_python_version()},
        "host": runtime_info.get("host", {}),
        "runtime": {key: runtime_info.get(key) for key in ("backend", "actual_device", "runtime_version", "provider_status")},
        "models": _installed_model_fingerprints(settings),
        "config": {
            "config_file": str(args.config),
            "iterations": iterations,
            "warmup_iterations": warmup_iterations,
            "logical_cpu_count": (runtime_info.get("host") or {}).get("logical_cpu_count"),
            "inference_threads": settings.num_threads,
            "max_concurrent_inference": settings.max_concurrent_inference,
            "max_queued_inference": settings.max_queued_inference,
        },
        "corpus_note": "STT uses the bundled SenseVoice demo clips. Results measure performance only and do not establish recognition accuracy.",
        "measurement_note": "Each case runs in a fresh server process; process CPU and working set are sampled over the loopback API. Telemetry polling adds small overhead.",
        "server_startup_seconds_by_case": startup_times,
        "combined_model_residency": combined_residency,
        "cases": cases,
    }
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Benchmark report written to {output}")
    print(json.dumps({
        "host": report["host"], "server_startup_seconds_by_case": startup_times,
        "combined_model_residency": combined_residency, "cases": cases,
    }, indent=2))
    return 0


def platform_python_version() -> str:
    return platform.python_version()


if __name__ == "__main__":
    raise SystemExit(main())
