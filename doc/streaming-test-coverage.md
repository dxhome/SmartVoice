# 流式处理测试覆盖与剩余验收

更新：2026-10-09。本次补齐 benchmark 语音成功判定、严格护栏和失败终态 profiling；没有改模型或推理策略。既有测试运行结果是此前记录，本轮没有重跑测试。

## 自动测试入口

在项目虚拟环境、仓库根目录执行：

```sh
# 单独运行流式契约、稳定性与边界测试
python -m unittest tests.test_streaming tests.test_streaming_resilience tests.test_streaming_documentation
# 完整产品 CI；包含上述测试
python scripts/test.py ci
# 既有 benchmark 基础单元测试
python -m unittest discover -s benchmarks/tests -t .
```

CI 不需要安装模型资产或下载语料。文档客户端的 3 个执行测试需要 `streaming` extra 中的 `websockets`；未安装时明确跳过，其余测试照常运行。需要项目测试依赖；真实原生进程测试仅执行受控 Python 函数。`tests/test_streaming_resilience.py` 使用模拟 ASR/翻译/TTS，保留真实产品会话、编排、队列和事件逻辑。不能据此接受识别质量、音质、P90 延迟或并发容量。

## 覆盖矩阵

| 范围 | 自动覆盖 | 证据边界 |
|---|---|---|
| 六条链路 | 中/英文三种模式，多段源终稿、译文终稿、语音块及来源关联 | 模拟阶段，真实产品编排；不是实际模型输出 |
| 字幕修订 | 草稿修订撤回，旧修订失效，迟到草稿与重复 final 被抑制 | 直接调用编排器；未验证浏览器 DOM 修订 |
| 语义边界 | forced-length 后 dangling 英文从句等待并与下一段合并，来源音频区间保留；中文条件句/英文词边界 | 固定文本规则；不是 en_09_clean 音频回放 |
| 声音完成状态 | no_speech、质量跳过→no_audio、invalid PCM→error；incomplete/quality_issues 跳过可观测且不进入 TTS 队列 | 模拟静音识别及合成结果，不证明模型不会漏段 |
| 取消 | task 开始前、ASR/翻译/标点/TTS 初始化与执行期间、等待播放完成期间；释放后复开 | 阶段操作采用确定性暂停点 |
| 原生进程 | 正常完成关闭、挂死超时终止、执行中的取消后终止、退出确认 | 真实独占子进程，无模型推理 |
| 阶段错误 | ASR/标点/翻译/TTS 执行失败，单个终止 error、资源释放；初始化失败 | 注入结构化阶段错误 |
| 断线与关服 | ASGI WebSocket close/disconnect 后清理并多次复开；取消所有活动会话并拒绝新会话 | TestClient 路由层，不是 TCP 断网或进程强杀 |
| 输入与缓冲 | PCM 空/奇数字节/超大帧、输入队列满、逻辑时长上限、source-final 队列满、TTS 待处理字符上限 | 上限与拒绝行为，不是满负载吞吐验收 |
| 慢消费者 | ACK 窗口阻塞/释放/超时、音频积压超时、播放 started/played 顺序与重复 ACK、delivery 释放 | 超时使用局部假时钟；不改变 asyncio 全局时钟 |
| 准入与资源 | 会话上限、估算内存上限、缺失模型在创建阶段前拒绝、模型租约防卸载、清理失败隔离容量 | 内存为准入估算，不验证 RSS 硬限制 |
| REST 共存 | 同步 REST 推理许可在流式占用时等待，释放后继续，无许可泄漏 | 共享调度单元测试，不是混合真实 HTTP 负载 |
| 持续输入 | 双向语音各 140 个 1 秒逻辑音频块，超过 128 单元历史容量；序号、文本范围、排空和关闭检查 | 非 1x 回放、无模型，不能替代长音频真实回归 |
| 用户接口文档 | 直接执行文档 Python 客户端，六条链路配置、PCM 分帧/finish/ACK、WAV 保存、格式拒绝及防覆盖；安装命令 ID 对照产品目录 | 模拟 WebSocket 协议端，不是服务端或真实网络回放 |
| 测量基础 | 最近秩分位数、CPU/RSS 单位、无音频进入成功率分母；输出音频校验 WAV PCM16/mono/采样率/时长、payload、连续序号；完整成功要求 complete 终态且无跳过 | 循环回放 runner；不能代表浏览器真实播放 |

主要代码位置：[test_streaming.py](../tests/test_streaming.py)、[test_streaming_resilience.py](../tests/test_streaming_resilience.py)。[test_streaming_documentation.py](../tests/test_streaming_documentation.py) 新增 4 个文档测试，稳定性模块新增 20 个测试方法；子场景在 `subTest` 中分别执行。原模块保留 17 个测试方法，并将初始化失败测试改为准确名称，断线覆盖由新模块承担。

