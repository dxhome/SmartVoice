# Windows CPU Benchmark

This report records the current SmartVoice CPU-only benchmark run. It is an observed result, not a minimum hardware requirement or an acceptance SLA.

## Reference environment

| Item | Value |
| --- | --- |
| OS | Windows 11, build 10.0.26200 |
| CPU | AMD Ryzen AI 9 HX 370 with Radeon 890M |
| Logical CPUs | 24 |
| Physical memory | 100,521,586,688 bytes (about 93.6 GiB) |
| Python | 3.12.10 |
| sherpa-onnx | 1.13.8 |
| Device | CPU; 4 inference threads |
| Warm iterations | 5 per task/language |

The tested catalog contains SenseVoice Small INT8 for STT and Melo VITS ONNX for Chinese/English TTS. The bundled SenseVoice sample clips are used for STT performance measurements; they are not an accuracy corpus.

## Results

Times are seconds. RTF is inference time divided by audio duration. Memory is process working set after model loading; combined residency is after using both language paths for both tasks in one server process.

| Case | Clip/output duration | Cold first request | Warm request median | Warm inference median | Warm RTF median (p95) | Working set |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Chinese STT | 5.592 s | 1.181 | 0.159 | 0.145 | 0.0260 (0.0260) | 414 MB |
| English STT | 7.152 s | 1.198 | 0.200 | 0.183 | 0.0255 (0.0257) | 420 MB |
| Chinese TTS | 4.527 s | 2.745 | 0.940 | 0.919 | 0.2025 (0.2139) | 462 MB |
| English TTS | 5.263 s | 2.862 | 1.103 | 1.083 | 0.2058 (0.2102) | 470 MB |

With both STT language paths and both TTS language paths exercised in one process, working set reached 1,063,129,088 bytes (about 1.0 GiB); peak observed working set was 1,069,502,464 bytes. Warm inference used about 14–15% of all 24 logical CPUs for STT and about 16.6% for TTS when normalized across all logical CPUs. The process CPU time per call is higher than wall time because it sums CPU time across threads.

Cold first-request time includes model initialization and inference. Warm request time includes loopback HTTP overhead; warm inference time is measured by the API. The benchmark samples process memory and CPU while requests run, so telemetry polling adds a small amount of overhead. Startup time is separately recorded in the JSON report.

## Reproduce

With the two catalog models installed and the project's inference dependencies available, run from the repository root in PowerShell:

```powershell
.\.venv\Scripts\python.exe benchmarks/benchmark_cpu.py --config benchmarks/config/windows-cpu.json --output benchmarks/result/windows-cpu-ryzen-ai-9-hx-370.json
```

Edit [the Windows CPU benchmark configuration](config/windows-cpu.json) to change the host, port, warm iteration count, or warmup count. Result JSON files are stored under `result/`, while server output is stored under `log/`. The current machine-readable results, model archive hashes, timings, CPU and memory observations, and runtime versions are in [the benchmark JSON](result/windows-cpu-ryzen-ai-9-hx-370.json). Each individual case starts a fresh server process; a separate combined-residency pass loads all tested task/language paths into one process.

## Interpretation and open acceptance work

- These measurements describe one high-end Windows CPU host. They do not establish a low-end Windows hardware target or pass/fail SLA. Repeat on the agreed minimum reference machine before setting thresholds.
- They measure inference performance, not STT correctness. The bundled demo clips are too small and unrepresentative to establish CER/WER. A properly licensed, manually checked Chinese/English corpus and normalization rules are still needed.
- TTS quality still needs a fixed text set and human listening evaluation; automatic quality scores can be used as regression signals, not as the sole acceptance result.
- The benchmark covers the current SenseVoice/Melo catalog and CPU provider only. It does not characterize other models, GPU execution, sustained concurrent load, or long-duration soak behavior.
- TTS `language` is retained as request metadata; it does not force the model's pronunciation language. The current model/runtime derives pronunciation from the input text.
