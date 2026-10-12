<div align="center">
  <img src="https://raw.githubusercontent.com/dxhome/SmartVoice/main/assets/smartvoice-logo.png" alt="SmartVoice 标志" width="220">
  <p><strong>在自己的设备上运行语音识别和语音合成。</strong></p>
  <p>OpenAI 风格 REST 音频 API · 流式 WebSocket · 按语言路由模型 · 本地 CPU 推理</p>
  <p><a href="README.md">English</a> | <a href="README.zh-CN.md">简体中文</a></p>
</div>

# SmartVoice

SmartVoice 是面向开发者的本地语音识别、语音合成与流式语音服务。安装所需模型，在自己的设备上运行推理，并通过 OpenAI 风格 REST 音频 API 或版本化流式 WebSocket 接入应用。

**推理过程中的音频保留在本机。** 安装模型后，支持的工作流可离线运行，无需云端账号，也没有按请求收取的推理费用。SmartVoice 支持 Windows x64、macOS Apple Silicon 和 Linux x86_64 上的 CPU 推理。

[快速开始](#快速开始) · [浏览模型](#模型管理) · [试用 API](#api) · [架构](#架构) · [报告问题](https://github.com/dxhome/SmartVoice/issues)

## 可以用它构建什么？

- **私密语音输入：** 在本地转录录音，避免将音频发送到托管语音 API。
- **离线语音助手：** 为本地智能体或应用添加语音识别和语音播报能力。
- **多语言语音功能：** 使用 `smartvoice-auto` 模型 ID，按任务和语言路由 STT 与 TTS 请求，也可处理中文和英文流式语音场景。

## 为什么选择 SmartVoice？

- **以隐私为先：** 推理在你的设备上运行；模型、音频和服务配置的存放位置由你决定。
- **统一的语音 API：** 通过一个本地 API 接入兼容应用，支持语音转录、语音合成和流式处理。
- **内置流式语音能力：** 音频到达时即可转录、显示译文字幕或播放翻译语音。
- **自由选择和迁移模型：** 只安装需要的模型，可直接指定模型，也可导出模型包以便离线传输。

SmartVoice 支持 Windows x64、macOS Apple Silicon 和 Linux x86_64 上的 CPU 推理；暂不支持 GPU、Linux/Windows ARM64、Intel macOS、Android、HTTPS、打包安装程序或 MCP。下载模型需要互联网连接；安装完成后，语音推理在本机运行。服务默认绑定 `127.0.0.1`，且没有 API 身份验证；如需绑定外部接口，请限制在可信网络并配置防火墙。

## 功能

| 类别 | 支持内容 |
|---|---|
| 平台 | Windows x64、macOS Apple Silicon、Linux x86_64 CPU；Linux 模型推理已在 Ubuntu 26.04 x86_64 验证 |
| 语音识别 | Whisper Base 多语言、SenseVoice Small、Qwen3-ASR 0.6B |
| 语音合成 | Kokoro 1.1、Matcha Baker、Supertonic 3、Qwen3-TTS 0.6B；默认返回 MP3 |
| 流式处理 | 实时转录、中英双向译文字幕和译文语音，通过版本化 WebSocket API 与浏览器工作区使用 |
| 智能路由 | 可编辑的任务和语言模型优先级；`smartvoice-auto` 同时路由转录和语音合成 |
| Console | 浏览器模型管理、语音试用、路由配置和流式工作区 |
| 模型管理 | 安装、刷新、卸载、下载任务、离线导入/导出 |
| API | 版本化 REST API、流式 WebSocket、OpenAPI 文档及运行状态/能力查询 |

架构原则、平台兼容性和模型可用性说明请参阅[架构总结与指南](doc/architecture-guidelines.md)。SmartVoice 支持 OpenAI Audio API 的一部分约定，并非完全兼容；详情请见 [API 规范](doc/api-spec.md)。

## 智能路由

智能路由根据请求任务、语言和用户可编辑的有序 JSON 表，选择已安装的模型。STT 和 TTS 接口都可以使用 `smartvoice-auto` 按语言路由，也可以传入具体模型 ID 直接调用对应模型。

TTS 模型优先级：

1. 中文：Qwen3-TTS → Matcha → Kokoro
2. 德语、法语、西班牙语、日语、韩语：Supertonic → Qwen3-TTS
3. 英语：Supertonic → Qwen3-TTS → Kokoro
4. 葡萄牙语、俄语、意大利语：Supertonic → Qwen3-TTS
5. 其他支持的语言：Supertonic

STT 内置路由表优先为中文、英语、粤语、日语和韩语选择 SenseVoice；其他已配置语言优先选择 Qwen3-ASR，部分语言还会将 Whisper 作为后备候选。

SmartVoice 根据 API 接口和语言进行路由：

| 请求 | 虚拟模型 ID | 语言来源 |
|---|---|---|
| 语音转录 | `smartvoice-auto` | 显式提供的 `language`；省略或设为 `auto` 时自动检测 |
| 语音合成 | `smartvoice-auto` | 显式提供的 `language`；省略或设为 `auto` 时根据文本检测 |

首次启动时，内置路由表会复制到 `<data_dir>/router.json`。修改此文件即可自定义模型优先级，然后无需重启即可应用：

```bash
python -m smartvoice router reload
```

每次请求都会先按路由表中的任务和语言规则匹配，再选择优先级最高、已安装且当前运行时可用的模型。模型选择依据可编辑的路由策略，而不是模型安装顺序。模型选定后若推理失败，SmartVoice 会返回错误，不会静默切换其他模型。请求详情请参阅 [API 规范](doc/api-spec.md)。

## 流式处理

流式处理是 SmartVoice 的正式功能，可在音频到达时持续处理。它提供三种场景：实时转录并显示原文字幕、中英双向译文字幕，以及播放译文语音。可以在 `/console/streaming` 浏览器工作区处理本地音频文件，也可以通过 `WS /v1/audio/stream` 接入发送实时 PCM 音频的客户端。Console 会显示当前场景所需模型链路是否就绪。

API 客户端应先查询 `GET /v1/audio/stream/capabilities`，再建立会话。WebSocket 使用版本化协议 `smartvoice.stream.v1`；客户端发送 16 kHz、单声道 PCM16 音频帧，接收增量转录/翻译事件；译文语音场景还会收到按序排列的音频块。浏览器工作区支持字幕修订、顺序播放，以及服务进程仍在运行时的断线重连；服务重启后不能续传。

按场景和语言方向安装所需的流式模型组件：所有场景都需要在线语音识别和标点模型；译文字幕还需要对应方向的翻译模型；译文语音还需要兼容的 TTS 模型。安装流式翻译模型前，需要安装 `streaming` 和 `model-preparation` extras。详见[模型管理](#模型管理)和[流式安装与 API 契约](doc/streaming.md)。

## 模型管理

SmartVoice 将应用程序与模型文件分开管理。你可以在 Console、CLI 或 API 下载任务中浏览和安装模型。系统会同时验证模型文件和当前运行时是否可用。下载模型需要互联网；已安装模型可在离线环境中推理。模型及音色许可可能不同于 SmartVoice 的许可协议。

### 语音模型

| 模型 ID | 任务 | 语言 / 音色 | 预计模型文件大小 | 热请求 RTF | 每 CPU 核心 req/s |
|---|---|---|---:|---:|---:|
| `stt-whisper-base-multilingual-int8` | STT | 自动检测；英语、中文、日语、韩语、法语、德语 | ~0.16 GB | 0.052 | 1.237 |
| `stt-sensevoice-small-int8` | STT | 中文、英语、粤语、日语、韩语 | ~0.24 GB | 0.017 | 4.169 |
| `stt-qwen3-asr-600m-int8` | STT | 30 种语言代码，包括粤语；支持自动检测 | ~0.99 GB | 0.274 | 0.643 |
| `tts-kokoro-multilingual-v1-1-zh-en` | TTS | 中文、英语；103 个说话人 | ~0.43 GB | 0.324 | 0.329 |
| `tts-matcha-zh-baker` | TTS | 中文；一个音色 | ~0.15 GB | 0.030 | 2.379 |
| `tts-supertonic-v3-multilingual-int8` | TTS | 31 种语言；不含中文 | ~0.15 GB | 0.236 | 0.692 |
| `tts-qwen3-0-6b-customvoice` | TTS | 10 种语言；9 个预设音色 | ~2.50 GB | 0.557 | 未测量 |

### 流式模型组件

流式功能组合在线识别和标点，并按需增加翻译与译文语音合成。按语言方向和使用场景安装所需组件即可：

| 模型 ID | 用途 | 语言 |
|---|---|---|
| `streaming-stt-paraformer-zh-en-int8` | 在线语音识别 | 中文、英语 |
| `streaming-stt-zipformer-en-int8` | 在线语音识别 | 英语 |
| `streaming-ct-transformer-zh-en` | 标点 | 中文、英语 |
| `streaming-mt-opus-zh-en-int8` | 翻译 | 中文 → 英语 |
| `streaming-mt-m2m100-zh-en-int8` | 翻译 | 英语 → 中文 |

流式语音合成复用上方的普通 TTS 模型。详见[流式处理](#流式处理)。

上方语音模型的 RTF 为热请求中位数（模型支持中文时使用中文；Supertonic 使用英语），数值越低，相对于音频时长的处理速度越快。每 CPU 核心吞吐量取并发测试中最高的通过速率。两项数据均来自代表性样本，仅适用于 Apple Silicon Mac（macOS 27.0、10 个逻辑 CPU、16 GiB 内存、Python 3.11.9、sherpa-onnx 1.13.8）。Sherpa 测试使用两个实例、每个实例两个线程，等待队列上限为 4；Qwen3-TTS 使用单个串行原生运行时。由于未能在 CPU 采样中完整计入 Qwen3-TTS 原生引擎进程，因此未提供其每核心速率。详见[基准测试报告](benchmarks/README.md#concurrency-and-request-experience)。

模型大小为解压后的近似值；准确文件和大小请查看 [`catalog/models.json`](catalog/models.json)。安装 Qwen3-TTS 0.6B 至少需要 6 GiB 可用空间。Windows 运行时要求 AVX2 和 FMA。已发布的 Linux x86_64 wheel 要求 glibc 2.38 或更新版本，详见[平台兼容性说明](doc/architecture-guidelines.md#build-and-hardware-qualifications)。

### 安装与维护

按模型 ID 安装，或使用 `all` 安装整个目录及语言检测器（可能需要数 GiB 磁盘空间）：

```bash
python -m smartvoice models list
python -m smartvoice models install stt-sensevoice-small-int8
python -m smartvoice models install tts-kokoro-multilingual-v1-1-zh-en
python -m smartvoice models refresh
python -m smartvoice models export stt-sensevoice-small-int8 ./sensevoice.smartvoice.zip
python -m smartvoice models import ./sensevoice.smartvoice.zip
python -m smartvoice models uninstall stt-sensevoice-small-int8
```

Hugging Face 模型可用 `--source` 指定镜像基础地址；下载文件仍会按目录中固定的哈希值校验。也可在 Console 或通过 API 下载任务安装模型。模型正在被服务使用时不能卸载。

从模型目录安装流式翻译模型前，需先安装转换依赖：

```bash
python -m pip install 'smartvoice[streaming,model-preparation]'
```

翻译模型在安装阶段完成校验和准备，推理时使用本地准备好的资产。CLI 和 API 详情请参阅[流式安装说明](doc/streaming.md#最小安装与启动流程)和 [API 规范](doc/api-spec.md)。

## 快速开始

SmartVoice 要求 Python 3.11 或更新版本，并使用 CPU 进行推理。服务默认只绑定到 `127.0.0.1`。

### 1. 安装 SmartVoice

选择以下一种安装方式。日常使用建议通过 PyPI 安装；源码安装适用于开发或需要可编辑源码目录的场景。

<details>
<summary>从 PyPI 安装</summary>

为你的平台安装软件包。macOS Apple Silicon、Windows x64 和 Linux x86_64 wheel 内含原生 Qwen3-TTS 运行时。Linux wheel 使用 `manylinux_2_38_x86_64` 标签，内含 OpenBLAS 及所需运行库，要求 glibc 2.38 或更新版本。Linux x86_64 全新安装和模型推理已在 Ubuntu 26.04 上验证。不支持缺少原生运行时所需指令集的 CPU，也不支持 Linux ARM64。

macOS Apple Silicon：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install smartvoice
```

Linux x86_64（Ubuntu 24.04 或更新版本）：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install smartvoice
```

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install smartvoice
```

</details>

<details>
<summary>从源码安装</summary>

请在仓库根目录运行这些命令。Windows x64 源码构建会包含 Qwen 原生运行时，因此安装 SmartVoice 前请先按下文说明安装 MSYS2 构建工具。Linux x86_64 源码构建需要 GCC、Make 和 OpenBLAS 开发文件。

macOS：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

Ubuntu 26.04 x86_64（经过验证的 Linux 基线）：

```bash
sudo apt-get update
sudo apt-get install -y python3-venv build-essential libopenblas-dev
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

在 macOS Apple Silicon 上，源码安装会使用 Xcode Command Line Tools 构建原生 INT8 Qwen3-TTS 运行时。Windows x64 和 Linux x86_64 源码部署还需要按下文所述显式构建原生运行时。

#### 在 Linux 上构建 Qwen3-TTS 运行时

Linux x86_64 源码构建使用 GCC、Make 和 OpenBLAS。构建程序会检测当前机器可用的 CPU 指令，并在放置运行时前执行原生内核自检。在 Ubuntu 上安装依赖：

```bash
sudo apt-get install -y build-essential libopenblas-dev
```

对于可编辑源码目录，请显式运行构建程序，将运行时放入源码包：

```bash
SMARTVOICE_QWEN_OUTPUT="$PWD/src/smartvoice/resources/bin" \
  python scripts/build_qwen3_tts_linux.py
```

构建程序会将 `qwen_tts` 和运行时依赖许可说明放入包资源目录。源码构建链接到系统 OpenBLAS，因此请保留已安装的 OpenBLAS 运行库。已发布的 Linux wheel 会包含 OpenBLAS 和所需运行库。

#### 在 Windows 上构建 Qwen3-TTS 运行时

Windows 源码部署使用 MSYS2 UCRT64 GCC 和 OpenBLAS 构建 Qwen3-TTS。在提升权限的 MSYS2 UCRT64 终端中安装 MSYS2 和 UCRT64 工具链软件包，然后再安装 SmartVoice：

```bash
pacman -S --needed make gcc diffutils mingw-w64-ucrt-x86_64-openblas
```

然后在 PowerShell 的 SmartVoice 仓库根目录中构建运行时并检查能否启动：

```powershell
# 如果 MSYS2 安装在 C:\msys64 或 %USERPROFILE%\msys64，则无需设置此项。
$env:SMARTVOICE_MSYS2_ROOT = "C:\msys64"
$env:SMARTVOICE_QWEN_OUTPUT = "$PWD\src\smartvoice\resources\bin"
.\.venv\Scripts\python.exe .\scripts\build_qwen3_tts_windows.py
```

构建脚本会运行原生运行时自检，并将 `qwen_tts.exe`、所需运行时 DLL 和许可说明放到 `src/smartvoice/resources/bin`，供可编辑源码目录使用。Windows 软件包构建的输出也会包含该运行时。这些生成的二进制文件是本地构建产物，不会提交到仓库。如果 MSYS2 安装在其他目录，请设置 `SMARTVOICE_MSYS2_ROOT`。Python 源码安装保持可编辑状态；无需构建 SmartVoice wheel。

</details>

运行下面的命令前，请先激活 `.venv`。Windows PowerShell 也可以不激活环境，而是将命令中的 `python` 替换为 ``.\.venv\Scripts\python.exe``。

### 2. 启动 SmartVoice

启动本地服务：

```bash
python -m smartvoice --host 127.0.0.1 --port 8000
```

服务默认只监听 `127.0.0.1`。如需让其他设备连接，请显式绑定到外部网络接口，例如 `--host 0.0.0.0`。对外绑定时使用的是没有 API 身份验证的纯 HTTP；请仅在可信网络中使用，并按需配置机器防火墙。目前不支持 HTTPS。

### 3. 管理模型并在 Console 中试用语音功能

打开 [SmartVoice Console](http://127.0.0.1:8000/console)。在 **Models** 中浏览目录、安装和管理模型。下载需要互联网连接，模型文件存放在源码目录之外：macOS 为 `~/Library/Application Support/SmartVoice`，Linux 为 `~/.smartvoice`，Windows 为 `%LOCALAPPDATA%\SmartVoice`。安装后，推理在本机运行。模型、语言和估算大小见[模型管理](#模型管理)。

在 Console 工作区中使用已安装的模型试用语音识别和合成。可以使用根据所选模型和设置生成的 API 示例，也可以直接在 Console 中编辑 Smart Router 优先级；保存的路由配置会立即生效。你也可以打开交互式 [API 文档](http://127.0.0.1:8000/docs)。

试用流式处理时，安装对应场景需要的模型组件，然后打开[流式工作区](http://127.0.0.1:8000/console/streaming)。页面可将本地音频文件流式处理为原文字幕、译文字幕或译文语音。安装翻译模型前需执行 `python -m pip install 'smartvoice[streaming,model-preparation]'`，详见[模型管理](#模型管理)。

服务设置保存在 `<data_dir>/smartvoice.json`；路由优先级单独保存在 `<data_dir>/router.json`。

也可以通过 CLI 管理模型。例如，安装一组基础 STT 和 TTS 模型以及语言检测器：

```bash
python -m smartvoice models install stt-sensevoice-small-int8
python -m smartvoice models install tts-kokoro-multilingual-v1-1-zh-en
python -m smartvoice models install-language-id
```

通过 PyPI 安装到 macOS Apple Silicon、Windows x64 或 Linux x86_64 时，会附带 Qwen3-TTS 运行时。从源码目录安装时，请按上文说明在 Windows 和 Linux 上构建运行时；macOS 源码安装会自动编译。然后安装 Qwen3-TTS：

```bash
python -m smartvoice models install tts-qwen3-0-6b-customvoice
```

### 4. 接入智能体或应用

在智能体或应用的设置中选择 **OpenAI-compatible API**，并将 SmartVoice API 基础地址设为：

```text
http://127.0.0.1:8000/v1
```

API 支持 OpenAI Audio API 的一部分约定。从 Console 复制 STT 或 TTS 示例，也可以查看 [API](#api) 中的示例。API 基础地址以 `/v1` 结尾；Console 和交互式 API 文档由同一个本地 SmartVoice 实例提供。

## API

音频接口支持 OpenAI Audio API 的一部分约定。请求字段、响应格式和错误码请参阅 [API 规范](doc/api-spec.md)。

### 使用智能路由转录音频

```powershell
curl.exe -F "file=@sample.wav" -F "model=smartvoice-auto" -F "language=auto" `
  http://127.0.0.1:8000/v1/audio/transcriptions
```

支持上传 WAV、MP3、M4A 和 FLAC 格式。默认音频大小上限为 25 MiB，时长上限为 10 分钟。

### 使用智能路由合成语音

```powershell
curl.exe -X POST http://127.0.0.1:8000/v1/audio/speech `
  -H "Content-Type: application/json" `
  -d '{"model":"smartvoice-auto","input":"Hello from SmartVoice.","language":"auto"}' `
  --output speech.mp3
```

TTS 默认返回单声道 MP3（96 kbps）；如需 WAV，请设置 `"response_format":"wav"`。请求参数、支持语言、响应格式、限制和错误码请参阅 `/docs` 或 [API 规范](doc/api-spec.md)。

### 流式音频

建立流式会话前，先查询本机可用的模型链路：

```bash
curl http://127.0.0.1:8000/v1/audio/stream/capabilities
```

客户端连接 `ws://127.0.0.1:8000/v1/audio/stream`，使用 `smartvoice.stream.v1` 协议配置 `transcription`、`translated_subtitles` 或 `spoken_interpretation` 场景，再发送 16 kHz、单声道 PCM16 音频帧。客户端会收到增量转录和翻译事件；译文语音场景还会返回按序排列的音频块。协议和客户端示例请参阅[流式安装与 API 契约](doc/streaming.md)。

## 架构

```text
客户端 / 智能体 / 浏览器 Console ──► 版本化 REST API + 流式 WebSocket
                                                   │
本地 CLI ─────────────────────────────────────────┤
                                                   ▼
                                             应用服务
                         ┌─────────────────────┼──────────────────────┐
                         ▼                     ▼                      ▼
                    语音转录与合成          智能路由              流式语音
                                           与模型选择        识别 / 翻译 / 合成
                         └─────────────────────┼──────────────────────┘
                                               ▼
                                             领域契约
                               ┌───────────────┴───────────────┐
                               ▼                               ▼
                           推理接口                       模型管理
                               │                               │
                               ▼                               ▼
                   sherpa-onnx 与原生 Qwen       模型目录、下载和本地存储
                         运行时适配器
```

支持的源码和 wheel 部署可在 Windows x64、macOS Apple Silicon 和 Linux x86_64 上执行 CPU 推理。后端和平台详情请参阅[架构总结与指南](doc/architecture-guidelines.md)。

## 开发

```bash
python -m pip install -e ".[dev]"
python scripts/test.py ci
```

`python scripts/test.py regression` 运行功能测试及当前环境已安装模型的真实推理测试。`python scripts/test.py full` 还会运行长音频 STT 和真实模型流式回归，要求安装相应模型资产和语料。测试模式及前置条件请参阅 [`doc/testing.md`](doc/testing.md)。

版本管理、GitHub Release 和可选的 PyPI 发布流程请参阅 [`doc/releasing.md`](doc/releasing.md)。

## 仓库目录

```text
.github/       GitHub Actions CI 与发布工作流
assets/        项目标志和浏览器图标
benchmarks/    语音质量、性能与流式评测工具和报告
catalog/       模型目录、默认路由表和流式工作流配置
config/        服务配置示例
doc/           API、架构、测试、发布和产品文档
native/        Qwen3-TTS 原生运行时源码和构建支持
samples/       多语言语音样本和转录文本
scripts/       构建、测试、模型准备和验证命令
src/           应用 API、Console、CLI、服务和推理适配器
tests/         单元、API、流式和真实模型推理回归测试
pyproject.toml 软件包元数据、依赖和构建配置
```

## 许可协议

SmartVoice 源代码使用 [Apache License 2.0](LICENSE)。模型权重、语音和其他第三方资源可能适用单独的许可条款；使用或再分发前请先查看相关条款。
