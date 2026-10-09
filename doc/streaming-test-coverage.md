# 流式处理测试覆盖与剩余验收

更新：2026-10-09。本次仅补文档和测试，产品实现、模型/策略、评测 runner 均未改变。

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
| 测量基础 | 最近秩分位数、CPU/RSS 单位、无音频进入成功率分母 | 已知 runner 漏判暂未修复，见下方 |

主要代码位置：[test_streaming.py](../tests/test_streaming.py)、[test_streaming_resilience.py](../tests/test_streaming_resilience.py)。[test_streaming_documentation.py](../tests/test_streaming_documentation.py) 新增 4 个文档测试，稳定性模块新增 20 个测试方法；子场景在 `subTest` 中分别执行。原模块保留 17 个测试方法，并将初始化失败测试改为准确名称，断线覆盖由新模块承担。

## 本轮验证结果

2026-10-09，在本地项目虚拟环境和隔离的 `SMARTVOICE_HOME` 下执行：

- 完整 CI：275 个测试通过。
- 流式专项：41 个测试通过（既有 17 个、稳定性 20 个、文档 4 个）；本环境已安装 `websockets`，文档客户端测试未跳过。
- benchmark 基础单元测试：8 个测试通过。
- 文档本地链接检查与 `git diff --check` 通过。

上述结果证明所列自动契约和故障场景通过，不构成真实模型质量、P90 延迟、长录音播放或并发容量验收。

## 本轮暂缓

1. 修复 benchmark 音频完整性护栏、取消状态成功率判定、失败 profiling；目前不能仅用 strict_quality_guard 或完整音频成功率判断通过。
2. 将评估框架、90 条质量语料、已有人工评审、起点标注及冻结基线独立迁入 benchmark 管理。
3. 全六链路真实模型三轮回放、低音量/噪声、高风险语义、en_08_clean/zh_05_clean/en_09_clean 与实际长录音。
4. 实际浏览器连续播放、字幕 DOM、慢播放/断网/后台调度、真实 TCP 断线/进程退出、重复运行资源稳定性及多平台。
5. 完整固定到达率容量、进程树 CPU/RSS/PSS、有效 sessions/core 及性能优化。
6. 可选协议字段的完整组合、适配器替换与所有安装资产异常的专项回归仍可继续扩展；本轮覆盖不等同于所有路径穷举。

已有手工浏览器及 12 会话模型 smoke 见[迁移记录](streaming-migration.md)。正式指标为 source partial P90<1s、target partial P90<2s、双向语音 onset TTFO P90<3s；本轮未重测这些指标。
