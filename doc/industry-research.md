# 本地 STT/TTS 一体化推理方案：业界调研

**调研日期：2026-09-27**
**范围：** 面向 PC 与智能手机等边缘设备；长期覆盖 Windows、macOS、Linux、Android，第一阶段 Windows；关注轻量离线推理、多语言（中文/英语优先且可扩展）、STT/TTS Web API、Agent 互操作、模型分发与许可。

## 1. 执行摘要

“业界没有 STT/TTS all-in-one 推理程序”并不完全准确。`sherpa-onnx` 已经提供离线 ASR、TTS、VAD 等能力，覆盖 Windows/Linux/macOS，支持 CPU 和 NVIDIA CUDA，并有 Python、C/C++、C# 等接口；FunASR 则是成熟的 ASR 工具链，faster-whisper 是 Whisper 的高效本地推理实现，Piper、Kokoro、CosyVoice 等提供不同侧重点的 TTS 模型/实现。

真正未被这些组件共同解决的是产品化整合：普通用户可安装的跨平台程序、统一且稳定的本地 HTTP API、可发现/下载/校验/删除模型、运行时自动选择 CPU/GPU、模型能力与许可透明、网络断开后仍可完整工作。建议建设一个“统一控制面 + 多推理后端适配器”的本地服务，而不是押注单一模型框架包办所有模型。

建议首期围绕 Windows 构建轻量服务/API 控制面，以 sherpa-onnx 承载已验证的轻量 ONNX 模型，并保留独立适配器。将 API 与推理引擎解耦，为后续 macOS/Linux/Android 的原生运行时留接口。先交付 CPU 必可运行、GPU 可选加速、文件转写和非流式 TTS；实时流式识别作为首期目标能力，但应限定在明确支持的模型。不得宣称任意 STT/TTS 模型均可用，也不得把“参数低于 7B”当作可在任意 PC 流畅运行的充分条件。

## 2. 问题与术语

- **STT/ASR：** 输入音频，输出文本、语言、时间戳/分段等。
- **TTS：** 输入文本及可选说话人/风格，输出音频。
- **离线运行：** 推理期间无需网络；首次下载、更新模型需要联网，除非通过离线模型包导入。
- **推理框架与产品：** 推理框架提供算子/模型运行能力；产品还需安装升级、模型管理、API、配置、日志、权限、错误处理和文档。
- **all-in-one：** 此需求应定义为一个进程/安装包/服务对外提供 STT 与 TTS，而非要求所有模型使用同一底层推理引擎。

## 3. 市场与技术现状

### 3.1 推理框架/运行时