## 本轮验证结果

2026-10-09，在本地项目虚拟环境和隔离的 `SMARTVOICE_HOME` 下执行：

- 完整 CI：275 个测试通过。
- 流式专项：41 个测试通过（既有 17 个、稳定性 20 个、文档 4 个）；本环境已安装 `websockets`，文档客户端测试未跳过。
- benchmark 基础单元测试：8 个测试通过。
- 文档本地链接检查与 `git diff --check` 通过。

上述结果证明所列自动契约和故障场景通过，不构成真实模型质量、P90 延迟、长录音播放或并发容量验收。

## 本轮暂缓

1. S5 人工复核当前产品 22 个高风险 clean 样本；工作包已生成，空白评级保持 pending。风险提示见 `benchmarks/streaming/reviews/current-product-quality-review-triage-v1.md`。
2. 完成 6 条人声 onset spot-check，填入 reviewer 和 human onset sample；当前 VAD 起点不能作为 TTFO 正式验收。
3. 实际浏览器连续播放、字幕 DOM、慢播放/断网/后台调度、真实 TCP 断线/进程退出、重复运行资源稳定性及多平台。
4. 完整固定到达率容量、进程树 CPU/RSS/PSS、有效 sessions/core 及性能优化。
5. 可选协议字段的完整组合、适配器替换与所有安装资产异常的专项回归仍可继续扩展；本轮覆盖不等同于所有路径穷举。

## P1 验证工具进展（2026-10-09）

