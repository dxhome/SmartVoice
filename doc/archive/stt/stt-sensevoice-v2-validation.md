# SenseVoice 新候选：质量、稳定负载与资源验收

日期：2026-10-05。本轮只优化验证工具，产品默认值不变。Whisper 优化继续暂缓；Qwen3-ASR 作为跨模型短请求，Supertonic 作为 TTS 并行请求。Qwen3-TTS 长文本不在范围内。

## 候选与证据范围

新候选 `relative-minimum8` 使用相对静音阈值、八秒最小窗口，保留所有切点一秒重叠和 SenseVoice 十五秒上限。旧候选 `candidate` 使用仅强制切点重叠；旧候选的质量失败、混合进程 RSS 与本轮结果分开保存。生产音频 adapter 已支持这些实验参数；共享服务没有增加模型分支。

人工通过只作为继续实验的工作假设。真实人工审核仍为 `pending`，不能用机器错误率代替边界、数字、人名和合法重复表达的核对。旧试听包针对旧候选，不能用于签署新候选。

沿用已固定的 AISHELL-4 中文和 AMI 英文各两份录音、75/300/590 秒连续前缀及三组 FLEURS 拼接控制。来源、许可、通道、参考文本提取规则和校验和见 `sandbox/stt-continuous/manifest.json` 和 [连续验收记录](stt-continuous-acceptance-validation.md)。三个轮次是同批录音的重复执行，长短前缀也非独立录音。混合语言为拼接控制；重叠说话只记录时长，没有独立对齐的说话人错误率。

原问题 MP3 已由用户确认身份；当前 SenseVoice 与 Qwen3-ASR 的真实 HTTP 请求均完成并覆盖全长。Whisper 当前代码复测因本地模型资产缺失未执行；历史参考文本尚未固定进可复现的元数据报告，因此质量验收仍未完成。详情见 [原 MP3 复测记录](stt-admission-boundary-validation.md)。

## 三轮完整质量比较：已执行

六个独立进程、每进程十五个输入，共九十次请求；每次窗口调用数和返回分段数一致，覆盖到真实末尾。归一化沿用大小写折叠、去标点，不做数字或繁简等价变换。

| 分组 | 当前 | 新候选 | 汇总错误率不退化 |
| --- | ---: | ---: | --- |
| 连续中文 CER | 20.186% | 19.466% | 是 |
| 连续英文 WER | 23.586% | 23.445% | 是 |
| 拼接中文 CER | 6.090% | 5.769% | 是 |
| 拼接英文 WER | 10.938% | 8.333% | 是 |
| 混合拼接 CER | 10.779% | 4.146% | 是 |

这通过了预先约定的语言与连续/拼接分组门槛，但有四个输入退化：`L_R003S02C02-75` 中文增加三个错误；`EN2001a` 的 75/300/590 秒输入分别增加七、二十五和一个错误。尚不能声明每份录音改善或边界零新增错误。

每轮调用数当前 420、新候选 408。三轮累计请求耗时分别为当前 76.691/78.022/76.046 秒、新候选 78.713/79.184/76.230 秒。候选调用更少不等于总体更快。质量工具 RSS 包含服务和客户端，不能与下述独立服务器 RSS 混用。

记录：`sandbox/tts-output/runs/stt-sensevoice-v2-quality.json` 及六份轮次 JSON。记录包含生产代码、依赖、模型清单、样本及参考文本指纹、实际策略、逐阶段独占耗时和窗口位置；不保存完整转写。

## 稳定负载、实例与资源

使用 `scripts/validate_speech.py stt-isolated-load`：服务在自有子进程的临时 TCP 端口运行，客户端独立；服务与客户端 RSS 分开采样。私有进程管道只用于实验状态查询、超时校准与重启，没有增加公开 API。模型目录只读使用，配置和路由副本位于独立临时状态，不修改个人配置。

固定到达率：同模型短 STT 间隔 0.4 秒、Qwen3-ASR 短 STT 间隔 1.6 秒、Supertonic 短 TTS 间隔 0.8 秒。每组每策略五百请求，背景连续执行 120 秒合成中文 STT。输入是功能与负载样本，不能替代自然语音质量评估。

运行于 Apple Silicon M1 Pro、16 GiB macOS 主机，CPU provider；Python 3.11、sherpa-onnx 1.13.8+smartvoice.whisper2、NumPy 2.4.6、FastAPI 0.141.1、HTTPX 0.28.1。实际设置为 `num_threads=4`、`min_instances=1`、`max_instances=2`、`max_concurrent_inference=1`、每模型等待上限 2、队列超时 60 秒、执行超时 600 秒、实例空闲回收 300 秒。

保留实际线程、队列、执行预算和实例设置。记录冷启动、所有允许实例预热、串行对照、定时请求和最终排空；预热不关闭生产的空闲回收。成功和失败请求的延迟分别报告，快速拒绝不得当作延迟改善。

配对门槛：全成功、等待趋势稳定、调度滞后小于到达间隔；短 P95、背景长请求平均完成时间、服务器峰值 RSS 的候选/当前比值均不超过 1.1。P99 基于五百请求，局限于本机器和此负载；本轮不是多硬件容量认证。

单实例实验只改隔离状态中的 `max_instances`，使用相同同模型负载。不同模型和 TTS 的单实例配置没有五百请求确认。

生命周期使用同一服务器进程重复加载、长请求、超时、真实 TCP 断连、恢复和关闭五轮。`tracemalloc` 仅用于生命周期；性能测试不启用它。超时预算以热 30 秒探针校准，且必须观察到真实原生推理调用，取消后不允许启动后续窗口。检查租约、传输预留、临时 spools、实例弱引用和临时状态删除。

