# REST 音频优化：P0–P3 验证和迁移记录

日期：2026-10-04。本机 macOS 27 / Apple Silicon arm64 / CPU，每实例 2 个原生线程，实例上限 2。独立实验在 `sandbox/tts-output`；产品迁移依照 [架构指导](architecture-guidelines.md)，不涉及另一项 streaming sandbox 的 S 阶段。实验目录被 Git 忽略，本文件保留可版本化的结论。

## 已迁移

| 范围 | 当前行为 |
|---|---|
| 统一音频输出 | 所有 TTS adapter 输出规范的单声道 PCM16 WAV；Service 经同一个编码端口返回默认 MP3 96 kbps，或显式 WAV 字节直通。 |
| 能力和接口 | capability schema 1.1 区分模型生成格式与服务输出格式；缺少编码能力时推理前失败。控制台可选择格式，下载扩展名匹配实际响应。 |
| Supertonic 长文本 | 应用一次规划 200 字符分段，保留原文；每段结束释放模型实例并按 FIFO 重新排队。原生 adapter 避免重复切分；直接调用原生 adapter 仍保留原来的切分路径。音色 0–9 已在实验中验证，并同步到产品能力描述。 |
| 长音频 STT | 一次解码到有上限的私有临时存储；离线 STT 按能力声明分段：Whisper Base 25 秒，其余默认 15 秒，优先静音切分，所有边界重叠 1 秒。自动语言识别只做一次并复用于后续片段。逐窗释放实例，累计真实音频时长，原生时间戳加解码时间轴偏移。 |
| 内存和取消 | 拼接/解码临时存储的内存阈值 1 MiB；完整 WAV 和最终编码响应仍占用有上限的内存。单一执行截止时间贯穿原生调用和编码；取消后阻止新分段，正在运行的原生调用结束前保留租约。 |
| 默认限制 | 保持 STT 25 MiB / 600 秒；TTS 4000 字符 / 180 秒。内部 WAV、最终响应分别默认 32 MiB；TTS JSON 解析前限制 64 KiB。 |
| 旧配置 | 新字段有默认值；已有配置未填写内部预算时沿用其旧的输出字节限额，启动不重写保存的配置。 |

Qwen3-TTS 长文本优化依用户要求暂缓，但已接入统一输出编码。当前窗口来自模型能力声明，Whisper Base 在本次中文样本验证后改为 25 秒，其余默认 15 秒。Whisper 原生解码修复与验证范围见 [专项记录](whisper-chinese-decoding-fix.md)。此前 Whisper 的候选验证不覆盖当前 15 秒窗口、每个边界 1 秒重叠和单次语言识别组合；Qwen3 的长时资源评估也不覆盖该策略。需用所提供的多语言长音频补充真实模型回归；本次代码调整本身不等同于准确率验收。

## 测量结果

这些是本机探索性测量，不是 SLA。不同启动、模型缓存和分配器状态会改变峰值。

| 项目 | 旧路径 | 优化路径 |
|---|---|---|
| SenseVoice：300 秒重复英语 | 34.38 秒，峰值约 2039 MiB | sandbox 两次约 8.5–9.1 秒，峰值约 649–708 MiB |
| SenseVoice：600 秒重复英语 | 实验因超过 2 GiB 保护阈值被终止，无成功基线 | sandbox 约 16.3–17.5 秒，峰值约 717–747 MiB；产品两次约 15.5–15.8 秒，峰值约 600–667 MiB |
| Supertonic：长请求下短请求，单实例 3 轮 | 9 个短请求 P95 15.20 秒 | P95 3.86 秒；长请求本身可能略慢 |
| 相同输入 WAV → MP3 96 kbps | WAV 原始大小 | 压缩后约 13.7%，编码约 0.11 秒 / 16 秒音频；用户试听认为差异不大 |
| 相同 TTS 分段计划的拼接内存 | 浮点累计中位峰值约 638 MiB | spool 中位峰值约 571 MiB，下降约 10.5% |

产品 HTTP 固定每两秒到达、持续 60 秒的 30 项混合请求，包含长 TTS、300 秒 ASR 和短请求，全部成功，最终模型活跃/等待队列均为零。其余 27 个短请求中位耗时约 0.47 秒、最慢约 1.08 秒；这是固定的低到达率检查，未测出系统容量上限。独立 sandbox 的同类 60 秒负载也全部成功。