- 新增 `scripts/validate_speech.py streaming-capacity`：固定到达率的 open-loop WebSocket 会话调度，按 1x 回放 PCM；报告 offered rate、重叠会话峰值、失败率、输出成功率、首输出延迟、CPU core-equivalent、RSS、每物理核重叠数和每观测 CPU 核等效会话数。每个方向/模式独立运行，并需三轮重复。
- 新增 `scripts/validate_speech.py streaming-stability`：重复会话、拼接固定音频形成的长输入、可选进程树资源采样，以及只对 runner 自己启动的服务进程做终止/退出和残留子进程检查。
- 新增 [真实浏览器 QA 清单](../benchmarks/streaming/browser-qa-v1.md)：覆盖六条路由、DOM 草稿/定稿替换、连续音频序号及播放完整性、慢网、断线、后台调度和重复运行。
- 低负载本地 smoke：固定到达率工具发送 1 个 zh 原文字幕会话，终态 complete、功能失败 0；资源采样版也完成，但当前 macOS 沙箱禁止枚举子进程，报告正确标记 `process_tree_complete=false`，仅采样 API 根 PID，不能当完整进程树容量结果。长音频 soak 使用 15 秒重复输入、连续两次会话，两次均 complete、失败 0。
- 小型到达率探针：当前服务广播 `max_sessions=1`。zh 原文字幕单轮 0.5 session/s、16 秒共发起 8 个会话；2 complete、6 `session_overload`，失败率 75%，两条成功会话首 partial P90 1.88s；client attempts 峰值 2，成功会话重叠峰值 1。API 根 PID 的 CPU P90/P95 为 0.059/0.065 core，峰值 RSS 40.5 MB；受沙箱限制，不包含可能的子进程。该点位超过单会话 admission 配置且只有 8 条样本，只用于验证过载处理/测量脚本，不是容量结论。
- 该轮两个成功会话各运行约 9.5–9.9 秒，实际完成速率约 0.103 sessions/s；只是在 0.5/s offered load 下观察到的吞吐，不能直接外推持续容量或 per-core capacity。0.1/s 的单会话 smoke 仅为 1/1 complete，也不足以证明 99% 成功率。
- `max_sessions=2/4/8/16` 隔离服务矩阵：每个配置使用独立本地服务，10 秒窗口内按配置并发数发起请求，固定 `streaming_memory_mib=2800`、`compute_slots=2`，路由分别测试原文字幕（中/英）、译文字幕（中→英/英→中）和中→英译文语音。服务实例均正常退出。原文字幕在 max=2/4 时分别 2/2、4/4 成功；max=8/16 时仍只各有 4 个成功，其他请求因内存准入拒绝（另有 max=16 的 1 个中文、2 个英文输入队列过载）。成功会话的 source final 均与冻结基线严格一致。译文字幕中→英在四档均为 2 个成功，英→中均为 1 个成功，其余均被内存准入拒绝；成功会话 target final 均与冻结基线一致。对应成功样本的 partial P90：原文中/英约 1.80–2.59 秒，译文中→英约 2.57 秒、英→中约 2.74–2.89 秒；每点只有 1–4 个成功样本，不能作为 P90 验收结论，且当前观测高于既定 `<1s`/`<2s` 目标。
- 矩阵语音只测试中→英：每档均 1 个会话成功，分别有 1/3/7/15 个请求因内存准入被拒绝；成功会话音频完整且块序/文本与冻结基线一致。first-PCM 到首个音频段 TTFO 约 7.11–7.13 秒（每档 n=1），未达到 `<3s` 目标，也不是经过人工 onset 修正的正式值。英→中语音的估算为 3800 MiB/会话（ASR、格式化、M2M100、Matcha 及其 Kokoro 混合脚本回退），超过本轮 2800 MiB 预算，因此当时未测到有效推理。全矩阵使用的请求量和每档样本数只适合发现准入/过载行为，不构成统计性容量或质量验收。
- `max_sessions` 是会话数上限之一，不代表该数量一定能被接纳。按当前保守内存估算，2800 MiB 预算下原文字幕最多约 4 路、中→英译文字幕最多约 2 路、英→中译文字幕最多约 1 路、中→英语音最多约 1 路；因此 8/16 配置主要验证了拒绝路径，没有验证 8/16 路同时推理。采样器受 macOS 沙箱限制，`process_tree_complete=false`，CPU/RSS 仅代表 API 根 PID，不能用于完整服务资源或每核容量结论。详细逐样本报告位于本机 ignored 目录 `.smartvoice-dev/streaming-product/max-session-matrix-*.json`，成功会话质量复核汇总为 `max-session-matrix-rechecked-2026-10-09.json`。
- 默认并发上限过载矩阵：扩展 runner 支持固定 `max_sessions=2`，在 2 秒到达窗口内分别按 1×/2×/4×/8× 上限发请求，每点独立重复 3 轮；覆盖中文/英文原文、双向译文字幕和双向译文语音，共 540 个尝试。报告分别列出 offered attempts、内存准入、成功重叠、拒绝原因、首输出 P50/P90/P95、严格 final/audio 护栏以及资源采样范围。1× 时源语两路均 6/6 成功；中→英译文字幕 6/6；英→中译文字幕因 1800 MiB/会话只成功 3/6；中→英语音因 2350 MiB/会话只成功 3/6；英→中语音因 3800 MiB/会话超过 2800 MiB 预算，0/6。2×/4×/8× 下，源语字幕和中→英译文字幕分别为 6/12、6/24、6/48，超出会话上限的请求均为 `session_overload`；英→中译文字幕和中→英语音分别各成功 3/12、3/24、3/48，其余均因 `memory_admission` 被拒绝；英→中语音所有 12/24/48 个请求均为内存拒绝。所有实际成功且有基线比较的会话严格匹配冻结 final；没有成功输出的英→中语音不计作通过质量护栏。
- 英→中语音内存复测（2026-10-10）：确认链路估算为 3800 MiB/会话（ASR 300 + 格式化 400 + M2M100 1100 + Matcha 1200 + Kokoro 回退 800）。产品默认 `streaming_memory_mib=4096` 因而可接纳 1 路、不能接纳 2 路；默认预算下 3 轮共 6 次尝试，3/6 complete、其余 3 次 `memory_admission`，峰值成功重叠 1，成功会话音频 3/3 完整。将隔离测试的 `streaming_memory_mib` 提至 8000（足以容纳两路各 3800 MiB 估算），保持 `max_sessions=2`，2 秒窗口按 1×及 2×上限负载各重复 3 轮，共 18 次尝试。1×为 6/6 complete，成功会话重叠峰值 2；2×为 6/12 complete，另 6 次因 `session_overload` 被拒绝，成功重叠峰值仍为 2。全部 12 个成功会话音频完整、无跳过，final/audio 护栏与冻结基线一致。first PCM 到首个音频段 P90 为 1× 6.289 秒（n=6）、2× 6.295 秒（n=6）；这不是人工 onset TTFO，且远高于 `<3s` 目标。8000 MiB 测试的系统级可用内存最低 2621 MiB，高于 1536 MiB 中止线；隔离服务正常退出。根进程 CPU P95 最大约 0.103 core、RSS 最大约 90.3 MiB，但 macOS 不允许完整进程树采样。逐轮矩阵和内存监控报告在 `.smartvoice-dev/streaming-product/capacity-en-zh-{default-memory,open-memory}-r3-2026-10-10.json` 及对应 `*-watch-2026-10-10.json`。这证明当前环境在安全余量保护下可承载两路英→中语音，不代表更高 max_sessions 或长期容量。
- 全场景解除应用资源准入后的并发复测（2026-10-10）：对原文字幕中/英、译文字幕中→英/英→中、译文语音中→英/英→中分别运行 1× 与 2× 到达负载，每点三轮，每种语言使用一个固定 clean 样本；配置 `max_sessions=2`、`streaming_memory_mib=16000`、`compute_slots=8`。1× 下六条路由均为 6/6 成功且成功重叠峰值为 2；2× 下各路由均为 6/12 成功，另外 6 次均由 `session_overload` 拒绝，成功重叠峰值仍为 2。所有成功会话严格 final/audio 护栏通过；双向语音各 12/12 成功会话出音频且完整。source partial P90：英文 1.769 秒、中文 1.887 秒；target partial P90：中→英 2.557 秒、英→中 2.934 秒；首音频段延迟 P90：中→英 7.277 秒、英→中 6.448 秒（从首 PCM 起算，非人工 onset TTFO）。各模式系统可用内存最低分别为 3100、2661、2835 MiB，均高于 1536 MiB 安全中止线，服务均正常退出。这里“解除资源限制”指放宽应用内存准入和 compute slot 配额；宿主机内存安全监控仍保留，`max_sessions=2` 仍是硬上限，因此本轮证明的是每条链路可稳定接纳两路，而超额请求被正确背压/拒绝，并未测出 4 路以上容量。每点仅 6 个成功延迟样本，不足以作为正式 P90 验收；根进程 CPU/RSS 受沙箱采样限制，不能解释为完整进程资源。汇总：[allroutes report](../.smartvoice-dev/streaming-product/capacity-allroutes-unrestricted-summary-2026-10-10.json)；各模式逐轮报告及系统内存监控均在同目录。
- 每点只有 3–6 个成功会话，不能作为 P90 验收样本。1×时 source partial P90 为中文 1.885 秒、英文 1.767 秒；target partial P90 为中→英 2.609 秒、英→中 2.972 秒；中→英语音首个音频段 P90 为 7.146 秒（first PCM 起算，不是人工 onset TTFO），均未达到目标或不具备正式判定条件。所有配置服务均正常退出。根进程 CPU P95 最大约 0.149 core、RSS 最大约 87 MiB，但进程树枚举受限，不能解读为完整服务资源数据。固定上限压力测试逐样本和汇总报告位于 `.smartvoice-dev/streaming-product/capacity-default2-{source,translation,speech}-r3-2026-10-09.json` 与 `capacity-default2-overload-summary-2026-10-09.json`。
- 长输入重复 soak：15 秒重复 PCM，两轮各有 source partial 约 1.87–1.88 秒、source final 约 6.65 秒，会话 wall time 16.64 秒和 17.26 秒；2/2 complete，0 个功能失败、音频完整性错误或跳过项。API 根 PID CPU P90/P95 为 0.081/0.088 core，RSS 起始 34.3 MB、结束 32.0 MB、峰值 38.7 MB；进程枚举受限，仍不包含子进程。RSS 在这次短 run 中没有增长，但两次重复不足以判断长期泄漏或内存稳定性。
- 未执行生产容量爬坡、浏览器慢网/后台/物理播放、长时间 soak 和自管服务退出验证。当前没有 Playwright/Selenium，本地 in-app browser 对 loopback URL 返回 `ERR_BLOCKED_BY_CLIENT`；所以浏览器 QA 需在可访问 sandbox 的真实桌面浏览器完成。共享服务未被终止；进程退出 probe 需用隔离端口启动由 runner 自己拥有的服务实例。
- 这些 smoke 只证明工具与协议基本可运行，不代表容量、资源、浏览器体验或稳定性达到验收门槛。完整命令和解释见 [benchmark README](../benchmarks/streaming/README.md)。

