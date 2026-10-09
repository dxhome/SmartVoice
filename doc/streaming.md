# 流式处理预览：启用与接口

## 范围与启用

提供三种模式：`transcription`（原文字幕）、`translated_subtitles`（译文字幕）、`spoken_interpretation`（译文语音）。源语言必须指定 `zh` 或 `en`；翻译目标必须是另一个语言。默认禁用，当前验证平台为 macOS arm64。既有一次性接口保持原语义，在线 ASR 模型不能作为一次性 ASR 使用。

安装项目的 `streaming` 额外依赖，在配置中设置 `streaming_enabled: true`，或启动前设置 `SMARTVOICE_STREAMING_ENABLED=true`。使用正常 `smartvoice --port 8766` 入口；若直接启动 Uvicorn，配置 `--ws-max-size 32768 --ws-max-queue 8`。服务启动和会话创建不会下载模型。

中文在线 ASR 使用 Paraformer，英文使用 Zipformer English；标点使用 CT-Transformer。中→英使用 OPUS-MT，英→中使用 M2M100。英文语音为 Supertonic 3，中文为 Matcha；中文中的拉丁词读音需要声明的 Kokoro 回退资产。依赖、资产和语言方向须在收音前验证。

模型管理分为 STT、TTS、Streaming 三个同级分类。流式专用 ID 使用 `streaming-stt-*`（在线识别）、`streaming-mt-*`（翻译）、`streaming-ct-*`（文本标点）；共用 TTS 保留 `tts-*`，只安装一份。`models list --category streaming` 查看专用模型；REST 的 `/v1/models?category=streaming` 列出已验证安装资产，`/v1/catalog?category=streaming` 包含未安装模型。前者的 `runtime_check_required` 不等于链路可用，仍需查询流式 capabilities。

所有目录模型均支持 `models install MODEL_ID`，`models install all` 包含流式模型及语言检测器。翻译模型在安装阶段下载固定源文件、校验 SHA-256、离线转换到 int8，再核对固定产物 SHA-256；需安装 `model-preparation` extra。缺少或版本不匹配时明确报错，全量安装在开始下载前检查这些依赖。启动/推理不下载或转换模型。已有旧 ID、目录和离线包可继续识别；列表和处理计划统一返回新 ID，新安装/导出使用新命名。

访问 `/console/streaming` 选择三个场景、语言和音频文件。页面支持实时重放、修订字幕、按顺序播放译文语音和动态首输出指标；当前没有麦克风采集 UI。SDK 可发送实时 PCM。先查询 `GET /v1/audio/stream/capabilities`；`enabled` 与每条链路的 `available` 都需要成立，会话仍受即时容量限制。

## 最小安装与启动流程

在项目根目录、已创建的 Python 虚拟环境中执行：

```sh
python -m pip install -e '.[streaming]'
# 使用独立数据目录；安装、导入、启动都使用同一个 SMARTVOICE_HOME。
export SMARTVOICE_HOME="$PWD/.smartvoice-dev/streaming"
export SMARTVOICE_STREAMING_ENABLED=true
python -m smartvoice models list
python -m smartvoice models install streaming-stt-paraformer-zh-en-int8
python -m smartvoice models install streaming-stt-zipformer-en-int8
python -m smartvoice models install streaming-ct-transformer-zh-en
```

以上三个模型支持双语原文字幕。翻译模型直接安装：

```sh
python -m pip install -e '.[streaming,model-preparation]'
python -m smartvoice models install streaming-mt-opus-zh-en-int8
python -m smartvoice models install streaming-mt-m2m100-zh-en-int8
# 全量安装所有目录模型及语言检测器：
# python -m smartvoice models install all
```

只试一个方向时，只需安装对应翻译模型。离线部署仍可用 `models export/import`；已有准备好的 tokenizer/CT2 目录可通过 `scripts/prepare_streaming_bundle.py MODEL_ID --root /prepared/tree --output /tmp/model.zip` 打包后导入。该打包脚本只校验和打包，不下载或转换，不能覆盖已有输出包。

语音模式再安装目标语言 TTS 及声明的回退：

```sh
python -m smartvoice models install tts-supertonic-v3-multilingual-int8
python -m smartvoice models install tts-matcha-zh-baker
python -m smartvoice models install tts-kokoro-multilingual-v1-1-zh-en
python -m smartvoice --host 127.0.0.1 --port 8766
```

另一个终端查询能力，浏览器访问 `http://127.0.0.1:8766/console/streaming`：