| 方案 | STT | TTS | CPU/GPU 与平台 | 适配性与限制 |
|---|---|---|---|---|
| [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) | 流式和非流式 | 有 | CPU；CUDA GPU；Windows/Linux/macOS 等 | 最接近“本地一体化推理引擎”的成熟基础。预训练模型列表丰富，模型须符合其支持的架构/导出格式；不能直接运行任意 Hugging Face checkpoint。适合作为轻量首选后端。 |
| [FunASR](https://github.com/modelscope/FunASR) | 强项，含 Paraformer、SenseVoice 等 | 非其主要能力 | CPU/GPU，依赖 PyTorch 生态 | 中文识别生态强，提供模型和 VAD/标点等组合。适合作为 ASR 适配器；不能单独满足统一 TTS。其依赖、模型下载和运行环境需与轻量 ONNX 路线分别管理。 |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | Whisper 文件/片段转写 | 无 | CPU int8、NVIDIA GPU fp16/int8；Python | CTranslate2 推理可节省内存并支持量化；CUDA 依赖版本匹配是安装风险。适合多语言转写的可选后端，不等同实时低延迟流式方案。 |
| [whisper.cpp](https://github.com/ggml-org/whisper.cpp) | Whisper 转写 | 无 | CPU、部分 GPU 加速与多平台 | C/C++、GGML 量化生态适合桌面发行；单独不提供 TTS，需统一 API 层封装。 |
| PyTorch / Transformers | 取决于模型 | 取决于模型 | CPU 可用但通常较慢；CUDA 可加速 | 模型覆盖广、迭代快，代价是依赖较重、CUDA/权重格式复杂、模型实现差异大。作为高质量模型插件路线，而不宜当作所有用户的唯一安装路径。 |
| ONNX Runtime | 支持已导出模型 | 支持已导出模型 | CPU Execution Provider；CUDA 等 EP | 利于轻量部署；硬件执行提供程序和模型图兼容性需验证。不能仅凭“ONNX”保证加速或模型兼容。 |

**结论：** 统一 API 可以做到，统一模型运行时不现实也不必要。后端必须以 capability manifest 声明模型支持的任务、语言、流式能力、设备、精度、输入输出格式和依赖。

### 3.2 STT 模型候选

| 模型/系列 | 规模/定位 | 语言/能力摘要 | 推荐定位 | 注意事项 |
|---|---|---|---|---|
| [SenseVoiceSmall](https://huggingface.co/FunAudioLLM/SenseVoiceSmall) / FunASR | 约 234M（FunASR 模型目录标注） | 中、英、粤、日、韩；支持情绪/音频事件相关标签 | 中文优先默认候选 | 原生模型与 ONNX/sherpa 适配模型的能力、精度不一定相同；需实测具体 checkpoint。 |
| [Paraformer](https://www.funasr.com/en/models.html) | 多种规模；常见中文模型 | 中文及部分英语模型；可配 VAD/标点 | 中文离线高吞吐候选 | 需要声明完整模型组合和语言范围；不要将 VAD/标点作为核心网络推理能力混淆。 |
| [Whisper](https://github.com/openai/whisper) tiny/base/small 等 | small 约 244M；另有更小版本 | 多语言转写和翻译 | 多语言/英文覆盖候选 | 标准 Whisper 偏文件/分段任务；逐字流式响应需分块策略，并非模型原生流式。不同实现/量化格式许可和质量需核对。 |
| [Moonshine](https://github.com/usefulsensors/moonshine) | tiny/base 等轻量系列 | 以英语为主的低延迟路线 | 英语实时识别备选 | 语言覆盖较窄；作为按语言选择的 profile，而非全语言默认。 |
| sherpa-onnx 预训练 ASR 模型 | Zipformer、Paraformer、SenseVoice、Whisper 等 | 按模型不同，含中英、多语、流式模型 | 首期 CPU/小内存模型目录 | 每个仓库独立发布；准确度、字典/tokenizer、采样率、许可均需模型级登记。 |

官方 FunASR 文档将 SenseVoiceSmall 标为五语言 234M 模型，并列出 Paraformer、SenseVoice 等 CLI 选择；sherpa-onnx 模型目录展示从在线流式识别到离线多语言模型的多种选择。模型“主流”应理解为产品可选目录，不代表同时预装。语言覆盖不是统一属性：STT 与 TTS、模型及具体 voice 各自声明语言及地区变体；中文和英语优先验收，同时允许通过目录扩展日语、韩语、粤语及其他经验证语言。

### 3.3 TTS 模型候选

| 模型/系列 | 规模/定位 | 语言/能力摘要 | 推荐定位 | 注意事项 |
|---|---|---|---|---|
| [Piper](https://github.com/OHF-Voice/piper1-gpl) | 多个轻量 ONNX 语音 | 语音/语言因 voice 而异，CPU 友好 | 最低资源、快速本地合成 | 每个 voice 有自己的模型卡和许可；Piper 软件许可与 voice 权重许可要分别记录。 |
| [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) / ONNX 社区转换版 | 82M | 轻量、高质量候选；官方/转换版本语言覆盖不同 | 英语等高效自然度候选 | 语言和 voice 对应关系取决于准确的 checkpoint 与前端；转换版不是自动等同原版。确认许可证、词典/G2P依赖及商用边界。 |
| [CosyVoice 2 0.5B](https://huggingface.co/FunAudioLLM/CosyVoice2-0.5B) | 0.5B | 中英等多语；零样本/跨语言、流式等能力依具体实现 | 中文自然度/克隆能力候选 | PyTorch/显存及依赖较重；许可和克隆语音授权必须逐项核对。普通 CPU 不能承诺实时。 |
| [Qwen3-TTS 0.6B 系列](https://github.com/QwenLM/Qwen3-TTS) | 0.6B | 多语言生成，具备多种说话人/控制能力，按子模型区分 | 高质量可选模型 | 对 7B 上限而言规模合适，但显存、延迟和依赖明显高于轻量 TTS；应作为可选 profile，确认许可证和模型分发条款。 |
| [XTTS-v2](https://huggingface.co/coqui/XTTS-v2) | 约 0.5B 级多语克隆 | 多语言、参考音频语音克隆 | 对比候选 | 模型卡标注 Coqui Public Model License，不能因为代码开源就默认模型权重可自由商用。首发建议不纳入默认可下载集合。 |
| sherpa-onnx TTS 模型 | Piper、VITS、Matcha、Kokoro/ZipVoice 等支持项随发布更新 | 依模型而异 | CPU 优先目录及统一轻量运行 | 用户下载前仍须呈现模型卡、许可、语言、资源需求和后端兼容性。 |

### 3.4 模型获取与许可

- 公开仓库不意味着免许可。代码、模型权重、声学前端、词典/音素化器、预置 voice 可能各有不同许可。
- 模型注册表要记录来源 URL、固定 revision/commit、文件清单、SHA-256、模型卡 URL、许可证 SPDX/原文、商业使用条件、模型大小、任务、语言、运行时和设备要求。
- 下载流程应支持 Hugging Face、ModelScope 或可信直链等 provider；下载源在用户环境不可达时可更换镜像/离线导入。不得在程序内写死某个地区的镜像。
- 支持断点续传、临时文件、校验后原子安装、失败清理/续传、磁盘空间预检和删除；安装阶段需提示网络活动，推理阶段默认不联网。
- 不承诺“可通过 Hub 搜到的所有模型都能一键跑”；目录只列本项目验收过的版本。
- 隐私方面：语音输入、参考音频、生成结果默认只保存在本机配置目录，不上传，不用于训练；远程 LAN API 必须显式开启。

## 4. 竞品空白与产品机会

现有工具分别擅长模型推理、特定语言 ASR、单一模型的 TTS 或模型服务部署。用户仍需自行解决 Python/CUDA 安装、多个项目端口和接口差异、模型仓库挑选与许可筛查、路径/缓存/删除、设备冲突、错误定位、离线迁移和自动启动。因此产品价值应集中在：

1. 一套服务同时暴露 STT 与 TTS API；
2. 安装器对 CPU 路径零 GPU 前置依赖，对 GPU 路径做明确兼容检测；
3. 模型目录区分轻量默认模型和可选高质量模型；
4. 一个模型管理器支持下载、校验、激活、卸载和离线导入；
5. 明确告诉用户实际加载的设备、量化类型、模型许可、预计下载空间与已知限制。

## 5. 推荐架构

```text
客户端 / 本机脚本
        │ HTTP JSON + multipart；可选 WebSocket
统一 API 服务（OpenAPI、鉴权/绑定、校验、请求队列）
        ├── 模型注册表与本地模型管理（下载、校验、缓存、许可/能力元数据）
        ├── STT 适配器：sherpa-onnx / FunASR / faster-whisper（可选）
        └── TTS 适配器：sherpa-onnx / Piper / PyTorch TTS（可选）
             │
        CPU 默认；兼容时选择 GPU；显式设备状态及回退原因
```

建议 API 以 OpenAPI 3.x 描述，语言/locale 使用明确代码并从模型 capability 查询，兼容 OpenAI 风格的 `/v1/audio/transcriptions`、`/v1/audio/speech` 常用子集，并提供 `/v1/models` 与本项目扩展的能力/下载/状态端点。OpenAI 风格是降低集成成本的事实约定，不等于中立标准或完整云端兼容。

### 关键架构原则

- 独立适配器接口：`load/unload/health/capabilities/transcribe/synthesize`；每次请求不重新加载模型。
- 控制面负责模型生命周期、API 校验和服务状态；适配器隔离依赖冲突，可在不同进程运行，首版按依赖复杂度决定是否拆分。
- 显式 `device=auto|cpu|cuda`；`auto` 先检测可用后端，再按兼容矩阵加载；加载失败提供原因并允许配置 CPU 回退。不得把 CPU 回退伪装为 GPU 成功。
- 管理 VRAM/RAM 并发：同一 GPU 上默认限制同时驻留的大模型数量；负载冲突给出可操作错误/排队状态。
- 推理数据本地闭环；离线启动不触发遥测、远程模型检查或自动更新。
- 兼容性取决于安装包、Python/Runtime、驱动及具体模型版本，使用锁定版本和可审计构建清单。

## 6. 证据来源

以下来源均为官方项目文档/官方模型仓库或论文；在线内容会变化，落地实现应记录访问日期、模型 revision 与文件校验值。

1. [sherpa-onnx GitHub](https://github.com/k2-fsa/sherpa-onnx)：离线 ASR/TTS/VAD 功能、平台和模型示例。
2. [sherpa-onnx 官方文档总览](https://k2-fsa.github.io/sherpa/intro.html)：CPU/GPU、平台、API 绑定及 sherpa 子项目差异。
3. [sherpa-onnx Python 安装与 CUDA](https://k2-fsa.github.io/sherpa/onnx/python/install.html)：CPU 与 NVIDIA GPU 安装路线。
4. [FunASR 官方文档](https://funasr.com/en/docs/command-line.html) 与 [模型目录](https://www.funasr.com/en/models.html)：SenseVoice、Paraformer 型号与语言范围。
5. [FunASR 论文](https://arxiv.org/abs/2305.11013)：Paraformer/VAD/标点工具链背景。
6. [faster-whisper GitHub](https://github.com/SYSTRAN/faster-whisper)：CPU int8、GPU 精度、依赖及基准说明。
7. [Piper 官方仓库](https://github.com/OHF-Voice/piper1-gpl)：本地 TTS 与 voice 模型许可注意项。
8. [Kokoro 模型卡](https://huggingface.co/hexgrad/Kokoro-82M)：模型与 voice 资料；应以实际所选转换仓库为准。
9. [CosyVoice2 官方模型卡](https://huggingface.co/FunAudioLLM/CosyVoice2-0.5B)：0.5B 模型及获取说明。
10. [Qwen3-TTS 官方代码库](https://github.com/QwenLM/Qwen3-TTS)：模型代码、推理方式与发布模型信息。
11. [XTTS-v2 模型卡](https://huggingface.co/coqui/XTTS-v2)：模型许可标注。
12. [Whisper 官方仓库](https://github.com/openai/whisper)：模型、任务及实现说明。


## 7. Web API 与 Agent 互操作标准调研

### 7.1 是否存在统一 STT/TTS Web API 标准？

目前没有一个跨厂商、被广泛实现、规定完整 STT/TTS REST 路径、字段、音频流与错误语义的单一行业标准。需要区分三类规范：

- **OpenAPI Specification（OAS）：** 描述 REST 接口的机器可读规范，便于生成文档/客户端/校验；它不规定转写或语音合成具体应该叫什么字段或路径。
- **OpenAI Audio API 兼容约定：** 常见接口包括 `POST /v1/audio/transcriptions`（multipart 文件转写）和 `POST /v1/audio/speech`（文本转语音）。LocalAI 等本地推理服务主动兼容该 API，说明它对既有 SDK/Agent 工具有现实互操作价值；但这是事实上的兼容约定，不是中立标准，厂商字段和响应存在差异。
- **MCP（Model Context Protocol）：** Agent 客户端发现并调用工具的协议。可把 `transcribe_audio`、`synthesize_speech` 暴露为工具，音频可以通过 MCP 内容块返回；MCP 不替代底层 HTTP 音频端点，较适合作为可选适配器。

### 7.2 推荐接口策略

1. 用 OpenAPI 3.x 发布本地服务契约，生成 Swagger UI/客户端并纳入版本管理。
2. 文件转写与语音合成优先兼容 OpenAI Audio API 常见路径/字段，供使用 OpenAI SDK 或支持 base URL 的 Agent/应用连接。认证允许本机无密钥或本地 token，但不得要求云端 API Key。
3. 对流式音频，独立定义 WebSocket 消息 schema（连接初始化、音频 chunk、partial/final、错误、取消和结束）；在 OpenAPI 中链接该 schema。不可将云端 Realtime 协议宣称为通用标准。
4. 为用户选择 MCP stdio 或本地 HTTP transport 的可选 server，暴露语义明确的两个工具；避免工具参数里塞任意路径，音频输入采用受限文件句柄/数据块方案并规定大小限制。
5. 明确兼容性表：与 OpenAI API 路径、字段、响应格式、错误结构、流式方式分别达到“兼容/部分兼容/不兼容”。

### 7.3 官方参考

- [OpenAPI Specification](https://spec.openapis.org/oas/latest.html)：REST API 描述规范。
- [OpenAI Audio API Reference](https://platform.openai.com/docs/api-reference/audio)：转写与语音合成的广泛采用接口约定示例。
- [LocalAI TTS API](https://localai.io/features/text-to-audio/)：本地推理服务兼容 OpenAI TTS API 的实例。
- [MCP Specification](https://modelcontextprotocol.io/specification/2025-11-25)：Agent 工具互操作协议；具体部署应锁定实现的规范版本。

## 8. 调研结论更新

目标从“普通 PC 的 all-in-one”扩展为“边缘设备、多平台演进、Agent 友好 API”。因此轻量性必须覆盖端侧资源账本和按平台的推理/打包策略。Windows 首发不应以桌面服务架构限制后续 Android：API/capability schema 可以复用，推理引擎和模型格式可以按平台替换。下文包含 sherpa-onnx 与新目标的详细差距评估。


## 9. sherpa-onnx 与 SmartVoice 新目标差距评估

**评估日期：2026-09-27**
**目标：** 轻量边缘设备（PC/手机）、多语言（中文/英语优先并可扩展）、多平台（第一阶段 Windows）、标准 Web API 接入 Agent。

### 9.1 结论

`sherpa-onnx` 是很强的**端侧推理引擎/SDK**，与“轻量、多平台、离线 STT+TTS”目标的核心推理部分相当接近；但它本身不等同于 SmartVoice 所需的**可安装产品与统一 Agent API 服务**。差距主要在产品外层：Windows 一键安装与生命周期、成套标准 REST API、OpenAI 音频 API 兼容承诺、完整 STT+TTS API 一致性、模型目录管理和 API 安全/文档。

推荐定位：把 sherpa-onnx 作为首期 Windows 的默认推理后端/SDK 候选，先验证目标模型、性能和打包；SmartVoice 独立实现 API facade、模型管理、配置与产品 UX。对 sherpa 无法覆盖或质量不够的模型，通过适配器补入其他 runtime，而不要 fork 引擎去承担产品层功能。

### 9.2 按目标逐项对照

| 目标 | sherpa-onnx 现状 | 差距 | 判断 |
|---|---|---|---|
| 轻量、离线、边缘设备 | 基于 ONNX Runtime，有 C/C++、C API 等，提供轻量 ASR/TTS 模型；文档强调离线推理 | 轻量不是所有模型的固有保证；不同模型的 RAM/延迟/能耗必须按目标设备测量。项目仍需模型推荐和资源 profile | **核心能力接近** |
| 多语言扩展 | sherpa-onnx 模型目录涵盖不同语言的 STT/TTS 候选，个别模型支持中英粤日等多语 | 不同模型与 voice 的语言覆盖差异很大；SmartVoice 需提供按任务/模型的语言 capability，不能假设“多语言模型”语言集相同 | **底层可选项丰富，产品目录需定义** |
| STT + TTS 同一底层 | 支持在线/离线 ASR 和 TTS，另有 VAD 等 | 各模型架构和能力不同；不是任意 checkpoint 的统一 runtime，也不代表有统一模型安装/激活 UX | **推理覆盖接近** |
| Windows 首发 | 提供 Windows 支持，且有 C/C++/Python/JavaScript 等 API/示例 | 需确认所选编译/预编译库、CPU 指令集、音频编解码、安装器、签名、升级、杀软误报与依赖打包 | **有底座，产品化差距中等** |
| 后续 macOS/Linux/Android | 项目覆盖多个桌面/移动平台并提供 Android 相关示例/绑定 | 同一模型未必所有平台都有相同执行 provider/性能；Android app 生命周期、JNI/NDK、模型资产和权限需各自集成 | **引擎覆盖好，跨平台产品差距中等** |
| 标准 Web REST API | 有非流式 WebSocket 服务示例、流式 WebSocket server；Python 非流式示例脚本含 HTTP 处理 | 示例服务不等于正式统一 REST API；未看到 OpenAPI 描述或 OpenAI `/v1/audio/*` 兼容的承诺；TTS 没有同等的标准 REST 产品面 | **主要差距** |
| 实时 STT | 有流式 WebSocket server/client | 需要统一消息 schema、鉴权、错误、取消、版本化、连接限额及 API 文档 | **核心推理有，协议产品化缺** |
| TTS API | 有模型推理 API/库和示例 | 需要 REST 输入/输出、voice 枚举/能力发现、音频格式、错误、分块流式等统一封装 | **推理有，HTTP API 缺** |
| Agent 对接 | 可经 SDK/示例连接 | 缺 OpenAPI 契约与显式兼容清单；无内建 MCP 工具服务器的证据 | **需要 SmartVoice facade/adapter** |
| 模型发现与下载 | 官方文档列有大量预训练模型及下载链接 | 不等同应用内注册表、固定 revision、哈希校验、断点续传、许可筛查、删除与导入 | **需要自建管理层** |
| 隐私/安全与部署 | 可本地执行；WebSocket 示例可配 TLS | 示例常用于开发验证；SmartVoice 仍需默认 loopback、上传限制、token、请求日志脱敏、安装更新签名等产品安全要求 | **需要产品化** |

### 9.3 API 标准方面的差距

#### 当前可复用

- sherpa-onnx 已有 WebSocket 识别 server/client 示例：在线流式和离线识别均有示例；离线 server 使用 WebSocket，流式 server 提供音频 chunk 会话。
- 存在非流式 WebSocket 服务和 Python 非流式 HTTP/WebSocket 示例代码，可用于验证模型服务思路。
- sherpa-onnx 暴露多语言库/绑定，SmartVoice 可直接嵌入或由本地服务调用。

#### SmartVoice 仍需定义

1. **OpenAPI 3.x REST 契约：** `/v1/audio/transcriptions`、`/v1/audio/speech`、`/v1/models`、健康状态与错误 schema。
2. **OpenAI API 兼容边界：** multipart 字段、格式支持、verbose JSON、错误体、模型 ID 语义、TTS 返回媒体类型逐项说明；只兼容实际实现的子集。
3. **流式协议：** WebSocket 消息 envelope、PCM 编码/采样率、序号、partial/final、backpressure、取消与重连。
4. **Agent adapter：** OpenAPI 可由支持 HTTP 的 agent/client 使用；若要 MCP 原生工具发现，再增加轻量 MCP server wrapper。MCP 不是 REST API 标准的替代物。
5. **生命周期 API：** capability/model metadata、模型下载任务、设备状态、加载/卸载和就绪状态。

所以在 API 层，sherpa-onnx 不是需要“差一点配置”的开箱产品，而是提供服务端可复用组件，SmartVoice 需要构建独立的接口产品面。

### 9.4 粗略差距评分

评分只衡量和新目标的相对贴近程度（5=非常接近目标，1=基本无覆盖），不代表质量排名。

| 维度 | 评分 | 说明 |
|---|---:|---|
| 轻量离线推理引擎 | 4.5/5 | CPU/ONNX/移动与嵌入式路线贴合；模型和硬件仍需实测。 |
| STT/TTS 推理能力 | 4/5 | 两者均覆盖，但特定语言、质量与模型架构需要选型。 |
| 多平台底层支持 | 4/5 | 目标 OS/移动端均有项目支持；交付包和统一表现仍须开发。 |
| Windows 可交付产品 | 2.5/5 | Windows 支持不等于安装器、更新、配置、运行服务和诊断体验。 |
| 统一标准化 Web API | 1.5/5 | 有 demo server/transport，但未提供 SmartVoice 要求的 OpenAPI+稳定 REST 产品契约。 |
| 模型商店/许可/生命周期 | 2/5 | 有模型列表/链接；注册表、可校验下载、安装删除和许可 UX 需要自建。 |
| Agent 即插即用体验 | 2/5 | 可通过 Web API/SDK 封装；OpenAI 音频 API 兼容与 MCP 需要额外适配。 |

### 9.5 建议的 SmartVoice 与 sherpa-onnx 边界

```text
Agent / 桌面客户端
      │
      ├── REST：OpenAPI 描述 + OpenAI Audio API 常用接口兼容
      ├── WebSocket：SmartVoice 流式 STT schema
      └── 可选 MCP Server：transcribe_audio / synthesize_speech
                  │
       SmartVoice Windows Service（API、模型目录、设备/任务、日志和配置）
                  │ Adapter interface
                  ├── sherpa-onnx（首选轻量 CPU/ONNX/移动共用模型）
                  └── 其他引擎适配器（仅用于必要的质量/模型覆盖差异）
```

API 契约属于 SmartVoice，不应直接暴露 sherpa 内部类、命令行参数或示例 WebSocket 消息格式。这样以后 Android 可以复用 API/capability 语义，但按设备选择 sherpa C API/JNI 或其他 native backend；API 是否在手机上作为本地 HTTP 服务运行，还需评估移动系统对后台服务、localhost 暴露和省电策略的限制。

### 9.6 第一阶段 Windows 需补齐的工作

#### P0：技术验证和范围冻结

- 在目标 Windows x64 环境确认 sherpa-onnx CPU wheel/原生依赖、最低 Windows 版本、CPU 指令集和音频解码支持。
- 选择中文和英语优先、并覆盖至少一个额外目标语言的 STT/TTS 候选模型（若候选支持），固定来源 revision/哈希，测模型体积、冷启动、峰值 RAM、RTF、TTS 首包延迟和质量。
- 验证模型是否覆盖离线中文/英文需求，记录流式能力；没有经过验证不能只因模型出现在上游清单就标记兼容。
- 确认发行和许可义务（引擎、ONNX Runtime、模型、voice、音素化依赖分别核对）。

#### P1：SmartVoice 产品层

- Windows 服务/CLI 启动器、用户数据目录、配置、日志轮转和干净卸载。
- OpenAPI REST facade：STT/TTS 两端点与模型/健康查询。
- 模型目录 UI 或 CLI，下载校验、状态、激活/卸载、离线导入。
- 受限 loopback 监听和本地 token；API 不暴露任意本地文件路径/任意模型下载 URL。
- 安装包、依赖固定、更新和签名流程；安装后不要求用户装 Python/编译工具。

#### P2：Agent 与平台扩展

- OpenAI API 兼容测试客户端/示例；Agent 配置文档。
- 可选 MCP stdio server，适合本机 Agent 启动；远程部署场景再评估 HTTP transport。
- 兼容性抽象稳定后开展 macOS/Linux，再做 Android JNI/NDK 端侧组件与移动端生命周期设计。

### 9.7 最终建议

**不建议把“使用 sherpa-onnx”当作目标完成。** 它可以显著减少 STT/TTS runtime 和跨平台 native inference 的研发，但不能替代 SmartVoice 的 Windows 产品发行、标准 Web API、模型治理和 Agent 对接工作。

建议采用“SmartVoice API/产品层 + sherpa-onnx 默认后端 + 可选模型适配器”。在决定是否增加 FunASR、faster-whisper 或大型 TTS runtime 之前，先用 Windows 参考机对 sherpa-onnx 的候选模型完成质量、资源和许可验证；如果轻量目标优先，谨慎引入较重的 PyTorch/CUDA 依赖。

### 9.8 资料依据

- [sherpa-onnx 官方项目](https://github.com/k2-fsa/sherpa-onnx)：支持的能力、平台和语言绑定。
- [sherpa-onnx Python 安装文档](https://k2-fsa.github.io/sherpa/onnx/python/install.html)：CPU 与 CUDA 安装选择。
- [流式 WebSocket server 文档](https://k2-fsa.github.io/sherpa/onnx/python/streaming-websocket-server.html)：流式 ASR 示例。
- [非流式 WebSocket server 文档](https://k2-fsa.github.io/sherpa/onnx/websocket/offline-websocket.html)：离线识别 server 示例。
- [sherpa-onnx 示例 HTTP handler](https://github.com/k2-fsa/sherpa-onnx/blob/master/python-api-examples/non_streaming_server.py)：现有非流式示例服务代码。它是项目示例，不视为生产 REST 兼容承诺。
- [OpenAPI Specification](https://spec.openapis.org/oas/latest.html)：REST API 的描述规范。
- [OpenAI Audio API Reference](https://platform.openai.com/docs/api-reference/audio)：转写与合成常用路径/字段参考。
- [LocalAI TTS API](https://localai.io/features/text-to-audio/)：本地服务兼容 OpenAI TTS API 的实例。
- [MCP Specification](https://modelcontextprotocol.io/specification/2025-11-25)：Agent 工具调用协议。
