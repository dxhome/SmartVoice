# SmartVoice 实施计划

## 目标与当前约束

- 首期交付 Windows x64 上可从源代码启动的本地服务；当前不规划安装器或独立安装包。
- 中文和英语优先，首批模型、运行时和性能目标需经评估后确定。
- 当前可运行原型采用 sherpa-onnx；阶段 1 的最终决策仍需基于模型质量、性能、硬件和许可评估。STT 与 TTS 后续可分别选择后端。
- 前端和调用方只依赖版本化 HTTP API 与 capability 契约；平台和推理差异由适配层承接。
- 默认仅监听 loopback，默认不记录请求文本或音频。

## 阶段 0：冻结首期范围

确认 Windows 最低版本/架构、参考硬件、可接受的延迟与音频时长、GPU 首期要求、模型许可策略、模型源、OpenAI Audio API 兼容范围，以及 CLI/REST/管理界面的首期边界。

**产出：** 首期范围和量化验收基线。性能阈值不在缺乏参考机与语料时臆定。

## 阶段 1：推理后端与模型评估

比较 sherpa-onnx 与必要备选方案；分别评估中英文 STT/TTS 候选模型。记录固定 revision、文件哈希、许可证、格式、来源、模型与依赖体积、冷启动、RTF/延迟、峰值 RAM/VRAM、设备实测和质量结果。

**产出：** 后端决策记录、首批模型目录和兼容矩阵。只有实测并完成许可审查的模型才能标记“已验证”。