```sh
curl http://127.0.0.1:8766/v1/audio/stream/capabilities
```

检查顶层 `enabled`、所需链路的 `available` 和 `missing_models`。`available` 不代表质量已验收，也不保证当前还有空闲会话容量。

| 模式 | configure.mode | 输入源语言 | 目标语言 | 额外阶段 |
|---|---|---|---|---|
| 原文字幕 | transcription | zh / en | 不填写 | 在线 ASR、标点 |
| 译文字幕 | translated_subtitles | zh / en | en / zh | 对应方向翻译 |
| 译文语音 | spoken_interpretation | zh / en | en / zh | 对应方向翻译、目标 TTS |

## smartvoice.stream.v1

连接 `WS /v1/audio/stream`，第一条消息是 JSON：

```json
{"type":"configure","protocol":"smartvoice.stream.v1","mode":"spoken_interpretation","source_language":"en","target_language":"zh","audio":{"encoding":"pcm_s16le","sample_rate":16000,"channels":1},"output_consumption":"playback","ack_window":16}
```

原文字幕省略 `target_language`。可选字段：`models`（asr/formatting/translation/tts 的活动阶段 ID）、`include_source_text`、`glossary`、`output_consumption`、`ack_window`。未知字段或不兼容配置被拒绝；`session_ready.plan` 返回已解析模型、策略和提交语义。等待 ready 后发送二进制 PCM16 little-endian，16kHz 单声道，建议每帧 20ms（640 字节），最多 32000 字节。不是上传 WAV/MP3 容器。输入结束发送 `{"type":"finish"}`；取消发送 `{"type":"cancel"}`。

事件包含 `session_id`、递增 `event_sequence` 和服务端单调时间。`source_partial/source_final` 是声学段；显示字幕使用 `source_unit_partial/source_unit_final`；翻译为 `target_partial/target_final`。客户端按 unit/revision 替换草稿，接受显式撤回，忽略过期修订；final 是提交后的不可变文本。目标事件的源引用提供来源范围，不代表模型级逐词对齐。

`audio_segment` 是 JSON 描述符，紧接一条二进制 WAV；两条消息共同构成该音频块。校验 `audio_bytes`、`audio_sequence` 和目标文本区间，严格按序解码/播放。不能等整段输入结束才播放。`audio_skipped` 明确报告未播出的原因；客户端不能把它视为成功音频。TTS 只消费已提交且质量策略允许的文本。

对非终止 JSON 事件发送 `{"type":"ack","event_sequence":N}`；音频在接收完整二进制块后 ACK。`delivery` 模式仅确认消费；`playback` 模式还需按块顺序发送 `audio_started` 和 `audio_played`（字段 `audio_sequence`），用于播放积压控制。投递 ACK 不等于播放结束，定时调度也不等于物理可听时间。

成功、部分完成、无音频、无语音或取消以一个 `session_complete` 终止；失败以一个 `error` 终止（含 code、stage/诊断信息）。错误与 complete 不应同时出现。客户端应读取 terminal.status，而不是仅凭连接关闭判断成功。当前 error 事件不附带阶段 profiling；失败 profiling 为暂缓补齐项。规范错误涵盖 invalid_config、invalid_message、unsupported_language_pair、model_unavailable、session_overload、memory_admission、scheduler_overload 及阶段超时/执行失败。

## 容错、资源与计时

默认会话上限 1，估算模型内存预算 4096MiB，共享推理许可 2；初始化 30s、原生调用 15s、空闲输入 10s、会话墙钟 300s。墙钟包括初始化和排空，不能保证接受完整 300s 文件后还能播完。队列、音频积压和输出 ACK 均有限额；慢消费者可能超时退出。断线、取消或关服清理独占进程；若无法证明资源释放，保留容量和模型占用，重启后恢复。内存预算是准入估算，不是 RSS 硬限制。

浏览器 Origin 必须与服务同源；SDK 可不提供 Origin。默认仅回环，当前没有 API 认证。默认诊断不保存音频/原文/译文，评测输出含文本和语料引用须由使用者管理。

质量、性能、并发分开评估。性能目标为原文 partial P90<1s、译文 target partial P90<2s、双向语音 TTFO P90<3s，后者从原声音起点计时。浏览器同时观测 PCM 首帧、首显示、音频到达及播放调度；原声音起点需要人工标注或注明 VAD 暂定。服务 profiling 不能与客户端时钟直接相减。语音 TTFO 分位数按成功出音频会话计算，同时报告全量成功率/无音频率/跳过原因。目标不是本次迁移已验收的承诺。