## P0 产品基线（2026-10-09）

使用修正后的产品 runner 对 10 个中文、10 个英文 clean 样本跑了三轮，共 180 个会话（六链路各 30）。完整逐事件报告在本机 ignored 路径 `.smartvoice-dev/streaming-product/p0-baseline-2026-10-09.json`；便携的 FLEURS 音频/标注和 60 条首轮输出基线已提交到 benchmark 目录。六链路轮间严格比较均通过；180 个会话无协议失败，收到的 WAV 无完整性错误。

| 场景 | first PCM 到首输出 P90 | 起点修正 P90 | 质量/完整性观察 |
|---|---:|---:|---|
| 中文原文字幕 | 3.08s | 0.89s（VAD） | CER 9.62% |
| 英文原文字幕 | 2.09s | 0.92s（VAD） | WER 17.19% |
| 中→英译文字幕 | 3.73s | 1.58s（VAD） | CER 9.62% |
| 英→中译文字幕 | 2.97s | 1.91s（VAD） | WER 17.19% |
| 中→英译文语音 | 11.79s | 10.56s（VAD） | 有音频 30/30；完整音频 27/30；`incomplete` 跳过 3 次 |
| 英→中译文语音 | 10.15s | 8.98s（VAD） | 有音频及完整音频均 30/30 |

