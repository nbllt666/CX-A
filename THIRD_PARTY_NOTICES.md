# 第三方组件与素材许可清单（THIRD_PARTY_NOTICES）

> 适用范围：CX-A v0.1.0 安装包（`CX-A-Setup-0.1.0.exe`）、安装期装配物、可选下载内容。
> 核对基准（2026-11-21）：`installer/manifest.json`（schema `cxa-installer-manifest/v1`，version 0.1.0，generated_at 2026-10-04）、`frontend/package.json` 与 `frontend/node_modules` 实测版本、`installer/installer.iss`、`installer/backend.spec`、`installer/build.py`、`installer/bootstrap.py`、`installer/conda_runtime.py`、`data/SenseVoiceSmall/README.md`、`MeloTTS/LICENSE`、`installer/bundled/` 实际目录。
> 本文件为事实整理，不构成法律意见。许可条款以各项目官方仓库、模型卡与许可全文为准；标注“以官方为准”的条目不得视为已确认。

## 0. 总体声明

1. 本项目自有代码（`lite/` 后端、`frontend/` 源码、`installer/` 脚本等）以 **MIT** 许可发布，全文见仓库根 [LICENSE](LICENSE)；安装程序载荷根随附 `LICENSE.txt`（由 `installer/build.py::assemble` 落位）。
2. 下列第三方软件、模型、数据集与素材 **不属于** 本项目 MIT 许可范围，各自按其权利人的许可或条款分发与使用。MIT 不授予这些组件的任何再许可权；两者冲突时以第三方许可为准。
3. 分发形态定义：
   - **随包**：安装程序载荷（`PayloadDir`）直接携带，安装时落位到安装目录（部分随包源在安装期再做一次装配/拷贝）。
   - **安装期装配**：安装期由内置 Miniconda / conda / pip 从网络下载并安装，版本与体积随上游变化。
   - **可选下载**：由首启向导按用户选择下载，默认不随包、不预装。

---

## 1. 随包分发的组件

