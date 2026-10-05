# 长音频完成性与 HTTP 回归

后续完整回归与原 MP3 候选复测见 [最新记录](stt-admission-boundary-validation.md)。

日期：2026-10-04。环境：本机 macOS / Apple Silicon / Python 3.11 / CPU，sherpa-onnx `1.13.8+smartvoice.whisper2`。本轮保持现有输入、输出、超时和并发上限；Whisper Base 使用能力声明的 25 秒窗口，其余 STT 默认 15 秒，所有切点重叠 1 秒。

## 本轮改动

- LID 的 `InferenceTimeoutError` 直接传播；原生 LID 返回或失败后再次检查总截止时间与取消信号，避免进入自动模型降级或开始转写。
- 增加 LID 超时和取消的确定性回归。
- 直接运行完整 STT 测试模块时也检查 Whisper 修复包；标准 `scripts/test.py full` 已有依赖和模型前置检查。
- 长 TTS 用例明确使用 Supertonic 的英语输入。当前能力不支持中文，因此不能把原中文长文本直接替换为 Supertonic；Qwen3-TTS 保留短输入兼容检查，长文本优化仍暂缓。
- 真实推理测试读取已验证的模型资产快照，通过测试 repository 提供原始模型路径；运行状态、路由副本、完整性缓存和拼接文件写入临时目录。测试不会以模型目录符号链接绕过产品的安装安全校验；仅 LID 资产链接到临时状态。
- 新增真实 TCP HTTP 验证脚本，使用自有临时端口，结束时关闭服务和实例池。报告仅保留元数据，不保存识别全文。

## 验证结果

最终代码下 CI 176 项全部通过；完整套件 244 项全部通过，包含三种 STT 的中英文短输入、窗口边界、75 秒以及 300/590 秒长输入。完整套件同时覆盖七个已安装模型的短输入和修正后的 Supertonic 长文本用例。没有测试跳过。

首次完整运行发现长 TTS 用例使用了 Supertonic 不支持的中文；改为英语后重跑又暴露测试向用户数据目录写临时文件的沙箱权限错误。完成语言样本和运行状态隔离后，单项及最终完整套件均通过。这两个测试问题不属于长 STT 推理失败。

真实 TCP HTTP 矩阵完成 20 条长 STT 成功路径：三种模型各覆盖 75 秒 WAV/MP3、显式中文/model-auto、300 秒英语 WAV、600 秒中文 MP3；另含中文与英文的 smartvoice-auto + auto。

| 模型 | 600 秒中文 MP3 HTTP 耗时 | 分段 / 成功原生调用数 |
|---|---:|---:|
| SenseVoice Small INT8 | 11.35 秒 | 62 / 62 |
| Whisper Base INT8 | 42.23 秒 | 36 / 36 |
| Qwen3-ASR 600M INT8 | 71.41 秒 | 62 / 62 |

静音切点会增加分段数，因此不能用固定窗口除法推导所有样本的预期段数。所有成功请求的原生调用数与返回分段数相等；具体模型请求执行零次独立 LID，两个智能路由请求各执行一次 LID。所有输入返回真实总时长，包括精确 600 秒输入。

- 601 秒 WAV 返回 413；损坏音频返回 422。
- Supertonic 的 MP3/WAV 请求均返回正确 MIME 和 200。
- 2 秒执行截止时间返回 504，返回后观察到一个仍活跃的原生实例；共完成 11 次原生调用后停止，随后释放实例和队列预约。
- 客户端断连后仅完成 1 次原生调用，未继续完整长音频。
- 最终所有实例池 active/waiting 与 HTTP 队列预约均为零；自有服务器已关闭，临时测试目录删除。

## 复现

```sh
.venv/bin/python scripts/test.py ci
.venv/bin/python scripts/test.py regression
.venv/bin/python scripts/test.py full
.venv/bin/python scripts/validate_speech.py stt-http --report sandbox/tts-output/runs/stt-http-current.json
```

原始本机报告在被 Git 忽略的 `sandbox/tts-output/runs/stt-http-current.json`。固定语音资产和来源见 `tests/fixtures/stt/README.md`；运行与依赖说明见 `doc/testing.md`。

## 验证边界与下一步

本轮使用 Kokoro 生成的固定中英文语句，循环拼接到指定时长，验证的是完整链路、限额、分段调用和资源释放。没有重新运行此前用户的电影解说 MP3，也没有计算本轮 CER/WER。当前结果不证明连续自然语音准确率、混合语言能力、跨平台兼容或并发容量。

耗时来自单个本机顺序 HTTP 矩阵，包含不同缓存状态，不是模型排名或 SLA。macOS 测得整个测试进程峰值约 3594 MiB；它包含多个模型、语言缓存和分配器状态，是整个混合矩阵的累计峰值，不能归因于某个模型，也不是请求结束后的常驻内存。因此后续缓存优化仍需独立测量模型加载、语言切换和回收后的内存。

下一步依次处理：明确 model-auto 分段语言语义与缓存身份；建立自然语音质量基线并修正重叠合并；对比窗口/重叠策略；运行混合任务并发与资源验证。默认长度、超时和并发数暂不调整。