真实 HTTP 2 秒执行截止时间返回 `504`；返回时 TTS/ASR 的原生租约仍活跃，原生调用结束后才释放。断连检查同样保留当前租约，且不继续完整长文本。成功、失败、取消、限额拒绝后的临时文件和队列释放通过检查。

## 质量决策

固定 FLEURS revision `d16ac437ac42fb543bf2893b934dac1af970e361` 的四条英文、四条中文人工录音拼接，与相同录音逐条识别对照：

- SenseVoice 30 秒：英文 WER 8.45% → 8.45%；中文 CER 4.42% → 5.31%，相差一个字。该小样本支持本次有限范围实现，未构成全面质量验收。
- Whisper 的 10/15/30 秒候选有英文误差增加，未默认启用。
- Qwen3-ASR 30 秒候选的英文/中文误差良好，但同一进程两种语言的实验峰值约 2213 MiB；需要独立的长时长、缓存和资源评估，不能推广 SenseVoice 的内存结论。
- 精确数字静音返回空识别；噪声输入仍可能产生原生识别幻觉。没有宣称通用 VAD、噪声过滤或静音阈值能区分任意低音量语音。
- 强制边界的文字去重是有限精确重叠匹配，没有模糊改写；它仍是启发式，不保证任意连续语音无损拼接。未提供原生字时间戳的模型不会获得伪造时间戳。

FLEURS 为 Google/FLEURS、CC BY 4.0；使用本机已有固定缓存，原始音频未放入产品或本报告。参考资料与语料转换记录见独立实验的 `sandbox/streaming/data/real.manifest.json`。长时长资源样本为重复合成语音，其输出非空或长度正常不能当作准确率验收。

## 验证和复现

- 独立 sandbox：31 项契约测试通过，P0–P2 的原始矩阵/资源/过载/取消记录保留在 `sandbox/tts-output/runs`；P3 报告为 `p3.json`、`p3-controls.json` 和 `p3-*-30.json`。
- 产品 CI：160 项测试通过，新增默认 MP3、显式 WAV、三种模型采样率/三档码率、累计限额、一次分段、真实偏移、语言检测采样复用、预解析上限、取消、旧配置兼容等验证。
- 七个已安装模型的实际产品推理通过：SenseVoice、Whisper、Qwen3-ASR、Supertonic、Kokoro、Matcha、Qwen3-TTS。四种 TTS 均验证默认 MP3/显式 WAV；SenseVoice 实际执行 600 秒输入；Supertonic 验证长短请求并行及音色 9。其他模型只作短输入兼容性检查。
- 产品真实 HTTP 混合负载及取消/超时检查通过，记录为 `product-load.json`、`product-controls.json`。本机模型资产通过只读 repository 使用，运行缓存和新配置只写入实验工作目录。控制台浏览器验证 MP3/WAV 均成功并显示正确音色/格式选项；截图保留为 `sandbox/tts-output/reports/product-console-wav.jpg`。
- macOS arm64 wheel 构建和独立 target 安装通过，检查安装后的编码、目录能力及默认配置；不是干净操作系统或 Windows/Linux 验收。Qwen3-TTS 的单服务器进程内存指标不包含其原生子进程，未用于总内存结论。

```sh
.venv/bin/python -m unittest discover -s sandbox/tts-output/tests -q
.venv/bin/python sandbox/tts-output/verify_p3.py
.venv/bin/python sandbox/tts-output/verify_p3_controls.py
.venv/bin/python sandbox/tts-output/verify_p3_models.py stt-sensevoice-small-int8 30
.venv/bin/python scripts/test.py ci
.venv/bin/python sandbox/tts-output/verify_product.py tts-supertonic-v3-multilingual-int8
.venv/bin/python sandbox/tts-output/verify_product.py stt-sensevoice-small-int8
.venv/bin/python sandbox/tts-output/verify_product_load.py
.venv/bin/python sandbox/tts-output/verify_product_controls.py
```

## 保留的验证边界

本次完成的是本机 REST 实现和迁移范围。Windows/Linux 新编码路径、任意连续长语音的边界质量、完整长文本韵律及正式多语言质量基线未验收。已有跨平台原生引擎验证不会自动覆盖这些新路径；后续应使用独立样本和目标平台检查，不以当前有限数据宣布正式质量或并发容量。
