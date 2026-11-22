# current-note — fix-local-mode-and-pet-ux

> 当前活跃变更：**prepare-public-release（20261121）**，交接与状态见文末「发布前准备」段。
> 以下为历史变更归档，仅供追溯。

## 做到哪了

Task 0 完成（变更文档 + 本 note 已创建）；Task 1-6 未开始。

## 为什么

用户指出四个设计不合理（本地模式连云端 / 音色不能自选模型文件 / 无主动视觉配置 / 悬浮窗大小不可调且点击无反应）。根因与方案契约见 `.trae/specs/fix-local-mode-and-pet-ux/spec.md`（已经 GN-004 计划审查：警示放行，Agent ID 3a8c7809-e769-4468-918d-a7da7126e2d1；人类已批准）。变更留痕见 `.trae/documents/20261002_模块0_本地模式与桌宠交互设计修复.md`。

## 工程过程

1. Spec 模式：探索确认根因（fallback.py 在线一律走云端 / voices 无 API / vision 未装配 / 悬浮窗固定）→ 写 spec 三件套 → GN-004 计划审查（警示放行，F1-F4 已修正入任务书）→ 人类批准。
2. Task 0：创建变更文档与本 note（rules-6 文档先行）。
3. Task 1（Agent 330c3d78）→ Task 2（Agent 56459cf7）串行后端；Task 3（Agent aae8f60b）+ Task 4（Agent 716ef0ea）并行前端；Task 5 主线程复验全部闸门。
4. Task 6：6.1 变更文档最终结果回填；6.3 GN-004 交付前审查（Agent 1ed80fe0）结论=警示放行（无阻断无 SOFT_BLOCK；观察项 1/4 已修正，2/3 登记为规范瑕疵，5 备忘）；待人类 [V] 裁决。

## 交接状态

| Task | 状态 | 说明 |
|------|------|------|
| 0 | 已闭合 | 文档先行完成 |
| 1 | 已闭合 | 后端A 本地模式真正本地（Agent 330c3d78；pytest 1357 passed；本地优先路由+动态加载+local_llm.ready） |
| 2 | 已闭合 | 后端B+C 音色API+视觉装配（Agent 56459cf7；pytest 1384 passed；voices两端点+热切换+screen_backend+tick线程） |
| 3 | 已闭合 | [P] 前端设置页（Agent aae8f60b；vitest 82+tsc 0+build 通过） |
| 4 | 已闭合 | [P] 悬浮窗交互（Agent 716ef0ea；vitest 82+tsc 0+build+E2E 2 passed） |
| 5 | 已闭合 | 集成验证（主线程复跑：pytest 1384 passed/1 skipped；三重闸门 单测82→E2E 2→check:vrm ready；GUI 手感类冒烟标注「当前不可判定」移交人类真机验收） |
| 6 | 已闭合 | [V] 交付收口：6.1 文档回填；6.3 GN-004 交付前审查（Agent 1ed80fe0）警示放行且观察项已处理；人类裁决「批准交付」（2026-10-02） |

三值口径：已闭合 / 未闭合 / 当前不可判定——「当前不可判定」项见未闭合段。

## 未闭合项

- 无阻塞性未闭合项。
- **当前不可判定（移交人类日常使用时体验）**：悬浮窗拖拽手感与三档记忆重开保持；导入音色系统对话框真机流程与实听效果；断网状态下本地聊天实听（自动化覆盖路由语义，无法覆盖主观听感）。有问题随时可提新变更。
- 备忘：start_vision_tick_thread 无显式 stop 钩子（知情决策，装配函数无统一关闭链；未来引入关闭链须补）；两篇变更文档 issue_id 当日序号未递增（规范瑕疵，已登记）。

## 接续入口

任务全部闭合，无接续工作。若人类反馈手感类问题，以新 change-id 开启变更（严禁回溯修改本冻结 spec）。

## 终态处理