| 组件 | 版本 / 来源 | 许可类型 | 分发形态 | 备注 |
|------|------------|---------|---------|------|
| Electron | 33.4.11（`frontend/package.json` 声明 `^33.2.0`，实测安装 33.4.11）；https://github.com/electron/electron | MIT | 随包 | 载荷根 `electron.exe` 及资源；内含 Node.js（MIT）。Electron 发行目录自带 `LICENSE` 与 `LICENSES.chromium.html` |
| Chromium | Electron 33.4.11 内置（Chromium 130 系列） | BSD-3-Clause 及第三方许可 | 随包 | 完整第三方清单见发行目录 `LICENSES.chromium.html`；精确小版本以 Electron 33.x 官方发布说明为准 |
| FFmpeg（Electron 内置） | Electron 内置 `ffmpeg.dll` | LGPL-2.1（Chromium/Electron 构建口径，以 `LICENSES.chromium.html` 为准） | 随包 | 许可与源码获取说明见 https://ffmpeg.org/legal.html |
| SwiftShader（Vulkan/GL 软件渲染） | Electron 内置 `vk_swiftshader.dll` | Apache-2.0 | 随包 | 同属 Electron 第三方清单 |
| Vulkan Loader | Electron 内置 `vulkan-1.dll` | Apache-2.0 | 随包 | 仅加载层；显卡驱动由用户系统提供 |
| DirectX Shader Compiler | Electron 内置 `d3dcompiler_47.dll` | Microsoft DirectX 再分发条款 | 随包 | 以 Microsoft 官方条款为准 |
| React | 18.3.1 | MIT | 随包（renderer 构建产物） | https://github.com/facebook/react |
| React DOM | 18.3.1 | MIT | 随包（renderer 构建产物） | 同上 |
| three | 0.186.0 | MIT | 随包（renderer 构建产物） | https://github.com/mrdoob/three.js |
| @pixiv/three-vrm | 3.5.5 | MIT | 随包（renderer 构建产物） | https://github.com/pixiv/three-vrm |
| lucide-react | 1.48.0 | ISC | 随包（renderer 构建产物） | https://github.com/lucide-icons/lucide |
| Python | 3.10（安装期 conda voice env）；后端冻结运行时 | PSF-2.0 | 随包（后端）/ 安装期装配（voice env） | 以 Python 官方 `LICENSE` 为准 |
| PyInstaller | 后端构建工具（bootloader 编入 `backend.exe`） | GPL-2.0-or-later with bootloader exception | 随包（bootloader） | 例外条款允许以任意许可分发打包产物；https://pyinstaller.org/en/stable/license.html |
| LanceDB | 随后端冻结（`backend.spec` 于 20261005 解除排除，`hiddenimports=['lancedb','pyarrow','numpy']`） | Apache-2.0 | 随包（编入 `runtime/backend`） | `installer/manifest.json` 旧注记称“不随安装包分发”，与当前 `backend.spec` 不一致，以实际构建为准 |
| pyarrow | LanceDB 依赖，编入后端 | Apache-2.0 | 随包（编入 `runtime/backend`） | 以 Apache Arrow 官方许可为准 |
| numpy | LanceDB 依赖，编入后端 | BSD-3-Clause | 随包（编入 `runtime/backend`） | 以 NumPy 官方许可为准 |
| llama.cpp（CUDA 构建） | b11391（manifest 20261004 经 `scripts/update_llama_bundled.py` 升级）；`bundled/llama_cpp` → `runtime/llama` | MIT | 随包（安装期落位） | 含 `llama-cli.exe`、`llama-server.exe`、`ggml-cuda.dll` 等；https://github.com/ggml-org/llama.cpp |
| NVIDIA CUDA 运行时（cudart / cuBLAS） | `cudart64_12.dll`、`cublas64_12.dll`、`cublasLt64_12.dll`（随 llama.cpp CUDA 构建） | NVIDIA CUDA Toolkit EULA / NVIDIA 软件许可协议 | 随包（`runtime/llama`） | 非开源组件；再分发条款以 NVIDIA 官方 EULA 为准（https://docs.nvidia.com/cuda/eula/index.html） |
| LLVM OpenMP 运行时（libomp） | `libomp.dll`（随 llama.cpp 构建） | Apache-2.0 WITH LLVM-exception | 随包（`runtime/llama`、`runtime/llama_vulkan`） | 许可全文随附：`bundled/llama_cpp/LICENSE-LLVM-OpenMP` |
| llama.cpp（Vulkan 构建） | b11391（与 CUDA 构建同版本）；`bundled/llama_cpp_vulkan` → `runtime/llama_vulkan` | MIT | 随包（安装期落位） | 使用用户系统的 Vulkan 驱动/加载器，未随包分发显卡驱动 |
| Qwen3-Embedding-0.6B（GGUF Q8_0） | `Qwen3-Embedding-0.6B-Q8_0.gguf`（639,150,592 字节）；https://huggingface.co/Qwen/Qwen3-Embedding-0.6B | Apache-2.0 | 随包（安装期落位 `data/local_llm/qwen3-embedding-0.6b`） | 1024 维；由 `runtime/llama/llama-server.exe` 的 `/v1/embeddings` 提供；以模型卡为准 |
| MeloTTS（源码） | MyShell.ai；https://github.com/myshell-ai/MeloTTS | MIT | 随包（`bundled/melotts_src`，安装期 `pip install --no-deps` 装入 voice env） | 声明基于 Coqui TTS / VITS / Bert-VITS2 改写，衍生权利以 MeloTTS 仓库声明为准 |
| 默认音色 auto_DS-008（cx-open） | 项目训练产物：`bundled/melotts/ckpt.txt`（622,848,775 字节）+ `config.json`（DS-008/1 speaker）；另含 `bundled/tts_onnx/*.onnx` 导出 | 项目训练/提供，**不宣称 MIT** | 随包（组装期拷入载荷 `data/voices/cx-open`） | 版权、训练数据（DS-008）与权重再分发权利由项目方确认；ONNX 导出资产权利与权重同源 |
| SenseVoiceSmall | FunASR / 通义实验室；本地模型卡 `data/SenseVoiceSmall/README.md`；https://huggingface.co/FunAudioLLM/SenseVoiceSmall | Apache-2.0 | 随包（安装期落位 `data/SenseVoiceSmall`，约 896 MB） | 含 `model.pt`、`config.yaml`、BPE 词表等；以模型卡为准 |
| NLTK 数据（cmudict、averaged_perceptron_tagger、averaged_perceptron_tagger_eng） | NLTK data；`bundled/nltk_data` | 各自许可：cmudict 为 CMUdict（BSD 风格）；两个 tagger 为 Apache-2.0 | 随包（安装期拷入 `data/nltk_data`） | 各包许可见 https://www.nltk.org/nltk_data/ 与包内 README |
| Miniconda 安装器 | `miniconda_installer.exe`（132,526,808 字节） | BSD-3-Clause（conda/安装器）；Anaconda 默认源另受 Anaconda 服务条款约束 | 随包（安装期静默安装至 `runtime/conda`） | 语音环境经 `--override-channels -c 清华 anaconda 镜像` 创建，镜像内容属 Anaconda 默认源，受 Anaconda ToS 约束 |
| Inno Setup（安装程序本体） | Inno Setup 6 编译生成的 `CX-A-Setup-0.1.0.exe` | Inno Setup License | 随包（安装程序本身） | https://jrsoftware.org/isinfo.php；简体中文语言文件 `installer/languages/ChineseSimplified.isl` 为社区维护翻译（kira-96），其许可以翻译仓库为准 |
| 默认桌宠 VRM 素材（CX-A_S） | `data/pet/cx-open.vrm`（19,327,908 字节，与 `bundled/pet_model` 同 MD5 `DE92F10DD5D21015E0F985FD973C8021`） | CX-A 项目专用素材许可（见 [PET_MODEL_LICENSE.md](PET_MODEL_LICENSE.md)）：仅限随 CX-A 使用与分发，禁止其他用途 | 随包（组装期拷入载荷，含 `PET_MODEL_LICENSE.md`） | 2026-11-21 已更新 VRM 0.x 元数据：`allowedUserName=Everyone`、`licenseName=Other`、`otherLicenseUrl` 指向 `PET_MODEL_LICENSE.md`；VRM 枚举无法表达“仅限本项目”，具体以该许可文件为准 |

