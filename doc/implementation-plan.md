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

实现目录查询、下载任务、取消与恢复、文件大小/哈希检查、临时目录和原子安装、激活/停用、卸载、磁盘提示、离线导入/导出。只允许目录登记的 HTTPS 来源或本地文件，不接受任意下载 URL。

**产出：** 模型从发现到激活、使用、卸载的完整本地生命周期。

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

- 已支持源码启动、loopback HTTP 服务、OpenAPI 页面、模型目录列表/安装、中英文文件 STT、中文/英文文本 WAV TTS、capability/runtime/ready 状态。
- 已通过契约测试、模型包安全解压/安装测试，以及下载模型后的真实中英 STT 和 TTS smoke test。
- 示例语料只有官方模型包内的演示音频，不能据此宣称质量达标。发布前仍需准备有许可的本地测试集、正式质量/性能基线和人工 TTS 听测。
- 下载目前由 CLI 同步完成，不支持断点续传/取消，也没有长任务 API；下载清单固定 HTTPS 来源，并校验固定 SHA-256。该摘要由原型使用的 HTTPS 文件计算并固定，上游未提供独立发布的校验和；更新模型时仍须审阅来源和摘要变更。
- 目前只验 CPU provider；GPU、模型激活选择、离线导入/导出、远程鉴权、两类真实后端替换契约和其它平台仍待实现。
