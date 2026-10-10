<div align="center">
  <img src="https://raw.githubusercontent.com/dxhome/SmartVoice/main/assets/smartvoice-logo.png" alt="SmartVoice 标志" width="220">
  <p><strong>在自己的设备上运行语音识别和语音合成。</strong></p>
  <p>统一的 OpenAI 风格音频 API · 按语言智能路由模型 · CPU 推理 · 无按请求计费</p>
  <p><a href="README.md">English</a> | <a href="README.zh-CN.md">简体中文</a></p>
</div>

# SmartVoice

SmartVoice 是面向开发者的本地语音转文字（STT）和文字转语音（TTS）服务。安装所需模型，在自己的设备上运行推理，并通过统一的 OpenAI 风格音频 API 接入应用。

**推理过程中的音频保留在本机。** 安装模型后，支持的工作流可离线运行，无需云端账号，也没有按请求收取的推理费用。SmartVoice 支持 Windows x64、macOS Apple Silicon 和 Linux x86_64 上的 CPU 推理。

[快速开始](#快速开始) · [浏览模型](#模型目录) · [试用 API](#api) · [架构](#架构) · [报告问题](https://github.com/dxhome/SmartVoice/issues)

## 可以用它构建什么？

- **私密语音输入：** 在本地转录录音，避免将音频发送到托管语音 API。
- **离线语音助手：** 为本地智能体或应用添加语音识别和语音播报能力。
- **多语言语音功能：** 使用 `smartvoice-auto` 模型 ID，按任务和语言路由 STT 与 TTS 请求。

## 为什么选择 SmartVoice？

- **以隐私为先：** 推理在你的设备上运行；模型、音频和服务配置的存放位置由你决定。
- **一个 API 同时支持 STT 和 TTS：** 通过统一的音频 API 接入兼容的智能体和应用。
- **自由选择和迁移模型：** 只安装需要的模型，可直接指定模型，也可导出模型包以便离线传输。

**流式预览：** 默认提供版本化 WebSocket 和浏览器页面，支持中文/英文原文字幕、中英双向译文字幕及译文语音；浏览器入口 `/console/streaming`，实时接口 `WS /v1/audio/stream`，能力查询 `GET /v1/audio/stream/capabilities`。使用各场景前需安装所需模型资产。当前实测平台为 macOS arm64；参阅[接口契约与安装](doc/streaming.md)及[迁移证据和剩余验收](doc/streaming-migration.md)。

流式会话使用共享模型 worker 和私有有界状态；默认不限制会话墙钟或累计音频时长，暂停输入时应继续发送心跳。参阅[长会话资源边界](doc/streaming-shared-workers.md)。

**当前范围：** 支持 CPU 推理；暂不支持 GPU 推理、打包安装程序和 Android。安装或更新模型需要从配置的数据源下载文件。服务默认绑定到 `127.0.0.1`；对外开放网络时没有 API 身份验证，因此请仅在可信网络中使用并配置防火墙访问限制。

## 功能

| 类别 | 支持内容 |
|---|---|
| 平台 | Windows x64、macOS Apple Silicon、Linux x86_64 源码构建和平台 wheel；Linux 在 Ubuntu 26.04 x86_64 上验证 |
| 推理 | 支持 sherpa-onnx CPU 推理；原生 C INT8 Qwen3-TTS 运行时支持以上三个平台，包括 Linux x86_64 上的模型推理 |
| STT | Whisper Base 多语言、SenseVoice Small、Qwen3-ASR 0.6B |
| TTS | Kokoro 1.1、Matcha Baker、Supertonic 3、Qwen3-TTS 0.6B |
| 智能路由 | 根据任务和语言，按可编辑的优先级列表选择已安装模型；`smartvoice-auto` 同时适用于 STT 和 TTS |
| API | OpenAPI 文档、语音转录、语音合成、模型目录和运行状态 |
| 模型管理 | 安装、卸载、离线导入/导出和可续传下载 |
| 暂不支持 | GPU 推理、Linux/Windows ARM64、Intel macOS、Android、HTTPS、打包安装程序、MCP |

架构原则、实现概览、各推理后端的操作系统/架构/设备支持矩阵，以及模型安装状态和推理可用状态的区别，请参阅[架构总结与指南](doc/architecture-guidelines.md)。

SmartVoice 支持 OpenAI Audio API 的一部分约定，但不代表与其完全兼容。请求和响应详情请参阅 [API 规范](doc/api-spec.md)。

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

系统会选择第一个已安装且验证通过的候选模型。推理失败时不会自动重试下一个候选模型。请求详情请参阅 [API 规范](doc/api-spec.md)。

## 模型目录

语言支持情况表示模型目录中声明的支持范围，不代表不同模型之间的效果比较。各模型和语音的许可协议可能不同于 SmartVoice 的许可协议。

| 模型 ID | 任务 | 语言 / 音色 | 预计模型文件大小 | 热请求 RTF | 每 CPU 核心 req/s |
|---|---|---|---:|---:|---:|
| `stt-whisper-base-multilingual-int8` | STT | 自动检测；英语、中文、日语、韩语、法语、德语 | ~0.16 GB | 0.052 | 1.237 |
| `stt-sensevoice-small-int8` | STT | 中文、英语、粤语、日语、韩语 | ~0.24 GB | 0.017 | 4.169 |
| `stt-qwen3-asr-600m-int8` | STT | 30 种语言代码，包括粤语；支持自动检测 | ~0.99 GB | 0.274 | 0.643 |
| `tts-kokoro-multilingual-v1-1-zh-en` | TTS | 中文、英语；103 个说话人 | ~0.43 GB | 0.324 | 0.329 |
| `tts-matcha-zh-baker` | TTS | 中文；一个音色 | ~0.15 GB | 0.030 | 2.379 |
| `tts-supertonic-v3-multilingual-int8` | TTS | 31 种语言；不含中文 | ~0.15 GB | 0.236 | 0.692 |
| `tts-qwen3-0-6b-customvoice` | TTS | 10 种语言；9 个预设音色 | ~2.50 GB | 0.557 | 未测量* |

RTF 为热请求的中位数（支持中文的模型使用中文，Supertonic 使用英语）；数值越低，相对于音频时长的处理速度越快。每 CPU 核心吞吐量取并发测试中最高的通过速率。两项数据均使用代表性样本，并且仅适用于测试平台：Apple Silicon Mac、macOS 27.0、10 个逻辑 CPU、16 GiB 内存、Python 3.11.9 和 sherpa-onnx 1.13.8。Sherpa 测试使用两个实例、每个实例两个线程，等待队列上限为 4；Qwen3-TTS 使用单个串行原生运行时。由于采样 CPU 使用率时未能完整计入 Qwen3-TTS 原生引擎进程，因此没有提供它的每核心速率。详情请参阅[基准测试报告](benchmarks/README.md#concurrency-and-request-experience)。

模型大小为解压后的近似值；准确文件和大小请查看 [`catalog/models.json`](catalog/models.json)。安装 Qwen3-TTS 0.6B 至少需要 6 GiB 可用空间。Windows 运行时要求 AVX2 和 FMA；Linux 构建会使用当前机器提供的 CPU 指令集。更多信息请参阅[平台兼容性说明](doc/architecture-guidelines.md#build-and-hardware-qualifications)。

STT 请求使用 `language=auto` 时，SmartVoice 会使用可选的 Whisper Tiny 语言检测器，再根据检测出的语言选择模型。运行 `python -m smartvoice models install all` 可安装它；否则 SmartVoice 会使用已安装且支持自动检测的模型。其可用状态会显示在 `/v1/capabilities` 中。

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

打开 [SmartVoice Console](http://127.0.0.1:8000/console)。在 **Models** 中浏览模型目录、安装和管理模型。下载模型需要互联网连接，模型文件存储在源码目录之外：macOS 为 `~/Library/Application Support/SmartVoice`，Linux 为 `~/.smartvoice`，Windows 为 `%LOCALAPPDATA%\SmartVoice`。安装后，推理在本机运行。支持的语言和估算文件大小请参阅[模型目录](#模型目录)。

在 Console 工作区中使用已安装的模型试用语音识别和合成。可以使用根据所选模型和设置生成的 API 示例，也可以直接在 Console 中编辑 Smart Router 优先级；保存的路由配置会立即生效。你也可以打开交互式 [API 文档](http://127.0.0.1:8000/docs)。

服务设置保存在 `<data_dir>/smartvoice.json`；路由优先级单独保存在 `<data_dir>/router.json`。Sherpa 模型使用惰性实例池支持并行推理，详情请参阅[推理并发说明](doc/architecture-guidelines.md#inference-concurrency-lifecycle-and-limits)。

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

TTS 默认返回单声道 MP3（96 kbps）；如需 WAV，请设置 `"response_format":"wav"`。请求最多支持 4,000 个字符；生成音频最长为 180 秒，规范化 WAV 和最终响应分别有 32 MiB 大小上限。Supertonic 会将较长文本分块处理；长 STT 输入会按模型声明的窗口进行静音感知切分（Whisper Base 为 25 秒，默认 15 秒），窗口之间重叠 1 秒，并在各片段之间使用单一语言判定，同时保留 10 分钟输入上限。可用参数、语言行为和错误码请参阅 `/docs` 或 [API 规范](doc/api-spec.md)。本地 Whisper 中文解码修复需要打过补丁的原生 wheel，详情请参阅[构建与验证说明](doc/archive/stt/whisper-chinese-decoding-fix.md)。

## 管理模型

CLI 支持 `models list`、`refresh`、`install`、`uninstall`、`export` 和 `import`。`models list` 先按安装状态分组，再按 STT、TTS、Streaming（STT/MT/CT 子类）和 SmartVoice native 分类；每个模型都会显示可用状态，不可用时还会显示原因。可用性检查会同时验证模型文件完整性和当前推理运行时/设备。运行中的服务在进程范围内维护一份模型可用性快照：服务启动时初始化，并在模型管理操作后更新。如果模型文件是在服务外部修改的，可运行 `python -m smartvoice models refresh` 或调用 `POST /v1/models/refresh` 更新快照。作为后备机制，服务每 10 分钟检查一次，并在后台刷新，同时继续使用最近一次成功的快照。TTL 刷新失败后，会分别在 5、10 和 15 秒后重试；重试三次仍失败后，将等待手动刷新或模型管理操作触发恢复扫描。可通过 `model_availability_ttl_seconds` 或 `SMARTVOICE_MODEL_AVAILABILITY_TTL_SECONDS` 配置检查间隔。运行 `python -m smartvoice models install all` 可安装目录中尚未安装的所有模型以及 Whisper Tiny 语言检测器。模型按目录配置的数据源顺序下载；发现已有模型目录无效时会跳过并提示修复方法。安装所有模型可能需要数 GiB 磁盘空间。Windows x64 上只有构建了原生运行时后才能使用 Qwen3-TTS，详情请参阅[在 Windows 上构建 Qwen3-TTS 运行时](#在-windows-上构建-qwen3-tts-运行时)。也可以通过 API 下载任务安装模型。导出的模型包可传到离线机器并导入。服务正在使用某个模型时，不能卸载该模型。

流式专用模型使用 `streaming-stt-*`、`streaming-mt-*`、`streaming-ct-*` ID，共用 TTS 的 ID 保持不变。可用 `models list --category streaming` 过滤 CLI；`GET /v1/models?category=streaming` 查询已验证安装资产，`GET /v1/catalog?category=streaming` 查询全部目录条目。`install all` 包含翻译模型，需先运行 `python -m pip install -e '.[streaming,model-preparation]'` 安装转换依赖；翻译模型在安装阶段校验源文件、离线转换并验证固定产物，推理阶段不会转换。旧流式 ID 和安装目录继续兼容。详见[安装与就绪检查](doc/streaming.md)。

使用 `python -m smartvoice models install <model-id>` 安装模型。默认情况下，SmartVoice 使用模型目录中指定的数据源。对于 Hugging Face 模型，可以通过 `--source` 传入兼容的镜像基础 URL 来更换数据源，例如：

```bash
python -m smartvoice models install stt-qwen3-asr-600m-int8 --source https://hf-mirror.com
```

该基础 URL 会与目录中固定的仓库、修订版本和文件路径组合。只有所有必需模型文件都托管在 Hugging Face 上时才能使用此选项。下载后仍会依据目录中的 SHA-256 值校验文件。

```bash
python -m smartvoice models list
python -m smartvoice models refresh
python -m smartvoice models export stt-sensevoice-small-int8 ./sensevoice.smartvoice.zip
python -m smartvoice models import ./sensevoice.smartvoice.zip
python -m smartvoice models uninstall stt-sensevoice-small-int8
```

## 架构

```text
客户端 / 智能体 ──► 版本化 HTTP API ──► 应用服务
                         ▲                    │
                         │                    ▼
                    本地 CLI              领域契约
                                              │
                               ┌──────────────┴──────────────┐
                               ▼                             ▼
                         推理端口                    模型仓库端口
                               │                             │
                               ▼                             ▼
                      组合式推理提供器              文件系统目录适配器
                       ┌───────┴────────┐
                       ▼                ▼
                  sherpa-onnx      原生 C INT8 Qwen-TTS
                    适配器               适配器
                       │
                       ▼
                  平台诊断适配器
```

调用方使用有版本控制的 API 和能力接口；推理细节封装在提供器和仓库接口之后。源码部署支持 Windows x64、macOS Apple Silicon 和 Linux x86_64 上的 CPU 推理。Qwen3-TTS 在这三个平台上均通过原生 C INT8 适配器运行；Linux 源码部署按上文说明链接到系统 OpenBLAS。SmartVoice 暂不提供 GPU 推理后端和 Android 运行时。详细矩阵和模型可用性说明请参阅[架构总结与指南](doc/architecture-guidelines.md)。

## 开发

```bash
python -m pip install -e ".[dev]"
python scripts/test.py ci
```

`python scripts/test.py regression` 运行默认推理回归套件，包括现有功能和真实推理测试、SenseVoice/Qwen3-ASR/Whisper 的中英双语短样本、音频窗口边界以及 75 秒分段输入。当前运行环境未安装的模型会分别跳过。运行 `python scripts/test.py full` 可执行全部 CI 和回归测试，以及三个 STT 模型的 300 秒和 590 秒中英双语输入。完整模式要求安装全部三个模型和已验证的 Whisper 补丁运行时；如果前置条件不满足，会直接报告错误，不会静默跳过。默认回归测试有意不包含长音频扩展测试。STT 测试检查功能是否成功及响应元数据，不评估转录准确率。套件详情请参阅 [`doc/testing.md`](doc/testing.md) 和 [`tests/fixtures/stt/README.md`](tests/fixtures/stt/README.md)。

Full 还要求默认流式模型链路及固定 FLEURS 语料，在六条真实流式链路中分别
以有界加速输入处理一小时循环语音。可使用
`python scripts/test.py full --streaming-only` 独立执行流式部分。
该测试验证长输入、输出完整性及资源回收，不等同于一小时墙钟 soak 或实时延迟验收。
详见[完整流式回归](scripts/README.md#full-streaming-regression)。

版本管理、GitHub Release 和可选的 PyPI 发布流程请参阅 [`doc/releasing.md`](doc/releasing.md)。

## 仓库目录

```text
catalog/       模型元数据和默认路由表
src/           API/CLI 入口、应用服务、领域契约、端口和适配器
tests/         单元测试、API 测试和可选的真实推理测试
benchmarks/    基准测试代码、配置和结果
config/        示例服务配置
doc/           API 规范、测试、发布、需求和设计说明
assets/        项目标志
```

## 许可协议

SmartVoice 源代码使用 [Apache License 2.0](LICENSE)。模型权重、语音和其他第三方资源可能适用单独的许可条款；使用或再分发前请先查看相关条款。
