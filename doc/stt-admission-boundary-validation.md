# STT 完整回归、过载及切点补充验证

日期：2026-10-04。macOS / Apple Silicon / Python 3.11 / CPU，原生包 `sherpa-onnx 1.13.8+smartvoice.whisper2`。承接 [上一轮交互与时间戳验证](stt-interaction-alignment-validation.md)。

## B 阶段：完成性与错误链路

### 完整回归

本轮先执行 `scripts/test.py full`，255 项通过、无跳过，约 506 秒。覆盖三种 STT 的中英文短输入、边界、75 秒、300/590 秒，以及已安装模型和 Supertonic 长文本。随后新增的六项实验策略检查由 CI 193 项通过覆盖；随后以最终实验代码执行默认真实模型回归，249 项通过、无跳过。完整套件的 255 项计数属于新增实验选项之前，不能与后续 CI 计数直接相减推导跳过项。

### 真实 TCP 过载和排队超时

使用真实 Qwen3-ASR，先预热 1 个实例，再提交 70 秒中文后台任务；观察到原生租约占用后发短请求。测试只改变隔离应用的容量和排队超时，不阻塞或替换原生推理。

| 场景 | 并行开关 | 允许排队 | 短请求状态 | 拒绝耗时 |
|---|---:|---:|---:|---:|
| HTTP 全局容量满 | 0 | 0 | 503 / inference_overloaded | 0.0068 秒 |
| HTTP 全局排队超时 | 0 | 1 | 503 / inference_overloaded | 0.1064 秒 |
| 模型实例容量满 | 1 | 0 | 503 / inference_overloaded | 0.0141 秒 |
| 模型实例排队超时 | 1 | 1 | 503 / inference_overloaded | 0.1220 秒 |

排队超时设为 0.1 秒；同时核对公开错误信息以区分拒绝来源。四次后台任务均返回 200、7 个窗口，成功原生调用增量均为 7，证明拒绝请求未进入原生推理。每次池 active/waiting 和 HTTP 队列预留归零，恢复短请求再次返回 200。自有服务器退出，临时状态删除。这是有界错误链路检查，不是过载容量或持续吞吐测量。

### 原失败 MP3 候选复测

在 Downloads 找到文件名与电影解说用途一致、解码时长与旧记录精确一致的候选：

`2023-09-12 19.18.52_耗时一个月只为看懂传奇之作Incepti_video.mp3`

- 文件 1,287,308 字节，实际解码 53.5684375 秒。
- SHA256：`22977910b0775e63acdcf11fc26fe290e3cdb621644db330dd2eb3f5caccfebd`。
- 路径为 `/Users/xuan/Downloads/` 下同名文件；只读使用，未复制原音频到报告或仓库。
- 尚无旧上传的 SHA256 可核对，候选身份与人工参考全文已向用户询问，不能自动视为已确认的原上传，也没有做本轮 CER 验收。

| 模型/语言 | 状态 | 分段 / 原生调用 | HTTP 耗时 |
|---|---:|---:|---:|
| SenseVoice / zh | 200 | 4 / 4 | 1.79 秒 |
| SenseVoice / auto | 200 | 4 / 4 | 1.66 秒 |
| Whisper / zh | 200 | 3 / 3 | 8.51 秒 |
| Whisper / auto | 200 | 3 / 3 | 8.44 秒 |
| Qwen3-ASR / zh | 200 | 4 / 4 | 11.82 秒 |
| Qwen3-ASR / auto | 200 | 4 / 4 | 8.54 秒 |
| smartvoice-auto / auto | 200 | 4 / 4 | 1.80 秒 |

具体模型路径的独立 LID 调用数为 0，智能路由为 1；全部返回 53.568 秒。冷加载与缓存状态不同，不据此做速度排名。

同一自有 HTTP 服务还通过 601 秒拒绝、损坏音频、Supertonic MP3/WAV、执行截止时间和断连检查。截止时间返回 504 时仍有一个原生租约，完成 6 次原生调用后停止并释放；本次断连发生在原生启动前，调用数为 0。最终所有池 active/waiting 和 HTTP 队列预留归零。

## C 阶段：切点与相对能量实验

沿用同一批 FLEURS 中文/英语各 10 条 clean 录音、固定 revision、SHA256、CC BY 4.0 和参考归一化。来源及限制见上一轮报告。长中文 96.74 秒、英语 87.56 秒、交替混合 184.30 秒。共完成 27 条绝对阈值比较、18 条相对阈值比较及 18 条较长最短窗口比较，63 条 API 请求均成功并覆盖末尾。

### 发现与实验边界

原静音判定为 20 ms 帧 RMS < 0.006，并在窗口最后三分之一查找至少 200 ms 的连续低能量。该阈值会把低音量语音判为安静：本数据的 `en_05_clean` 至 `en_09_clean` 全部帧均低于此阈值，但包含语音且有非空原生识别结果。因此“静音优先”切点并不必然落在真正停顿中。

新增实验选项只影响窗口选择，不改变送给模型的原始 PCM：