起点修正列全部是 VAD provisional，不是人工 onset；TTFO 尚不能按人工语音起点做正式验收。source/target 字幕指标按 first PCM 起算时未达 `<1s` / `<2s`；即便采用 provisional 起点修正，也仅作诊断。语音 TTFO 距 `<3s` 目标仍很远。WER/CER 是参考转写误差，不等同于译文语义或 TTS 音质评审。三轮稳定只证明本批固定 clean 样本的输出一致性，不表示通过人工质量门槛。

本轮将 20 条 FLEURS clean 语音、VAD onset 文件和 60 条首轮机械输出基线迁入 `benchmarks/streaming/data`、`baselines`。音频许可和来源见 `data/fleurs/NOTICE.md`。11 条历史流式评审和 8 条 TTS A/B 评审已复制到 `reviews/`，原评级不改写；quality acceptance 仍未批准。6 条 onset 人工 spot-check 清单也已准备，但人工时间点尚空缺。

修正后的 portable asset smoke 完成六条链路各 1 个中/英文样本：严格输出护栏通过，6/6 终态 complete，0 个 WAV 完整性错误。该 smoke 的样本量不足以判定 P90。

`acceptance.json` 固定了延迟目标，将 pinned clean 语音完整播报设为 100% 回归门槛，并为生产环境确定了每个语言方向至少 300 个 eligible speech sessions、full-audio success 不低于 99% 的初始门槛。无音频、无效音频、不完整终态和跳过块计为失败。新报告会分别输出 per-route latency target status 和 clean-fixture audio gate，样本不足、需要人工 onset、未达目标会显式标记。生产门槛需要结合上线遥测复核。

90-case 质量 corpus（48 条新确认 +42 条既有中文变体/控制）现已连同历史真实模型记录、22 个 pending review case 打包。用产品代码跑完了单轮 ASR 和译文字幕回放，共 180 个会话：0 协议/功能失败，静音控制 4/4 按预期返回 no_speech。清晰/变体源语 CER/WER 按方向、variant 和 partition 分组写入版本化摘要 [product-quality-90-2026-10-09-summary.json](../benchmarks/streaming/reports/product-quality-90-2026-10-09-summary.json)；逐会话报告留在本机 `.smartvoice-dev/streaming-product/quality90-product-run-2026-10-09.json`。译文语义没有自动打分。当前报告中的性能样本数不足 P90 门槛，不作为延迟验收。

本轮源语转写诊断（只作方向判断，非人工质量验收）：中文 CER 19.03%（非空参考 72 条；clean 16.61%、quiet 17.65%、10 dB noise 21.45%、room-proxy 20.42%）；英文 WER 20.57%（16 条；clean 13.92%、quiet 17.72%、10 dB noise 34.18%、room-proxy 16.46%）。Clean-only 首输出样本为中文 18、英文 4、译文 partial 分别 18/4，低于每路至少 30 次的性能判定下限；不要把这轮质量语料的 latency 标签当作目标通过。

已有手工浏览器及 12 会话模型 smoke 见[迁移记录](streaming-migration.md)。正式指标为 source partial P90<1s、target partial P90<2s、双向语音 onset TTFO P90<3s；本轮未重测这些指标。

## 2026-10-10：中→英终端尾句修复与 300 会话回放

- 根因：`zh_05_clean` 的 ASR 在句尾输出“尽管……并不严”，文本整理器据此标记 `incomplete=true`。该译文是明确提交的输入结束尾句；播报层仍按与中途 forced-length 片段相同的策略跳过，导致 P0 30 次回放中 3 次不完整。源文和译文质量护栏均未发现文本差异。
- 修复：仅对 `reason=finish` 的已提交终端尾句允许播报，并在每个对应 `audio_segment` 标注 `source_unit_incomplete=true`。会话中途的 incomplete/forced-length 单元仍跳过，翻译质量问题仍跳过。新增回归覆盖终端尾句放行、forced-length 拒绝和质量问题拒绝。
- 固定 clean 集复验：`zh_05_clean` 从 `partial + audio_skipped(incomplete)` 变为 `complete`；源 final 与 target final 和旧快照严格一致。仅音频终态/内容预期改变，旧基线保留，新建 [机械修复快照](../benchmarks/streaming/baselines/product-p0-speech-terminal-tail-repair-2026-10-10-v1.json)。20 条双向 clean smoke 全部完整出音频，strict baseline guard 通过。
- 300 会话/方向回放：10 条中/英文 clean FLEURS 录音各重复 30 轮，两路并发，合计 600 个产品 WebSocket 会话。中→英 300/300、英→中 300/300 完整音频；协议失败、非 complete 终态、跳过段和 WAV 完整性错误均为 0。两条路由的 99%/300-session **回放数值门槛均通过**，600 条 strict baseline guard 全部通过。便携汇总见 [speech audio gate replay summary](../benchmarks/streaming/reports/speech-audio-gate-replay-2026-10-10-summary.json)；逐会话原始报告在本机 ignored 路径 `.smartvoice-dev/streaming-product/speech-production-gate-replay-2026-10-10.json`。
- 范围限制：这批数据是 10 个固定录音的重复本地回放，不是 300 个独立说话人，也不是生产遥测；因此验证了测量口径和当前产品路径，不构成真实生产成功率达标证明。接入生产事件/遥测后仍需按每个方向累计 300 个合格会话重算该门槛。