---

## 2. 安装期装配（联网下载）

| 组件 | 版本 / 来源 | 许可类型 | 分发形态 | 备注 |
|------|------------|---------|---------|------|
| conda 基础包（`python=3.10` 及依赖） | 清华 anaconda 镜像（`--override-channels`，不读用户 `.condarc`）；落 `runtime/voice` | 各包各自许可；Anaconda 默认源另受 Anaconda ToS 约束 | 安装期装配 | 版本随镜像上游变化，不锁清单 |
| pip 依赖包（GPU 自动分叉） | `torch`/`torchaudio`（pytorch 官方 cu128 或 cpu 轮子）、`onnxruntime`/`onnxruntime-gpu`/`onnxruntime-directml`、`llama-cpp-python`、`funasr`、`modelscope`、`transformers`、`tokenizers`、`jieba` 等 | 各自许可（常见：torch/torchaudio 为 BSD 系；onnxruntime、llama-cpp-python、funasr 等以各自仓库 LICENSE 为准；transformers/tokenizers 为 Apache-2.0） | 安装期装配 | 以 PyPI 包元数据与上游仓库为准；不在此逐项固定版本 |
| MeloTTS 官方中文权重 | HuggingFace `myshell-ai/MeloTTS-Chinese` | 以 HF 模型卡为准（不在此断言为 MIT） | 安装期装配（预热缓存 `data/hf_cache`，约 1.5 GB 含 BERT） | 仅当本地音色权重不可用时的官方权重；本项目默认音色为项目训练产物 |
| BERT 中文权重 | HuggingFace `hfl/chinese-roberta-wwm-ext-large` | Apache-2.0（以 HF 模型卡为准） | 安装期装配 | MeloTTS 中文链路依赖 |
| MeloTTS 其他语种官方权重 | `myshell-ai/MeloTTS-*`（首次按语种合成时下载） | 以 HF 模型卡为准 | 安装期/运行期下载 | 不随包 |

---

## 3. 可选下载（默认不随包）

| 组件 | 版本 / 来源 | 许可类型 | 分发形态 | 备注 |
|------|------------|---------|---------|------|
| Gemma 4（E2B-Q4 / E2B-Q6 / E4B-Q4 / E4B-Q6，含 mmproj-BF16 视觉组件） | `unsloth/gemma-4-E2B-it-GGUF`、`unsloth/gemma-4-E4B-it-GGUF`（HuggingFace / 魔塔双源）；档位表见 `lite/runtime/model_downloader.py` | **Gemma Terms of Use** | 可选下载（首启向导，默认不下载） | 必须遵守 Gemma Terms of Use（含使用与再分发限制）：https://ai.google.dev/gemma/terms 。不得表述为 MIT 或 Apache-2.0 |
| Qwen2.5（0.5B Instruct GGUF 等） | `Qwen/Qwen2.5-*-GGUF` | Apache-2.0 | 可选下载（历史/手工档位） | 20261004 起档位表已全换 Gemma 4；仓库内仅前端测试夹具出现该仓库名示例，当前默认不提供该档 |

---

## 4. 构建与开发期工具（不随包）

以下仅用于构建/测试，不进入安装包，此处不逐项展开：Vite（MIT）、TypeScript（Apache-2.0）、Tailwind CSS（MIT）、electron-builder（MIT）、Vitest（MIT）、Playwright（Apache-2.0）、pytest（MIT）、Inno Setup 编译器（Inno Setup License）。各项以官方仓库 LICENSE 为准。

