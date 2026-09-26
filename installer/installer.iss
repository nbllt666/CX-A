; CX-A 独立安装程序（Inno Setup 6）——载荷展开 + 安装期一次性装配运行时
;
; 由 installer/build.py 步骤6 调用（也可手工编译）：
;   iscc /DPayloadDir=<便携根绝对路径> /DOutputDir=<产物目录> /DAppVersion=<版本> "installer\installer.iss"
;
; 未传 define 时的缺省值（便于手工试编译）：
;   PayloadDir = ..\release\portable   便携根（壳 + 后端 + 随包资产 + 语音桥脚本）
;   BundledDir = bundled               本 .iss 同目录下的运行时源（安装期装配用）
;   OutputDir  = ..\release            安装程序输出目录
;   AppVersion = 0.1.0                 版本号（构建链从 lite.__version__ 读取后传入）

#ifndef PayloadDir
  #define PayloadDir "..\release\portable"
#endif
#ifndef BundledDir
  #define BundledDir "bundled"
#endif
#ifndef OutputDir
  #define OutputDir "..\release"
#endif
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

#define MyAppName "CX-A 赛博伴侣"
#define MyAppPublisher "CX-A"
#define MyAppExeName "CX-A.exe"

[Setup]
; AppId 一经发布不得变更（升级/卸载识别依据）
AppId={{8F2A6C41-9D3B-4E7C-B5A2-1C4E7F0A9D63}
AppName={#MyAppName}
AppVersion={#AppVersion}
AppVerName={#MyAppName} {#AppVersion}
AppPublisher={#MyAppPublisher}
; 逐用户安装（PrivilegesRequired=lowest）：不弹 UAC、不污染系统目录，
; 与「便携语义」（数据/运行时都在安装目录内）一致
DefaultDirName={localappdata}\CX-A
DisableDirPage=no
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=CX-A-Setup-{#AppVersion}
; 载荷含 896MB 模型权重等已压缩资产：normal + 非固实压缩（编译速度优先，体积增益有限）
Compression=lzma2/normal
SolidCompression=no
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupLogging=yes

[Languages]
; 简体中文语言包（社区维护版，随仓提交于 installer/languages/）
Name: "chinese"; MessagesFile: "languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："

[Files]
; 便携载荷：壳 + 后端 + 随包资产（data/pet、data/voices 等）+ 语音桥脚本
; （runtime/voice_bridge/bridge.py 由 build.py 在组装便携根时落位）
; Excludes：用户数据不入安装集——顶层 config.json（含云端 Key / 完成态）与
; data/memories.db（记忆库）；升级重装不得覆盖既有用户数据，与 zip 口径一致
; （首启由后端 auto-init 按默认值生成）。
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Excludes: "config.json,data\memories.db,logs"; Flags: recursesubdirs createallsubdirs ignoreversion
; 随包运行时源：安装期装配（内置 conda / MeloTTS 源码 / nltk 数据）读取此落点
; （conda_runtime.find_bundled_source 的第 2 候选：<root>/runtime/_bundled/<name>）
Source: "{#BundledDir}\miniconda_installer.exe"; DestDir: "{app}\runtime\_bundled"; Flags: ignoreversion
Source: "{#BundledDir}\melotts_src\*"; DestDir: "{app}\runtime\_bundled\melotts_src"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#BundledDir}\nltk_data\*"; DestDir: "{app}\runtime\_bundled\nltk_data"; Flags: recursesubdirs createallsubdirs ignoreversion
; SenseVoice 识别模型（约 896 MB）：直接落位最终路径（无需加工），不进便携 zip
Source: "{#BundledDir}\sensevoice\*"; DestDir: "{app}\data\SenseVoiceSmall"; Flags: recursesubdirs createallsubdirs ignoreversion
; llama.cpp 预编译二进制（约 184 MB）：本地推理运行时（llama-cli.exe + CUDA 等 DLL），
; 直接落位最终路径（运行时按 <root>/runtime/llama/llama-cli.exe 推导；见
; 20260925_模块0_llama二进制随包分发.md），不进便携 zip；本地小 LLM 模型不由本安装集携带。
Source: "{#BundledDir}\llama_cpp\*"; DestDir: "{app}\runtime\llama"; Flags: recursesubdirs createallsubdirs ignoreversion
; 嵌入模型（约 609 MB，Qwen3-Embedding-0.6B-Q8_0.gguf，1024 维）：记忆检索真实语义嵌入
; （主进程拉起 runtime/llama/llama-server.exe 常驻子进程经 /v1/embeddings 取向量），
; 直接落位最终路径（运行时按 <root>/data/local_llm/qwen3-embedding-0.6b/*.gguf 解析；
; 见 .trae/documents/20260926_模块0_真实嵌入与向量持久化.md），不进便携 zip。
Source: "{#BundledDir}\embedding_model\*"; DestDir: "{app}\data\local_llm\qwen3-embedding-0.6b"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; 安装期一次性装配运行时（人类裁决③）：内置 conda → 语音 sidecar 环境 → 语音/推理依赖
; （GPU 自动探测分叉）→ nltk 数据 → TTS 权重预热（约 1.5GB，落 data/hf_cache）。
; 口径：任何一步失败不阻断安装（backend.exe --provision-runtime 恒返回 0）；
; 细粒度进度与错误见 {app}\runtime\provision.log 与 {app}\data\install_report.json。
Filename: "{app}\runtime\backend\backend.exe"; Parameters: "--provision-runtime --root ""{app}"""; \
  StatusMsg: "正在安装语音引擎与运行环境（内置 conda + 语音模型依赖，首次可能需要较长时间，请勿关闭）…"; \
  Flags: runhidden waituntilterminated

[UninstallDelete]
; 卸载清理：运行时（conda / 语音环境 / 装配日志 / 随包源，可重新装配的巨量文件）
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\logs"
; 注意：data\ 为用户数据（记忆库 / 配置 / 加密密钥 / 下载的模型权重），**不自动删除**