**吸收完毕**（2026-10-02）：fix-local-mode-and-pet-ux 全部 Task 0-6 闭合、人类批准交付。本 note 可在下一个变更启动时归档/精简，交接信息已被 spec 三件套与 .trae/documents/ 完整承载。

---

## 追加变更：品牌视觉统一向导风（20261002_模块0_品牌视觉统一向导风）

**做到哪了**：已完成并验证。应用内 BrandMark 品牌徽章（顶栏/侧边栏/向导头部统一）、发送按钮胶囊统一、应用图标（make_icon.py 合成 PNG+ico；BrowserWindow 窗口图标 + build.py 打包后 rcedit 嵌 exe 图标）全链路打通。

**为什么**：用户反馈「logo 和整体风格不满意，改成向导那样的风格」；经确认范围=布局不动、视觉皮肤+应用内外 logo 全做（Q1 选 2 / Q2 两项都要）。

**未闭合项**：无。

**接续入口**：无。详情见 `.trae/documents/20261002_模块0_品牌视觉统一向导风.md`（含 rcedit 具名导出、text_to_image 占位图等教训）。

---

## 追加变更：向导本地优先流程（20261003_模块0_向导本地优先流程）

**做到哪了**：**全部闭合，人类已批准交付**（2026-10-03）。Task 0-3 完成：实现+单测 87/87+便携根重打+打包态 e2e 2 passed（快车道真机验证）+GN-004 交付前审查警示放行[Agent 005670d8]+人类裁决批准。排查证据链：已装副本 asar 含全部新 UI 字符串且引用最新渲染包——「四项修复无效」根因为向导流程设计，本变更已修复。

**为什么**：用户实测三个抱怨（推荐后仍被要求选完/本地路线仍强制云端步骤/开本地仍要填云端）；方案=快车道+云端可跳过+云端显式入口+提交契约放宽（cloud 段省略，零后端改动）。

**未闭合项**：无。备忘（GN-004 O5）：云端填钥匙后点「跳过，先用本地」→ 确认页钥匙行显示与提交不一致（跳过语义明确，影响极小），未来向导迭代时让钥匙行随 cloudSkipped 联动。

**接续入口**：无（任务闭合）。提醒：本次只重打了便携根，**安装程序尚未含本变更**——用户需要时执行 `python installer/build.py` 重出 Setup exe。

---

## 追加变更：管理面 CX-A 管理 CX-O（establish-fleet-admin-plane，20261004）

**做到哪了**：spec 三件套定稿（GN-004 T1/T2 两轮计划审查 [Agent f4241310 / 2ce0525b]，SB-1/SB-2/SB-3 人类裁决：health 过闸 / 错误码原样透传 / 注册体按源码实证；人类批准 spec）。Task 1-3 实现闭合：`lite/management/fleet.py`（台账/注册/补录/透传）+ remote.py 向后兼容扩展（headers 注入 + RemoteError.status_code）+ api_server `/api/fleet/*` 与 `/api/admin/register`（令牌闸豁免 + 回环校验）+ 25 单测 + 18 契约用例 + README/CX-O 文档 §7。回归：pytest 241 passed、vitest 101/101、tsc 0、双 e2e 6+2。真实冒烟 SMOKE OK（注册上门 → 补录 → api_token.json 令牌透传 manifest，fleet.json 留痕保留）。