---

## 5. manifest key → 清单条目对照

`installer/manifest.json` 中全部组件 key 均可在此表查到对应条目：

| manifest key | manifest status | 清单条目 | 本文件位置 |
|--------------|-----------------|---------|-----------|
| `llama_cpp` | runtime | llama.cpp（CUDA 构建）+ NVIDIA CUDA 运行时 + LLVM OpenMP | §1 |
| `llama_cpp_vulkan` | runtime | llama.cpp（Vulkan 构建）+ LLVM OpenMP | §1 |
| `embedding_model` | runtime | Qwen3-Embedding-0.6B（GGUF） | §1 |
| `lancedb` | optional | LanceDB（实际已编入后端）+ pyarrow + numpy | §1 |
| `melotts` | builtin | 默认音色 auto_DS-008（cx-open）+ MeloTTS 源码 | §1 |
| `sensevoice` | runtime | SenseVoiceSmall | §1 |
| `pet_model` | builtin | 默认桌宠 VRM（CX-A_S） | §1 |
| `local_llm` | optional | Gemma 4 四档（含 mmproj） | §3 |
| `miniconda_installer.exe` | runtime | Miniconda 安装器 | §1 |
| `voice_env` | runtime | 安装期 conda 包 + pip 依赖包（含 MeloTTS/BERT 权重预热） | §2 |

> 说明：Electron 壳、Python 后端与前端运行时依赖不在 `manifest.json` 登记（由 `build.py` 组装流程直接落位），其许可见 §1。

---

## 6. 不确定项与处理方式

| 事项 | 现状 | 处理方式 |
|------|------|---------|
| 默认 VRM 素材再分发 | 原元数据为 `Redistribution_Prohibited`，与随包公开发布冲突 | **已处理（2026-11-21）**：项目方（author=nbllt666）裁决“授权给使用本项目的所有人，禁止除此以外的用途”；VRM meta 已改为 `allowedUserName=Everyone` / `licenseName=Other`，并新增根 `PET_MODEL_LICENSE.md` 与载荷随装副本；发布前确认该 URL 在公开仓库可达 |
| 默认音色 auto_DS-008 权利 | 项目训练产物，训练数据 DS-008 来源与权重再分发权利未在仓库内声明 | 本文件不宣称 MIT；由项目方确认并补充书面说明 |
| MeloTTS 官方权重许可 | 仓库未附模型卡许可声明 | 标注“以 HF 模型卡为准”，未断言具体许可 |
| NVIDIA CUDA 运行时再分发 | 具体条款随 CUDA EULA 版本变化 | 附官方 EULA 链接，未转述条款细节 |
| Gemma 4 版本与条款 | 上游仓库与 Gemma Terms 可能更新 | 标注 Gemma Terms of Use 并附链接；未写 MIT/Apache |
| Qwen2.5 可选档位 | 当前档位表已不含 Qwen2.5，仅测试夹具出现 | 标为历史/手工档位，避免与实际分发不符 |
| Inno Setup 中文语言文件 | 社区维护翻译，无独立许可声明 | 标注“以其翻译仓库为准” |
| NLTK 数据各包许可 | 多个数据包许可不同 | 分别标注 cmudict 与 tagger，并指向 NLTK 官方许可页 |
| LanceDB 分发状态 | manifest 旧注记与 `backend.spec` 现状冲突 | 以 `backend.spec` 为准确认随包，并在备注中说明冲突 |

---

## 7. 许可全文与来源位置

- 项目自有代码：仓库根 `LICENSE`（安装包载荷根 `LICENSE.txt`）。
- Electron / Chromium / FFmpeg 等：`frontend/node_modules/electron/LICENSE`、`frontend/node_modules/electron/dist/LICENSES.chromium.html`。
- llama.cpp：上游仓库 `LICENSE`（MIT）；LLVM OpenMP 全文随附于 `installer/bundled/llama_cpp/LICENSE-LLVM-OpenMP`。
- MeloTTS：`MeloTTS/LICENSE`（MIT，Copyright (c) 2024 MyShell.ai）。
- SenseVoiceSmall：`data/SenseVoiceSmall/README.md` 模型卡（Apache License 2.0）。
- NLTK 数据：`installer/bundled/nltk_data/` 各包内 README 与 https://www.nltk.org/nltk_data/ 。
- 安装期 conda/pip 包：各自包内 `LICENSE` / `METADATA`（安装后位于 `runtime/voice` 环境）。
