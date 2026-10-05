# STT 与语音验证档案

按验证主题保留阶段报告和可复现记录。项目级测试入口在 [`../../testing.md`](../../testing.md)，当前实施状态在 [`../../implementation-plan.md`](../../implementation-plan.md)。sandbox 中的原始 JSON、音频和日志按各报告所列路径保留；本目录集中保存结论、限制和复现方法。

## 历史与综合报告

- [`audio-optimization-validation.md`](audio-optimization-validation.md) — REST 音频格式、长文本/音频优化的 sandbox 到产品迁移。
- [`stt-completion-validation.md`](stt-completion-validation.md) — 长音频终止修复及早期完整 HTTP 回归。
- [`stt-language-cache-policy-validation.md`](stt-language-cache-policy-validation.md) — 分段语言语义、缓存复用、重叠合并与窗口候选。
- [`stt-interaction-alignment-validation.md`](stt-interaction-alignment-validation.md) — 时间戳合并、真实语音对照、混合负载与内存记录。
- [`stt-continuous-acceptance-validation.md`](stt-continuous-acceptance-validation.md) — 连续语音样本、质量基线与候选窗口结果。
- [`stt-admission-boundary-validation.md`](stt-admission-boundary-validation.md) — 长音频 API/过载、边界策略及已确认原失败 MP3 的复测。
- [`stt-sensevoice-boundary-diagnosis.md`](stt-sensevoice-boundary-diagnosis.md) — SenseVoice 边界因素诊断与人工试听材料说明。
- [`stt-sensevoice-v2-validation.md`](stt-sensevoice-v2-validation.md) — SenseVoice 新候选、固定到达率负载和生命周期/RSS 验收。
- [`whisper-chinese-decoding-fix.md`](whisper-chinese-decoding-fix.md) — Whisper 中文解码补丁、构建方式和质量限制。
