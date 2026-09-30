# Qwen3-TTS native CPU runtime

Vendored from [gabriele-mastrapasqua/qwen3-tts](https://github.com/gabriele-mastrapasqua/qwen3-tts)
at commit `ef339be58a778b062e1c14382347964552eae007` (MIT License).

SmartVoice builds and packages this CPU runtime for macOS arm64. The provider uses this C INT8
runtime on every platform where a matching executable is installed; SmartVoice does not retain a
PyTorch Qwen3-TTS implementation. The server listener is patched to bind to `127.0.0.1`; SmartVoice
owns the child process and forwards local synthesis requests to it. Inference runs with `--int8`
and does not enable the native engine's experimental quantization modes. Native Windows packaging
is not available yet; the upstream build instructions support Windows through WSL2.

The build also includes `third_party/ingot` (MIT) and the ARM kernels in `third_party/kleidiai`
(see its `NOTICE.md`). Their upstream licenses/notices are kept beside the vendored sources.

To refresh the source, update the pinned commit and `.source_fingerprint`, then reapply and review
the loopback listener patch in `qwen_tts_server.c`. Do not update the vendored engine without
rerunning the SmartVoice API, Chinese quality, and performance checks.