**当前原型选择：** 已把 sherpa-onnx 作为首个可运行原型后端，用 SenseVoice Small INT8 做 STT、Melo VITS ONNX 做中英 TTS。官方 Python API 和 Windows 轮子可用；本机实测已跑通两项推理。此决定支持端到端验证，仍不构成最终性能、模型质量或再分发许可承诺。参考：[SenseVoice Python API](https://k2-fsa.github.io/sherpa/onnx/sense-voice/python-api.html)、[Melo VITS 模型说明](https://k2-fsa.github.io/sherpa/onnx/tts/pretrained_models/vits.html)。

## 阶段 2：领域架构与 API 契约

冻结领域数据结构、OpenAPI、稳定错误结构、capability schema、模型元数据和适配器接口。领域层不依赖 Windows、GPU 厂商或推理框架；Windows 文件/设备/生命周期能力由平台适配器提供。

**产出：** 架构说明、OpenAPI 初版、版本和兼容策略。

## 阶段 3：CPU 源码部署纵向切片

从源代码启动服务，提供健康/就绪、运行时/capability、模型列表、文件 STT 和 WAV TTS。完善输入边界、错误映射、请求 ID、临时音频清理和默认脱敏日志。

**产出：** Windows CPU 环境可运行、可由 REST 调用的首个语音版本。

## 阶段 4：模型目录与生命周期

当前 Windows CPU 范围已实现目录查询、后台下载任务、取消与 HTTP Range 恢复、文件哈希/路径检查、临时目录和原子安装、激活/停用、卸载、磁盘空间查询及验证后的离线导入/导出。来源仅允许目录登记的 HTTPS URL；离线导入仅接受已登记模型 ID 的包。

**产出：** 当前目录模型从发现到激活、使用、卸载和离线迁移的本地生命周期。

## 阶段 5：GPU、诊断与首期发布验收

按验证矩阵加入 GPU provider；报告实际设备和回退原因。补齐资源限制、并发/取消/超时语义、配置迁移、运行说明、API 文档和第三方声明。

**产出：** 达到首期基线的 Windows 源码版本与可复现验收报告。

## 阶段 6：后续扩展

再评估流式 STT、更多语言/模型、镜像 provider、轻量管理 UI，以及 macOS/Linux/Android 平台适配。移动端可采用嵌入式 SDK，不强制常驻 Web 服务。

## 当前仓库结构

```text
SmartVoice/
├─ README.md
├─ LICENSE
├─ pyproject.toml                 # 当前实现选用 Python；依赖保持精简
├─ doc/
│  ├─ requirements-spec.md
│  ├─ industry-research.md
│  └─ implementation-plan.md
├─ catalog/models.json             # 固定 HTTPS 来源与模型文件清单
├─ src/smartvoice/
│  ├─ __main__.py                  # 本地 API 与模型 CLI
│  ├─ app.py
│  ├─ api/v1/routes.py
│  ├─ config/settings.py
│  ├─ domain/
│  ├─ ports/inference.py
│  ├─ adapters/inference/sherpa_onnx/
│  └─ services/model_catalog.py
├─ tests/
│  ├─ test_api.py
│  ├─ test_model_catalog.py
│  └─ test_real_inference.py       # 安装模型后执行，否则自动跳过
└─ examples/
   ├─ evaluate_models.py
   └─ stt-reference-manifest.csv
```

模型权重保存在用户数据目录，不进入 Git。后续再按模块增长情况拆分 API schema、平台服务、运行时诊断和模型任务管理。

## 验收方式

### STT 质量与性能

维护固定、人工校对的中文/英语语料及清晰的许可记录，覆盖口音、噪声、远讲、专名、中英混说、静音、损坏、超长等条件。对同一模型版本运行完整语料，保存逐条参考文本、识别结果与环境信息。

- 英语报告 WER；中文报告 CER，并另报数字/专名等关键实体错误。所有文本使用固定规范化规则。
- 记录冷启动、推理耗时、RTF、内存/显存峰值及真实执行设备。
- JiWER 可用于 WER/CER 计算；需针对中文定义稳定的规范化和分词规则。

### TTS 质量与性能

固定中英文文本集，覆盖数字、标点、缩写、多音字、专名和长文本。保存生成 WAV 和运行数据。

- 以人工盲听 MOS/成对偏好为主要质量判断；按自然度、可懂度、发音/韵律分项记录。
- DNSMOS、UTMOS 可用于自动回归筛查；TTSDS 可用于阶段性多维模型对比。自动预测分数不作为用户感知质量的唯一结论。
- 独立 STT 对 TTS 输出的识别结果可辅助评估可懂度，但会受该 STT 自身误差影响。
- 记录生成耗时、首音频延迟、RTF、峰值内存/显存、采样率和音频时长。PESQ/STOI 不作为无配对 TTS 音频的主指标。

### API、稳定性、离线与隐私

- 按 OpenAPI 契约检查正常请求、非法字段、错误码、取消/超时、队列上限和资源清理。
- 覆盖磁盘不足、下载中断、哈希不匹配、模型缺失、显存不足和设备回退等故障路径。
- 已安装模型断网推理；检查 loopback 默认监听、日志脱敏和临时音频清理。
- 在两种 provider/契约替身上验证适配器一致性，确认客户端契约不随后端变化。

所有质量/性能门槛由阶段 0 在参考机器与基准语料确定。每份报告记录模型 revision/hash、语料版本、规范化规则、硬件、驱动和运行时版本。

## 当前代码阶段验收

- Windows x64 CPU 源码运行纵向切片已具备：健康/按任务就绪、runtime/capability/model 查询、中文/英语文件 STT 和 WAV TTS、常见 WAV/MP3/FLAC/M4A 解码、请求 ID、字段级错误、脱敏诊断日志、并发队列上限及 STT/TTS 输入输出边界。
- Windows 主机和进程运行指标可通过 runtime、响应头和日志读取；包括 CPU/内存、进程工作集/峰值工作集及每次请求的 CPU 时间。默认推理并发为 1，排队容量和超时可配置。
- 真实模型测试覆盖中英 STT/TTS、四种输入音频容器、损坏音频和超长文本分块路径。性能脚本记录冷/热延迟、RTF、CPU 时间、归一化 CPU 利用率、进程内存，以及同进程加载任务/语言路径后的驻留峰值。
- 单机实测数据和复现步骤见 [Windows CPU Benchmark](../benchmarks/windows-cpu-benchmark.md)、[运行配置](../benchmarks/config/windows-cpu.json) 及机器可读 [JSON 报告](../benchmarks/result/windows-cpu-ryzen-ai-9-hx-370.json)。当前结果来自 Ryzen AI 9 HX 370，不是最低配置承诺或验收 SLA。
- 模型生命周期现已提供 CLI 和 REST 操作：按任务激活/停用、卸载保护、后台下载进度/取消、Range 续传、离线 ZIP 导入/导出和磁盘占用信息。服务端推理超时会返回 504；底层原生推理线程无法强制终止，因此该线程完成前仍占用推理槽位，避免超时后额外推理挤占资源。
- 配置支持 JSON 文件、环境变量覆盖和关键 CLI 启动覆盖；启动时报告数据目录/服务地址，并能识别已有服务实例。
- 核心验收仍有明确缺口：尚无获准使用且人工校对的代表性中英 STT 语料，因此没有可靠 CER/WER 和正式质量门槛；TTS 尚无固定听测集和人工盲听结论；尚未在最终约定的参考硬件上确定性能 SLA。TTS `language` 作为期望语言提示，按输入文字的主导字符脚本做冲突校验，实际发音仍由双语模型依据文本决定。
- 当前只验证 SenseVoice/Melo 与 sherpa-onnx CPU 路径。GPU、其他 OS、替代模型/后端的实测与契约替换验证、远程鉴权、流式 API 和管理 UI 仍待后续阶段；不纳入本次 Windows CPU 目标。
