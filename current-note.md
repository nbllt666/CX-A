# current-note — fix-local-mode-and-pet-ux

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