## 2026-10-10：并发爬坡、真实浏览器与稳定性补测

本轮只新增隔离服务验证与本地记录，没有改产品实现。所有容量场景均使用 pinned clean FLEURS 样本，每档三轮；延迟统计包含成功会话，失败仍保留在成功率分母。应用内存准入在标注 unrestricted 的场景中设为 1,000,000 MiB，compute slots 设为 1,000；主机可用内存低于约 2 GiB 时由安全监控中止，因此这不是无限主机资源测试。

### 容量爬坡结果

| 链路/配置 | offered / 成功 | 峰值有效重叠 | 首输出 P90 | 质量/音频结果 |
|---|---:|---:|---:|---|
| 原文字幕，max=4，预算 16 GiB/8 slots | 中英各 12/12 | 4 | 中 1.88s，英 1.77s | strict final 护栏通过 |
| 原文字幕，max=8，预算 16 GiB/8 slots | 中英各 24/24 | 8 | 中 2.35s，英 1.77s | strict final 护栏通过 |
| 原文字幕，max=16，预算 16 GiB/8 slots | 中英各 27/48 | 10 | 中 2.32s，英 2.44s | 其余均为 input_overload；成功项护栏通过 |
| 译文字幕，unrestricted max=4 | 中英各 12/12 | 4 | 中→英 2.55s，英→中 2.98s | strict final 护栏通过 |
| 译文字幕，unrestricted max=8 | 中英各 24/24 | 8 | 中→英 2.59s，英→中 6.37s | strict final 护栏通过；英→中延迟明显退化 |
| 译文语音，unrestricted max=4，8 slots | 中英各 12/12 | 4 | 中→英 7.14s，英→中 6.33s | 两方向均 12/12 有完整音频；strict guard 通过 |
| 译文语音，unrestricted max=8，8 slots | 中→英 21/24；英→中 4/24 | 中 8，英 3 | 中 8.72s；英 7.42s | 中→英 3 次、英→中 20 次 input_overload；成功项护栏通过 |

译文字幕 max=16 使用了 16 GiB/8 slots 的早期设置，不属于 unrestricted 结果：英→中成功 6/48（18 input_overload、24 memory_admission），中→英 28/48（11 input_overload、9 memory_admission）。之后 unlimited application memory 的 max=16 长窗口尝试触及主机安全线（可用内存约 1.98 GiB）后被停止，没有形成有效报告。译文语音 max=16 同样因主机安全线停止（约 2.01 GiB）；没有可报告的 max=16 语音容量。

因此，目前可证明 source subtitles 在配置 max=8、译文字幕在 unrestricted max=8 下可以让所有 offered 请求完成；译文语音只有 max=4 达到 100% 完整音频，max=8 已出现过载。它们都不能推导为生产容量上限：每方向样本数少，source/translation 延迟分布未满足产品目标，翻译语音 TTFO 仍远高于 3 秒。macOS 采样器无法枚举子进程，报告中的 CPU/RSS 只代表 API 根进程，不能换算为进程树总资源或可信 sessions/core。

并发原始报告保存在本机 ignored 目录 `.smartvoice-dev/streaming-product/`，文件前缀为 `capacity-ramp-{source,translation,speech}-l{4,8,16}-2026-10-10`；另有 `capacity-ramp-translation-unrestricted-l{4,8}-2026-10-10.json`。受安全阈值中止的 max=16 运行不应引用为有效容量结果。

### 浏览器验证

通过本地 Chrome 访问隔离服务 `http://127.0.0.1:9160/console/streaming`，逐项上传固定 WAV 并检查输出：

| 浏览器场景 | 结果 | 浏览器端观察 |
|---|---|---|
| 中文原文字幕 | 完成 | source partial 约 1.86 秒，随后显示终稿 |
| 英文原文字幕 | 完成 | `en_02_clean` 有 source partial 和终稿，partial 约 1.76 秒 |
| 中→英译文字幕 | 完成 | `zh_02_clean` source partial 约 1.87 秒、target partial 约 2.66 秒，显示双语结果 |
| 英→中译文字幕 | 完成 | `en_02_clean` source partial 约 1.76 秒、target partial 约 2.88 秒，显示双语结果 |
| 中→英语音 | 完成 | `zh_02_clean` TTFO 约 7.14 秒；页面报告播放队列已排空 |
| 英→中语音 | 完成 | `en_02_clean` TTFO 约 6.29 秒；页面报告播放队列已排空 |

