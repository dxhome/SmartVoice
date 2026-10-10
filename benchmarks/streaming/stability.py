"""Repeated-session, extended-audio, and owned-server shutdown probe."""
import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
import signal
import subprocess
import tempfile
import time
import urllib.request
import wave
from collections import deque
from pathlib import Path
from importlib.metadata import version

from .run import MODES, percentiles, replay
from .resources import ResourceSampler


def owned_server(command, url, timeout):
    if not command:
        return None
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    health_url = url.replace("wss://", "https://").replace("ws://", "http://").split("/v1/audio/stream", 1)[0] + "/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Owned server exited before health check (exit={process.returncode})")
        try:
            with urllib.request.urlopen(health_url, timeout=1) as response:
                if response.status == 200:
                    return process
        except Exception:
            time.sleep(.25)
    stop_owned_server(process)
    raise TimeoutError(f"Owned server did not become healthy at {health_url}")


def stop_owned_server(process):
    if process.poll() is not None:
        return True
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=15)
        return True
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        process.wait(timeout=10)
        return False


async def run(args, process):
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    case = next((item for item in manifest["cases"] if item["id"] == args.case), None)
    if case is None or case.get("variant") != "clean":
        raise ValueError("Choose a clean pinned manifest case")
    audio_path = args.audio_root / case["path"]
    temp_path = None
    original_digest = hashlib.sha256(audio_path.read_bytes()).hexdigest()
    if original_digest != case["sha256"]:
        raise ValueError("Pinned fixture checksum mismatch")
    if args.long_audio_seconds:
        with wave.open(str(audio_path), "rb") as source:
            params = source.getparams()
            frames = source.readframes(source.getnframes())
        frame_bytes = params.nchannels * params.sampwidth
        wanted = math.ceil(args.long_audio_seconds * params.framerate)
        repetitions = math.ceil(wanted / (len(frames) // frame_bytes))
        long_pcm = (frames * repetitions)[:wanted * frame_bytes]
        handle, name = tempfile.mkstemp(prefix="smartvoice-stream-long-", suffix=".wav", dir=args.output.parent)
        os.close(handle)
        temp_path = Path(name)
        with wave.open(str(temp_path), "wb") as dest:
            dest.setparams(params)
            dest.writeframes(long_pcm)
        case = dict(case, id=f"{case['id']}_long_{args.long_audio_seconds}s", path=str(temp_path.resolve()),
                    sha256=hashlib.sha256(temp_path.read_bytes()).hexdigest(), samples=wanted)

    sampler = ResourceSampler(process.pid if process else args.server_pid, args.cpu_cores) if (process or args.server_pid) else None
    if sampler:
        sampler.start()
    records = []
    pool_samples=deque(maxlen=720)  # At most one hour of recent five-second samples.
    pool_peak={}
    def fetch_pool():
        url=args.url.replace('wss://','https://').replace('ws://','http://')
        url=url.split('/v1/audio/stream',1)[0]+'/v1/audio/stream/capabilities'
        with urllib.request.urlopen(url,timeout=3) as response: return json.load(response).get('worker_pool',{})
    async def monitor_pool():
        while True:
            try:
                sample=await asyncio.to_thread(fetch_pool)
                pool_samples.append(dict(at_monotonic_seconds=time.monotonic(),**sample))
                for key,value in sample.items():
                    if isinstance(value,(int,float)):pool_peak[key]=max(pool_peak.get(key,0),value)
            except Exception: pass
            await asyncio.sleep(5)
    monitor=asyncio.create_task(monitor_pool())
    try:
        for i in range(args.sessions):
            session_started = time.perf_counter()
            row = await replay(args.url, case, args.audio_root, args.mode, None)
            row["session_index"] = i
            row["session_wall_seconds"] = time.perf_counter() - session_started
            records.append(row)
            print(f"session {i+1}/{args.sessions}: {row.get('failure') or row.get('terminal_status')}", flush=True)
            if args.pause_seconds and i + 1 < args.sessions:
                await asyncio.sleep(args.pause_seconds)
    finally:
        monitor.cancel();await asyncio.gather(monitor,return_exceptions=True)
        resources = await sampler.finish() if sampler else None
        if temp_path:
            temp_path.unlink(missing_ok=True)

    cleanup = None
    if process:
        descendants = []
        try:
            import psutil
            descendants = [child.pid for child in psutil.Process(process.pid).children(recursive=True)]
        except Exception:
            pass
        exited = stop_owned_server(process)
        await asyncio.sleep(args.cleanup_wait_seconds)
        alive_descendants = []
        try:
            import psutil
            for pid in descendants:
                if psutil.pid_exists(pid):
                    alive_descendants.append(pid)
        except Exception:
            alive_descendants = None
        cleanup = dict(owned_server_pid=process.pid, graceful_exit=exited, exit_code=process.returncode,
                       observed_descendant_pids=descendants, descendants_still_alive=alive_descendants,
                       wait_after_exit_seconds=args.cleanup_wait_seconds)

    failures = sum(not row.get("functional_success", False) for row in records)
    first_kind = ("audio_segment" if args.mode == "spoken_interpretation" else
                  "target_partial" if args.mode == "translated_subtitles" else "source_unit_partial")
    output_values = [row["first_output_seconds"][first_kind] for row in records
                     if first_kind in row.get("first_output_seconds", {})]
    result = dict(
        schema="smartvoice.stream.stability.v1", mode=args.mode, case=args.case,
        source_language=case["language"], requested_long_audio_seconds=args.long_audio_seconds,
        repeated_sessions=args.sessions, pause_seconds=args.pause_seconds,
        completed_sessions=len(records), functional_failures=failures,
        failure_rate=failures/len(records) if records else None,
        session_wall_seconds=percentiles([row["session_wall_seconds"] for row in records]),
        first_output_metric=first_kind,first_output_latency_seconds=percentiles(output_values),
        audio_integrity_failures=sum(bool(row.get("audio_integrity_errors")) for row in records),
        skipped_audio_chunks=sum(len(row.get("final_snapshot", {}).get("audio_skipped", [])) for row in records),
        full_audio_success_rate=(sum(row.get("full_audio_success", False) for row in records)/len(records)
                                 if args.mode == "spoken_interpretation" and records else None),
        resources=resources, owned_process_cleanup=cleanup,
        shared_model_pool=dict(peak=pool_peak,recent_samples=list(pool_samples),
            scope='server admission estimates and queue counts; not measured native RSS'),
        interpretation="Repeated PCM fixture content is a soak/stability probe, not natural long-form language-quality coverage.",
        manifest_sha256=hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        environment=dict(platform=platform.platform(), python=platform.python_version(),
                         versions={name: version(name) for name in ("websockets", "psutil")}),
        records=records)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(args.output, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://127.0.0.1:8766/v1/audio/stream")
    parser.add_argument("--manifest", type=Path, default=Path("benchmarks/streaming/data/fleurs/clean.manifest.json"))
    parser.add_argument("--audio-root", type=Path, default=Path("benchmarks/streaming"))
    parser.add_argument("--case", default="zh_02_clean")
    parser.add_argument("--mode", choices=MODES, default="transcription")
    parser.add_argument("--sessions", type=int, default=10)
    parser.add_argument("--pause-seconds", type=float, default=1)
    parser.add_argument("--long-audio-seconds", type=float, default=0,
                        help="Repeat fixture PCM into one long session; 0 disables this probe")
    parser.add_argument("--server-pid", type=int, help="Sample an externally owned server; it will not be stopped")
    parser.add_argument("--server-command", nargs="+",
                        help="Start and stop only this explicitly supplied server subprocess; never use against a shared instance")
    parser.add_argument("--startup-timeout-seconds", type=float, default=90)
    parser.add_argument("--cleanup-wait-seconds", type=float, default=3)
    parser.add_argument("--cpu-cores", type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.sessions < 1 or args.pause_seconds < 0 or args.long_audio_seconds < 0:
        parser.error("sessions must be positive; pause and long-audio duration cannot be negative")
    if args.server_command and args.server_pid:
        parser.error("choose owned server-command or external server-pid, not both")
    if args.cpu_cores is not None and args.cpu_cores <= 0:
        parser.error("cpu-cores must be positive")
    if args.output.exists():
        parser.error("Output exists; use a fresh report path")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    process = owned_server(args.server_command, args.url, args.startup_timeout_seconds) if args.server_command else None
    try:
        asyncio.run(run(args, process))
    finally:
        if process and process.poll() is None:
            stop_owned_server(process)


if __name__ == "__main__":
    main()