- 相对阈值：`min(0.006, 0.1 × 搜索区域 RMS 的第 95 百分位)`；完全数字静音仍保持原阈值判断。
- 提前搜索：分别从第 2 秒和第 8 秒寻找切点。
- 重叠：比较所有切点重叠和仅强制切点重叠。

这些选项是音频 adapter 的通用数据参数。共享服务没有模型名称分支；生产组装未启用实验选项。窗口上限仍由已有模型能力声明。相对能量不是经过验证的 VAD，录音边界标签也不是词级标注。

### 主要结果

以下只列当前默认及两个相对能量/仅强制重叠候选。完整绝对阈值和相对阈值对照保留在 JSON 报告。

| 模型 | 策略 | 中文 CER | 英语 WER | 混合 CER | 中文/英语/混合段数 |
|---|---|---:|---:|---:|---:|
| SenseVoice | 当前默认 | 6.09% | 10.94% | 10.78% | 9 / 8 / 17 |
| SenseVoice | 相对能量，最短 2 秒 | 6.09% | 9.90% | 9.70% | 20 / 15 / 40 |
| SenseVoice | 相对能量，最短 8 秒 | 6.41% | 8.85% | 6.05% | 10 / 9 / 19 |
| Whisper | 当前默认 | 44.87% | 25.00% | 82.42% | 6 / 5 / 10 |
| Whisper | 相对能量，最短 2 秒 | 60.26% | 27.08% | 49.92% | 20 / 14 / 39 |
| Whisper | 相对能量，最短 8 秒 | 34.94% | 9.38% | 51.41% | 9 / 8 / 18 |
| Qwen3-ASR | 当前默认 | 3.21% | 13.54% | 21.14% | 9 / 8 / 17 |
| Qwen3-ASR | 相对能量，最短 2 秒 | 2.56% | 9.90% | 9.37% | 20 / 15 / 40 |
| Qwen3-ASR | 相对能量，最短 8 秒 | 4.17% | 7.81% | 28.77% | 10 / 9 / 19 |

直接把搜索起点提前到 2 秒、继续使用绝对阈值和所有切点重叠，会造成过碎分段和明显退化：Whisper 中文 CER 达 96.79%，SenseVoice 混合 CER 23.55%，Qwen 混合 CER 29.10%。该候选不推广。

相对阈值保留原搜索区域/所有重叠也不统一提升：SenseVoice 混合 CER 12.44%，Whisper 82.09%，Qwen 12.27%。重复的九条该策略请求在两轮相对阈值实验中 CER/WER完全相同，但最短 2/8 秒候选各仅运行一轮，不声称已经正式复现或通过泛化验收。

记录了窗口真实起点、重叠、时长、原生报告语言、文字长度及能量统计，核对了终止覆盖。跨录音语言标签仅代表已知录音来源的交集，不证明窗口内实际语种或逐词切点。现有绑定没有可靠的 EOT/预算退出遥测，本轮没有重建原生依赖；不能据这些元数据宣布 Whisper 具体原生根因已经解决。

## 决策与剩余工作

保持生产默认：Whisper 25 秒、其他 STT 15 秒、所有切点重叠 1 秒，以及现有长度、输出与执行预算。

1. B 的主要服务验证已补齐；仍需确认候选 MP3 身份及人工参考，才能完成原上传的全文质量验收。
2. C 已有不同参数的真实质量证据。SenseVoice/Whisper 的 8 秒候选、Qwen 的 2 秒候选值得分别复测，但不是统一默认；Qwen 的更多调用需独立测交互延迟与资源成本。
3. 补连续录音、低音量/背景噪声、快速中文和合法重复表达。以后若采用不同策略，通过验证后的能力/配置表达并与能力查询保持一致，不能在服务按模型 ID 写分支。
4. E 的持续混合负载、其他引擎组合、重复加载/关闭内存归因仍未完成，不能用本轮错误链路或 CER 实验代替。

## 复现

```bash
.venv/bin/python scripts/test.py full
.venv/bin/python scripts/verify_stt_admission.py --report sandbox/tts-output/runs/stt-admission.json
.venv/bin/python scripts/verify_stt_http.py --sample '/absolute/path/sample.mp3' --sample-only --report sandbox/tts-output/runs/stt-local-sample-http.json
.venv/bin/python scripts/compare_stt_policies.py --quiet-boundaries --report sandbox/tts-output/runs/stt-quiet-boundaries.json
.venv/bin/python scripts/compare_stt_policies.py --relative-boundaries --report sandbox/tts-output/runs/stt-relative-boundaries.json
.venv/bin/python scripts/compare_stt_policies.py --relative-boundaries --minimum-quiet-seconds 8 --report sandbox/tts-output/runs/stt-relative-boundaries-8.json
.venv/bin/python scripts/test.py ci
.venv/bin/python scripts/test.py regression
```

原始报告保留在被 Git 忽略的 sandbox。测试读取已验证资产，隔离状态，不下载模型，也不保存原音频或转写内容。所有自有服务器已关闭；不操作其他聊天启动的服务。