每次场景切换后重新加载页面并开始新会话，避免复用上一次会话的动态指标。以上验证了浏览器上传、字幕渲染、音频传输和播放队列排空；页面状态不代表物理扬声器的实际可听性，也没有验证人耳听感。

慢网节流、真实 TCP 断线、后台标签调度、字幕 partial 撤回/修订的逐事件 DOM 检查，以及物理扬声器可听性仍未验证。浏览器传输和 ACK/benchmark 播放完整性不能替代耳机/扬声器听感验收。

### 长音频、重复会话与进程清理

- 中→英语音：60 秒重复 zh PCM 单会话 complete；TTFO 7.14 秒，full audio 1/1，音频完整性错误 0，跳过块 0。API 根 PID RSS 起始约 84.7 MiB、结束约 78.8 MiB，差值约 -5.9 MiB。
- 英→中语音：60 秒重复 en PCM 单会话 complete；TTFO 6.27 秒，full audio 1/1，错误 0，跳过块 0；会话 wall time 63.70 秒。根 PID RSS 差值约 -25.1 MiB。
- 中→英语音重复会话：10 次连续会话 10/10 complete、完整音频 10/10、错误/跳过均为 0；session wall P90 10.71 秒、首音频 P90 7.14 秒。根 PID RSS 起始约 84.7 MiB、结束约 75.4 MiB，未见本轮累计增长。
- 英→中语音重复会话：10 次连续会话 10/10 complete、完整音频 10/10、错误/跳过均为 0；session wall P90 14.39 秒、首音频 P90 6.27 秒。根 PID RSS 起始约 86.3 MiB、结束约 48.5 MiB，未见本轮累计增长。
- 双向持续压力：中→英与英→中各 1 个 60 秒重复 PCM 会话同时以 1x 输入。两路均 complete，完整音频各 1/1；分别产生 9、12 个音频段，顺序/格式/时长完整性错误为 0，跳过块为 0；TTFO 分别约 7.11、6.26 秒。整轮约 65.0 秒；主机可用内存从约 4,451 MiB 降至最低约 2,516 MiB，未触发 2.2 GiB 测试中止线。该自定义运行采样了主机可用内存，但没有完整的 API 子进程树 CPU/RSS 指标。
- 双向 5 分钟持续压力：中→英与英→中各 1 个 300 秒重复 PCM 会话同时以 1x 输入。第一次按默认 `streaming_lifetime_seconds=300` 运行，两路均因 `session_timeout` 未能正常结束；这是输入时长与会话生命周期相等导致的边界问题，不计为通过。随后仅在隔离测试服务把生命周期设为 420 秒重跑：两路均 complete，完整音频各 1/1；分别产生 39、59 个音频段，音频完整性错误 0、跳过块 0；TTFO 分别约 7.11、6.34 秒。运行约 305.2 秒，主机可用内存从约 4,802 MiB 降至最低约 2,912 MiB，未触发 2.2 GiB 安全中止线。API 根进程 RSS 起始约 84.8 MiB、结束约 43.1 MiB；根 PID CPU P95 约 0.102 core-equivalents。生命周期 420 秒只用于隔离验证，不代表产品默认值或产品配置已改变。
- 三个由稳定性 runner 自己启动的测试服务均在测试后被终止并退出；等待 3 秒后未发现残留子进程。这里 `graceful_exit=true` 表示进程在 SIGTERM 后于超时内退出；退出码为 -15 反映进程收到 SIGTERM，不代表异常崩溃。

长音频是固定 FLEURS 语音重复拼接，适合检测流长、排空、音频顺序/完整性和清理，不是自然长篇讲话的语言质量验证。双向同时持续输入已完成 5 分钟各 1 个会话，但尚非多小时或生产到达率 soak。这轮旧实现的默认会话生命周期为 300 秒，测试当时需预留初始化和排空余量；下面的共享模型改造已把新默认值改为 0（不设墙钟上限）。资源采样受 macOS 权限限制，无法得到 API 完整子进程树 CPU/RSS；退出清理中的残留进程观察只覆盖 runner 可枚举的 PID。

稳定性机器报告：`stability-speech-zh-60s-2026-10-10.json`、`stability-speech-en-60s-2026-10-10.json`、`stability-speech-zh-repeat10-2026-10-10.json`、`stability-speech-en-repeat10-2026-10-10.json`、`duplex-speech-zh-en-simultaneous-60s-2026-10-10.json` 和 `duplex-speech-zh-en-simultaneous-300s-lifetime420-2026-10-10.json`，均位于本机 ignored 目录 `.smartvoice-dev/streaming-product/`。