首个生命周期预试验失败：固定微小超时在启用 Python 分配追踪后发生在解码阶段，没有覆盖原生推理取消。失败记录保留为 `stt-sensevoice-v2-lifecycle-pilot.json`；修正校准后的两轮预试验通过，不能替代正式五轮。RSS 不回到初值不直接判定泄漏，Python 追踪也不能覆盖原生分配器。

正式执行记录与最终判定由 `stt-sensevoice-v2-release.json` 及配套子报告保存。CI 228/228、SenseVoice 真实回归 254/254、SenseVoice 完整回归 258/258 均通过。回归测试在本轮期间新增的路由配置 API 改动之后完成。

截至本次跨模型运行完成的初步结果：同模型负载配对通过；SenseVoice 长任务＋Qwen3-ASR 短任务候选为 475/500（95%），有 25 次 HTTP 503 `inference_overloaded`，当前策略为 500/500。候选 P95/当前比为 5.057，长请求完成时间比为 1.223，独立服务器峰值 RSS 比为 1.117；因此跨模型组明确不通过。虽候选自身等待分箱仍稳定，拒绝率与延迟门槛失败已足够否决。Qwen 排队过载在当前样本上与候选策略同时出现，报告证明了相关性，尚不能单独归因到相对静音切分；此结果也限制新候选的整体推荐。

Supertonic 短 TTS 配对两边均为 500/500 成功，但候选 P95/当前为 1.321、长 STT 完成时间比为 1.148，RSS 比为 0.991；TTS 组不通过延迟门槛。单实例同模型 500 请求对照成功，`max_instances=1` 相对二实例的 P95 为 1.594、长请求完成时间为 1.191、RSS 为 0.629，单实例配置不通过体验/延迟门槛。以上负载使用同一设备、设置和模型，不能外推至其他硬件。

当前和候选各进行了五轮单进程生命周期验证。每轮超时、断连均发生于原生推理调用期间；下一窗口调用数为零，运行时实例弱引用数、租约和 spool 数归零，隔离状态目录删除成功。当前策略 RSS（轮前/加载后/关闭后）分别从 96/693/692、692/779/776、776/555/556、557/679/677、677/689/468 MiB 波动；候选从 95/428/462、462/540/453、443/443/427、428/488/495、495/390/438 MiB 波动。没有单调 RSS 上升。

Python 堆追踪当前字节数却每轮增加约 0.33 MiB。额外三轮对象计数显示首轮主要是一次性 importlib 导入缓存（约 5.3 MiB）；后续每次重建 app 后仍多出约 341 个 list、261 个 dict、199 个闭包 cell、48 个 `pydantic.fields.FieldInfo`、35 个 Pydantic schema validator/serializer，以及 33 个 FastAPI `ModelField`/`TypeAdapter`。分配位置也落在动态生成代码、Pydantic、FastAPI 路由与 JSON 解码对象。

这把持续增长范围收敛到同一进程反复创建 FastAPI app 时仍存活的路由/schema 对象族；适配器实例弱引用都已归零。但对象计数及单帧分配栈没有指出具体保留者，因此还不能判定它是 FastAPI/Pydantic 全局缓存、被关闭 app 的引用，还是泄漏。常规产品进程只创建一次 app；如需支持同进程反复重启 app，应追查这些对象的引用链并验证缓存上限。RSS 在五轮正式运行中没有单调增长，不支持原生推理内存泄漏结论。

配对压测每组的当前/候选生产源文件哈希完全一致。压测开始后工作区另有路由配置 API、服务和测试页变更，导致总报告的最终 `product_code_unchanged` 标记为 false；这些 API 改动后的 CI、回归及完整回归均已通过。报告保留实验开始与结束各自的代码指纹，不把压测描述成针对后续路由改动重新测得。

## 复现

独立运行，避免同时进行其他原生模型压测：

```bash
.venv/bin/python scripts/validate_speech.py stt-quality --models stt-sensevoice-small-int8 --candidate-policy relative-minimum8 --resume --report sandbox/tts-output/runs/stt-sensevoice-v2-quality.json
.venv/bin/python scripts/validate_speech.py sensevoice-release --quality-report sandbox/tts-output/runs/stt-sensevoice-v2-quality.json --report sandbox/tts-output/runs/stt-sensevoice-v2-release.json --resume
.venv/bin/python scripts/validate_speech.py stt-isolated-load --scenario lifecycle --policy current --cycles 3 --report sandbox/tts-output/runs/stt-sensevoice-v2-heap-attribution.json
```

编排按策略交替顺序串行执行三组配对、同负载单实例、两策略各五轮生命周期，最后 CI、SenseVoice 范围真实回归与完整回归。完成记录仅在产品源代码、依赖、模型、实际配置和关键测量工具指纹相符时复用；失败记录归档保留。堆分配位置补充记录在 `stt-sensevoice-v2-heap-attribution*.json`，对象类型计数在 `stt-sensevoice-v2-heap-object-attribution.json`。跟踪快照和类型差异用于定位 Python 保留对象，不会覆盖或改变产品行为。

## 配置建议

保持生产十五秒上限、全部切点一秒重叠及现有长度、超时、实例和并发设置。候选虽通过分组质量汇总与同模型负载门槛，但跨模型和 TTS 延迟门槛失败，逐样本也有退化；不推荐晋升。真实人工审核仍为 `pending`，不把“假设通过”记录成真实验收。原有双实例和并发默认值保留；单实例对照未达延迟门槛。