**为什么**：用户明确「管理面应该是 CX-A 管理 CX-O 的」。CX-O 侧已有完整控制平面（manifest/status/control/batch/audit + cx_a_endpoint 主动注册），CX-A 侧仅单实例无鉴权遥控；本变更补齐「注册接收 → 实例台账（data/fleet.json，token 脱敏）→ Bearer 透传（错误码原样透传）」闭环，管理 Agent 经 logs/api_token.json 调 /api/fleet/*。

**未闭合项**：Task 4.1 已闭合（GN-004 收口审查 [Agent 3d4b409e] 警示放行，F1-F4 修正已完成：F3 端到端 403 用例、F4 冒烟留痕、F1/F2 文档回填）；Task 4.2（人类裁决 + 重跑打包）进行中。实现期修订「补录分支」（注册实例无 token，同 base_url 手动登记即补录）已记入 spec 并经审查认可。

**接续入口**：Task 4.2 人类裁决 → `python installer/build.py` 重打包（合并管理面 + 去伴侣化 + 推荐免选 + 令牌落盘 + 人设迁移）→ 变更文档第五章收尾。

---

## 新变更：管理面前端·隐藏入口（add-fleet-frontend-hidden，20261004）

**做到哪了**：spec 三件套定稿（GN-004 T1 计划审查警示放行 [Agent 3253e959]，D-1/D-2 修正、建议 1-5 吸收；人类批准）。Task 1-4.2 实现闭合：api.ts 6 封装 + App 路由 'fleet' + Sidebar 连点 5 次解锁（3 秒窗口）+ FleetPage（台账脱敏/登记补录/注销两段确认/健康探测/能力清单/治理面板矩阵级联）+ 单测 21 项。回归：vitest 122/122、tsc 0、build 通过、app.e2e 7 passed（含场景7 连点进入+降级卡）、pet-vrm 2 passed。Task 4.3 GN-004 收口审查**通过** [Agent 6246fb40]（观察项 O-1~O-4，无阻断）。

**为什么**：用户「管理面用的那部分前端也搞一下（入口藏深一点，普通用户不需要）」；裁决：连点侧栏 logo 5 次解锁 + 含治理操作面板。解除上一 spec「前端管理界面 out of scope」边界（仅此一条）。

**未闭合项**：无阻塞性未闭合项。Task 4.4 已闭合（人类裁决「批准并重打包」；CX-A-Setup-0.1.0.exe 15:37 产出，含管理面前端 + 管理面 API + 此前全部改动）。观察项登记：O-1 补录信号取 POST 响应体（语义等价，备查）；O-4 build chunk 警告既有问题备忘。「当前不可判定」（移交人类日常使用）：真机连点手感；真实 CX-O 实例的治理端到端。

**接续入口**：任务全部闭合，无接续工作。若连点手感或管理面页面体验需调整，以新 change-id 开启变更。

---

## 追加变更：本地模型全换 Gemma 4 + 悬浮窗语音/视觉/授权闭环 + 对话持久化（20261004_模块0_本地模型换装Gemma4与悬浮窗语音闭环）

**做到哪了**：Task 1-5、7-10 已闭合（主线程内联执行，无 subagent）：①MODEL_TIERS 四档全换 Gemma 4（E2B-Q4/Q6、E4B-Q4/Q6，HF+魔塔双站实测，mmproj 双文件下载，DEFAULT_TIER=E2B-Q4）；②llama-server --mmproj 同目录自动挂载 + CHAT_SERVER_N_CTX=8192 + 多模态数组 content 透传；③视觉链路本地优先（screen_backend 纯标准库 PNG base64 截图 → sampler 剧变事件 image_b64 → pipeline local_understanding 回调，云端维持灰度拓扑隐私红线）；④对话持久化 data/chat_history.json（cap200 原子写，占位文案不落盘）+ GET /api/chat/history + vision 回调装配；⑤前端六项悬浮窗菜单（说话=ASR 开关→录音→识别→直发→流式朗读+pushMood+chatTick；屏幕共享 vision.enabled 热开关；操作授权 confirm+authorize）+ ChatPage 历史加载/chatTick 刷新 + pushMood 改造。验证：后端全量 pytest 1450 passed/1 skipped；前端 vitest 141 passed、tsc 0、build 通过、双 e2e 7+2 全绿。

**为什么**：用户指出「说话应是语音链路 ASR 输入开关（不应是静音）」，并要求补屏幕共享、操作授权、测试操作功能、本地模型视觉；裁决全换 Gemma 4（原生多模态）、一次做完、含历史持久化、本地视觉=本地多模态真图片理解。

**未闭合项**：
- ~~Task 6 / Task 11~~：均已闭合（2026-10-04 23:20 升级完成、23:30 打包完成）。
- 真机待验 5 项（当前不可判定，移交人类日常使用验收）：Gemma 4 下载与本地聊天；开屏幕共享问「我屏幕上是什么」；悬浮窗说话语音回复；授权后电脑控制；跨窗口历史同步。
- GN-004 交付前审查（Agent 1775b23a）：**警示放行**（无阻断无 SOFT_BLOCK）；两项警示（F-1 note 状态滞后 / F-2 Setup 体积数字失真）已在人类裁决前修正。遗留观察：全量测试未全量复跑（抽样一致采信）；release/ 目录历史调试残留（移交 s0602 技术债扫描）。

**接续入口**：无（人类 [V] 裁决「批准交付」，2026-10-04）。接续仅剩真机体验反馈——新问题开新变更（严禁回溯修改本变更文档）。

**终态处理**：**吸收完毕**（2026-10-04）：全 Task 闭合 + GN-004 警示放行（警示已修正）+ 人类批准交付。

---

## 追加变更：CXFC 与 ACP 从 CX-O 全量迁移（20261017_模块0_迁移CXFC与ACP全量对齐）

**做到哪了**：7 批次全部闭合 + GN-004 交付前审查收口（警示放行 [Agent 03f8e53e]，发现项 D1/D2/O1-O4 全部处理）。交付：①ACP 节点互联全量（wire 契约冻结、UDP 发现 9999、JSON 持久化、投递双轨、心跳清扫、REST 9+3 端点含开放接收面、自动回复经桌宠聊天管线）②CXFC 全量（TOFU direct 注册/跨机调用链 X-API-Key/UDP 发现双端口收发 9996+9997/心跳巡检/持久化）③前端节点互联页（#/nodes，FleetPage 入口）+ 设置页两开关 ④远端插件进桌宠工具表（BuiltinToolRegistry.refresh_remote_tools 懒刷新接线）。验证：后端 1634 passed/1 skipped、前端 291 passed、tsc 0（GN-004 独立复跑一致）。

**为什么**：用户裁决链——尽量 1:1 全量对齐 CX-O；CX-A 单角色（ACP=节点互联，不做本机多角色 CRUD）；前端=节点互联视图；CXFC 要能调远端插件；互通限局域网；开关默认关（关闭零网络行为）。

**未闭合项**：真机互通实测（打包后移交人类：CX-A⇄CX-O 互发消息/跨机调插件需填 cxfc.remote_admin_key）。其余全部闭合。

**接续入口**：无代码接续。真机实测问题开新变更；「远端插件自主调用进聊天决策环」已具备工具表基础，若需强化以新变更立项。

**终态处理**：待人类真机实测确认后归档。

## 追加变更：真机语音视觉测试（20261018_模块0_真机语音视觉测试）

**做到哪了**：安装版真机测试完成——T1 语音链路（虚拟线喂音→VAD→ASR 逐字正确→LLM→落盘）、T2 视觉语音融合（语音请求带图，回复描述屏幕内容）、T4 节点页 Issue1 错误提示（cxfc_disabled 503 用户可见）、管理面隐藏入口（连点 5 次→#/fleet→#/nodes）全部通过；T3 主动视觉/T5 跨机调用因环境不具备跳过（原因见文档）。

**为什么**：验证本批交付（视觉语音融合 + CXFC/ACP 迁移 + Issue1 修复）在真实安装环境的端到端行为；测试全程 OBS 录像（Videos/2026-10-18 11-56-16.mp4，801.9MB）。

**未闭合项**：①melo TTS 合成塌陷再实证（tmp_t1.wav 包络平坦识别乱码，换 SAPI 对照音频后全链路正常——链路无缺陷，塌陷为引擎既有问题建议单独立项）②T5 跨机互通仍待第二台机器。

**接续入口**：无代码接续。塌陷问题若立项，从「melo decoder 对特定文本近静音」既有教训接入。

**终态处理**：测试记录已归档 .trae/documents/20261018_模块0_真机语音视觉测试.md；音频设备已恢复（输入=麦克风/输出=耳机 TX 301），临时文件已清理。

## 追加变更：替换默认 TTS 模型（20261031_模块0_替换默认TTS模型）

**做到哪了**：默认音色 cx-open 换装 auto_DS-008 训练产物（G_727.pth 594MB + config.json）——零代码改动，纯资产落位 `release/_install_voice_20260930/data/voices/cx-open/`（权重按合成链约定改名 ckpt.txt，MD5 校验一致）。验证全过：voices 列表识别（622853288 bytes）、合成波形健康（peak 0.597/rms 0.1009/包络明显音节起伏，对照旧官方权重塌陷特征 peak 0.104/包络平坦）、回环 ASR 逐字识别回原文。

**为什么**：20261018 真机测试登记的「melo TTS 合成塌陷」未闭合项，实证塌陷根因在官方默认权重，用户提交新训练模型替换。

**未闭合项**：无（塌陷项就此关闭）。

**接续入口**：无代码接续。开发目录 data/voices 如需同换按文档第五章方式落位；回滚=删除 cx-open 目录即回退官方默认。

**终态处理**：文档已回填最终结果并标记已完成；CX-A 已重启运行（新 token 落盘 logs/api_token.json）。

**打包分发追加（同日）**：新模型已进安装程序——installer/bundled/melotts/（config.json + ckpt.txt）+ manifest size_estimate 更新（160MB→594MB），重打包（复用前端/壳/后端产物）产出 Setup 2810.0MB。全新安装即内置新默认音色（不再联网下载官方权重）；重装对已有 cx-open 数据目录按 HP1 跳过覆盖。此前 30.1MB 残留 Setup 已被本次产物覆盖。

## 追加变更：并行 VAD 排除全双工错误判停（add-parallel-vad-stop-guard，20261106）

**做到哪了**：spec 三件套（GN-004 两轮审查：首轮阻断→修正→复审警示放行，S1 已采纳）→ 实施完成。改动：①audioRecorder.ts EnergyVad 静音态持续累计（speech-end 不归零，跨态连续）+ 只读 silenceSec + ContinuousRecordingSession.getSilenceSec()；②voiceSession.ts duplex SEND 确认闸门（passSendConfirmGate：静音 ≥ confirmSilenceSec 默认 1.0s 立即发送；未达标挂起 + 补足确认定时器到点发送；开口即清 confirmTimer；闸门降级不计 pendingTurnCount；stop/打断清定时器）；③单测 audiorecorder 6 新例 + voicesession 11 新例。Task1/2 并行双 subagent（agent 3ca367d0 / 86aa8c78）按文件所有权分区零撕裂。

**为什么**：duplex 判停链 SEND 放行不确认用户真的停了——换气/犹豫停顿叠加 ASR 自动补句号或 turnCheck 误判时半截话抢发；并行慢速 VAD 判停通道（宁迟勿断）排除误切，未注入 getSilenceSec 直通保证既有行为零回归。

**未闭合项**：真机听感验证（确认延迟 ≤0.55s 是否可感知、换气场景实际体验）留用户日常使用反馈；确认阈值调参如需暴露到设置页另立变更。

**接续入口**：无代码接续。调参入口 = VoiceSessionDeps.confirmSilenceSec（默认 1.0、≤0 直通、>10 clamp 10）；打包分发时按既有 build.py 流程即可（前端产物已更新需全量重打包）。

**终态处理**：留痕 .trae/documents/20261106_模块0_并行VAD排除全双工错误判停.md（status=已完成）；spec 三件套 .trae/specs/add-parallel-vad-stop-guard/（台账 agent id 已回填）；验证 vitest 310/310 + tsc 0。

## 追加变更：撤回窗口延伸至 TTS 出声（20261106_模块0_撤回窗口延伸至TTS出声）

**做到哪了**：用户对并行 VAD 闸门的关键修正——确认静音的语义终点是「TTS 出声」而非「SEND 放行」。voiceSession.ts：①handleLevel 打断检测窗口扩展（播放期高门限 + 未出声在途期正常门限双窗口）②interrupt 双模式分流——出声前（LLM 计算/TTS 合成中）用户开口 = **撤回**（abort 在途流式请求，后端断开不落盘=判定作废；inflightSentence + queue 剩余句文本回合并池；不清采集缓冲保用户在说的话）+ 2s 兜底防沉默；出声后回落现状打断（停播丢文本清缓冲）。单测 4 新例（撤回合并/兜底/门限区分/出声后现状）。

**为什么**：闸门只延迟 SEND 到静音 1.0s，SEND 后 LLM 计算与 TTS 合成期间用户继续说话仍会被抢话（processing 期无打断检测）——「宁迟勿断」要贯彻到出声那一刻；撤回复用后端「断开不落盘」既有语义，零后端改动。

**未闭合项**：真机听感验证（撤回后重发节奏、无自触发误撤回）留日常使用反馈。

**接续入口**：无代码接续。撤回窗口门限 = deps.vad.speechThreshold（未出声期）/ deps.interrupt.threshold（播放期）；打包时前端产物已更新需全量重打包。

**终态处理**：留痕 .trae/documents/20261106_模块0_撤回窗口延伸至TTS出声.md（status=已完成，含排障记录：合并重发断言口径=按句非整段）；验证 vitest 314/314 + tsc 0。

## 追加变更：出声阻断 VAD 与 CONTINUE 不兜底（20261107_模块0_出声阻断VAD与CONTINUE不兜底）

**做到哪了**：用户两点语义裁决落地。①**出声阻断**：TTS 出声后（activeSpeaks>0）handleLevel 直接 return——播放期打断语义（20261006 冻结）被推翻，说完比撤回好；speaking 态 utterance 丢弃扩展到 duplex（AI 回采切段不进识别）；interrupt 简化纯撤回。②**CONTINUE 不兜底**：turnCheck=CONTINUE（话轮被打断/未说完）移除 2s 兜底强发（连带删除 handleLevel 开口重启 pendingTurnTimer 的 else 分支），出口=用户后续语音 SEND 合并或 count≥2 强制 SEND（第 3 段语音触发）。撤回窗口（未出声）与撤回回池 2s 兜底保持不变。

**为什么**：出声后回采误判风险 + 打断作废对话轮的体验差于让 AI 说完；CONTINUE 兜底强发违背 LLM「未说完」判定本身，是抢话源。

**未闭合项**：真机听感（CONTINUE 静默悬挂的体感——用户说完 AI 不应答需再说一句解锁；AI 播放期不可插话）留日常使用反馈。

**接续入口**：无代码接续。播放期高门限 interruptThreshold 已无消费方（仅 deps 兼容保留）；打包时前端产物需全量重打包。

**终态处理**：留痕 .trae/documents/20261107_模块0_出声阻断VAD与CONTINUE不兜底.md（status=已完成，含语义变化对照表）；验证 vitest 313/313 + tsc 0。

## 追加变更：话轮判定四值语义扩展（20261107_模块0_话轮判定四值语义扩展）

**做到哪了**：turn_check 三值扩为五枚举（用户四值裁决 + 保留 IGNORE）：CONTINUE 继续等待 / **INTERRUPT**（新）LLM 打断人——AI 插话，挂起文本+本段立即发送（撤回窗口照常，用户强势可自然撤回）/ SEND 说完回复 / **SILENT**（新）说完不回复——清空挂起池整轮作废 AI 沉默 / IGNORE 无意义音节——行为修正为「丢弃本段、池子保留挂起不兜底」（原 2s 兜底发池子与 CONTINUE 语义冲突）。后端 judge_text prompt 五枚举 + 解析抽模块级纯函数 `_parse_turn_verdict`（安全侧排序，参数化 10 单测）；前端 api.ts VoiceTurnVerdict + voiceSession resolveTurn 透传五值 + 分支处理。

**为什么**：三值枚举缺「AI 主动插话」与「说完无需回复」两个判定出口，LLM 只能在 SEND（抢话）与 CONTINUE（悬挂）间二选一；IGNORE 兜底发池子与「宁迟勿断」裁决不一致。

**未闭合项**：真机听感——INTERRUPT 插话的 LLM 判定准确率依赖主模型对五枚举 prompt 的遵循度（Gemma 4 单词输出温度 0），留日常使用观察。

**接续入口**：无代码接续。解析链 = lite/server/api_server.py `_parse_turn_verdict`（安全侧排序）；prompt 文案在 `_handle_voice_turn_check` 的 judge_text。打包时后端+前端均需重打包（非复用）。

**终态处理**：留痕 .trae/documents/20261107_模块0_话轮判定四值语义扩展.md（status=已完成，含五值行为对照表与快筛短路排障记录）；验证后端 213 passed + 前端 316/316 + tsc 0。

**精修追加（同日）**：①SILENT 保留挂起池（与 IGNORE 同构，仅本段沉默——后续 SEND 合并发出，上下文不丢）②INTERRUPT 加 300ms 人类不可打断窗（interruptGuardUntilTs，窗内撤回抑制给人反应停嘴时间，窗后持续说话=否决插话）。验证 vitest 317/317 + tsc 0；详见留痕文档第五章。

**长静音兜底追加（同日三裁决）**：CONTINUE 误判悬挂用**长窗口 VAD 兜底**解决——scheduleContinueFallback：挂起后静音持续达 continueSilenceSec（默认 3.0s，≤0 禁用 >30 clamp，远大于确认闸门 1.0s）照发挂起池；新语音清 timer 重判（不违背 LLM「未说完」判定，与已移除的固定 2s 盲等本质不同=VAD 证据驱动）；仅 CONTINUE 分支调度（SILENT/IGNORE 池子定位「等后续 SEND 合并」不调度）；撤回回池 2s 兜底与 count≥2 保留。验证 vitest 320/320 + tsc 0；详见留痕文档第六章。

---

## 新变更：发布前准备（prepare-public-release，20261121）

**做到哪了**：Spec 三件套已写完并获人类批准；实现已推进到 **Task 6（全功能验收）进行中**。已完成：Task 0 变更留痕；Task 1 MIT/许可元数据 + 安装器 LicenseFile + `PET_MODEL_LICENSE.md`/VRM 元数据按用户裁决更新；Task 2 `THIRD_PARTY_NOTICES.md`；Task 3 `RELEASE_NOTES.md` + README 事实修正；Task 4 后端 1646 passed / 前端 321 passed / E2E 9 passed；Task 5 两次重建（最终 `CX-A-Setup-0.1.0.exe` 2,946,518,946B @23:11:48），打包态首启 E2E 2 passed。期间修复 3 个缺陷：衰减统计 permanent 分桶、pet-vrm 过时按钮名「说话」→「麦克风」、首启向导后端慢启动竞态（App 门控 10 秒有限重试）。

**为什么**：用户要求发布前准备——MIT 协议、整理卖点、确保所有功能跑通；用户明确「全量测试不等于所有功能跑通并且符合预期」，故设计 F01–F14 真实功能验收矩阵；卖点产出形式选「独立发布说明」；第三方许可说明选「需要」。旧 Setup 为 2026-10-31，HEAD 为 2026-11-07，已重建。

**GN-004 审查记录**：首轮 f36603a0（警示放行/含 SOFT_BLOCK，已按人类裁决全部修正）；第二轮 5d560b47（警示放行）；第三轮 abbe3e4f（警示放行，F06 `decision`/兜底假通过口/F03 口径/F09 证据路径已修正）。三轮均无阻断。

**未闭合项（2026-11-22 14:00 更新）**：
- **F01-F13 已全部通过**：F02/F07/F12/F13 + F03/F04/F05/F06 的 API 证据；F01 最终构建 packaged E2E 2 passed；F08/F09/F10/F11/F12 UI/视觉/键鼠截图与实测；F10 电脑控制屏幕/键鼠后端已实现并通过打包态 API 验收（`logs/api_keyboard_final.log`）。
- **F14 已通过**（最终产物 2,946,528,675 B @ 2026-11-22 13:22:07）：全新安装到 `release/installed_release2`，`data/voices/cx-open` 的 `config.json`+`ckpt.txt` 落位；`dependencies_installed=true`、`tts_warmed=true`、`errors=[]`；安装版 TTS 200（peak 0.6816/rms 0.1215/1.89s）+ ASR 200 逐字一致；卸载 `Removed all? Yes`。证据 `logs/f14_evidence/`。
- 人类「要求修正」已落地：torch 官方源失败 → 清华镜像 CPU 回退（两次全新安装均实际触发并成功）；节点页 503 技术错误条收口；不可判定项保持不动。
- 复验另暴露并修复两处安装链缺陷：Inno `Excludes` 递归误伤音色 `config.json`；`setuptools>=81` 移除 `pkg_resources` 导致 librosa 导入失败（已钉 `<81`）。
- `release/portable` 验收 junction 与验收 config 已清理，重建后无 junction；安装包 SHA256 `5341F0F8...94CE`。
- 跨机 CXFC、真实麦克风手感、主观听感、中文 IME 直输仍为「当前不可判定」，移交人类日常使用。

**F14 结论（2026-11-22 14:00，最终复验）**：安装/核心文件落位/安装版启动/运行期依赖装配/TTS/ASR/卸载全部通过；`dependencies_installed=true`、`tts_warmed=true`、`errors=[]`；原始证据 `logs/f14_evidence/`。F01-F14 全部通过，Task 6 闭合。

**GN-004 交付审查记录**：
- 第一轮 c573c017：警示放行，含 SOFT_BLOCK SB-B（F14 证据随临时目录删除、卸载无独立日志）。已按人类裁决「重跑并留原始证据」重装/卸载，证据落 `logs/f14_evidence/`。
- 第二轮 bfabfa5e：**警示放行，无阻断、无 SOFT_BLOCK**。O1 状态滞后与 O3/O4/O5 证据簿记已修正；O7 portable junction 已移除；O2 文档未提交待人类决定；O6 运行期装配网络限制须人类知情接受。
- 第三轮（修正后复审）d8402d35-1a1f-4ec4-b6dd-2da7217ab56b：**警示放行，无阻断、无 SOFT_BLOCK**。W1 spec 交接段、W2 checklist 产物数字、W3 变更文档 status、W4 全量回归日志（1655 passed / 1 skipped，`logs/pytest_release_final.log`）均已修正；O1/O2/O5/O7/O8 已处理；复审新增 R1（`.prev.exe` 哈希/后端构建时点）与 R2（交接措辞）亦已修正；O3/O4/O6/O9 登记为观察项。待人类 [V] 最终裁决。

**接续入口**：发布前准备已收口——最终产物 `release\CX-A-Setup-0.1.0.exe`（2,946,528,675 B @13:22:07，SHA256 `5341F0F8...94CE`），代码/测试/发布文档已提交 **4271489（未推送）**。对外发布动作由人类执行；如需继续修正，开新 change-id，不得回溯修改本冻结 spec。