## 最小 Python 客户端示例

将下面代码保存为本地 `stream_client.py`。依赖已包含在 `streaming` extra 中。示例输入必须已经是 **16kHz、单声道、16-bit WAV**；浏览器页面可以解码重采样其他支持的音频格式。示例通过 `delivery` 确认接收，将语音块按顺序保存为独立 WAV，不承担播放 ACK 或物理可听计时。

```sh
python stream_client.py /absolute/path/to/audio.wav transcription zh
python stream_client.py /absolute/path/to/audio.wav translated_subtitles en
python stream_client.py /absolute/path/to/audio.wav spoken_interpretation zh
```

```python
import asyncio
import contextlib
import json
import sys
import wave
from pathlib import Path
import websockets

async def main(path, mode, source):
    if mode not in ('transcription', 'translated_subtitles', 'spoken_interpretation') or source not in ('zh', 'en'):
        raise ValueError('Expected a documented mode and explicit zh/en')
    with wave.open(path, 'rb') as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (16000, 1, 2):
            raise ValueError('Expected 16kHz mono PCM16 WAV')
        pcm = wav.readframes(wav.getnframes())
    config = dict(type='configure', protocol='smartvoice.stream.v1', mode=mode,
                  source_language=source, output_consumption='delivery',
                  audio=dict(encoding='pcm_s16le', sample_rate=16000, channels=1))
    if mode != 'transcription': config['target_language'] = 'en' if source == 'zh' else 'zh'
    async with websockets.connect('ws://127.0.0.1:8766/v1/audio/stream', max_size=1000000) as ws:
        await ws.send(json.dumps(config))
        ready = json.loads(await ws.recv())
        if ready['type'] != 'session_ready': raise RuntimeError(ready)
        await ws.send(json.dumps(dict(type='ack', event_sequence=ready['event_sequence'])))
        async def feed():
            start = asyncio.get_running_loop().time()
            for offset in range(0, len(pcm), 640):
                block = pcm[offset:offset + 640]
                await ws.send(block)
                delay = start + (offset + len(block)) / 32000 - asyncio.get_running_loop().time()
                if delay > 0: await asyncio.sleep(delay)
            await ws.send(json.dumps(dict(type='finish')))
        feeder = asyncio.create_task(feed()); descriptor = None; sequence = 1
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    if descriptor is None or len(message) != descriptor['audio_bytes'] or descriptor['audio_sequence'] != sequence:
                        raise RuntimeError('Invalid audio descriptor, payload or sequence')
                    # Create-only output; choose a fresh working directory for each replay.
                    with Path(f'target-{sequence:04d}.wav').open('xb') as output: output.write(message)
                    await ws.send(json.dumps(dict(type='ack', event_sequence=descriptor['event_sequence'])))
                    descriptor = None; sequence += 1
                    continue
                event = json.loads(message)
                if event['type'] == 'audio_segment':
                    if descriptor is not None: raise RuntimeError('Missing audio payload')
                    descriptor = event
                    continue
                print(json.dumps(event, ensure_ascii=False))
                if event['type'] in ('error', 'session_complete'):
                    if descriptor is not None: raise RuntimeError('Missing final audio payload')
                    if event['type'] == 'error' or event['status'] != 'complete': raise RuntimeError(event)
                    break
                await ws.send(json.dumps(dict(type='ack', event_sequence=event['event_sequence'])))
            else: raise RuntimeError('Connection closed without a terminal event')
        finally:
            feeder.cancel()
            with contextlib.suppress(asyncio.CancelledError): await feeder

if __name__ == '__main__': asyncio.run(main(*sys.argv[1:]))
```

真实客户端还需要限制总输入/输出大小、管理草稿撤回、处理超时及重试策略。重连会创建新会话，目前没有跨连接续传或音频去重承诺；不要自动重新播出已经播放过的内容。示例及浏览器指标不替代正式 benchmark。


## 测试与评测状态

产品自动覆盖与剩余验收见[测试矩阵](streaming-test-coverage.md)。独立评测入口见[流式 benchmark](../benchmarks/streaming/README.md)：runner 已位于产品评测目录，但固定语料/标注/基线仍依赖本地 sandbox，音频完整性护栏和取消会话计数存在已记录的待修项。当前不能将 smoke、自动测试通过或单个 guard 标志视为产品级验收。