## 共享模型与长会话改造验证（2026-10-10）

### 标准回归入口新增覆盖

`scripts/test.py full` 包含六条真实模型的 3600 秒音频加速输入案例。
`full --streaming-only` 可独立运行流式契约、故障与这六条长音频案例；
缺少默认链路模型（含中文回退）、依赖或固定语料时预检失败。
长音频按有界窗口输入真实产品会话，保留原生在线 ASR、文本整理、MT、TTS
及其正常队列/计算限制，增量校验事件顺序、音频文本区间、源引用、播放载荷、
输入/输出排空与 worker/PID/模型租约回收。测试不保存整个小时 PCM 或所有输出事件。
真实 WebSocket、浏览器播放、自然长篇质量与一小时墙钟 soak 的范围仍独立。

新增快测还覆盖运行中取消后的同模型句柄存活、排队时 worker 失效传播、
另一个模型的故障隔离、并发初始化复用，以及长音频生成器与输出检查器的反例。
同一句柄并发关闭及关闭等待者取消也有回归：清理只执行一次，模型引用只释放一次。

### 一小时真实模型加速回归结果

2026-10-10，`full --streaming-only` 的 69 项测试全部通过，运行约 32.6 分钟；
最新 CI 305 项通过。六条链路各处理 3600 秒固定 FLEURS PCM 循环输入，
没有替换模型、模拟推理或修改产品时钟。

| 链路 | 音频时长 | 实际运行秒数 | 音频处理倍速 | 音频块数 |
| --- | ---: | ---: | ---: | ---: |
| 中文原文字幕 | 3600s | 155.8 | 23.11× | — |
| 英文原文字幕 | 3600s | 165.8 | 21.71× | — |
| 中→英译文字幕 | 3600s | 174.2 | 20.67× | — |
| 英→中译文字幕 | 3600s | 329.9 | 10.91× | — |
| 中→英语音 | 3600s | 599.1 | 6.01× | 906 |
| 英→中语音 | 3600s | 488.2 | 7.37× | 789 |

六条链路均正常 complete，处理到最后一个输入采样；语音零跳过，音频块顺序、
载荷、译文区间与源引用检查通过。输入窗口峰值 2 帧，保留文本单元峰值 128，
上下文峰值 269 字符/20 词条；各链路关闭后模型池及其已知原生进程均回收。
这些是状态大小与进程退出证据，不是完整进程树 RSS/CPU 测量。

加速输入可能把译文草稿合并为直接输出的定稿，因此持续输出断言接受译文草稿或定稿。
英→中字幕另用最新代码补跑一小时音频：337.3 秒通过，首次译文定稿约 2.78 秒，
结束输入前已发布 517 个译文定稿，明确验证了边输入边输出，关闭后无 worker 残留。
其余五条首轮链路的首输出时刻均早于未送完音频时的进度记录。
该测试通过产品会话服务运行，使用传输 ACK；不会证明 WebSocket 慢网、
浏览器物理播放、自然会议语料质量、实时延迟 SLA、并发容量或一小时墙钟 soak。
机器摘要：[一小时加速回归报告](../benchmarks/streaming/reports/full-hour-regression-v1.json)。

- 自动测试：流式契约、韧性、共享模型池、文档客户端、benchmark、配置共 65 项通过。新增真实 spawned-process 池测试覆盖模型复用、独立计数状态、取消等待、队列超限、初始化失败重试、进程崩溃、挂起调用和空闲驱逐。双向各 3600 个一秒模拟音频块验证有界历史和完整排空，不能等同于一小时真实模型运行。
- 最新代码、计算许可 2 的六链路小样本回放均 complete；与既有 P0 基线的最终原文、最终译文、音频块及跳过记录严格一致。每链路只有一个案例，不据此判断 P90 或完整质量验收。
- 真实模型双向同时输入各 300 秒重复 FLEURS 音频，304.05 秒内排空；中→英 39 块、英→中 59 块，均 complete，零跳过、零载荷/顺序错误。期间额外两条英文原文会话完成，活跃会话峰值 4，模型进程仍为 7。合计驻留 **估算** 5750MiB，测试使用显式放宽预算，不能作为默认 4096MiB 下任意双向混合链路都可准入的证据。
- 回归结束且超过空闲回收期限后，pool 的模型数、会话句柄、驻留估算、执行/等待任务均归零；测试服务正常关闭。完整进程树 RSS/CPU 和物理可听性仍未据此证明。
- 摘要：[共享模型验证报告](../benchmarks/streaming/reports/shared-model-pool-validation-v1.json)。设计与限制：[长会话/共享 worker](streaming-shared-workers.md)。8/16 路新架构容量、小时级真实 soak、后台调度和跨连接恢复需要后续证据。
