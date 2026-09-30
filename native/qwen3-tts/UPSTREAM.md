# Qwen3-TTS native CPU runtime

Vendored from [gabriele-mastrapasqua/qwen3-tts](https://github.com/gabriele-mastrapasqua/qwen3-tts)
at commit `ef339be58a778b062e1c14382347964552eae007` (MIT License).

SmartVoice builds and packages this CPU runtime for macOS arm64 and Windows x64. The Windows build
uses MSYS2 GCC with the UCRT64 OpenBLAS package and bundles its required runtime DLLs beside the
executable. Set `SMARTVOICE_MSYS2_ROOT` when MSYS2 is not installed at `C:\msys64`. The provider
uses this C INT8 runtime on every platform where a matching executable is installed; SmartVoice
does not retain a PyTorch Qwen3-TTS implementation. The server listener is patched to bind to
`127.0.0.1`; SmartVoice owns the child process and forwards local synthesis requests to it.
Inference runs with `--int8` and does not enable the native engine's experimental quantization
modes. The Windows build uses the MSYS2 POSIX compatibility runtime (`msys-2.0.dll`), not WSL.
That runtime is GPL-3.0-or-later; its license text and matching source-package URL are included in
the wheel under `smartvoice/resources/bin/licenses/`. Review those source-availability terms before
redistributing the Windows wheel. See the [MSYS2 license overview](https://www.msys2.org/license/).

To build on Windows, install MSYS2 and run:

```sh
pacman -S --needed diffutils make mingw-w64-ucrt-x86_64-gcc mingw-w64-ucrt-x86_64-openblas
```

Then build the wheel with the normal Python packaging command. The isolated smoke builder is
`python scripts/build_qwen3_tts_windows.py`; it writes the executable and DLLs to
`.smartvoice-dev/qwen-windows-bin` by default. Run `qwen_tts.exe --self-test` and a model-backed
synthesis before distributing a wheel.

The build also includes `third_party/ingot` (MIT) and the ARM kernels in `third_party/kleidiai`
(see its `NOTICE.md`). Their upstream licenses/notices are kept beside the vendored sources.

To refresh the source, update the pinned commit and `.source_fingerprint`, then reapply and review
the loopback listener patch in `qwen_tts_server.c`. Do not update the vendored engine without
rerunning the SmartVoice API, Chinese quality, and performance checks.
