"""Explicit local TCP HTTP regression; uses installed assets and isolated state.

Run with the project runtime. Reports contain metadata, never transcripts.
Synthetic fixtures verify completion and resource ownership, not ASR quality.
"""
from __future__ import annotations

import argparse
import hashlib
from dataclasses import replace
import io
import json
import logging
from pathlib import Path
import resource
import socket
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import av
import httpx
import sherpa_onnx
import uvicorn
from smartvoice.app import create_app
from smartvoice.config.settings import Settings
from tests.stt_regression_audio import build_audio
from tests.inference_environment import isolated_runtime, language_id_ready
from scripts.validation.stt_validation_common import TraceStore, WindowTrace, check_coverage, provenance, TemporaryStorageTrace

MODELS = ("stt-sensevoice-small-int8", "stt-whisper-base-multilingual-int8", "stt-qwen3-asr-600m-int8")


def mp3(wav):
    output = io.BytesIO()
    with av.open(io.BytesIO(wav)) as source, av.open(output, "w", format="mp3") as target:
        stream = target.add_stream("libmp3lame", rate=16000)
        stream.layout = "mono"
        stream.bit_rate = 96000
        resampler = av.AudioResampler(format="s16p", layout="mono", rate=16000)
        for frame in source.decode(audio=0):
            for converted in resampler.resample(frame):
                for packet in stream.encode(converted):target.mux(packet)
        for converted in resampler.resample(None):
            for packet in stream.encode(converted):target.mux(packet)
        for packet in stream.encode(None):target.mux(packet)
    return output.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--sample", type=Path, help="Optional local MP3 for explicit completion regression; transcript is not saved")
    parser.add_argument("--sample-only", action="store_true", help="Run the MP3 sample and error/resource checks instead of the synthetic success matrix")
    parser.add_argument("--models",nargs="+",choices=MODELS,default=list(MODELS))
    args = parser.parse_args()
    models=tuple(args.models)
    if args.sample_only and args.sample is None:parser.error('--sample-only requires --sample')
    if "stt-whisper-base-multilingual-int8" in models and sherpa_onnx.__version__ != "1.13.8+smartvoice.whisper2":
        raise RuntimeError("Requires the validated Whisper repair wheel")
    installed_settings = Settings.from_env()
    assets = installed_settings.models_dir
    for model in (*models, "tts-supertonic-v3-multilingual-int8"):
        if not (assets / model / "smartvoice-model.json").is_file():
            raise RuntimeError(f"Missing model: {model}")
    if not language_id_ready(installed_settings):
        raise RuntimeError("Requires installed and verified LID assets")
    report = {"tested_models":list(models),"deferred_models":[m for m in MODELS if m not in models],"runtime": sherpa_onnx.__version__, "fixture": "fixed synthetic bilingual speech",
              "cases": [], "completed": False, "provenance": provenance(installed_settings)}
    sample = None
    if args.sample:
        if args.sample.stat().st_size>installed_settings.max_upload_bytes:raise ValueError('Sample exceeds upload budget')
        sample=args.sample.read_bytes()
        from smartvoice.adapters.audio.input import BoundedAudioInput
        with BoundedAudioInput(installed_settings.max_audio_seconds).prepare(sample) as prepared:
            sample_duration=prepared.duration
        report['local_sample']={'name':args.sample.name,'sha256':hashlib.sha256(sample).hexdigest(),
                                'bytes':len(sample),'decoded_seconds':sample_duration,
                                'identity':'caller-selected local candidate; no automatic identity or accuracy claim'}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    logging.getLogger("smartvoice.api").setLevel(logging.WARNING)
    try:
        with TemporaryStorageTrace() as storage, isolated_runtime(installed_settings) as (settings, provider):
            app = create_app(settings, provider=provider)
            tracing = TraceStore(); app.add_middleware(tracing.middleware())
            windows = WindowTrace(app.state.transcription_service.audio_input)
            app.state.transcription_service.audio_input = windows
            logging.getLogger("smartvoice.api").setLevel(logging.WARNING)
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            try:
                with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=650) as client:
                    until = time.monotonic() + 30
                    while not server.started:
                        if not thread.is_alive() or time.monotonic() > until:raise RuntimeError("Server did not start")
                        time.sleep(.05)
                    def transcribe(model, language, duration, fmt, spoken="zh", expected=200, input_audio=None):
                        raw = build_audio(duration, spoken) if input_audio is None else input_audio
                        if fmt == "mp3" and input_audio is None:raw = mp3(raw)
                        started = time.perf_counter()
                        before = client.get("/v1/runtime").json()["backends"]["sherpa-onnx"]["instance_pools"]
                        request_id = f"stt-validation-{len(report['cases'])}"
                        response = client.post("/v1/audio/transcriptions", headers={"x-request-id": request_id},
                            files={"file": (f"fixture.{fmt}", raw, "audio/mpeg" if fmt == "mp3" else "audio/wav")},
                            data={"model": model, "language": language, "response_format": "verbose_json"})
                        body = response.json()
                        case = {"model": model, "language": language, "spoken": spoken, "duration": duration,
                                "format": fmt, "status": response.status_code, "seconds": round(time.perf_counter()-started, 3),
                                "fixture": "local-sample" if input_audio is not None else "synthetic"}
                        case["stages"]=tracing.snapshot(request_id)
                        report["cases"].append(case)
                        if response.status_code!=expected:case['error']=body.get('error',{})
                        assert response.status_code == expected, case
                        if expected == 200:
                            case.update(selected_model=body["model"], actual_duration=body["duration"],
                                        resolved_language=body.get("language"),
                                        chunks=body.get("chunk_count", 1), text_characters=len(body["text"]),
                                        processing_seconds=body.get("processing_seconds"), runtime_wait_seconds=body.get("runtime_wait_seconds"))
                            assert body["text"].strip() and abs(body["duration"]-duration) < .1, case
                            if duration > 25.1: assert case["chunks"] > 1, case
                            case["coverage_end"] = check_coverage(windows.rows, duration, case["chunks"])
                            case["windows"] = list(windows.rows)
                            case["stages"] = tracing.snapshot(request_id)
                            after = client.get("/v1/runtime").json()["backends"]["sherpa-onnx"]["instance_pools"]
                            selected = body["model"]
                            calls = after[selected]["completed"]-before.get(selected, {}).get("completed", 0)
                            assert calls == case["chunks"], (case, calls)
                            lid_calls = after.get("language-identification", {}).get("completed", 0)-before.get("language-identification", {}).get("completed", 0)
                            assert lid_calls == (1 if model == "smartvoice-auto" and language == "auto" else 0), (case, lid_calls)
                            case.update(native_calls=calls, adapter_calls=calls, native_decode_calls=case['stages'].get('native_inference',{}).get('calls',0), lid_calls=lid_calls)
                        print(json.dumps(case), flush=True)
                    if not args.sample_only:
                        for model in models:
                            for duration in (8, 25.02):
                                for fmt in ("wav", "mp3"):
                                    for language in ("en", "auto"):
                                        transcribe(model, language, duration, fmt, spoken="en")
                            transcribe(model, "zh", 70, "wav")
                            for fmt in ("wav", "mp3"):
                                for language in ("zh", "auto"):transcribe(model, language, 75, fmt)
                            transcribe(model, "en", 300, "wav", spoken="en")
                            transcribe(model, "zh", 600, "mp3")
                            transcribe(model, "auto", 590, "mp3")
                        for language in ("zh", "en"):
                            for requested in (language, "auto"):
                                transcribe("smartvoice-auto", requested, 75, "mp3", spoken=language)
                        transcribe("smartvoice-auto", "auto", 300, "wav", spoken="en")
                    if sample is not None:
                        for model in models:
                            for language in ("zh","auto"):
                                transcribe(model,language,sample_duration,"mp3",input_audio=sample)
                        transcribe("smartvoice-auto","auto",sample_duration,"mp3",input_audio=sample)
                    transcribe(models[0], "zh", 601, "wav", expected=413)
                    bad = client.post("/v1/audio/transcriptions", headers={"x-request-id":"invalid-audio"}, files={"file": ("bad.wav", b"invalid", "audio/wav")},
                                      data={"model": models[0], "language": "zh"})
                    assert bad.status_code == 422, bad.status_code
                    report["invalid_audio_status"] = bad.status_code
                    report["invalid_audio_stages"] = tracing.snapshot("invalid-audio")
                    oversized = client.post("/v1/audio/transcriptions", files={"file": ("oversized.wav", b"x"*(settings.max_upload_bytes+1), "audio/wav")}, data={"model": models[0], "language": "zh"})
                    assert oversized.status_code == 413
                    report["upload_limit_status"] = oversized.status_code
                    for fmt in ("mp3", "wav"):
                        response = client.post("/v1/audio/speech", json={"model": "tts-supertonic-v3-multilingual-int8",
                            "input": "Hello, this is a regression check.", "language": "en", "response_format": fmt})
                        assert response.status_code == 200 and response.headers["content-type"] == ("audio/mpeg" if fmt == "mp3" else "audio/wav")
                    report["tts_formats"] = ["mp3", "wav"]
                    # Change only the test application's request deadline.
                    # The native lease must survive the response/disconnect.
                    saved_settings = app.state.settings
                    app.state.settings = replace(saved_settings, inference_execution_timeout_seconds=2)
                    def group():
                        return client.get("/v1/runtime").json()["backends"]["sherpa-onnx"]["instance_pools"][models[0]]
                    def drain():
                        until = time.monotonic()+30
                        while time.monotonic()<until:
                            state = group()
                            if not state["active"] and not state["waiting"] and not app.state.inference_queue._reserved:return state
                            time.sleep(.05)
                        raise AssertionError("Cancelled native lease did not drain")
                    raw = build_audio(600, "en")
                    before = group()["completed"]
                    response = client.post("/v1/audio/transcriptions", headers={"x-request-id":"deadline"}, files={"file": ("long.wav", raw, "audio/wav")},
                                           data={"model": models[0], "language": "en"})
                    assert response.status_code == 504, response.status_code
                    active_after_response = group()["active"]
                    windows_at_timeout = len(windows.rows)
                    after = drain()
                    report["deadline"] = {"status": 504, "active_after_response": active_after_response,
                                          "native_calls": after["completed"]-before}
                    assert len(windows.rows) == windows_at_timeout, "Started a window after timeout"
                    report["deadline"].update(no_later_window=True,stages=tracing.snapshot("deadline"),emitted_windows=windows_at_timeout)
                    before = group()["completed"]
                    try:
                        client.post("/v1/audio/transcriptions", headers={"x-request-id":"disconnect"}, files={"file": ("long.wav", raw, "audio/wav")},
                                    data={"model": models[0], "language": "en"}, timeout=httpx.Timeout(650, read=.5))
                        raise AssertionError("Expected client read timeout")
                    except httpx.ReadTimeout:pass
                    # Wait for the disconnect monitor, even if the request has
                    # not entered native inference yet.
                    time.sleep(.5)
                    active_after_disconnect = group()["active"]
                    windows_at_disconnect = len(windows.rows)
                    after = drain()
                    report["disconnect"] = {"active_after_disconnect": active_after_disconnect,
                                            "native_calls": after["completed"]-before}
                    assert len(windows.rows) == windows_at_disconnect, "Started a window after disconnect"
                    report["disconnect"].update(no_later_window=True,stages=tracing.snapshot("disconnect"),emitted_windows=windows_at_disconnect)
                    assert report["deadline"]["native_calls"] < 40
                    assert report["disconnect"]["native_calls"] < 40
                    app.state.settings = saved_settings
                    runtime = client.get("/v1/runtime").json()
                    pools = runtime["backends"]["sherpa-onnx"]["instance_pools"]
                    assert all(not p["active"] and not p["waiting"] for p in pools.values())
                    assert app.state.inference_queue._reserved == 0
                    report["final_pools"] = pools
                    report["audio_spools"] = storage.assert_closed()
                    report["process_peak_rss_native_units"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            finally:
                server.should_exit = True
                thread.join(timeout=60)
                sock.close()
                if thread.is_alive():raise RuntimeError("Server did not shut down")
        report["owned_server_stopped"] = True
        report["isolated_state_removed"] = not settings.data_dir.exists()
        assert report["isolated_state_removed"]
        report["completed"] = True
    finally:
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":main()
