# -*- coding: utf-8 -*-
"""轻量后端 REST API 服务（A9）——基于标准库 http.server 实现，无第三方重型框架依赖。

面向前端 MemoriesPage 提供记忆浏览与管理接口：
    GET    /api/health              健康检查 -> {"status": "ok"}
    GET    /api/memories            记忆列表（?type= &agent_id= &limit=）
    GET    /api/memories/search     记忆检索（?q= &agent_id= &top_k=，走 MemoryRetrievalPipeline）
    DELETE /api/memories/{id}       软删除一条记忆

记忆页完整控制与核心能力（20261005，spec: align-wizard-settings-memory-pet）：
    POST   /api/memories            新建记忆（body {content, memory_type?, importance?,
                                    tags?, agent_id?}；memory_type 四值域
                                    long_term/short_term/permanent/diary；非法 400 中文）
    PUT    /api/memories/{id}       编辑记忆（字段补丁，走 MemoryStore.update；404/400）
    POST   /api/memories/batch-delete  批量软删（body {ids:[]}；permanent 保护跳过，
                                    返回 deleted_count 与 skipped 明细）
    GET    /api/memories/decay-stats  遗忘衰减统计（CX-O 遗忘曲线口径；permanent 豁免；
                                    含即将遗忘/已衰减分布）
    POST   /api/memories/sync-decay   执行衰减同步（低分记忆软删归档；permanent 豁免）
    GET    /api/memories/diary      日记视图（?date= &type= &agent_id= &limit=；按
                                    created_at 本地日期分组，日期降序）
    GET    /api/memories/3d         三维加权检索（?query= &w_importance= &w_time= &w_rel=
                                    &type= &agent_id= &limit=；默认权重 0.35/0.25/0.4）

记忆管理助手工具环（20261005）：POST /api/chat/message 命中内置 agent memory-agent 时，
    解析回复中的 [memory:op {...}] 指令标签（对齐 [emotion:x] 自造协议先例），经
    BuiltinToolRegistry 记忆工具（memory_search/read/write/update/delete）执行并把
    结果回注一轮（最多 2 轮），最终 clean_text 剥离全部指令标签；工具失败以中文
    说明原因（不崩、不污染记忆库）。

电脑控制（Task D3，走 ToolBridge 全链路）：
    GET    /api/computer/status     授权状态（authorized / confirm_dangerous）
    POST   /api/computer/authorize  开启/撤销授权（body {enabled: bool}）
    POST   /api/computer/call       执行工具调用（body {tool, arguments}；未授权 403）

配置与管理 API（记录在 `.trae/documents/20260826_模块0_差异审查登记与处理计划.md`）：
    GET    /api/status              轻量系统状态（app / version / uptime）
    GET    /api/settings            用户可读配置视图（api_key 脱敏回显 sk-****尾4位，明文绝不外泄）
    PUT    /api/settings            更新可热更配置（白名单键；body 为补丁；download.channel
                                    合法时派生写入 local_llm.source，与向导同口径）
    POST   /api/chat/messages       聊天发送（未启用守卫：明确提示走前端 Mock）
    GET    /api/chat/history        聊天历史（20261004 持久化：data/chat_history.json 最近 200 条）

生产装配接线 API（批次E，记录在 `.trae/documents/20260828_模块0_生产装配接线.md`）：
    GET    /api/tools               内置工具清单（含 usage 端点用法自述）
    POST   /api/tools/call          调用内置工具（body {name, arguments}；未授权 403 / 未知工具 404）
    POST   /api/memory/distill      记忆蒸馏（body {messages, agent_id?}；未配置云端 400 / 云端离线 503）
    POST   /api/voice/synthesize    文本合成语音（body {text, voice?}；后端异常 503）
    POST   /api/voice/transcribe    语音转文本（body {audio_base64, sample_rate?}；后端异常 503）

音色管理（Task B「音色自由选」，20261002）：
    GET    /api/voices              内置默认音色 + data/voices 目录音色包合并列表
                                    （{ok, voices:[{id, path, size, is_default, builtin}]}；
                                    目录同名 cx-open 与内置项合并去重，保留目录项 size/path）
    POST   /api/voices/import       导入本机音色包目录到 data/voices/<name>/
                                    （body {source_path, name?, overwrite?}；校验链任一失败
                                    400 中文 message 不产生写入副作用；name 过
                                    is_unsafe_voice_id 防目标目录逃逸）

主动视觉装配（Task C「主动视觉接线」，20261002）：
    服务启动时经 make_handler 装配 VisionPipeline（纯标准库 GDI 屏幕后端 +
    AdaptiveSampler + 云端理解 + 记忆沉淀），daemon tick 线程每拍驱动
    ``run_once``（整体 try/except，线程永不因异常退出）。``vision.enabled``
    默认 False：run_once 零开销返回且绝不采样（隐私红线）；经 PUT /api/settings
    的 ``vision.enabled``（布尔校验，非法入 ignored）热更新开启，下一拍即生效。
    音色热切换：``/api/voice/synthesize`` 与 ``/api/voice/synthesize_stream``
    未显式指定音色时现读 config ``tts.voice``（设置页改默认音色即生效，无需重启）。

CXFC relay 前端转接（Task H1，对齐 C:\\CX-O\\docs\\CXFC开发文档.md §2.2）：
    GET    /api/cxfc/relay/pending  取走待执行 relay 调用（?plugin_id= 过滤 &limit= 上限；
                                    at-most-once：取走即从队列移除，未回报最终 RELAY_TIMEOUT）
    POST   /api/cxfc/relay/result   前端回报一次 relay 调用结果（body {request_id, plugin_id,
                                    success, result|error}；命中 {"status":"ok"}；
                                    未知/已超时 request_id 404）

表情聊天（Task H3，对齐 spec「虚拟形象表情优化」；本地模式真正本地，20261002）：
    POST   /api/chat/message        表情聊天（body {message, agent_id?}）：本地模式
                                    （local_llm.enabled）开启时**本地优先**——不探测
                                    网络、不连云端，就绪走本地小 LLM、未就绪产中文
                                    引导提示（绝不静默回落云端）；未开启时走 CloudAdapter
                                    流式调用拼接完整回复，经 EmotionTagParser 解析标签后
                                    返回 {ok, clean_text, mood, raw}；无 api_key / 云端
                                    不可达时返回固定友好文案 + mood=calm（offline:true），
                                    不抛 5xx；agent_id 命中本地 Agent 时以其 persona
                                    作为 system 人设。既有 /api/chat/messages（复数，
                                    未启用守卫）保持不变。

悬浮桌宠模型分发（20260924_模块0_接入VRM悬浮桌宠）：
    GET    /api/pet/model           分发悬浮桌宠默认 VRM 模型原始字节（200，
                                    Content-Type: model/gltf-binary；文件缺失
                                    404 pet_model_missing）。路径经 app_root()
                                    （frozen-aware）解析为 <app_root>/data/pet/cx-open.vrm。
                                    过既有 Host / 令牌闸（不新增豁免端点），仅 GET。
    POST   /api/pet/model/import    导入用户自选 VRM 模型替换默认模型（Task 4；
                                    body {source_path}；原模型自动备份 .bak，
                                    非 .vrm/不可读 400，复制失败 500 原模型不动）
    POST   /api/pet/model/reset     从 .bak 备份还原默认模型（备份缺失 404
                                    pet_model_backup_missing）。均过既有令牌闸。

首启向导接口族（Task 6 / Task 7，对齐 spec「首启向导后端接口」「向导内下载」）：
    GET    /api/setup/status            向导门控（completed / wizard_required / 当前通道与模型仓库；不含 api_key）
    GET    /api/setup/recommend         硬件画像 + 推荐配置补丁 + 候选档位 + 建议仓库（探测异常不 5xx）
    POST   /api/setup/complete          应用向导选择（白名单 + applied/ignored 回显）并置 setup.completed；
                                        ``local_llm.source`` 由 ``download.channel`` 派生（不一致的显式值入 ignored）
    POST   /api/setup/model/download    启动本地小 LLM 后台下载（幂等：进行中返回 already_running）
    GET    /api/setup/model/progress    即时返回下载进度（后台线程执行，不阻塞服务）
    POST   /api/setup/model/cancel      取消下载（保留 .tmp 供断点续传）
    以上端点沿用既有 Host / 令牌校验，**不新增豁免端点**。

> 管理面已收敛为纯 API：前端不再路由 Agents/Remote/Status，管理能力以上述端点
> + /api/agents、/api/remote/* 外露，供另一 Agent 或管理工具调用。

端口默认 8600（与前端 frontend/src/renderer/api.ts 的 API_PORT 约定一致）。
支持命令行参数：-h/--host、-p/--port；Ctrl+C 优雅退出。
所有 JSON 响应以 UTF-8 编码且 ensure_ascii=False，中文不做转义。

线程安全说明：本服务使用单线程 HTTPServer（一次只处理一个连接/请求），
MemoryStore 的 sqlite3 连接与 MemoryRetrievalPipeline 均在主处理线程内串行使用，
不引入并发读写，故无需加锁。若要切换到并发服务器，需另行处理存储连接竞争。
relay 两端点仅做「取走队列 / 回填等待者」轻量操作：relay 调用的阻塞等待由
LiteCXFC 内部 Event 在调用方线程承载，不占用 HTTP 线程（Task H1）。
"""

import argparse
import atexit
import base64
import datetime
import hmac
import importlib.util
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

# 原生日志记录器（低-5：内部异常完整消息仅写日志，不外泄到响应体）
LOGGER = logging.getLogger(__name__)

# 聊天服务未启用守卫错误码（前端本期走 Mock 演示，端点存在但明确提示）
CHAT_SERVICE_DISABLED = "chat_service_disabled"

# 表情聊天离线兜底文案（Task H3）：无 api_key / 云端不可达时作为 clean_text 返回
CHAT_OFFLINE_TEXT = "现在连不上云端…"

# 表情聊天默认 system 提示（agent_id 未命中本地 Agent 时使用）：引导 LLM 以
# [emotion:x] 标签表达情绪，情绪集与 lite/avatar/tags.SUPPORTED_EMOTIONS 一致
_CHAT_DEFAULT_SYSTEM = (
    "你是 CX-A，回复请自然、温暖；可在句中插入情绪标签表达当下心情，"
    "格式为 [emotion:情绪]，支持：happy/calm/sad/surprised/angry/sleepy/shy。"
)

# 对话持久化（20261004 悬浮窗语音闭环）：/api/chat/message 成功后追加落盘，
# GET /api/chat/history 返回最近消息——桌宠语音对话与主窗口聊天页共享同一份
# 对话真相（跨窗口刷新经前端 localStorage ``cx-a.chatTick`` 总线驱动）。
_CHAT_HISTORY_FILENAME = "chat_history.json"
_CHAT_HISTORY_CAP = 200
_CHAT_HISTORY_LOCK = threading.Lock()


def _chat_history_path(data_dir=None):
    """对话历史文件绝对路径（缺省 ``<data>/chat_history.json``，frozen-aware 根解析）。

    :param data_dir: 数据目录；None 时经 :func:`data_root` 解析（生产默认）。
        handler 场景由 make_handler 按其数据目录传入（测试 tmp_path 隔离）。
    """
    return os.path.join(data_dir or data_root(), _CHAT_HISTORY_FILENAME)


def _load_chat_history(path=None):
    """读取对话历史（缺失 / 损坏静默回空——历史损坏不得阻断聊天主链路）。

    :param path: 历史文件绝对路径；None 用 :func:`_chat_history_path` 默认解析。
    :return: list[dict]（``{role, content, time}``；非列表结构按空处理）。
    """
    try:
        with open(path or _chat_history_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _append_chat_history(user_text, assistant_text, path=None):
    """追加一轮对话（用户原文 + clean 回复 + 时间），cap 截断 + 原子写。

    写失败仅告警不抛（历史落盘不得阻断聊天主链路）；``.tmp`` + ``os.replace``
    原子改名，避免损坏一半的历史文件落在正式位。

    :param user_text: 用户消息原文（已 strip）
    :param assistant_text: 助手回复 clean_text（已剥离情绪标签）
    :param path: 历史文件绝对路径；None 用 :func:`_chat_history_path` 默认解析。
    """
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    entries = [
        {"role": "user", "content": str(user_text), "time": now},
        {"role": "assistant", "content": str(assistant_text), "time": now},
    ]
    with _CHAT_HISTORY_LOCK:
        history = _load_chat_history(path)
        history.extend(entries)
        if len(history) > _CHAT_HISTORY_CAP:
            history = history[-_CHAT_HISTORY_CAP:]
        target = path or _chat_history_path()
        tmp = target + ".tmp"
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(history, fh, ensure_ascii=False)
            os.replace(tmp, target)
        except OSError as exc:
            LOGGER.warning("对话历史落盘失败（不阻断聊天主链路）：%s", exc)


def _offline_placeholder_texts():
    """离线占位文案集合（fallback 管理器的引导提示；延迟导入防导入环）。

    这些文案经 ``/api/chat/message`` 成功路径返回（offline:true），但**不是
    真实对话**——持久化前据此跳过，避免占位文案污染 chat_history.json。
    """
    try:
        from lite.cloud.fallback import (  # noqa: PLC0415
            CONFIG_ERROR_PROMPT,
            LOCAL_NOT_READY_PROMPT,
            OFFLINE_PROMPT,
        )
    except Exception:  # noqa: BLE001 - 导入失败按仅自身常量兜底
        return {CHAT_OFFLINE_TEXT}
    return {OFFLINE_PROMPT, LOCAL_NOT_READY_PROMPT, CONFIG_ERROR_PROMPT, CHAT_OFFLINE_TEXT}


def _warm_chat_runtime(runtime):
    """后台预热常驻 chat 服务（20260930）；失败仅告警，不抛（GN-004 E11）。

    预热失败不阻断装配——首次真实请求会兜底拉起常驻服务（只是首问多等一次
    冷加载）。此处补一次告警保留可观测性（模型损坏等场景不再静默）。

    :param runtime: 已装配的本地小 LLM 运行时（LlamaRuntime）
    """
    try:
        if not runtime.warm_local_llm():
            LOGGER.warning(
                "常驻 chat 服务预热失败（首次真实请求将兜底拉起）：%s",
                "; ".join(getattr(runtime, "warnings", None) or []) or "未知原因",
            )
    except Exception as exc:  # noqa: BLE001 - 预热线程不允许影响主流程
        LOGGER.warning("常驻 chat 服务预热线程异常：%s", exc)


def build_local_chat_runtime(config):
    """按 local_llm 配置段组装本地小 LLM 运行时（供离线聊天兜底）。

    - 未启用 / 未配置 model_path → None（不接兜底，行为同旧版）
    - 延迟导入 llama_runtime（llama-cpp-python 缺席不污染顶层导入路径）
    - 文件缺失 / 加载失败 / 依赖缺席 → None 并 LOGGER.warning（聊天端点必须可用，绝不抛错）
    """
    local_llm_cfg = {}
    if isinstance(config, dict):
        sec = config.get("local_llm")
        if isinstance(sec, dict):
            local_llm_cfg = sec
    elif config is not None:
        inner = getattr(config, "config", None)
        if isinstance(inner, dict) and isinstance(inner.get("local_llm"), dict):
            local_llm_cfg = inner["local_llm"]
    if not bool(local_llm_cfg.get("enabled", False)):
        return None
    model_path = str(local_llm_cfg.get("model_path", "") or "").strip()
    if not model_path:
        return None
    try:
        # 函数内延迟导入：llama_runtime 依赖链不进入本模块顶层导入路径
        from lite.runtime.llama_runtime import LlamaRuntime

        runtime = LlamaRuntime(
            config={
                "local_llm": {
                    "enabled": True,
                    "model_path": model_path,
                    "n_ctx": local_llm_cfg.get("n_ctx"),
                    "device": local_llm_cfg.get("device"),
                    "n_gpu_layers": local_llm_cfg.get("n_gpu_layers"),
                }
            }
        )
        if not runtime.load_local_llm(model_path):
            LOGGER.warning(
                "本地小 LLM 加载失败，离线兜底降级为提示：%s",
                "; ".join(runtime.warnings) or "未知原因",
            )
            return None
        # 优雅退出回收：常驻 chat 服务为 llama-server 子进程（20260930 常驻化改造；
        # 冻结态硬终止路径同样由 Electron 壳的进程树回收兜底，见 main.js）
        atexit.register(runtime.close)
        # 后台预热常驻 chat 服务（20260930）：规避"应用启动后第一句话"再吃一次模型
        # 冷加载（实测 8B Q4 GPU 约 5.3~5.5s）；非阻塞、失败仅告警（首次真实请求兜底拉起）
        threading.Thread(
            target=_warm_chat_runtime, args=(runtime,), name="chat-warmup", daemon=True
        ).start()
        return runtime
    except Exception as exc:  # noqa: BLE001 - 依赖缺席 / 加载异常：聊天端点必须可用
        LOGGER.warning(
            "本地小 LLM 运行时装配失败（%s）：%s", exc.__class__.__name__, exc
        )
        return None


class _LocalChatRuntimeHolder:
    """本地聊天运行时持有器（本地模式真正本地，20261002）。

    职责：让 ``OfflineFallbackManager`` 经 ``get``（零参 callable / resolver 形态）
    在每次 chat 时取得**当前**运行时，使运行中开启本地模式 / 模型后台加载完成
    无需重启服务即动态生效。

    - ``get()``：返回当前运行时或 None（锁内快照，供 chat 路径现解析）；
    - ``set_runtime()``：装配期写入启动期加载好的运行时（**启动期去重**：
      ``make_handler`` 装配时同步调用 ``build_local_chat_runtime`` 一次，其结果
      存入本持有器复用，禁止同一模型加载两份）；
    - ``ensure_started(config)``：幂等拉起后台 daemon 线程执行
      ``build_local_chat_runtime(config)``——已有运行时或已在加载中则直接返回；
      加载失败仅 LOGGER.warning（结果为 None），可再次触发。**立即返回，不阻塞
      请求线程，也不持有 ``_CONFIG_WRITE_LOCK``**（模型加载在后台线程进行）；
    - ``release()``：清空运行时；运行时具备 ``close``（LlamaRuntime.close 为
      幂等回收常驻子进程，见 llama_runtime.py）则调用之，否则置 None 由 GC 回收；
    - ``ready()``：当前运行时非 None（/api/settings 的 ``local_llm.ready`` 口径）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runtime = None
        self._loading = False
        #: 加载代次：release() 会使在途加载线程的结果失效（防止关闭后写回）
        self._generation = 0

    def get(self):
        """返回当前本地聊天运行时（None 表示未加载）。零参 callable，可直传
        ``OfflineFallbackManager(local_llm=...)`` 作 resolver。"""
        with self._lock:
            return self._runtime

    def ready(self) -> bool:
        """本地聊天运行时是否已加载（/api/settings ``local_llm.ready`` 口径）。"""
        return self.get() is not None

    def set_runtime(self, runtime) -> None:
        """写入运行时（装配期启动加载结果复用入口；None 亦允许写入）。"""
        with self._lock:
            self._runtime = runtime

    def ensure_started(self, config) -> None:
        """幂等拉起后台加载线程；立即返回，不阻塞请求线程。

        - 已有运行时 → 幂等返回（禁止同一模型加载两份）；
        - 已在加载中 → 幂等返回；
        - 否则置 loading 标记并启动 daemon 线程执行
          ``build_local_chat_runtime(config)``，结果（含失败 None）写回；
          失败仅告警，不崩，可再次触发。
        """
        with self._lock:
            if self._runtime is not None or self._loading:
                return
            self._loading = True
            self._generation += 1
            generation = self._generation

        def _load():
            try:
                runtime = build_local_chat_runtime(config)
                if runtime is None:
                    LOGGER.warning("本地聊天运行时后台加载未成功（详见装配告警），可再次触发")
            except Exception as exc:  # noqa: BLE001 - 后台加载绝不拖垮服务
                LOGGER.warning(
                    "本地聊天运行时后台加载异常（%s）：%s", exc.__class__.__name__, exc
                )
                runtime = None
            with self._lock:
                # 代次不匹配（加载期间被 release）→ 丢弃结果，不覆盖状态
                if generation != self._generation:
                    return
                # 失败（None）同样写回以清除 loading 标记；下次 PUT true 可重试
                self._runtime = runtime
                self._loading = False

        threading.Thread(target=_load, name="local-llm-load", daemon=True).start()

    def release(self) -> None:
        """释放运行时：具备 ``close``（幂等）则调用，否则置 None 由 GC 回收。

        同时递增加载代次并复位 loading 标记，使在途加载线程的结果被丢弃——
        用户关闭本地模式后，后台未完成的加载不得在关闭后悄悄生效。
        """
        with self._lock:
            runtime = self._runtime
            self._runtime = None
            self._generation += 1
            self._loading = False
        if runtime is None:
            return
        close = getattr(runtime, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:  # noqa: BLE001 - 释放失败不抛（进程随系统回收）
                LOGGER.warning("本地聊天运行时释放异常（%s）：%s", exc.__class__.__name__, exc)


# ------------------------------------------------------------------ 启动令牌鉴权（N1）
# 环境变量 CXA_API_TOKEN 非空时，除 OPTIONS 预检与 GET /api/health 外的所有请求
# 必须携带匹配的 X-Client-Token 头（常量时间比较），否则回 403 unauthorized_client。
# Electron 生产态由 main.js 启动时生成随机令牌经 spawn env 注入本进程；env 未设置
# （纯浏览器 dev / 测试态）保持开放模式并告警一次。
_API_TOKEN = os.environ.get("CXA_API_TOKEN", "").strip()
#: 开放模式告警是否已发出（仅告警一次，避免刷屏）
_TOKEN_OPEN_MODE_WARNED = False

# 请求体大小上限（N6）：1MB，超出直接 413，防超大 body 阻塞单线程服务
_MAX_BODY_BYTES = 1048576

# 单次蒸馏请求的 messages 条数上限（H-5，第三轮体检批次2）：
# 超限 400——防单请求串行发起数百次云端 LLM 调用阻塞单线程服务
_MAX_DISTILL_MESSAGES = 200

# 语音合成文本长度上限（字符）（M-5）：超限 400——防超长文本分钟级合成阻塞服务
_MAX_SYNTH_TEXT_CHARS = 5000

# 记忆列表 limit / 检索 top_k 的允许上限（M-6）：负数 400、超上限钳制——
# 防 LIMIT -1 全表返回与超大整数触发 sqlite OverflowError
_MAX_LIST_LIMIT = 1000

# agent_id 最大长度（L-5）：入库字段限长，防任意长字符串写库
_MAX_AGENT_ID_CHARS = 100

# 记忆 id 合法范围（低-6，第四轮体检批次B）：SQLite INTEGER 为 64 位有符号，
# 范围外的 id 直接入库会触发 sqlite OverflowError → 500，边界处显式 400
_INT64_MIN = -(2 ** 63)
_INT64_MAX = 2 ** 63 - 1

# CXFC relay pending 单次默认取走条数（Task H1）：可用 ?limit= 覆盖（上限钳制）
_RELAY_PENDING_DEFAULT_LIMIT = 50

# 回环监听地址集合（中-4 启动安全闸判定口径，第四轮体检批次B）
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

# ------------------------------------------------------------------ 配置写入锁
# 配置落盘串行化（Task 7「并发配置写入串行化」）：本进程内**所有** ``config.save()``
# 调用点（请求线程的 /api/settings、/api/setup/complete，以及下载线程的
# local_llm.model_path 写回）必须共用这一把模块级写锁。归属声明：本模块持有锁对象，
# 下载管理器经 ``ModelDownloadManager(write_lock=_CONFIG_WRITE_LOCK)`` 注入**同一把**
# ——请求线程与下载线程共用，防止并发落盘导致 config.json 字段丢失 / 截断。
_CONFIG_WRITE_LOCK = threading.Lock()

# 语音引擎预热防重入（20261010_模块0_语音交互延迟优化）：/api/voice/warmup 的
# 后台预热线程进行中标志。预热尽力而为（失败仅记日志），引擎已热时重复调用
# 代价为一次微合成热路径，无需持久状态；防重入只为避免开关麦克风密集触发时
# 排队多个预热线程。
_VOICE_WARMUP_LOCK = threading.Lock()
_VOICE_WARMUP_INFLIGHT = False


def _is_loopback_host(host) -> bool:
    """判断监听地址是否为本机回环地址（127.0.0.1 / localhost / ::1）。

    兼容 IPv6 字面量的方括号形态（[::1]）与大小写混写。
    """
    return str(host or "").strip().strip("[]").lower() in _LOOPBACK_HOSTS


def _env_api_token() -> str:
    """实时读取启动令牌 env（main 安全闸判定用，不依赖 import 期快照）。"""
    return os.environ.get("CXA_API_TOKEN", "").strip()


class _BodyTooLarge(Exception):
    """请求体超过 ``_MAX_BODY_BYTES`` 的内部信号（413 响应已由 _read_body_json 发出）。"""


# settings PUT 已知只读顶层键：GET 视图可见但不在 PUT 白名单的 section
# （收到时收集进 ignored 数组回显，消除静默丢弃——L2 收口）
_SETTINGS_READONLY_TOP_KEYS = ("acp", "remote", "vector")


def _mask_api_key(value):
    """云端 API Key 脱敏回显（Task 5：向导选项入设置；spec「API Key 脱敏回显」）。

    规则（示例口径 ``sk-****last4``）：
    - 未配置（空串 / 非字符串）→ 空串，前端按「未配置」渲染；
    - 长度 ≤ 4 → 全遮 ``****``（避免短值泄露）；
    - 否则保留 ``sk-`` 前缀（若原文以此开头）+ ``****`` + 尾 4 位明文，
      如 ``sk-abcdefgh12345678`` → ``sk-****5678``。

    :param value: 配置中的 api_key 原文（ConfigManager 读回时已解密）。
    :return: str 脱敏展示值；明文任何形式不出现在返回结果中。
    """
    if not isinstance(value, str) or not value:
        return ""
    tail = value[-4:] if len(value) > 4 else ""
    prefix = "sk-" if value.startswith("sk-") else ""
    return f"{prefix}****{tail}"

# CSRF/CORS 加固：受信任的跨源 Origin 白名单。
# "null" 是 Electron 生产态（file:// 页面）发出的 Origin 字面量；
# 不使用通配符 *，未命中白名单的一律不返回 CORS 头（浏览器侧即被同源策略拦截）。
_ALLOWED_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173", "null")

# 防 DNS rebinding：Host 必须指向服务自身。生产默认端口 8600 精确列入白名单。
# 另放行"主机名为本机回环名、端口任意"的形态：测试起服绑定临时端口
# （HTTPServer(("127.0.0.1", 0))），urllib 自动发送 Host: 127.0.0.1:<随机端口>，
# 严格两值白名单会误伤；外部恶意域名（DNS rebinding 的真正攻击面）仍被拒绝。
_ALLOWED_HOSTS = ("127.0.0.1:8600", "localhost:8600")
_ALLOWED_HOST_NAMES = ("127.0.0.1", "localhost")

# 批次E：GET /api/tools 响应附带的端点用法自述（openapi 风格，供管理 Agent 自发现）
_TOOLS_USAGE = {
    "POST /api/tools/call": {
        "body": {"name": "工具 id（见 tools[].id）", "arguments": "工具参数 dict（可省略）"},
        "result": "200 {ok:true, result}；未授权/类别禁用 403 not_authorized；未知工具 404",
    },
    "POST /api/memory/distill": {
        "body": {"messages": "非空 [{role, content}, ...] 列表", "agent_id": "可选，默认 default"},
        "result": "200 {ok:true, sessions}；未配置云端 400 cloud_not_configured；云端离线 503 cloud_offline",
    },
    "POST /api/voice/synthesize": {
        "body": {"text": "待合成文本（非空）", "voice": "可选音色，缺省现读 config tts.voice（热切换）"},
        "result": "200 {ok:true, audio_base64, mime:'audio/wav'}；后端异常 503 voice_backend_unavailable",
    },
    "POST /api/voice/transcribe": {
        "body": {"audio_base64": "PCM 音频的 base64 编码", "sample_rate": "可选采样率（默认 16000；非 16k 时由语音桥重采样，避免慢放误识别）"},
        "result": "200 {ok:true, text}；解码失败 400；后端异常 503 voice_backend_unavailable",
    },
    "GET /api/pet/model": {
        "args": "无",
        "result": "200 原始 VRM 字节（Content-Type: model/gltf-binary）；文件缺失 404 pet_model_missing",
    },
    "POST /api/pet/model/import": {
        "body": {"source_path": "用户自选 VRM 模型的本地绝对路径"},
        "result": "200 {ok:true, message}（导入成功，原模型自动备份为 cx-open.vrm.bak）；"
        "缺 source_path / 非 .vrm 后缀 / 文件不存在 400 中文错误；"
        "复制失败 500 pet_model_import_failed（原模型保持不变）",
    },
    "POST /api/pet/model/reset": {
        "args": "无 body",
        "result": "200 {ok:true, message}（从 .bak 备份还原默认模型）；备份缺失 404 pet_model_backup_missing",
    },
}

# ------------------------------------------------------------------ 路径推导
# lite/server/api_server.py -> lite/server -> lite -> 项目根目录（逐级上溯 3 次）
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_LITE_DIR = os.path.dirname(_THIS_DIR)
_PROJECT_ROOT = os.path.dirname(_LITE_DIR)

# 直接以脚本方式运行时，脚本目录会被加入 sys.path 而非项目根，故需手动补入项目根
# 才能以包路径 import lite.memory 下的既有模块（tests 侧以包方式 import 时幂等）
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lite.memory.embedding import LiteEmbeddingProvider  # noqa: E402
from lite.memory.pipeline import MemoryRetrievalPipeline  # noqa: E402
from lite.memory.storage import MemoryStore  # noqa: E402
from lite.memory.vector_store import InMemoryVectorStore, SQLiteVectorStore  # noqa: E402
from lite import __version__ as LITE_VERSION  # noqa: E402
from lite.management.local_agents import AgentManager, AgentNotFound  # noqa: E402
from lite.management.fleet import (  # noqa: E402
    FleetInvalid,
    FleetManager,
    FleetNotFound,
    FleetRemoteError,
    is_loopback_client,
)
from lite.management.remote import (  # noqa: E402
    RemoteController,
    RemoteDisabled,
    RemoteError,
    RemoteUnreachable,
)
from lite.computer_control.control import (  # noqa: E402
    ComputerControl,
    NotAuthorizedError,
    PluginError,
)
from lite.computer_control.security import ControlAuthorizer  # noqa: E402
from lite.computer_control.tool_bridge import ToolBridge  # noqa: E402
from lite.config.config_manager import DEFAULTS, ConfigManager  # noqa: E402
from lite.config.paths import app_root, data_root  # noqa: E402
from lite.config.download_sources import (  # noqa: E402
    CHANNELS,
    DEFAULT_CHANNEL,
    MODEL_REPOS,
    model_repo_for_channel,
    normalize_channel,
)
from lite.config.paths import app_root  # noqa: E402
from lite.cloud.adapter import PROVIDER_BASE_URLS  # noqa: E402
from lite.cloud.adapter import CloudAdapter, CloudConfigError, CloudUnavailableError  # noqa: E402
from lite.avatar import EmotionTagParser  # noqa: E402
from lite.management.local_agents import MEMORY_AGENT_ID  # noqa: E402
from lite.memory.distillation import DistillationPaused, MemoryDistiller  # noqa: E402
from lite.memory.schema import MEMORY_TYPES as MEMORY_TYPES_ALLOWED  # noqa: E402
from lite.memory.scoring import (  # noqa: E402
    DEFAULT_WEIGHTS as _MEMORY_3D_DEFAULT_WEIGHTS,
)
from lite.memory.scoring import score_memories as _memory_score_3d  # noqa: E402
from lite.runtime.download_manager import ModelDownloadManager  # noqa: E402
from lite.runtime.hardware_profile import (  # noqa: E402
    accel_plan,
    derive_default_mode,
    detect_profile,
    recommend_for,
)
from lite.runtime.model_downloader import MODEL_TIERS, DEFAULT_TIER  # noqa: E402
from lite.tools.builtin_registry import BuiltinToolRegistry  # noqa: E402
from lite.audio import LiteVoicePipeline, build_default_pipeline  # noqa: E402
from lite.audio.text_splitter import split_by_punctuation  # noqa: E402
from lite.audio.voice_manager import DEFAULT_VOICE_ID, VoiceManager, is_unsafe_voice_id  # noqa: E402
from lite.cxfc import LiteCXFC  # noqa: E402
from lite.vision.pipeline import VisionPipeline  # noqa: E402
from lite.vision.sampler import AdaptiveSampler  # noqa: E402

# 云端 provider 白名单（L-8：从 adapter.PROVIDER_BASE_URLS 派生，单一真相源，
# 新增 provider 无需再同步本文件；置于 lite 包 import 之后——派生依赖其符号）
CLOUD_PROVIDER_ALLOWLIST = tuple(PROVIDER_BASE_URLS.keys())

# ---------------------------------------------------------------- 记忆管理助手指令协议（20261005，spec: align-wizard-settings-memory-pet）
# [memory:op {...}] 指令标签：对齐 [emotion:x] 自造协议先例。LLM 回复中的标签由
# 后端解析、经 BuiltinToolRegistry 记忆工具执行、结果回注一轮后从最终展示文本剥离。
#: op 名 -> BuiltinToolRegistry 工具 id 映射（工具由 builtin_registry.memory 系列提供）
_MEMORY_OP_TO_TOOL = {
    "search": "memory_search",
    "read": "memory_read",
    "write": "memory_write",
    "update": "memory_update",
    "delete": "memory_delete",
}
#: 指令标签起始标记
_MEMORY_TAG_MARKER = "[memory:"
#: 助手单次对话最多执行的指令标签数（防失控输出刷库）
_MEMORY_MAX_OPS_PER_TURN = 8
#: 工具环最多回注轮数（首轮 LLM 回复 + 最多 2 轮回注，符合「递归不超过 2 轮」约束）
_MEMORY_MAX_TOOL_ROUNDS = 2

#: 聊天记忆注入条数上限（RAG 闭环，20261005）：每次聊天检索并拼入 system 的
#: 记忆条数（经管线三维打分/衰减/去重后取前 N）
_MEMORY_INJECT_TOP_K = 8
#: 回注执行结果 JSON 的截断长度（防超长检索结果撑爆上下文）
_MEMORY_RESULT_SNIPPET_CHARS = 2000
#: 批量删除单请求 id 上限（防单请求串行刷全表）
_MAX_BATCH_MEMORY_IDS = 200
#: 3D 检索默认返回条数（对齐 CX-O /memories/3d 的 limit=10）
_MEMORY_3D_DEFAULT_LIMIT = 10
#: 3D 检索候选拉取条数（送三维打分前的候选池上限）
_MEMORY_3D_CANDIDATE_LIMIT = 100


def _match_json_object(text, brace_index):
    """从 ``text[brace_index] == '{'`` 起做括号平衡扫描，返回匹配 ``}`` 的下标。

    扫描考虑 JSON 字符串内的引号与反斜杠转义（字符串内的花括号不参与计数）。
    未闭合返回 -1。供指令标签解析与标签剥离共用（单一实现防口径漂移）。
    """
    depth = 0
    in_str = False
    escaped = False
    for i in range(brace_index, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _extract_memory_ops(text):
    """从 LLM 回复文本中按序提取全部 ``[memory:op {...}]`` 指令标签。

    Args:
        text: LLM 原始回复文本。
    Returns:
        list[tuple]: ``[(op, arguments, raw_tag), ...]``。
            - op: 指令名（str，可能是未知 op——由调用方白名单过滤）；
            - arguments: JSON 解析后的 dict；JSON 非法/非对象时为 None
              （调用方据此生成「参数非法」失败回执）；
            - raw_tag: 标签原文（回注消息中引用，便于 LLM 对应多条指令）。
    """
    s = str(text or "")
    if _MEMORY_TAG_MARKER not in s:
        return []
    ops = []
    cursor = 0
    while len(ops) < _MEMORY_MAX_OPS_PER_TURN * 2:  # 硬上限防异常输入死循环
        start = s.find(_MEMORY_TAG_MARKER, cursor)
        if start < 0:
            break
        head = start + len(_MEMORY_TAG_MARKER)
        # op token：marker 后的连续字母/数字/下划线（\w+）
        j = head
        while j < len(s) and (s[j].isalnum() or s[j] == "_"):
            j += 1
        op = s[head:j]
        cursor = j  # 至少推进到 op 之后，保证外层循环收敛
        if not op:
            continue
        brace = s.find("{", j)
        bracket = s.find("]", j)
        if brace < 0 or (0 <= bracket < brace):
            # 裸标签（无 JSON 参数体）：协议要求带参数，跳过由调用方提示
            continue
        end = _match_json_object(s, brace)
        if end < 0:
            json_text = s[brace:]
            cursor = len(s)
        else:
            json_text = s[brace:end + 1]
            cursor = end + 1
        try:
            args = json.loads(json_text)
            if not isinstance(args, dict):
                args = None
        except (ValueError, TypeError):
            args = None
        close = s.find("]", cursor if end < 0 else end + 1)
        raw = s[start:(close + 1) if close >= 0 else len(s)]
        ops.append((op, args, raw))
        if end < 0 or close < 0:
            break
    return ops


def _strip_memory_tags(text):
    """剥离文本中全部 ``[memory:...]`` 指令标签（含 JSON 非法的残缺标签）。

    用与 :func:`_extract_memory_ops` 同源的括号平衡扫描定位标签区间（非贪婪
    正则会误伤 JSON 内容里的 ``]``）；剥离后清理残留空白行。最终展示文本
    （clean_text）必须经本函数处理，保证指令标签不泄漏到前端。
    """
    s = str(text or "")
    while True:
        start = s.find(_MEMORY_TAG_MARKER)
        if start < 0:
            break
        brace = s.find("{", start + len(_MEMORY_TAG_MARKER))
        end = -1
        if brace >= 0:
            obj_end = _match_json_object(s, brace)
            if obj_end >= 0:
                close = s.find("]", obj_end + 1)
                end = close if close >= 0 else obj_end
        if end < 0:
            # 残缺标签（无 JSON 或未闭合）：兜底剥到下一个 "]"（无则剥到结尾）
            close = s.find("]", start)
            end = (close - 1) if close >= 0 else (len(s) - 1)
        s = s[:start] + s[end + 1:]
    # 清理剥离残留的多余空白行（保留正常段落结构）
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _memory_tool_result_message(op, raw_tag, outcome):
    """把一次指令执行结果格式化为回注消息内容（以 user 角色回注的系统回执）。

    本聊天链路的 messages 为简单 system/user 列表（无 role:"tool" 通道），
    故用带「系统回执」前缀的 user 消息承载工具结果（见 _handle_chat_message 注释）。

    隐私边界说明（GN-004 OB-2）：工具回执含记忆内容（≤2000 字符截断）。
    本地模式下（local_llm.enabled）聊天走本地运行时，回执全程不出本机；
    云端模式下回执随 messages 上行云端 LLM——与普通聊天把用户消息上行
    云端的语义一致，属该模式的既定隐私边界，不做额外截停。
    """
    success = bool(outcome.get("success"))
    if success:
        detail = json.dumps(outcome.get("result"), ensure_ascii=False, default=str)
        status = "成功"
    else:
        detail = str(outcome.get("error") or "未知错误")
        status = "失败"
    if len(detail) > _MEMORY_RESULT_SNIPPET_CHARS:
        detail = detail[:_MEMORY_RESULT_SNIPPET_CHARS] + "…（结果过长已截断）"
    return (
        "系统回执（你之前的指令已由系统执行，请据此用中文向用户汇报，"
        "不要输出任何 [memory:...] 指令标签）：\n"
        f"指令 {raw_tag} 执行{status}。\n结果：{detail}"
    )

# 全组件加速值域白名单（与 lite/config/config_manager.DEFAULTS 对齐；非法值一律入
# ignored 显式回显，不静默丢弃）。accel.mode = 运行偏好；tts.accel = 语音合成后端；
# tts.accel_device = 设备提示（""/igpu/dgpu）。
_ACCEL_MODES = ("performance", "eco")
_TTS_ACCEL_VALUES = ("auto", "cpu", "cuda", "dml", "rocm", "off")
_TTS_ACCEL_DEVICE_VALUES = ("", "igpu", "dgpu")
# local_llm.gpu_preference 白名单（20261006 LLM 显卡切换）：""=按运行模式自动 /
# igpu=强制核显 / dgpu=强制独显；非法入 ignored。
_LLM_GPU_PREFERENCE_VALUES = ("", "igpu", "dgpu")
# voice.interaction_mode 白名单（20261006 全双工降级版）：vad=传统自动断句 /
# duplex=全双工（按标点切句逐句轮询 LLM）；非法入 ignored。
_VOICE_INTERACTION_MODES = ("vad", "duplex")


def _accel_profile_from_plan(plan):
    """由 ``accel_plan`` 落点组装加速剖面（与 ``recommend_for`` 的 accel 字段同构）。

    :param plan: :func:`lite.runtime.hardware_profile.accel_plan` 的产出 dict。
    :return: 加速剖面 dict（mode / tts / asr / local_llm / embedding / reasons）。
    """
    plan = plan if isinstance(plan, dict) else {}
    return {
        "mode": plan.get("accel.mode"),
        "tts": {
            "accel": plan.get("tts.accel"),
            "accel_device": plan.get("tts.accel_device"),
        },
        "asr": {"device": plan.get("asr.device")},
        "local_llm": {"device": plan.get("local_llm.device")},
        "embedding": {"device": plan.get("embedding.device")},
        "reasons": list(plan.get("reasons") or []),
    }


def _apply_accel_plan_to_config(config, plan):
    """把 ``accel_plan`` 全部落点写入配置（模式 + 各组件设备）。

    落点与安装链 ``conda_runtime.apply_accel_plan`` 共用同一真相源，保证向导 /
    设置接口 / 安装链三处一致（禁止在调用点各自判断）。
    """
    config.set("accel", "mode", plan["accel.mode"])
    config.set("tts", "accel", plan["tts.accel"])
    config.set("tts", "accel_device", plan["tts.accel_device"])
    config.set("asr", "device", plan["asr.device"])
    config.set("local_llm", "device", plan["local_llm.device"])
    config.set("embedding", "device", plan["embedding.device"])


def _close_voice_backends(voice):
    """关闭语音编排器持有的 sidecar 常驻进程（幂等、不抛）。

    仅对桥后端有效（``LiteASR.backend.client`` / ``LiteTTS.backend.client``）；
    进程内后端 / Mock 后端无 ``client`` 属性时静默跳过。
    """
    for facade_name in ("asr", "tts"):
        facade = getattr(voice, facade_name, None)
        backend = getattr(facade, "backend", None)
        client = getattr(backend, "client", None)
        if client is not None and hasattr(client, "close"):
            try:
                client.close()
            except Exception:  # noqa: BLE001 - 关闭失败不阻断重建（进程随系统回收）
                pass


# ------------------------------------------------------------------ 主动视觉装配（Task C「主动视觉接线」）
#: 视觉管线 tick 线程的循环节拍（秒）：每拍驱动一次 run_once。
#: ``vision.enabled=False`` 时 run_once 自身零开销返回 None（隐私红线：绝不采样），
#: 线程空转 sleep 即可；测试可 monkeypatch 本常量加速 tick 节奏。
_VISION_TICK_INTERVAL_S = 1.0


def build_vision_pipeline(config=None, memory_store=None, cloud=None,
                          local_understanding=None):
    """装配主动视觉管线（Task C）：屏幕后端 + 自适应采样器 + VisionPipeline。

    - 屏幕后端：Windows 下构建纯标准库 GDI 后端
      (:class:`~lite.vision.screen_backend.WindowsGrayscaleScreenBackend`)；
      非 Windows 注入 None 并告警（管线空转告警，可接受——测试/CI 环境多为 Linux）；
    - 采样间隔读 config ``vision.min_interval_s`` / ``vision.max_interval_s``
      （缺失回默认 30 / 2）；配置非法（如 min < max 触发 AdaptiveSampler 校验、
      非数值触发 float 转换失败）时异常向上抛，由调用方 try/except 兜底告警置
      None（视觉不阻断服务启动）；
    - ``vision.enabled`` 默认 False：``run_once`` 直接返回 None 且绝不采样
      （隐私红线），经 PUT /api/settings 的 ``vision.enabled`` 热更新开启
      （ConfigManager 内存写即时生效，无需重启）。

    Args:
        config: ConfigManager（settings 同源实例——热更新开关经它即时生效）。
        memory_store: 记忆存储（理解结果沉淀目标；None 时理解结果告警跳过沉淀）。
        cloud: CloudAdapter 实例（云端理解通道；None 时走注入 understanding 或
            理解失败告警隔离）。
        local_understanding: 本地多模态理解回调（20261004 Gemma 4 本地视觉，
            callable(messages) -> str）；注入后默认理解优先走本地（截图帧
            image_url / 灰度拓扑交本地小 LLM），未就绪/失败回落云端。

    Returns:
        VisionPipeline: 已接线 queue consumer 的视觉管线实例。
    """
    backend = None
    if sys.platform == "win32":
        try:
            # 延迟导入：ctypes/GDI 依赖链不进入非 Windows 平台导入路径
            from lite.vision.screen_backend import WindowsGrayscaleScreenBackend

            backend = WindowsGrayscaleScreenBackend()
        except Exception as exc:  # noqa: BLE001 - 后端构建失败降级空转
            LOGGER.warning("屏幕采样后端构建失败（%s）：%s", exc.__class__.__name__, exc)
    else:
        LOGGER.warning("非 Windows 平台：未装配屏幕采样后端，主动视觉管线空转")
    min_interval_s = float(config.get("vision", "min_interval_s", 30)) if config is not None else 30.0
    max_interval_s = float(config.get("vision", "max_interval_s", 2)) if config is not None else 2.0
    sampler = AdaptiveSampler(
        backend, min_interval_s=min_interval_s, max_interval_s=max_interval_s
    )
    return VisionPipeline(
        sampler=sampler, cloud=cloud, memory_store=memory_store, config=config,
        local_understanding=local_understanding,
    )


def start_vision_tick_thread(pipeline):
    """启动视觉管线 tick 线程（daemon=True，随进程退出安静消亡）。

    循环体对 ``run_once`` **整体 try/except**——任何异常（含 mock backend
    capture 抛错、queue 异常）仅 LOGGER.warning，线程永不因异常退出；
    无需显式 stop 钩子（本服务装配函数无统一关闭链，daemon 线程随进程回收）。

    :param pipeline: :class:`~lite.vision.pipeline.VisionPipeline` 实例。
    :return: 线程对象（测试断言 is_alive / 运维观测用）。
    """

    def _tick_loop():
        while True:
            try:
                pipeline.run_once()
            except Exception as exc:  # noqa: BLE001 - tick 兜底：线程不自灭
                LOGGER.warning("主动视觉 tick 异常（%s）：%s", exc.__class__.__name__, exc)
            time.sleep(_VISION_TICK_INTERVAL_S)

    thread = threading.Thread(target=_tick_loop, name="vision-tick", daemon=True)
    thread.start()
    return thread


# 默认数据目录：项目根目录下 data/（与 storage._default_db_path 的 data/memories.db 一致）
DEFAULT_DATA_DIR = os.path.join(_PROJECT_ROOT, "data")
# 默认监听端口（与前端 API_PORT 一致）
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8600

# 悬浮桌宠默认 VRM 模型相对应用根的路径（模块级单一真相源，供测试 monkeypatch app_root 复用）。
# 与 installer/manifest.json 中 pet_model 组件的 install_target 保持同一口径。
PET_MODEL_REL_PATH = os.path.join("data", "pet", "cx-open.vrm")

# 导入用户自选模型前对现模型的备份相对路径（同目录 cx-open.vrm.bak，导入时覆盖旧备份；
# 「恢复默认」端点从该备份还原）。Task 4（用户自定义 VRM 桌宠模型）。
PET_MODEL_BACKUP_REL_PATH = PET_MODEL_REL_PATH + ".bak"


def pet_model_path() -> str:
    """推导悬浮桌宠默认 VRM 模型的绝对路径：``<app_root>/data/pet/cx-open.vrm``。

    根目录经 :func:`lite.config.paths.app_root`（frozen-aware）解析——开发态＝项目根，
    打包态＝便携根；**禁止**用 ``__file__`` 逐级上溯（PyInstaller onedir 冻结态下
    ``__file__`` 落在 ``runtime/backend/_internal/lite/...``，推导会指向
    ``_internal/data`` 而非便携根 ``data/``，真实模型装载必然失败）。

    :return: str 模型文件绝对路径（不保证存在，调用方需处理缺失）。
    """
    return os.path.join(app_root(), PET_MODEL_REL_PATH)


def _resolve_data_dir(data_dir=None) -> str:
    """归一化数据目录：未显式提供时回落项目根 data/。"""
    return data_dir or DEFAULT_DATA_DIR


def _build_embedding_provider(config):
    """装配嵌入提供者：真实（llama-server 外部路径）唯一路径，**禁止降级**。

    20260926_模块0_真实嵌入与向量持久化：模型路径经
    ``resolve_embedding_model_path`` 解析（``embedding.model_path`` 配置优先，
    否则约定目录 ``<root>/data/local_llm/qwen3-embedding-0.6b/*.gguf``）。

    20261005 人类裁决（嵌入禁止降级，关闭原"哈希桩回落"开放确认项）：模型路径
    为空 / llama-server 启动失败 / 加载未就绪，一律 raise RuntimeError（中文，
    含处置指引）——嵌入模型随安装包内置（装机即用），缺失说明安装不完整或被
    误删，绝不静默回落桩嵌入。

    Args:
        config: ConfigManager 实例。

    Returns:
        tuple[EmbeddingProvider, str, dict]: (提供者, 标识 ``"llama"``,
            元信息 ``{"dim": int|None, "model_tag": str}``)——元信息供持久向量库
            ``prepare()`` 校准（换模型 / 换维度时重置索引表）。

    Raises:
        RuntimeError: 嵌入模型 GGUF 缺失 / llama-server 启动失败 / 加载未就绪
            （禁止降级语义，启动中止）。
    """
    # 函数内延迟导入：llama_runtime 导入链不进入本模块顶层导入路径
    # （与 build_local_chat_runtime 同口径）
    from lite.runtime.llama_runtime import (
        LlamaEmbeddingProvider,
        LlamaRuntime,
        resolve_embedding_model_path,
    )

    model_path = resolve_embedding_model_path(config)
    if not model_path:
        raise RuntimeError(
            "未找到嵌入模型 GGUF（embedding.model_path 为空且约定目录 "
            "<root>/data/local_llm/qwen3-embedding-0.6b/*.gguf 下无文件）——"
            "嵌入模型随安装包内置，缺失说明安装不完整或被误删；禁止降级，"
            "请重新安装或将 embedding.model_path 指向有效的嵌入 GGUF 后重启"
        )

    runtime = LlamaRuntime(config=config)
    try:
        ready = runtime.load_embedding_model(model_path)
    except RuntimeError as exc:
        raise RuntimeError(
            f"嵌入模型加载失败（禁止降级，启动中止）：{exc}。"
            f"模型路径：{model_path}；请确认文件完整（可校验后从安装包重新释放）"
            "或将 embedding.model_path 指向有效的嵌入 GGUF。"
        ) from exc
    if not ready:
        raise RuntimeError(
            "嵌入模型加载未就绪（禁止降级，启动中止）："
            f"{model_path}；详情：{'; '.join(runtime.warnings) or '未知原因'}。"
            "请确认模型文件完整或将 embedding.model_path 指向有效的嵌入 GGUF。"
        )

    # 优雅退出回收：llama-server 为常驻子进程（PyInstaller 冻结态无 atexit 保障的
    # 硬终止路径由 Electron 壳的进程树回收兜底，见 frontend/src/main/main.js）。
    atexit.register(runtime.close)
    model_name = str(config.get("embedding", "model", DEFAULTS["embedding"]["model"]) or "")
    model_tag = f"{model_name}|{os.path.basename(model_path)}"
    # 运行期可见的就绪证据（本项目未配置 logging handler，INFO 级默认不显示；
    # 与 [LiteAudio] 同口径用 print 输出，便于装机实跑核对"真嵌入已生效"）
    print(
        f"[LiteMemory][INFO] 真实嵌入就绪：llama-server + {os.path.basename(model_path)}"
        f"（dim={runtime.emb_dim}）"
    )
    return (
        LlamaEmbeddingProvider(runtime),
        "llama",
        {"dim": runtime.emb_dim, "model_tag": model_tag},
    )


def _build_vector_store(config, data_dir, embed_kind, embed_meta):
    """装配向量库：按配置唯一真值构建，**禁止降级**（20261005 人类裁决）。

    - ``backend=lancedb``（默认）：``LanceVectorStore``——依赖缺失 / 初始化失败
      一律 raise RuntimeError（中文，含安装指引），**绝不静默回落**其他后端；
    - ``backend=sqlite``：``SQLiteVectorStore``（显式选择，非降级；cosine 口径
      与 InMemoryVectorStore 逐位一致，跨重启持久）；
    - 空串 / 未知值：raise（不再回落内存库——降级路径已按裁决移除）。

    Args:
        config: ConfigManager 实例。
        data_dir: 数据目录（memories.db 所在）。
        embed_kind: 嵌入标识（``"llama"``/``"stub"``，见 _build_embedding_provider）。
        embed_meta: 嵌入元信息 ``{"dim", "model_tag"}``（持久库 prepare 校准用）。

    Returns:
        VectorStore: 向量存储实例（SQLite 路径已完成 prepare 校准；不匹配时已重置）。

    Raises:
        RuntimeError: backend=lancedb 但 lancedb 依赖缺失 / 初始化失败；
            backend 为空串或未知值（中文消息，启动失败——禁止降级语义）。
    """
    backend = str(
        config.get("vector", "backend", DEFAULTS["vector"]["backend"]) or ""
    ).strip().lower()
    if backend == "lancedb":
        if importlib.util.find_spec("lancedb") is None:
            raise RuntimeError(
                "向量后端配置为 LanceDB 但当前环境未安装 lancedb（禁止降级："
                "请安装 lancedb（pip install lancedb），或将 vector.backend "
                "显式改为 sqlite 后重启）"
            )
        try:
            from lite.memory.vector_store import LanceVectorStore

            store = LanceVectorStore(db_path=os.path.join(data_dir, "lancedb"))
        except Exception as exc:  # noqa: BLE001 - 初始化失败按禁止降级语义硬失败
            raise RuntimeError(
                f"LanceDB 初始化失败（禁止降级，启动中止）：{exc}。"
                "请检查 lancedb 安装完整性或将 vector.backend 显式改为 sqlite。"
            ) from exc
        LOGGER.warning(
            "向量后端使用 LanceDB：排序语义为 L2 归一，与 cosine 口径不同轨"
        )
        return store
    if backend == "sqlite":
        # 嵌入禁止降级（20261005）后 embed_kind 恒为 "llama"，桩分支已移除
        store = SQLiteVectorStore(
            db_path=os.path.join(data_dir, "memories.db"),
            model_tag=str(embed_meta.get("model_tag", "")),
            dim=embed_meta.get("dim"),
        )
        result = store.prepare()
        if result["action"] == "reset":
            LOGGER.warning("嵌入模型标识已变化，向量索引已重置（将由启动预热按新模型重建）")
        return store
    raise RuntimeError(
        f"未知向量后端配置：{backend!r}（允许值：lancedb / sqlite；禁止降级语义下"
        "不再回落内存库，请修正 vector.backend 后重启）"
    )


def build_deps(data_dir=None, config_path=None):
    """组装服务依赖。

    Args:
        data_dir: 数据目录（None 用项目根 data/）。
        config_path: 配置文件路径（H-3，第三轮体检批次4；None 用
            ``data_dir/config.json``）。生产链由 backend_entry 显式传
            ``<root>/config.json``，与安装链（bootstrap/first_run）的
            用户配置落点统一为同一真相源。

    Returns:
        tuple[MemoryStore, MemoryRetrievalPipeline, AgentManager, RemoteController]:
            存储实例、检索管线、本地 Agent 管理器与远端遥控控制器，四者共享同一
            data_dir。遥控控制器由配置的 remote 段驱动
            （默认 enabled=false），测试时各自注入 mock transport。
    """
    data_dir = _resolve_data_dir(data_dir)
    os.makedirs(data_dir, exist_ok=True)
    # M5 配置接线：读取 memory 段注入检索管线（缺省值与 DEFAULTS["memory"] 一致），
    # pipeline 内部会把 dedup/permanent_threshold 透传给其持有的 MemoryManager
    config = ConfigManager(config_path=config_path or os.path.join(data_dir, "config.json"))
    store = MemoryStore(db_path=os.path.join(data_dir, "memories.db"))
    # 20260926_模块0_真实嵌入与向量持久化：生产装配＝真实嵌入（llama-server 外部
    # 路径）+ SQLite 持久向量库；任一环节不可用自动降级（桩嵌入 + 内存向量库），
    # 后端启动绝不因此失败。
    embed, embed_kind, embed_meta = _build_embedding_provider(config)
    vector_store = _build_vector_store(config, data_dir, embed_kind, embed_meta)
    pipeline = MemoryRetrievalPipeline(
        store=store,
        vector_store=vector_store,
        embed=embed,
        max_memories=int(config.get("memory", "max_memories", DEFAULTS["memory"]["max_memories"])),
        dedup_threshold=float(config.get("memory", "dedup", DEFAULTS["memory"]["dedup"])),
        permanent_threshold=float(
            config.get("memory", "permanent_threshold", DEFAULTS["memory"]["permanent_threshold"])
        ),
    )
    # 持久向量库启动预热（有界，见 pipeline.warmup_vectors）：补建"有记忆无向量"
    # 条目 + 清理孤儿向量；剩余条目留待下次启动 / 后续写入继续，不长时间阻塞启动。
    if embed_kind == "llama":
        stats = pipeline.warmup_vectors()
        if stats.get("supported") is False:
            LOGGER.warning("向量库未实现 vector_ids，已跳过启动预热（不影响检索）")
    manager = AgentManager(path=os.path.join(data_dir, "agents.json"))
    #: 遥控控制器：config 驱动（remote.enabled 默认 false），不主动发起真实网络
    remote = RemoteController(config=config)
    return store, pipeline, manager, remote


def build_computer_deps(data_dir=None):
    """组装电脑控制依赖：authorizer + computer + bridge（authorizer 单例复用）。

    Args:
        data_dir: 数据目录（None 用项目根 data/）。授权状态与审计日志落盘于此。

    Returns:
        tuple[ComputerControl, ControlAuthorizer, ToolBridge]:
            实际执行端、安全总控与接线层三者共享同一 data_dir。authorizer 默认
            安全关闭（authorized=False），computer 初始同步该状态，桥接层完成
            授权校验 → 高危确认 → 执行 → 审计 → 回填的完整链路。
    """
    data_dir = _resolve_data_dir(data_dir)
    os.makedirs(data_dir, exist_ok=True)
    authorizer = ControlAuthorizer(data_dir=data_dir)
    computer = ComputerControl(authorized=authorizer.is_authorized())
    bridge = ToolBridge(computer=computer, authorizer=authorizer)
    return computer, authorizer, bridge


def build_runtime_deps(data_dir=None, config=None, store=None, pipeline=None, computer_deps=None, config_path=None):
    """装配批次E生产运行时依赖：语音编排 / 内置工具注册表 / 记忆蒸馏器。

    三个引擎此前仅有能力实现、无生产构造点（20260828_模块0_生产装配接线），
    本函数为其提供统一装配入口：

    - voice：``build_default_pipeline(config)`` 装配三件套（funasr / melotts 缺席时
      自动回退 Mock 后端，装配零失败），再包装为 ``LiteVoicePipeline``（cloud 置
      None，保持纯本地离线形态）；
    - registry：``BuiltinToolRegistry``，电脑控制三件复用 ``computer_deps`` 产物
      （authorizer 单例同源），记忆读写复用 store / pipeline / pipeline.manager；
    - distiller：``MemoryDistiller``，云端适配器由同一 config 构造（CloudAdapter
      构造期零失败，CloudConfigError 延迟到 is_online / chat 时才抛出）。

    Args:
        data_dir: 数据目录（None 用项目根 data/）。
        config: 可选 ConfigManager；缺省按 data_dir 下 config.json 新建。
        store: 可选 MemoryStore；缺省随 pipeline 一起经 build_deps 补建。
        pipeline: 可选 MemoryRetrievalPipeline；缺省经 build_deps 补建。
        computer_deps: 可选 (computer, authorizer, bridge) 三元组；缺省经
            build_computer_deps 补建。

    Returns:
        tuple[LiteVoicePipeline, BuiltinToolRegistry, MemoryDistiller]:
            三者共享同一 config / store / pipeline 上下文。
    """
    data_dir = _resolve_data_dir(data_dir)
    os.makedirs(data_dir, exist_ok=True)
    if store is None or pipeline is None:
        built_store, built_pipeline, _manager, _remote = build_deps(data_dir, config_path=config_path)
        store = store or built_store
        pipeline = pipeline or built_pipeline
    if config is None:
        config = ConfigManager(config_path=config_path or os.path.join(data_dir, "config.json"))
    if computer_deps is None:
        computer_deps = build_computer_deps(data_dir)
    computer, authorizer, bridge = computer_deps

    # 语音全链路：Mock 兜底装配（缺依赖仅告警不失败），离线形态 cloud=None
    components = build_default_pipeline(config)
    voice = LiteVoicePipeline(
        vad=components["vad"],
        asr=components["asr"],
        tts=components["tts"],
        cloud=None,
        judge=components["judge"],
    )
    registry = BuiltinToolRegistry(
        computer=computer,
        computer_bridge=bridge,
        authorizer=authorizer,
        memory_store=store,
        pipeline=pipeline,
        manager=getattr(pipeline, "manager", None),
        config=config,
    )
    distiller = MemoryDistiller(
        cloud=CloudAdapter(config),
        store=store,
        manager=getattr(pipeline, "manager", None),
    )
    return voice, registry, distiller


def make_handler(
    store, pipeline, manager=None, remote=None, fleet=None,
    computer=None, authorizer=None, bridge=None, config=None,
    registry=None, distiller=None, voice=None, cxfc=None, chat_cloud=None,
    chat_fallback=None, download_manager=None, vision_pipeline=None,
):
    """基于指定依赖构建处理器类（闭包绑定 store / pipeline / manager / remote / computer，便于测试隔离）。

    Args:
        config: 可选 ConfigManager 实例（提供 /api/settings 读写；缺省新建，
            默认读写项目根 data/config.json）。
        registry: 可选内置工具注册表（批次E；供 /api/tools* 端点使用）。
        distiller: 可选记忆蒸馏器（批次E；供 /api/memory/distill 使用）。
        voice: 可选语音全链路编排器（批次E；供 /api/voice/* 端点使用）。
            registry / distiller / voice 遵循 N8 注入语义：仅对显式为 None 的
            依赖回落默认构建（voice 默认构建经 build_default_pipeline 的 Mock
            兜底零失败；registry / distiller 默认构建复用下方已解析的
            computer / authorizer / bridge 与 config / store / pipeline 上下文）。
        cxfc: 可选 LiteCXFC 实例（Task H1；供 /api/cxfc/relay/* 端点使用）。
            缺省按 config 的 cxfc 段默认构建（enabled 由配置驱动，默认 False）；
            测试可注入临时实例（enabled/embedded_only/relay_timeout_s 自定）。
        chat_cloud: 可选云端适配器实例（Task H3；供 /api/chat/message 表情聊天
            端点使用）。缺省按 config 构建默认 CloudAdapter（构造期零失败，
            CloudConfigError / CloudUnavailableError 延迟到 chat 调用时抛出，
            由端点兜底为离线文案）；测试注入内存 mock 以避免真实网络。
        chat_fallback: 可选离线兜底管理器（Task C3 接线；供 /api/chat/message
            统一在线/离线通道切换）。缺省按 `_chat_cloud` + 本地聊天运行时持有器
            构建默认 `OfflineFallbackManager`：``local_llm=`` 传持有器 ``get``
            （零参 callable / resolver 形态，每次 chat 现解析当前运行时），
            启动期同步调用一次 ``build_local_chat_runtime(config)`` 的结果经
            ``set_runtime`` 存入持有器复用（启动期去重，禁止同一模型加载两份）；
            运行中 PUT ``local_llm.enabled`` 经持有器 ``ensure_started`` /
            ``release`` 动态接线，无需重启。依赖缺席/装配失败仅告警并置 None，
            端点退化为旧的直连云端 + 固定离线文案路径。测试可注入替身。
        download_manager: 可选本地模型后台下载管理器（Task 6；供
            /api/setup/model/* 三端点使用）。缺省按 config 构建
            ``ModelDownloadManager``，并注入**本模块的模块级写锁**
            ``_CONFIG_WRITE_LOCK``（与请求线程共用，串行化 config.save()）；
            测试可注入替身（``downloader_factory`` 为假下载器，绝不触网）。
        vision_pipeline: 可选主动视觉管线实例（Task C；daemon tick 线程驱动）。
            缺省按 config / store / chat_cloud 构建默认管线
            （:func:`build_vision_pipeline`：Windows 纯标准库 GDI 屏幕后端 +
            自适应采样器，``vision.enabled`` 默认 False 隐私红线）；
            **注入实例同样由本函数启动 tick 线程**（测试注入 mock 后端管线
            验证 capture 计数 / 线程存活）；装配失败仅告警置 None（视觉不阻断
            服务启动），线程对象经 handler 类属性 ``_vision_thread`` 透出。
    """
    if manager is None:
        manager = AgentManager()
    if remote is None:
        remote = RemoteController()
    # N8 注入语义修正：仅对显式为 None 的电脑控制依赖逐项回落默认，不再无条件
    # 覆盖调用方注入的 computer / authorizer；默认构建的 data_dir 优先复用已注入
    # 依赖携带的 data_dir 属性（ComputerControl.data_dir / ControlAuthorizer._data_dir），
    # 取不到再落 DEFAULT_DATA_DIR。create_app 全量注入路径行为不变。
    default_data_dir = DEFAULT_DATA_DIR
    for _dep in (computer, authorizer, bridge):
        reused = getattr(_dep, "data_dir", None) or getattr(_dep, "_data_dir", None)
        if reused:
            default_data_dir = reused
            break
    # 对话历史文件（20261004）：跟随 handler 数据目录——测试经依赖注入的
    # tmp_path 天然隔离，生产为 <app_root>/data/chat_history.json
    chat_history_file = os.path.join(default_data_dir, _CHAT_HISTORY_FILENAME)
    if computer is None or authorizer is None or bridge is None:
        built_computer, built_authorizer, built_bridge = build_computer_deps(default_data_dir)
        if computer is None:
            computer = built_computer
        if authorizer is None:
            authorizer = built_authorizer
        if bridge is None:
            bridge = built_bridge
    if config is None:
        config = ConfigManager(config_path=os.path.join(default_data_dir, "config.json"))
    # 批次E（N8 语义延续）：仅对显式为 None 的运行时依赖回落默认构建。
    # voice 默认构建走 build_default_pipeline（funasr/melotts 缺席自动回退 Mock，零失败）；
    # registry / distiller 默认构建复用上方已解析的 computer/authorizer/bridge 与
    # config/store/pipeline 上下文，保证与既有注入依赖同源。
    if voice is None:
        _components = build_default_pipeline(config)
        voice = LiteVoicePipeline(
            vad=_components["vad"],
            asr=_components["asr"],
            tts=_components["tts"],
            cloud=None,
            judge=_components["judge"],
        )
    if registry is None:
        registry = BuiltinToolRegistry(
            computer=computer,
            computer_bridge=bridge,
            authorizer=authorizer,
            memory_store=store,
            pipeline=pipeline,
            manager=getattr(pipeline, "manager", None),
            config=config,
        )
    if distiller is None:
        distiller = MemoryDistiller(
            cloud=CloudAdapter(config),
            store=store,
            manager=getattr(pipeline, "manager", None),
        )
    # Task H1（N8 语义延续）：仅对显式为 None 的 CXFC 实例回落默认构建——
    # enabled / embedded_only / relay 窗口参数由 config 的 cxfc 段驱动（默认全关）。
    if cxfc is None:
        cxfc = LiteCXFC(config=config)
    # Task H3（N8 语义延续）：仅对显式为 None 的表情聊天云端适配器回落默认构建。
    # CloudAdapter 构造期零失败（CloudConfigError 延迟到 chat 调用时抛出），
    # 无 api_key 的默认配置下端点自然兜底为离线文案。
    if chat_cloud is None:
        chat_cloud = CloudAdapter(config)
    # Task C3 接线（N8 语义延续）：仅对显式为 None 的离线兜底管理器回落默认装配。
    # 默认管理器把「云端 ↔ 本地小 LLM」切换收敛为单一入口；本地运行时经
    # _LocalChatRuntimeHolder 持有（本地模式真正本地，20261002）：
    # - 启动期：同步调用一次 build_local_chat_runtime(config)（启动期加载），结果
    #   经 set_runtime 存入持有器复用——禁止同一模型加载两份；
    # - 运行期：local_llm= 传持有器 get（resolver 形态，每次 chat 现解析），
    #   PUT local_llm.enabled 经 ensure_started / release 动态接线，无需重启；
    # 装配异常仅告警并置 None——端点退化为旧的直连云端 + 固定离线文案路径，绝不崩。
    _local_holder = _LocalChatRuntimeHolder()
    _resolved_chat_fallback = chat_fallback
    if _resolved_chat_fallback is None:
        try:
            from lite.cloud.fallback import OfflineFallbackManager

            _local_holder.set_runtime(build_local_chat_runtime(config))
            _resolved_chat_fallback = OfflineFallbackManager(
                cloud=chat_cloud,
                local_llm=_local_holder.get,
                config=config,
            )
        except Exception as exc:  # noqa: BLE001 - 兜底装配失败不得拖垮聊天端点
            LOGGER.warning(
                "离线兜底管理器装配失败（%s）：%s", exc.__class__.__name__, exc
            )
            _resolved_chat_fallback = None
    # Task 6（N8 语义延续）：仅对显式为 None 的下载管理器回落默认构建。写锁显式传入
    # 本模块的 _CONFIG_WRITE_LOCK——请求线程与下载线程共用同一把，串行化 config.save()。
    if download_manager is None:
        download_manager = ModelDownloadManager(
            config_manager=config, write_lock=_CONFIG_WRITE_LOCK
        )
    # 20261004 Gemma 4 本地视觉：理解回调复用聊天运行时持有器（禁止同一模型
    # 加载两份），把多模态消息交本地小 LLM；未就绪 / 失败返回 None → 管线回落
    # 云端（本地优先、云端拓扑兜底的双通道语义见 lite/vision/pipeline.py）。
    def _vision_local_understanding(messages):
        runtime = _local_holder.get()
        if runtime is None:
            return None
        try:
            return runtime.offline_chat(messages)
        except Exception:  # noqa: BLE001 - LlamaNotReady / 服务失败 → 回落云端
            return None

    # Task C「主动视觉接线」（N8 语义延续）：仅对显式为 None 的视觉管线回落默认
    # 装配——纯标准库 GDI 屏幕后端（非 Windows 注入 None 空转）+ 自适应采样器
    # （间隔读 config vision 段）+ VisionPipeline（``vision.enabled`` 默认 False，
    # 隐私红线：关闭状态绝不采样）。装配失败仅告警并置 None，视觉不阻断服务启动。
    _resolved_vision = vision_pipeline
    if _resolved_vision is None:
        try:
            _resolved_vision = build_vision_pipeline(
                config=config, memory_store=store, cloud=chat_cloud,
                local_understanding=_vision_local_understanding,
            )
        except Exception as exc:  # noqa: BLE001 - 视觉装配失败不阻断服务启动
            LOGGER.warning("主动视觉管线装配失败（%s）：%s", exc.__class__.__name__, exc)
    # tick 线程（daemon）对注入实例同样启动：enabled=False 时 run_once 零开销空转，
    # PUT vision.enabled 热更新后下一拍即开始采样；线程对象经类属性透出供测试断言。
    _resolved_vision_thread = None
    if _resolved_vision is not None:
        _resolved_vision_thread = start_vision_tick_thread(_resolved_vision)

    class ApiHandler(BaseHTTPRequestHandler):
        """REST 请求处理器。单线程 HTTPServer 内串行执行，无共享状态竞争。"""

        server_version = "CXLiteAPI/0.1"
        #: socket 读写超时（秒）（H-4 配套，第三轮体检批次2）：防慢客户端
        # （slowloris / 声明超大 body 缓慢发送）把单线程服务永久阻塞在 read 上；
        # 超时抛 socket.timeout（OSError 子类），由调用方捕获后正常回错
        timeout = 30
        #: 服务进程启动时刻（/api/status 的 uptime 基准）
        _STARTED_AT = time.monotonic()
        _store = store
        _pipeline = pipeline
        _manager = manager
        _remote = remote
        _fleet = fleet
        _computer = computer
        _authorizer = authorizer
        _bridge = bridge
        _config = config
        _registry = registry
        _distiller = distiller
        _voice = voice
        _cxfc = cxfc
        _chat_cloud = chat_cloud
        _chat_fallback = _resolved_chat_fallback
        _chat_history_file = chat_history_file
        _download_manager = download_manager
        _vision = _resolved_vision
        _vision_thread = _resolved_vision_thread

        # ------------------------------------------------------------ 底层工具
        def log_message(self, fmt, *args):
            """精简访问日志（避免默认 stderr 冗长输出，服务启动信息仍打印）。"""

        def _check_host(self):
            """校验 Host 头是否指向本服务自身（防 DNS rebinding）。

            规则：Host 精确命中 _ALLOWED_HOSTS 放行；否则拆出主机名部分，
            仅当主机名为本机回环名（127.0.0.1 / localhost，端口任意，兼容
            测试态临时端口与本机自定义端口部署）时放行。缺失 Host 头一律拒绝。
            """
            host = (self.headers.get("Host") or "").strip().lower()
            if not host:
                return False
            if host in {h.lower() for h in _ALLOWED_HOSTS}:
                return True
            # IPv6 字面量形态 [::1]:port 与常规 host:port 分别拆出主机名
            name = host
            if name.startswith("["):
                name = name[1:].split("]", 1)[0]
            elif ":" in name:
                name = name.rsplit(":", 1)[0]
            return name in _ALLOWED_HOST_NAMES

        def _check_token(self):
            """校验启动令牌 X-Client-Token（N1：根治 CSRF→RCE 链）。

            规则：
            - 服务端令牌为空（env CXA_API_TOKEN 未设置）：开放模式放行，仅首次
              调用告警一次（提示当前无令牌保护，仅限开发 / 测试环境）；
            - 令牌非空：请求头 X-Client-Token 必须与令牌常量时间比较一致，
              否则回 403 ``unauthorized_client``。

            :return: True 放行；False 表示已发送 403，调用方直接 return。
            """
            global _TOKEN_OPEN_MODE_WARNED
            if not _API_TOKEN:
                if not _TOKEN_OPEN_MODE_WARNED:
                    _TOKEN_OPEN_MODE_WARNED = True
                    print(
                        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [WARNING] "
                        "CXA_API_TOKEN 未设置：API 服务运行于开放模式（无令牌校验），仅限开发/测试环境"
                    )
                return True
            supplied = (self.headers.get("X-Client-Token") or "").encode("utf-8", errors="replace")
            if hmac.compare_digest(supplied, _API_TOKEN.encode("utf-8")):
                return True
            self._send_json({"ok": False, "error": "unauthorized_client"}, 403)
            return False

        def _cors_headers(self):
            """按请求 Origin 计算应附带的 CORS 响应头。

            Origin 命中 _ALLOWED_ORIGINS 时返回放行头集合；未命中返回空 dict
            （不返回任何 Access-Control 头，更不放通配符 *），由浏览器同源策略兜底。
            """
            origin = self.headers.get("Origin")
            if origin in _ALLOWED_ORIGINS:
                return {
                    "Access-Control-Allow-Origin": origin,
                    "Vary": "Origin",
                    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                    # 中-3（第四轮体检批次B）：预检允许头补 X-Client-Token——
                    # 令牌模式下浏览器跨源请求必须携带该自定义头，缺了即与令牌闸矛盾
                    "Access-Control-Allow-Headers": "Content-Type, X-Client-Token",
                }
            return {}

        def _deny_bad_host(self):
            """Host 校验失败的统一 403 响应。"""
            self._send_json({"ok": False, "error": "forbidden", "message": "Host 校验失败：拒绝访问"}, 403)

        def _guard_internal_error(self, exc):
            """全局异常兜底：回结构化 500，确保畸形输入不致连接中断。

            低-5（第四轮体检批次B）：对外只回错误码与异常类别摘要（类名），
            完整异常消息（可能含内部路径/实现细节）仅经 LOGGER.exception 写
            服务端日志，不再外泄到响应体。
            """
            LOGGER.exception("接口处理异常 path=%s", getattr(self, "path", "?"))
            self._send_json(
                {"ok": False, "error": "internal error", "detail": exc.__class__.__name__}, 500
            )

        def _reject_bad_json(self):
            """malformed / 非 dict 请求体的统一 400（L2 收口：与空 body 明确区分）。"""
            self._send_json(
                {
                    "ok": False,
                    "error": "bad_json",
                    "message": "请求体必须是合法 JSON 对象且 Content-Type 为 application/json",
                },
                400,
            )

        def _send_json(self, payload, status=200):
            """以 UTF-8 编码、ensure_ascii=False 输出 JSON 响应（中文不转义）。"""
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for key, value in self._cors_headers().items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            """处理预检请求：直接 204 + CORS 头 + 空 body（无路由分发、无副作用）。"""
            if not self._check_host():
                self._deny_bad_host()
                return
            self.send_response(204)
            for key, value in self._cors_headers().items():
                self.send_header(key, value)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _parse_query(self):
            """解析查询串为 dict[str, str|None]（首个值优先，空串归一为 None）。

            20261005 追加记忆核心能力参数：query/w_importance/w_time/w_rel（3D
            检索）、date（日记视图）。保持白名单制（未登记参数一律丢弃）。
            """
            qs = parse_qs(urlparse(self.path).query)
            out = {}
            for key in (
                "type", "agent_id", "limit", "offset", "q", "top_k", "enabled", "plugin_id",
                "query", "w_importance", "w_time", "w_rel", "date",
            ):
                vals = qs.get(key)
                if vals:
                    out[key] = vals[0] or None
            return out

        # ------------------------------------------------------------ 状态 / 设置 / 聊天守卫接口
        def _handle_status(self):
            """GET /api/status：轻量系统状态（供管理 API 健康探测 / 状态页占位）。"""
            self._send_json(
                {
                    "status": "ok",
                    "app": "CX-A/CX-Lite",
                    "version": LITE_VERSION,
                    "uptime_seconds": round(time.monotonic() - self._STARTED_AT, 2),
                    "companion": True,
                }
            )

        def _local_llm_source_view(self):
            """当前 local_llm.source 视图值：配置真相优先，缺失/非法按通道派生回落。

            与 :meth:`_handle_setup_status` 同口径（读取真实值而非重新派生；
            ``local_llm.source`` 不在 ``MODEL_REPOS`` 时回落
            ``model_repo_for_channel(channel)``，保证与下载线路语义一致）。
            """
            channel = normalize_channel(self._config.get("download", "channel", DEFAULT_CHANNEL))
            source = str(self._config.get("local_llm", "source", "") or "").strip().lower()
            if source not in MODEL_REPOS:
                source = model_repo_for_channel(channel)
            return source

        def _settings_view(self):
            """组装用户可读配置视图（api_key 仅脱敏回显，明文绝不外泄）。"""
            return {
                "cloud": {
                    "provider": self._config.get("cloud", "provider", "deepseek"),
                    "base_url": self._config.get("cloud", "base_url", ""),
                    # Task 5（向导选项入设置）：api_key 脱敏回显（sk-****尾4位；
                    # 未配置为空串）——明文仅存在于 ConfigManager 内存/加密落盘，
                    # 视图层任何路径不回显明文
                    "api_key": _mask_api_key(self._config.get("cloud", "api_key", "")),
                },
                # Task 5：下载线路回显（normalize_channel 归一，写坏配置不致前端空白）
                "download": {
                    "channel": normalize_channel(
                        self._config.get("download", "channel", DEFAULT_CHANNEL)
                    ),
                },
                "tts": {
                    "voice": self._config.get("tts", "voice", "cx-open"),
                    "accel": self._config.get("tts", "accel", "auto"),
                    "accel_device": self._config.get("tts", "accel_device", ""),
                },
                # 运行偏好（性能/节能双模式 spec）：设置页首帧对齐 + 切换回显
                "accel": {"mode": self._config.get("accel", "mode", "performance")},
                # ready：本地聊天运行时持有器当前是否已加载（本地模式真正本地，
                # 20261002）——前端据此提示"本地大脑是否就绪"，与 enabled 分离
                "local_llm": {
                    "enabled": bool(self._config.get("local_llm", "enabled", False)),
                    "ready": bool(_local_holder.ready()),
                    # Task 5：当前模型仓库回显（缺失/非法按通道派生回落，见上方法）
                    "source": self._local_llm_source_view(),
                    # 20261005 设置页档位管理卡「当前模型」展示数据源（缺失为空串，
                    # 前端按「不可得即隐藏」降级）
                    "model_path": str(self._config.get("local_llm", "model_path", "") or ""),
                    # 20261006 LLM 显卡切换：用户偏好回显（""/igpu/dgpu）
                    "gpu_preference": str(
                        self._config.get("local_llm", "gpu_preference", "") or ""
                    ),
                },
                # 语音交互模式（20261006 全双工降级版）：vad=传统自动断句 / duplex=全双工
                "voice": {
                    "interaction_mode": str(
                        self._config.get("voice", "interaction_mode", "vad") or "vad"
                    ),
                },
                "acp": {"enabled": bool(self._config.get("acp", "enabled", False))},
                "remote": {"enabled": bool(self._config.get("remote", "enabled", False))},
                # 聊天记忆注入开关（RAG 闭环，20261005）：设置页「聊天时自动回忆」回显
                "memory": {
                    "context_inject": bool(
                        self._config.get("memory", "context_inject", True)
                    ),
                },
                # 主动视觉（Task C）：设置页开关回显；开启后由 vision tick 线程
                # 驱动采样（默认 False，隐私红线：关闭状态绝不产生屏幕采样）
                "vision": {"enabled": bool(self._config.get("vision", "enabled", False))},
            }

        def _handle_settings_get(self):
            """GET /api/settings：返回脱敏配置视图（供前端设置页首帧对齐默认值）。"""
            self._send_json(self._settings_view())

        def _handle_settings_update(self):
            """PUT /api/settings：应用白名单补丁并热更新落盘。

            支持键：``cloud.provider``（须在 provider 白名单内）、``cloud.api_key``
            （Fernet 加密落盘，GET 视图仅脱敏回显）、``download.channel``
            （mirror/official 白名单；合法时派生写入 ``local_llm.source``——经
            ``model_repo_for_channel`` 唯一映射，applied 登记 ``download.channel``
            与 ``local_llm.source`` 各一次，与向导 ``_collect_setup_patch`` 同口径）、
            ``tts.voice``、``tts.accel`` / ``tts.accel_device``（值域白名单，
            与向导同口径；非法入 ignored 显式回显）、
            ``local_llm.enabled``（本地模式真正本地：True 经本地聊天运行时持有器
            ``ensure_started`` 幂等拉起后台加载线程——立即返回不阻塞请求、不持
            ``_CONFIG_WRITE_LOCK``；False 经 ``release()`` 释放运行时，无需重启）、
            ``vision.enabled``（主动视觉开关，Task C：布尔校验；True 后由 vision
            tick 线程下一拍驱动采样，ConfigManager 内存写即时生效无需重启）、
            ``accel.mode``（性能/节能双模式，保存时经
            ``accel_plan`` 展开全部组件落点并在既有写锁内落盘，随后按新配置重建
            语音桥）。其余键被忽略并列入 ``ignored`` 返回，供调用方校正。
            段（cloud/tts/local_llm/accel/vision）存在但非 dict 时回 400，避免
            AttributeError 冒泡。

            L2 收口语义：
            - malformed / 非 dict JSON body → 400 ``bad_json``；
            - 空 body（``{}``）→ 400 ``empty_body``（与 malformed 明确区分）；
            - GET 视图可见但白名单只读的已知键（acp/remote/vector section 及
              cloud.base_url）收集进 ``ignored`` 数组随响应回显，消除静默丢弃。

            语音桥重建（N-6）：保存 ``accel.mode`` 后关闭旧 sidecar 进程并按新
            ``tts.accel`` / ``tts.accel_device`` 重建；重建失败**不静默**——响应
            附 ``voice_backend.needs_restart=true`` 与中文 message（需重启应用）。
            """
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            if not body:
                self._send_json(
                    {"ok": False, "error": "empty_body", "message": "PUT /api/settings 要求非空配置补丁"}, 400
                )
                return
            # 先做段类型校验——段存在但非 dict 一律 400，不做 .get() 取值
            invalid_sections = [
                name for name in ("cloud", "tts", "local_llm", "accel", "vision", "download", "voice", "memory")
                if name in body and not isinstance(body[name], dict)
            ]
            if invalid_sections:
                self._send_json(
                    {"ok": False, "error": f"invalid section type: {', '.join(invalid_sections)}"}, 400
                )
                return
            ignored = []
            applied = []

            # 已知只读键回显（GET 视图可见但不在 PUT 白名单）：不再静默丢弃
            for key in _SETTINGS_READONLY_TOP_KEYS:
                if key in body:
                    ignored.append(f"{key}（GET 视图可见但白名单只读，未应用）")
            cloud_section = body.get("cloud")
            if isinstance(cloud_section, dict) and "base_url" in cloud_section:
                ignored.append("cloud.base_url（GET 视图可见但白名单只读，未应用）")

            provider = body.get("cloud", {}).get("provider")
            if provider is not None:
                if provider in CLOUD_PROVIDER_ALLOWLIST:
                    self._config.set("cloud", "provider", provider)
                    applied.append("cloud.provider")
                else:
                    ignored.append(f"cloud.provider={provider!r}（不在白名单 {CLOUD_PROVIDER_ALLOWLIST}）")

            # H-6（第三轮体检批次4）：api_key 写入白名单——生产装配下唯一可用的
            # 云端 Key 配置入口（管理 API 供另一 Agent 调用，不进前端 UI）。
            # 走 ConfigManager 既有 Fernet 加密链路落盘（save 时统一加密）；
            # GET 视图继续不含 api_key，applied 只回键名不回显值。
            api_key = body.get("cloud", {}).get("api_key")
            if api_key is not None:
                if isinstance(api_key, str) and api_key.strip():
                    self._config.set("cloud", "api_key", api_key.strip())
                    applied.append("cloud.api_key")
                else:
                    ignored.append("cloud.api_key（必须为非空字符串）")

            # Task 5（向导选项入设置）：下载线路——mirror/official 白名单；合法时
            # 派生写入 local_llm.source（model_repo_for_channel 唯一映射），applied
            # 登记 download.channel 与 local_llm.source 各一次（与向导
            # _collect_setup_patch 同口径）；非法值入 ignored 显式回显，不静默丢弃。
            channel = body.get("download", {}).get("channel")
            if channel is not None:
                normalized_channel = channel.strip().lower() if isinstance(channel, str) else ""
                if normalized_channel in CHANNELS:
                    self._config.set("download", "channel", normalized_channel)
                    applied.append("download.channel")
                    self._config.set("local_llm", "source", model_repo_for_channel(normalized_channel))
                    applied.append("local_llm.source")
                else:
                    ignored.append(
                        f"download.channel={channel!r}（不在白名单 {list(CHANNELS)}）"
                    )

            voice = body.get("tts", {}).get("voice")
            if voice is not None:
                if isinstance(voice, str) and voice.strip():
                    self._config.set("tts", "voice", voice.strip())
                    applied.append("tts.voice")
                else:
                    ignored.append("tts.voice（必须为非空字符串）")

            # Task 5（向导选项入设置）：语音加速两键——值域白名单校验（与向导
            # _collect_setup_patch 同口径，_TTS_ACCEL_VALUES / _TTS_ACCEL_DEVICE_VALUES
            # 单一真相源），合法写入并在 applied 登记、非法入 ignored 显式回显。
            tts_accel = body.get("tts", {}).get("accel")
            tts_accel_applied = False
            if tts_accel is not None:
                normalized_accel = (
                    tts_accel.strip().lower() if isinstance(tts_accel, str) else ""
                )
                if normalized_accel in _TTS_ACCEL_VALUES:
                    self._config.set("tts", "accel", normalized_accel)
                    applied.append("tts.accel")
                    tts_accel_applied = True
                else:
                    ignored.append(
                        f"tts.accel={tts_accel!r}（不在白名单 {list(_TTS_ACCEL_VALUES)}）"
                    )

            tts_device = body.get("tts", {}).get("accel_device")
            if tts_device is not None:
                normalized_device = (
                    tts_device.strip().lower() if isinstance(tts_device, str) else None
                )
                if normalized_device in _TTS_ACCEL_DEVICE_VALUES:
                    self._config.set("tts", "accel_device", normalized_device)
                    applied.append("tts.accel_device")
                    tts_accel_applied = True
                else:
                    ignored.append(
                        f"tts.accel_device={tts_device!r}"
                        f"（不在白名单 {list(_TTS_ACCEL_DEVICE_VALUES)}）"
                    )

            local_enabled = body.get("local_llm", {}).get("enabled")
            if local_enabled is not None:
                if isinstance(local_enabled, bool):
                    self._config.set("local_llm", "enabled", local_enabled)
                    applied.append("local_llm.enabled")
                    # 本地模式动态接线（本地模式真正本地，20261002）：写 config 后
                    # True → 幂等拉起后台加载线程（立即返回，不阻塞请求、不持
                    # _CONFIG_WRITE_LOCK）；False → 释放运行时（close 幂等）。
                    if local_enabled:
                        _local_holder.ensure_started(self._config)
                    else:
                        _local_holder.release()
                else:
                    ignored.append("local_llm.enabled（必须为布尔）")

            # LLM 显卡切换（20261006）：gpu_preference 白名单校验（""/igpu/dgpu）。
            # 保存后经唯一真相源 accel_plan 重算全部落点（backend 等随动），且
            # local_llm.enabled 开启时释放并重新拉起运行时——backend 变化必须
            # 换构建目录重启 llama-server（CUDA 构建 ↔ Vulkan 构建不可热切）。
            gpu_pref = body.get("local_llm", {}).get("gpu_preference")
            if gpu_pref is not None:
                pref_norm = (
                    gpu_pref.strip().lower() if isinstance(gpu_pref, str) else None
                )
                if pref_norm is not None and pref_norm in _LLM_GPU_PREFERENCE_VALUES:
                    self._config.set("local_llm", "gpu_preference", pref_norm)
                    applied.append("local_llm.gpu_preference")
                    try:
                        profile = detect_profile()
                    except Exception as exc:  # noqa: BLE001 - 探测失败保守空画像
                        LOGGER.warning("切换 LLM 显卡时硬件探测失败，按空画像推导：%s", exc)
                        profile = {}
                    plan = accel_plan(
                        profile,
                        self._config.get("accel", "mode", "performance"),
                        llm_gpu_preference=pref_norm,
                    )
                    _apply_accel_plan_to_config(self._config, plan)
                    # 热重建：enabled 开启时旧运行时释放（close 幂等）→ 新偏好
                    # 后台重新加载；关闭时不加载（下次开启自然按新偏好拉起）。
                    if bool(self._config.get("local_llm", "enabled", False)):
                        _local_holder.release()
                        _local_holder.ensure_started(self._config)
                        applied.append(
                            "local_llm.reload_hint（本地大脑正在按新显卡重新加载；"
                            "嵌入服务将在重启应用后同步切换）"
                        )
                else:
                    ignored.append(
                        f"local_llm.gpu_preference={gpu_pref!r}"
                        f"（不在白名单 {list(_LLM_GPU_PREFERENCE_VALUES)}）"
                    )

            # 主动视觉开关（Task C）：布尔校验，非法入 ignored 显式回显。
            # ConfigManager 内存写即时生效——vision tick 线程的 run_once 每拍
            # 现读 vision.enabled（热更新段），开启后下一拍即开始采样，无需重启。
            vision_enabled = body.get("vision", {}).get("enabled")
            if vision_enabled is not None:
                if isinstance(vision_enabled, bool):
                    self._config.set("vision", "enabled", vision_enabled)
                    applied.append("vision.enabled")
                else:
                    ignored.append("vision.enabled（必须为布尔）")

            # 聊天记忆注入开关（RAG 闭环，20261005）：布尔校验，非法入 ignored。
            # memory 为热更段——内存写即时生效（_build_memory_context 逐次读取）。
            context_inject = body.get("memory", {}).get("context_inject")
            if context_inject is not None:
                if isinstance(context_inject, bool):
                    self._config.set("memory", "context_inject", context_inject)
                    applied.append("memory.context_inject")
                else:
                    ignored.append("memory.context_inject（必须为布尔）")

            # 语音交互模式（20261006 全双工降级版）：vad/duplex 白名单校验，
            # 非法入 ignored。会话开启时逐次读取（voiceSession 每次会话启动现读）。
            interaction_mode = body.get("voice", {}).get("interaction_mode")
            if interaction_mode is not None:
                mode_norm = (
                    interaction_mode.strip().lower()
                    if isinstance(interaction_mode, str) else ""
                )
                if mode_norm in _VOICE_INTERACTION_MODES:
                    self._config.set("voice", "interaction_mode", mode_norm)
                    applied.append("voice.interaction_mode")
                else:
                    ignored.append(
                        f"voice.interaction_mode={interaction_mode!r}"
                        f"（不在白名单 {list(_VOICE_INTERACTION_MODES)}）"
                    )

            # 运行偏好（性能/节能双模式）：保存 accel.mode 时经唯一真相源 accel_plan
            # 展开全部组件落点（tts.accel / tts.accel_device / asr / local_llm /
            # embedding），与安装链、向导共用同一决策函数（禁止本处另写判断）。
            accel_mode_raw = body.get("accel", {}).get("mode")
            accel_applied = False
            if accel_mode_raw is not None:
                normalized_mode = (
                    accel_mode_raw.strip().lower() if isinstance(accel_mode_raw, str) else ""
                )
                if normalized_mode in _ACCEL_MODES:
                    try:
                        profile = detect_profile()
                    except Exception as exc:  # noqa: BLE001 - 探测失败保守空画像推导
                        LOGGER.warning("保存运行偏好时硬件探测失败，已按空画像保守推导：%s", exc)
                        profile = {}
                    plan = accel_plan(
                        profile,
                        normalized_mode,
                        llm_gpu_preference=str(
                            self._config.get("local_llm", "gpu_preference", "") or ""
                        ),
                    )
                    _apply_accel_plan_to_config(self._config, plan)
                    applied.append("accel.mode")
                    for key in ("tts.accel", "tts.accel_device", "asr.device",
                                "local_llm.device", "embedding.device"):
                        applied.append(key)
                    accel_applied = True
                else:
                    ignored.append(
                        f"accel.mode={accel_mode_raw!r}（不在白名单 {list(_ACCEL_MODES)}）"
                    )

            voice_backend = None
            if applied:
                try:
                    # Task 7：与下载线程共用同一把模块级写锁（配置落盘串行化）
                    with _CONFIG_WRITE_LOCK:
                        self._config.save()
                except OSError as exc:  # noqa: BLE001 - 落盘失败不影响内存生效
                    self._send_json(
                        {"ok": False, "error": "config_save_failed", "message": str(exc),
                         "applied": applied, "ignored": ignored, "config": self._settings_view()},
                        status=500,
                    )
                    return
                # 语音桥重建（N-6）：配置已落盘 → 关旧 sidecar → 按新 tts.accel 重建；
                # 失败不静默（响应附 needs_restart + 中文 message）。Task 5：
                # 单独热更 tts.accel / tts.accel_device 同样改变 sidecar 参数，
                # 与 accel.mode 展开路径一并不重建则新值不生效，故一并触发。
                if accel_applied or tts_accel_applied:
                    voice_backend = self._rebuild_voice_backend()

            response = {"ok": True, "applied": applied, "ignored": ignored,
                        "config": self._settings_view()}
            if voice_backend is not None:
                response["voice_backend"] = voice_backend
            self._send_json(response)

        def _rebuild_voice_backend(self):
            """按当前配置重建语音后端（关闭旧 sidecar → 用新参数重建）。

            仅重建语音后端（对话历史保留）；重建失败**不静默**，返回
            ``{rebuilt: False, needs_restart: True, message: ...}`` 明确提示需重启应用。
            """
            try:
                old_voice = self._voice
                _close_voice_backends(old_voice)
                components = build_default_pipeline(self._config)
                new_voice = LiteVoicePipeline(
                    vad=components["vad"],
                    asr=components["asr"],
                    tts=components["tts"],
                    cloud=None,
                    judge=components["judge"],
                )
                try:
                    # 重建的是后端，非会话：保留既有对话历史
                    new_voice.messages = list(getattr(old_voice, "messages", []) or [])
                except Exception:  # noqa: BLE001 - 历史保留失败不影响重建
                    pass
                # 类属性持久化：BaseHTTPRequestHandler 每请求新建实例，实例属性会丢失
                type(self)._voice = new_voice
                return {"rebuilt": True, "needs_restart": False, "message": "运行偏好已切换并生效"}
            except Exception as exc:  # noqa: BLE001 - 重建失败明确提示需重启，不静默
                LOGGER.warning("语音加速切换重建失败：%s", exc)
                return {
                    "rebuilt": False,
                    "needs_restart": True,
                    "message": (
                        f"运行偏好已保存，但语音加速切换失败（{type(exc).__name__}）："
                        "需重启应用后生效"
                    ),
                }

        # ------------------------------------------------------------ 首启向导接口族（Task 6 / Task 7）
        def _handle_setup_status(self):
            """GET /api/setup/status：向导门控与当前下载源取值（**不含 api_key**）。

            返回 ``{completed, wizard_required, download:{channel}, local_llm_source}``：
            - ``completed`` 读 ``setup.completed``（老 config 无 setup 段时
              ConfigManager 已按升级兼容补 ``True``）；
            - ``wizard_required = not completed``，供前端首帧决定是否覆盖主界面；
            - ``channel`` 经 ``normalize_channel`` 归一（写坏的配置不导致前端空白）；
            - ``local_llm_source`` 直接读配置 ``local_llm.source`` 真相（可能被手工
              改过，读取真实值而非重新派生）；缺失或非法时回落
              ``model_repo_for_channel(channel)``，保证与通道语义一致。
            """
            completed = bool(self._config.get("setup", "completed", False))
            channel = normalize_channel(self._config.get("download", "channel", DEFAULT_CHANNEL))
            source = str(self._config.get("local_llm", "source", "") or "").strip().lower()
            if source not in MODEL_REPOS:
                source = model_repo_for_channel(channel)
            self._send_json(
                {
                    "completed": completed,
                    "wizard_required": not completed,
                    "download": {"channel": channel},
                    "local_llm_source": source,
                }
            )

        def _degraded_profile(self, exc):
            """硬件探测异常时的降级画像（保守走云，不返回 5xx）。"""
            return {
                "cpu_cores": None,
                "ram_gb": None,
                "gpu_vendor": "cpu",
                "vram_gb": None,
                "cuda_version": None,
                "disk_free_gb": None,
                "probe_notes": [f"硬件探测失败，已按最保守结论（建议走云端）处理：{exc.__class__.__name__}"],
            }

        def _degraded_recommendation(self, exc):
            """推荐推导异常时的降级结论（``local_llm.enabled=False`` 走云）。

            加速剖面按空画像保守推导（默认节能 + 组件落点回 CPU），保证
            ``recommendation.accel`` 结构完整、前端不因缺字段而崩。
            """
            note = f"硬件推荐推导失败，已降级为「先走云端」：{exc.__class__.__name__}"
            plan = accel_plan({}, derive_default_mode({}))
            return {
                "use_local": False,
                "device": "cpu",
                "tier": DEFAULT_TIER,
                "config_patch": {"local_llm": {"enabled": False, "device": "cpu"}},
                "model": None,
                "reasons": [note],
                "probe_notes": [note],
                "accel": _accel_profile_from_plan(plan),
            }

        def _handle_setup_recommend(self):
            """GET /api/setup/recommend：硬件画像 + 推荐配置 + 候选档位 + 建议仓库。

            - ``profile``：``detect_profile()`` 产出（其内部已对探测失败降级，此处
              仍再兜一层 try/except——探测能力缺失时**绝不返回 5xx**）；
            - ``recommendation``：``recommend_for(profile)`` 产出（异常同样降级为走云）；
            - ``tiers``：``MODEL_TIERS`` 转列表，每项补 ``tier`` 键；
            - ``suggested_source``：由当前通道经 ``model_repo_for_channel`` 唯一派生
              （镜像通道 → 魔塔 ``modelscope``，官方通道 → ``huggingface``）。
            """
            try:
                profile = detect_profile()
            except Exception as exc:  # noqa: BLE001 - 探测异常必须降级而非 5xx
                LOGGER.warning("硬件画像探测异常，已降级：%s", exc)
                profile = self._degraded_profile(exc)
            try:
                recommendation = recommend_for(profile)
            except Exception as exc:  # noqa: BLE001 - 推荐推导异常同样降级
                LOGGER.warning("硬件推荐推导异常，已降级：%s", exc)
                recommendation = self._degraded_recommendation(exc)
            tiers = [dict(entry, tier=name) for name, entry in MODEL_TIERS.items()]
            channel = normalize_channel(self._config.get("download", "channel", DEFAULT_CHANNEL))
            # 加速剖面（唯一真相源）：优先取 recommend_for 产物；缺失时按画像兜底
            # 重算（降级路径不得少字段——前端据此渲染模式询问与口语化结论）。
            accel = recommendation.get("accel") or _accel_profile_from_plan(
                accel_plan(profile, derive_default_mode(profile))
            )
            self._send_json(
                {
                    "profile": profile,
                    "recommendation": recommendation,
                    "accel": accel,
                    "tiers": tiers,
                    "suggested_source": model_repo_for_channel(channel),
                }
            )

        def _collect_setup_patch(self, body, applied, ignored):
            """收集向导提交的白名单键（非法取值入 ``ignored`` 显式回显，不静默丢弃）。

            ``download.channel`` 是面向用户的唯一选择（下载路线），``local_llm.source``
            随之**由通道派生**（经 ``model_repo_for_channel`` 唯一映射）：

            - 通道合法时，服务端派生并写入 ``local_llm.source``，``applied`` 登记
              ``download.channel`` 与 ``local_llm.source`` 各一次；
            - 请求同时显式给出 ``local_llm.source``：与派生值一致则不重复登记（照常
              applied），不一致则**不生效**并列入 ``ignored``（说明由下载路线决定）；
            - 请求未给通道（或通道非法）时保留既有手工配置能力：显式且合法的
              ``local_llm.source`` 照常 applied；
            - 取值不在白名单（通道 / 仓库）一律列入 ``ignored``，不静默丢弃。

            :return: ``dict[(section, key), value]``——显式传入且合法的配置补丁。
            """
            pending = {}
            cloud = body.get("cloud") or {}
            download = body.get("download") or {}
            local = body.get("local_llm") or {}
            accel = body.get("accel") or {}
            tts = body.get("tts") or {}

            provider = cloud.get("provider")
            if provider is not None:
                if provider in CLOUD_PROVIDER_ALLOWLIST:
                    pending[("cloud", "provider")] = provider
                    applied.append("cloud.provider")
                else:
                    ignored.append(f"cloud.provider={provider!r}（不在白名单 {CLOUD_PROVIDER_ALLOWLIST}）")

            api_key = cloud.get("api_key")
            if api_key is not None:
                if isinstance(api_key, str) and api_key.strip():
                    # 走 ConfigManager 既有 Fernet 加密链路（save 时统一加密）
                    pending[("cloud", "api_key")] = api_key.strip()
                    applied.append("cloud.api_key")
                else:
                    ignored.append("cloud.api_key（必须为非空字符串）")

            # 通道（唯一用户选择）：合法则派生模型仓库，作为 local_llm.source 的唯一来源
            derived_source = None
            channel = download.get("channel")
            if channel is not None:
                normalized = channel.strip().lower() if isinstance(channel, str) else ""
                if normalized in CHANNELS:
                    pending[("download", "channel")] = normalized
                    applied.append("download.channel")
                    derived_source = model_repo_for_channel(normalized)
                    pending[("local_llm", "source")] = derived_source
                    applied.append("local_llm.source")
                else:
                    ignored.append(f"download.channel={channel!r}（不在白名单 {list(CHANNELS)}）")

            source = local.get("source")
            if source is not None:
                normalized_source = source.strip().lower() if isinstance(source, str) else ""
                if normalized_source not in MODEL_REPOS:
                    ignored.append(f"local_llm.source={source!r}（不在白名单 {list(MODEL_REPOS)}）")
                elif derived_source is not None and normalized_source != derived_source:
                    # 与派生值冲突：不生效（派生值已写入并 applied，不重复登记）
                    ignored.append(
                        f"local_llm.source={normalized_source!r}"
                        "（由 download.channel 决定，未单独生效）"
                    )
                elif derived_source is None:
                    # 未给通道：保留手工配置能力，显式且合法的仓库照常生效
                    pending[("local_llm", "source")] = normalized_source
                    applied.append("local_llm.source")

            enabled = local.get("enabled")
            if enabled is not None:
                if isinstance(enabled, bool):
                    pending[("local_llm", "enabled")] = enabled
                    applied.append("local_llm.enabled")
                else:
                    ignored.append("local_llm.enabled（必须为布尔）")

            # 运行偏好（性能/节能双模式）：accel.mode / tts.accel / tts.accel_device
            # 白名单校验——合法入 pending 应用，非法入 ignored 显式回显（值域冻结，
            # 禁止扩张）。
            mode = accel.get("mode")
            if mode is not None:
                normalized_mode = mode.strip().lower() if isinstance(mode, str) else ""
                if normalized_mode in _ACCEL_MODES:
                    pending[("accel", "mode")] = normalized_mode
                    applied.append("accel.mode")
                else:
                    ignored.append(f"accel.mode={mode!r}（不在白名单 {list(_ACCEL_MODES)}）")

            tts_accel = tts.get("accel")
            if tts_accel is not None:
                normalized_accel = (
                    tts_accel.strip().lower() if isinstance(tts_accel, str) else ""
                )
                if normalized_accel in _TTS_ACCEL_VALUES:
                    pending[("tts", "accel")] = normalized_accel
                    applied.append("tts.accel")
                else:
                    ignored.append(
                        f"tts.accel={tts_accel!r}（不在白名单 {list(_TTS_ACCEL_VALUES)}）"
                    )

            tts_device = tts.get("accel_device")
            if tts_device is not None:
                normalized_device = (
                    tts_device.strip().lower() if isinstance(tts_device, str) else None
                )
                if normalized_device in _TTS_ACCEL_DEVICE_VALUES:
                    pending[("tts", "accel_device")] = normalized_device
                    applied.append("tts.accel_device")
                else:
                    ignored.append(
                        f"tts.accel_device={tts_device!r}"
                        f"（不在白名单 {list(_TTS_ACCEL_DEVICE_VALUES)}）"
                    )

            return pending

        def _apply_recommended_patch(self, body, pending, applied, ignored):
            """``apply_recommended=true`` 时把推荐 ``config_patch`` 并入补丁（低优先级）。

            **显式传入的键优先于推荐补丁**：已在 ``pending`` 中的 (section, key)
            不被推荐值覆盖（用户手改覆盖推荐）。
            """
            apply_recommended = body.get("apply_recommended")
            if apply_recommended is None:
                return
            if not isinstance(apply_recommended, bool):
                ignored.append("apply_recommended（必须为布尔）")
                return
            if not apply_recommended:
                return
            try:
                recommendation = recommend_for(detect_profile())
            except Exception as exc:  # noqa: BLE001 - 探测失败不阻断向导完成
                LOGGER.warning("采纳推荐配置时硬件探测异常，已跳过推荐补丁：%s", exc)
                ignored.append("apply_recommended（硬件探测失败，未应用推荐配置）")
                return
            patch = recommendation.get("config_patch") or {}
            for section, keys in patch.items():
                if not isinstance(keys, dict):
                    continue
                for key, value in keys.items():
                    if (section, key) in pending:
                        continue  # 显式传入优先
                    pending[(section, key)] = value
                    applied.append(f"config_patch.{section}.{key}")

        def _handle_setup_complete(self):
            """POST /api/setup/complete：应用向导选择并置 ``setup.completed = true``。

            口径与既有 ``/api/settings`` 对齐：
            - malformed / 非 dict JSON body → 400 ``bad_json``；
            - 空 body（``{}``）→ 400 ``empty_body``；
            - ``cloud`` / ``download`` / ``local_llm`` 段存在但非 dict → 400；
            - 非法取值（provider / channel / source 白名单外、api_key 非字符串、
              enabled 非布尔）**不生效**但列入 ``ignored`` 显式回显；
            - ``local_llm.source`` 由 ``download.channel`` 派生（``model_repo_for_channel``
              唯一映射）：显式给出且与派生值不一致时不生效并入 ``ignored``；
              未给通道时保留手工配置能力（显式且合法照常生效）；
            - ``apply_recommended=true`` 合并推荐 ``config_patch``（显式键优先）；
            - ``api_key`` 可省略（稍后补填），省略时向导仍可完成。

            配置写入（补丁 + setup 段 + save）在同一把模块级写锁内完成，
            与下载线程的 ``local_llm.model_path`` 写回串行。
            """
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            if not body:
                self._send_json(
                    {"ok": False, "error": "empty_body", "message": "POST /api/setup/complete 要求非空配置"},
                    400,
                )
                return
            invalid_sections = [
                name for name in ("cloud", "download", "local_llm", "accel", "tts")
                if name in body and not isinstance(body[name], dict)
            ]
            if invalid_sections:
                self._send_json(
                    {"ok": False, "error": f"invalid section type: {', '.join(invalid_sections)}"}, 400
                )
                return

            applied = []
            ignored = []
            pending = self._collect_setup_patch(body, applied, ignored)
            self._apply_recommended_patch(body, pending, applied, ignored)

            completed_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            try:
                # 配置落盘串行化：与下载线程共用同一把模块级写锁
                with _CONFIG_WRITE_LOCK:
                    for (section, key), value in pending.items():
                        self._config.set(section, key, value)
                    self._config.set("setup", "completed", True)
                    self._config.set("setup", "completed_at", completed_at)
                    self._config.save()
            except OSError as exc:  # noqa: BLE001 - 落盘失败：明确回错，不谎报完成
                self._send_json(
                    {"ok": False, "error": "config_save_failed", "message": str(exc),
                     "applied": applied, "ignored": ignored, "config": self._settings_view()},
                    status=500,
                )
                return
            self._send_json(
                {
                    "ok": True,
                    "applied": applied,
                    "ignored": ignored,
                    "setup": {"completed": True, "completed_at": completed_at},
                    "config": self._settings_view(),
                }
            )

        def _handle_setup_model_download(self):
            """POST /api/setup/model/download：启动本地小 LLM 后台下载（幂等）。

            body（可选）``{source, tier}``；缺省取配置 ``local_llm.source`` 与默认档
            ``E2B-Q4``。显式 ``source`` 须在 ``MODEL_REPOS`` 白名单内，否则 400（中文
            错误，不静默改换下载来源）。下载在后台 daemon 线程执行，本端点**立即
            返回**；进行中重复调用返回 ``already_running: true`` 且不启动第二个线程。
            未知档位 400（中文错误），不静默下载错误文件。
            """
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            source = body.get("source")
            if source is not None:
                normalized = source.strip().lower() if isinstance(source, str) else ""
                if normalized not in MODEL_REPOS:
                    self._send_json(
                        {
                            "ok": False,
                            "error": "bad_request",
                            "message": f"未知模型仓库：{source!r}，仅支持 {list(MODEL_REPOS)}",
                        },
                        400,
                    )
                    return
                source = normalized
            try:
                result = self._download_manager.start(source=source, tier=body.get("tier"))
            except ValueError as exc:
                self._send_json({"ok": False, "error": "bad_request", "message": str(exc)}, 400)
                return
            self._send_json(result)

        def _handle_setup_model_progress(self):
            """GET /api/setup/model/progress：即时返回下载进度（不阻塞服务）。

            快照由下载管理器加锁读取，绝不等待下载线程——单线程 HTTPServer 在
            1.7GB 下载期间仍可正常响应其他端点。
            """
            self._send_json(self._download_manager.snapshot())

        def _handle_setup_model_cancel(self):
            """POST /api/setup/model/cancel：取消进行中的下载（保留 ``.tmp`` 供续传）。

            置取消标记后立即返回；下载线程在下一次进度回调处中断，临时文件由
            ``LlmDownloader`` 的异常路径保留。
            """
            self._send_json(self._download_manager.cancel())

        def _handle_chat_send_guard(self):
            """POST /api/chat/messages：聊天服务未启用守卫（避免直连误 404）。"""
            self._send_json(
                {
                    "ok": False,
                    "error": CHAT_SERVICE_DISABLED,
                    "message": "聊天服务未启用（本期前端走 Mock 演示），请接入 lite/cloud 云端适配后启用",
                }
            )

        def _handle_chat_history(self):
            """GET /api/chat/history：返回最近对话历史（20261004 持久化接线）。

            ``{"ok": True, "messages": [...]}``（最近 ``_CHAT_HISTORY_CAP`` 条，
            每条 ``{role, content, time}``）；文件缺失 / 损坏返回空列表（不 5xx，
            前端按无历史渲染）。
            """
            self._send_json({"ok": True, "messages": _load_chat_history(self._chat_history_file)})

        # ------------------------------------------------------------ 表情聊天（Task H3）
        def _build_memory_context(self, message, agent_id):
            """聊天记忆注入（RAG 闭环，20261005）：检索 top-K 记忆拼【回忆】上下文块。

            - 开关 ``memory.context_inject``（默认开，热更段逐次读取即时生效）；
            - memory-agent 跳过（其工具环自带检索，双份注入语义混乱）；
            - 检索失败仅告警不阻塞聊天（功能容错，非向量库降级——向量库后端
              的禁止降级语义见 _build_vector_store）；
            - 空库不注入空块（无回忆时保持 prompt 干净）。

            :return: str 上下文块（含「【回忆】」头）或空串。
            """
            if agent_id == MEMORY_AGENT_ID:
                return ""
            try:
                enabled = bool(self._config.get("memory", "context_inject", True))
            except Exception:  # noqa: BLE001 - 配置读取异常按开启处理（默认行为）
                enabled = True
            if not enabled or self._pipeline is None:
                return ""
            try:
                result = self._pipeline.retrieve(
                    message, agent_id=agent_id or "default", top_k=_MEMORY_INJECT_TOP_K
                )
            except Exception as exc:  # noqa: BLE001 - 检索失败不阻塞聊天主链路
                LOGGER.warning("聊天记忆检索失败（不阻塞聊天）：%s", exc)
                return ""
            memories = (result or {}).get("memories") or []
            if not memories:
                return ""
            return str((result or {}).get("context_text") or "").strip()

        def _build_chat_messages(self, message, agent_id):
            """组装表情聊天的消息列表（system 人设 + 记忆注入 + user 输入）。

            :param message: 用户输入文本（已 strip 非空）
            :param agent_id: 归一化后的 agent_id；命中本地 Agent 时以其 persona
                作为人设前缀，未命中（含 AgentNotFound）回落默认提示
            :return: OpenAI 兼容消息列表 [{role, content}, ...]
            """
            persona = None
            try:
                persona = str(self._manager.get(agent_id).persona or "").strip()
            except AgentNotFound:
                persona = None
            if persona:
                system = f"你的角色设定：{persona}\n{_CHAT_DEFAULT_SYSTEM}"
            else:
                system = _CHAT_DEFAULT_SYSTEM
            # 记忆注入（RAG 闭环，20261005）：开关开且有回忆时把【回忆】块拼入 system，
            # 让聊天回复自然引用记忆（memory-agent 跳过、空库/失败不注入）
            memory_block = self._build_memory_context(message, agent_id)
            if memory_block:
                system = f"{system}\n{memory_block}"
            return [
                {"role": "system", "content": system},
                {"role": "user", "content": message},
            ]

        def _handle_chat_message(self):
            """POST /api/chat/message：表情聊天端点（Task H3）。

            body {message, agent_id?}：
            - message 必填非空，否则 400；
            - 走离线兜底管理器（Task C3）：在线透传 CloudAdapter 流式拼接、离线按
              本地小 LLM 兜底/提示；无兜底管理器时退回直连 CloudAdapter 旧路径。
              回复经 EmotionTagParser 解析 [emotion:x] 标签后返回
              {ok:true, clean_text, mood, raw}（clean_text 已剥离已识别标签、未知
              标签原文保留；mood 为首个已识别情绪，默认 calm；raw 为原始带标签全文）；
            - 兜底管理器 status != "cloud"（离线提示/本地承接）→ 附带 offline:true；
              兜底管理器自身抛 CloudConfigError / CloudUnavailableError（保底）→
              返回 200 固定友好文案 + offline:true，不抛 5xx——前端把提示文案作为
              回复气泡真实展示（后端真实回传，非前端伪造）；
            - 记忆管理助手工具环（20261005，spec: align-wizard-settings-memory-pet）：
              agent_id=memory-agent 时，回复中的 [memory:op {...}] 指令标签经
              BuiltinToolRegistry 记忆工具逐个执行，执行结果以「系统回执」user 消息
              回注再调一轮 LLM（最多 _MEMORY_MAX_TOOL_ROUNDS 轮，不无限递归）；
              最终 clean_text 剥离全部 [memory:...] 标签（协议细节不泄漏到展示层）；
              工具执行失败时回执带中文失败原因，由助手二次回复向用户说明（不崩）。
              memory-agent 对话不落 chat_history（管理通道非日常对话，落盘会污染
              悬浮窗/桌宠的日常历史气泡——取舍依据见下方落盘条件注释）。
            """
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            message = str(body.get("message") or "").strip()
            if not message:
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "message 为必填字段且不能为空"},
                    400,
                )
                return
            agent_id = self._sanitize_agent_id(body.get("agent_id"))
            messages = self._build_chat_messages(message, agent_id)
            # 离线标记：仅当兜底管理器确认非云端承接（离线提示/本地兜底）时为真
            offline = False
            try:
                # 流式拼接完整回复（H3 务实落地：后端非流式下发，前端整段渲染）
                if self._chat_fallback is not None:
                    full_text = "".join(self._chat_fallback.chat(messages))
                    offline = getattr(self._chat_fallback, "status", "cloud") != "cloud"
                else:
                    full_text = "".join(self._chat_cloud.chat(messages))
            except (CloudConfigError, CloudUnavailableError) as exc:
                LOGGER.warning(
                    "表情聊天云端不可用（%s）：%s", exc.__class__.__name__, exc
                )
                self._send_json(
                    {
                        "ok": True,
                        "clean_text": CHAT_OFFLINE_TEXT,
                        "mood": "calm",
                        "raw": CHAT_OFFLINE_TEXT,
                        "offline": True,
                    }
                )
                return
            # ---- 记忆管理助手工具环（20261005）----
            # agent_id=memory-agent 时：解析首轮回复中的 [memory:op {...}] 标签
            # （可零/多个），经 self._registry（make_handler 闭包绑定）逐个执行，
            # 结果以系统回执 user 消息回注二次调用；回注轮最多 _MEMORY_MAX_TOOL_ROUNDS。
            # 本地小 LLM 承接（offline=True）同样可能产出标签，故不按 offline 排除；
            # 无标签时解析零成本直接跳过，其他 agent 完全不受影响。
            if agent_id == MEMORY_AGENT_ID and self._registry is not None:
                pending_ops = _extract_memory_ops(full_text)
                follow_up_messages = list(messages)
                for _round in range(_MEMORY_MAX_TOOL_ROUNDS):
                    if not pending_ops:
                        break
                    follow_up_messages.append({"role": "assistant", "content": full_text})
                    for op, args, raw_tag in pending_ops[:_MEMORY_MAX_OPS_PER_TURN]:
                        tool_id = _MEMORY_OP_TO_TOOL.get(op)
                        if tool_id is None:
                            outcome = {"success": False, "error": f"未知记忆指令 op：{op!r}"}
                        elif args is None:
                            outcome = {"success": False, "error": "指令参数必须是合法 JSON 对象"}
                        else:
                            outcome = self._registry.call(tool_id, args)
                        follow_up_messages.append(
                            {"role": "user", "content": _memory_tool_result_message(op, raw_tag, outcome)}
                        )
                    try:
                        # 回注二次调用：messages + assistant 首轮回复 + 逐条工具回执
                        if self._chat_fallback is not None:
                            full_text = "".join(self._chat_fallback.chat(follow_up_messages))
                        else:
                            full_text = "".join(self._chat_cloud.chat(follow_up_messages))
                    except (CloudConfigError, CloudUnavailableError) as exc:
                        # 二轮调用云端不可用：剥离标签后以首轮文本兜底返回（不 5xx）
                        LOGGER.warning(
                            "记忆助手回注轮云端不可用（%s），以首轮回复剥标签兜底", exc.__class__.__name__
                        )
                        full_text = _strip_memory_tags(full_text)
                        break
                    pending_ops = _extract_memory_ops(full_text)
                # 轮次耗尽仍有标签：下方 _strip_memory_tags 强制剥离，不再递归
            # 最终展示文本：memory-agent 剥离全部 [memory:...] 指令标签（含轮次耗尽
            # 残留与 JSON 非法残缺标签）；其他 agent 保持 EmotionTagParser 既有语义
            display_text = _strip_memory_tags(full_text) if agent_id == MEMORY_AGENT_ID else full_text
            parsed = EmotionTagParser().parse(display_text)
            payload = {
                "ok": True,
                "clean_text": parsed["clean_text"],
                "mood": parsed["mood"],
                "raw": full_text,
            }
            # 保持既有成功响应形状：offline 仅在为真时附带，不新增噪声字段
            if offline:
                payload["offline"] = True
            # 对话持久化（20261004）：真实对话（云端与本地承接一视同仁）落盘；
            # 离线占位文案（断网 / 本地未就绪 / 配置未完成提示，offline:true 走
            # 成功路径）不落盘——不是真实对话，落盘会污染历史；
            # memory-agent（20261005）为记忆页管理通道，指令往返非日常对话，
            # 不落盘——避免管理指令污染悬浮窗/桌宠的日常聊天历史。
            if (
                agent_id != MEMORY_AGENT_ID
                and not (offline and parsed["clean_text"] in _offline_placeholder_texts())
            ):
                _append_chat_history(
                    message, parsed["clean_text"], path=self._chat_history_file
                )
            self._send_json(payload)

        def _handle_chat_message_stream(self):
            """POST /api/chat/message_stream：表情聊天流式版（NDJSON chunked 增量下发）。

            语音低延迟专用（20261010_模块0_语音交互延迟优化）：LLM 流式增量逐帧
            下发，前端增量剥 ``[emotion:x]`` 标签并按句边界切句，首个完整句即送
            TTS——不等完整回复（本地小模型生成 10~30 字回复的 1~3 秒里 TTS 不再
            空转，首响延迟显著下降）。协议（NDJSON 逐行）：

            - ``{"delta": "<raw 文本增量>"}``（原样增量，含标签，剥离由前端做）；
            - ``{"done": true, "clean_text": ..., "mood": ..., "raw": ..., "offline"?}``
              （mood/clean_text 为后端权威解析，前端流中提取值仅作提前驱动）；
            - 流中途生成失败：``{"error": "<说明>"}`` 后紧跟 done 帧（已收文本照常可用）。

            - 首帧探测：先拉第一个增量再切 NDJSON——云端不可用
              （CloudConfigError/CloudUnavailableError）在此即抛，回退为离线占位
              文案的 NDJSON 下发（前端把提示文案照常朗读/展示，语义与既有 200
              JSON offline 形状一致，前端无需按 Content-Type 分流）。
            - memory-agent 工具环需整轮完整文本做指令解析，流式无收益：本端点
              400 引导走既有非流式端点（语音会话默认 agent 为 "default"，不受影响）。
            - 落盘：done 时与非流式同条件 append chat_history（memory-agent 除外、
              离线占位文案不落盘）；客户端中断（打断）不落盘——被打断的回复不算
              完整对话。
            """
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            message = str(body.get("message") or "").strip()
            if not message:
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "message 为必填字段且不能为空"},
                    400,
                )
                return
            agent_id = self._sanitize_agent_id(body.get("agent_id"))
            if agent_id == MEMORY_AGENT_ID:
                self._send_json(
                    {
                        "ok": False,
                        "error": "bad_request",
                        "message": "memory-agent 请走非流式端点 /api/chat/message（工具环需完整文本）",
                    },
                    400,
                )
                return
            messages = self._build_chat_messages(message, agent_id)
            try:
                if self._chat_fallback is not None:
                    gen = self._chat_fallback.chat(messages)
                    offline = getattr(self._chat_fallback, "status", "cloud") != "cloud"
                else:
                    gen = self._chat_cloud.chat(messages)
                    offline = False
                first = next(gen, None)  # 首帧探测：云端不可用在此抛
            except (CloudConfigError, CloudUnavailableError) as exc:
                LOGGER.warning("表情聊天流式云端不可用（%s）：%s", exc.__class__.__name__, exc)
                self._handle_chat_stream_offline()
                return

            # chunked NDJSON 头（与 voice/synthesize_stream 同套路）
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Transfer-Encoding", "chunked")
            for key, value in self._cors_headers().items():
                self.send_header(key, value)
            self.end_headers()

            def _write_ndjson(obj):
                line = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
                self.wfile.write(f"{len(line):X}\r\n".encode("ascii"))
                self.wfile.write(line)
                self.wfile.write(b"\r\n")
                self.wfile.flush()

            def _finish(full_text):
                """done 帧下发 + 历史落盘（与非流式同条件，被打断不落盘）。"""
                parsed = EmotionTagParser().parse(full_text)
                done_payload = {
                    "done": True,
                    "ok": True,
                    "clean_text": parsed["clean_text"],
                    "mood": parsed["mood"],
                    "raw": full_text,
                }
                if offline:
                    done_payload["offline"] = True
                try:
                    _write_ndjson(done_payload)
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                if offline and parsed["clean_text"] in _offline_placeholder_texts():
                    return  # 离线占位文案不是真实对话，不落盘
                _append_chat_history(message, parsed["clean_text"], path=self._chat_history_file)

            parts = [str(first)] if first else []
            try:
                if parts:
                    _write_ndjson({"delta": parts[0]})
                for chunk in gen:
                    if not chunk:
                        continue
                    parts.append(str(chunk))
                    _write_ndjson({"delta": str(chunk)})
            except (BrokenPipeError, ConnectionResetError):
                # 客户端已断开（打断/关窗）：停止消费生成器，不落盘不收尾
                return
            except (CloudConfigError, CloudUnavailableError) as exc:
                # 流中途云端断：已收文本仍可用，错误帧说明后照常 done
                try:
                    _write_ndjson({"error": f"生成中断（{exc.__class__.__name__}）"})
                except (BrokenPipeError, ConnectionResetError):
                    return
                _finish("".join(parts))
                return
            except Exception as exc:  # noqa: BLE001 - 生成中途异常同样保已收文本
                try:
                    _write_ndjson({"error": str(exc)[:200]})
                except (BrokenPipeError, ConnectionResetError):
                    return
                _finish("".join(parts))
                return
            _finish("".join(parts))

        def _handle_chat_stream_offline(self):
            """流式聊天云端不可用兜底：离线占位文案以 NDJSON 单轮下发（不落盘）。"""
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Transfer-Encoding", "chunked")
            for key, value in self._cors_headers().items():
                self.send_header(key, value)
            self.end_headers()

            def _write_ndjson(obj):
                line = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
                self.wfile.write(f"{len(line):X}\r\n".encode("ascii"))
                self.wfile.write(line)
                self.wfile.write(b"\r\n")
                self.wfile.flush()

            try:
                _write_ndjson({"delta": CHAT_OFFLINE_TEXT})
                _write_ndjson(
                    {
                        "done": True,
                        "ok": True,
                        "clean_text": CHAT_OFFLINE_TEXT,
                        "mood": "calm",
                        "raw": CHAT_OFFLINE_TEXT,
                        "offline": True,
                    }
                )
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        # ------------------------------------------------------------ 悬浮桌宠模型分发（20260924_模块0_接入VRM悬浮桌宠）
        def _handle_pet_model(self):
            """GET /api/pet/model：分发悬浮桌宠默认 VRM 模型的原始字节。

            为何走 HTTP 而非前端 ``file://`` 直读：Chromium 对 ``file://`` 页面
            的 ``fetch``/``XHR`` 取本地文件默认拦截（``webSecurity`` 默认开启），
            打包态（Electron 加载本地页面）会「开发态能跑、装包后白屏」；改由既有
            回环 HTTP 通道分发，则**开发态与打包态完全同一条加载路径**，也与项目
            「资产归 ``data/``、由后端服务」的约定一致。

            实现要点：
            - 路径经 :func:`pet_model_path`（``app_root()`` / frozen-aware）解析
              为 ``<app_root>/data/pet/cx-open.vrm`` 绝对路径，禁止相对路径；
            - 一次性读全后单次 ``write``（VRM 约 15MB 量级，回环传输即可）；
            - 响应头 ``Content-Type: model/gltf-binary`` + 正确 ``Content-Length``，
              沿用既有 :meth:`_cors_headers`；附 ``Cache-Control: no-store``——
              用户可把同路径文件替换为自己的 VRM，no-store 避免浏览器/Electron
              缓存住旧模型导致「换了模型界面不变」；
            - 文件缺失 → 404 ``pet_model_missing`` + 中文 ``message``（说明期望路径）；
            - 沿用既有 Host / 令牌闸（不新增豁免端点，本方法在 do_GET 令牌放行后调用）。
            """
            path = pet_model_path()
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except (OSError, ValueError):
                # 文件缺失 / 不可读：结构化 404，绝不裸抛到连接中断
                self._send_json(
                    {
                        "ok": False,
                        "error": "pet_model_missing",
                        "message": f"未找到桌宠模型文件，期望路径：{path}（可将同名 VRM 放到该路径替换）",
                    },
                    404,
                )
                return
            self.send_response(200)
            self.send_header("Content-Type", "model/gltf-binary")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for key, value in self._cors_headers().items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def _handle_pet_model_import(self):
            """POST /api/pet/model/import：导入用户自选 VRM 模型（Task 4）。

            行为契约（spec「用户自定义 VRM 桌宠模型」）：
            - body ``{source_path}`` 必须存在、可读、后缀 .vrm（大小写不敏感），
              否则 400 中文错误（missing_source_path / invalid_source_path /
              source_not_found）；
            - 现模型（pet_model_path()）存在时先复制备份为同目录
              ``cx-open.vrm.bak``（覆盖旧备份）；备份失败 → 500 且现模型不动；
            - 新文件先复制到同目录临时名再 ``os.replace`` 原子替换——复制中途
              失败（磁盘满 / 权限）原模型文件保持原样（500 pet_model_import_failed）；
            - 成功返回 ``{ok: true, message}`` 中文（前端经 localStorage 总线
              触发 VRM 立即重载）；
            - 走既有 Host / 令牌闸（do_POST 统一入口，无豁免）。
            """
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            source_path = body.get("source_path") if isinstance(body, dict) else None
            if not isinstance(source_path, str) or not source_path.strip():
                self._send_json(
                    {
                        "ok": False,
                        "error": "missing_source_path",
                        "message": "缺少 source_path：请通过文件选择器选择要导入的 .vrm 模型文件",
                    },
                    400,
                )
                return
            source_path = source_path.strip()
            # 后缀白名单（大小写不敏感）：仅接受 .vrm，防止任意文件覆盖进模型位
            if not source_path.lower().endswith(".vrm"):
                self._send_json(
                    {
                        "ok": False,
                        "error": "invalid_source_path",
                        "message": "仅支持 .vrm 格式的桌宠模型文件，请重新选择",
                    },
                    400,
                )
                return
            if not os.path.isfile(source_path):
                self._send_json(
                    {
                        "ok": False,
                        "error": "source_not_found",
                        "message": f"所选模型文件不存在或不可读：{source_path}",
                    },
                    400,
                )
                return
            target = pet_model_path()
            backup = target + ".bak"
            tmp_target = target + ".importing"
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                # 现模型存在 → 先备份（覆盖旧备份）；备份失败则整体中止，现模型不动
                if os.path.isfile(target):
                    shutil.copyfile(target, backup)
                # 先复制到同目录临时名再原子替换（同盘 os.replace）：
                # 复制中途失败原模型不受影响，替换后不留临时残留
                shutil.copyfile(source_path, tmp_target)
                os.replace(tmp_target, target)
            except OSError as exc:
                # 兜底清理可能残留的临时文件（best-effort，不影响错误响应）
                try:
                    if os.path.isfile(tmp_target):
                        os.remove(tmp_target)
                except OSError:
                    pass
                LOGGER.error("导入桌宠模型失败（原模型未受影响）：%s", exc)
                self._send_json(
                    {
                        "ok": False,
                        "error": "pet_model_import_failed",
                        "message": f"导入桌宠模型失败，原模型保持不变：{exc}",
                    },
                    500,
                )
                return
            self._send_json(
                {
                    "ok": True,
                    "message": "桌宠模型导入成功，悬浮窗与桌宠页将立即加载新模型",
                }
            )

        def _handle_pet_model_reset(self):
            """POST /api/pet/model/reset：从备份还原默认桌宠模型（Task 4）。

            - ``cx-open.vrm.bak`` 存在 → 复制还原为 ``cx-open.vrm``，返回 ok:true；
            - 备份缺失 → 404 ``pet_model_backup_missing`` + 中文 message；
            - 还原同样经同目录临时名 + ``os.replace`` 原子替换，中途失败不破坏现模型；
            - 走既有 Host / 令牌闸（do_POST 统一入口，无豁免）。
            """
            target = pet_model_path()
            backup = target + ".bak"
            if not os.path.isfile(backup):
                self._send_json(
                    {
                        "ok": False,
                        "error": "pet_model_backup_missing",
                        "message": "没有可恢复的模型备份（尚未导入过自定义模型），无法恢复默认",
                    },
                    404,
                )
                return
            tmp_target = target + ".restoring"
            try:
                shutil.copyfile(backup, tmp_target)
                os.replace(tmp_target, target)
            except OSError as exc:
                try:
                    if os.path.isfile(tmp_target):
                        os.remove(tmp_target)
                except OSError:
                    pass
                LOGGER.error("恢复默认桌宠模型失败：%s", exc)
                self._send_json(
                    {
                        "ok": False,
                        "error": "pet_model_reset_failed",
                        "message": f"恢复默认桌宠模型失败：{exc}",
                    },
                    500,
                )
                return
            self._send_json(
                {
                    "ok": True,
                    "message": "已恢复默认桌宠模型，悬浮窗与桌宠页将立即重新加载",
                }
            )

        # ------------------------------------------------------------ 路由
        def do_GET(self):
            """处理 GET：health/status/settings/chat 守卫、记忆/Agent/远端/电脑状态、/api/tools、/api/pet/model。

            GET /api/health 豁免令牌校验（供 main.js 健康探测与运维探针）；
            /api/pet/model 分发悬浮桌宠 VRM 原始字节（过 Host / 令牌闸，无豁免）。
            """
            if not self._check_host():
                self._deny_bad_host()
                return
            path = urlparse(self.path).path
            # N1：健康检查豁免令牌，其余 GET 一律先过启动令牌闸
            if path != "/api/health" and not self._check_token():
                return
            try:
                query = self._parse_query()

                if path == "/api/health":
                    self._send_json({"status": "ok"})
                    return

                if path == "/api/status":
                    self._handle_status()
                    return

                if path == "/api/settings":
                    self._handle_settings_get()
                    return

                if path == "/api/setup/status":
                    self._handle_setup_status()
                    return

                if path == "/api/setup/recommend":
                    self._handle_setup_recommend()
                    return

                if path == "/api/setup/model/progress":
                    self._handle_setup_model_progress()
                    return

                if path == "/api/chat/history":
                    self._handle_chat_history()
                    return

                if path == "/api/remote/status":
                    self._handle_remote_status()
                    return

                # 管理面（20261004_模块0_管理面CX-A管理CX-O）：实例台账与治理透传
                if path == "/api/fleet/instances":
                    self._handle_fleet_list()
                    return
                fleet_route = self._parse_fleet_instance_route(path)
                if fleet_route is not None:
                    self._handle_fleet_instance_get(fleet_route[0], fleet_route[1], query)
                    return

                if path == "/api/memories":
                    self._handle_list(query)
                    return

                if path == "/api/memories/search":
                    self._handle_search(query)
                    return

                # 记忆页核心能力（20261005）：衰减统计 / 日记视图 / 3D 检索
                if path == "/api/memories/decay-stats":
                    self._handle_memory_decay_stats()
                    return

                if path == "/api/memories/diary":
                    self._handle_memory_diary(query)
                    return

                if path == "/api/memories/3d":
                    self._handle_memory_search_3d(query)
                    return

                if path == "/api/agents":
                    self._handle_agents_list(query)
                    return

                if path == "/api/computer/status":
                    self._handle_computer_status()
                    return

                if path == "/api/tools":
                    self._handle_tools_list()
                    return

                if path == "/api/cxfc/relay/pending":
                    self._handle_relay_pending(query)
                    return

                if path == "/api/pet/model":
                    self._handle_pet_model()
                    return

                if path == "/api/voices":
                    self._handle_voices_list()
                    return

                self._send_json({"ok": False, "error": "not_found", "message": f"未找到接口 {path}"}, 404)
            except Exception as exc:  # noqa: BLE001 - 兜底：任何畸形输入都得到结构化 500 而非连接中断
                # SystemExit / KeyboardInterrupt 继承 BaseException，不会被此处捕获
                self._guard_internal_error(exc)

        def do_POST(self):
            """处理 POST：chat 守卫/表情聊天、Agent/远端/电脑控制、tools/call、memory/distill、voice/*。"""
            if not self._check_host():
                self._deny_bad_host()
                return
            try:
                path = urlparse(self.path).path
                # N1：POST 统一过启动令牌闸（与 do_GET 次序对齐）；唯一豁免
                # /api/admin/register——CX-O 侧无 CX-A 令牌，回环来源校验在 handler 内执行
                if path != "/api/admin/register" and not self._check_token():
                    return
                if path == "/api/chat/messages":
                    self._handle_chat_send_guard()
                    return
                if path == "/api/setup/complete":
                    self._handle_setup_complete()
                    return
                if path == "/api/setup/model/download":
                    self._handle_setup_model_download()
                    return
                if path == "/api/setup/model/cancel":
                    self._handle_setup_model_cancel()
                    return
                if path == "/api/chat/message":
                    self._handle_chat_message()
                    return
                if path == "/api/agents":
                    self._handle_agents_create()
                    return
                # 记忆 CRUD（20261005）：新建 / 批量删除 / 衰减同步
                if path == "/api/memories":
                    self._handle_memory_create()
                    return
                if path == "/api/memories/batch-delete":
                    self._handle_memory_batch_delete()
                    return
                if path == "/api/memories/sync-decay":
                    self._handle_memory_sync_decay()
                    return
                if path == "/api/remote/control":
                    self._handle_remote_control()
                    return
                if path == "/api/remote/push_config":
                    self._handle_remote_push_config()
                    return
                # 管理面（20261004_模块0_管理面CX-A管理CX-O）：注册接收 / 台账登记 / 治理下发
                if path == "/api/admin/register":
                    self._handle_fleet_register()
                    return
                if path == "/api/fleet/instances":
                    self._handle_fleet_add()
                    return
                fleet_route = self._parse_fleet_instance_route(path)
                if fleet_route is not None:
                    self._handle_fleet_instance_post(fleet_route[0], fleet_route[1])
                    return
                if path == "/api/computer/authorize":
                    self._handle_computer_authorize()
                    return
                if path == "/api/computer/call":
                    self._handle_computer_call()
                    return
                if path == "/api/tools/call":
                    self._handle_tools_call()
                    return
                if path == "/api/memory/distill":
                    self._handle_memory_distill()
                    return
                if path == "/api/voice/synthesize":
                    self._handle_voice_synthesize()
                    return
                if path == "/api/voice/transcribe":
                    self._handle_voice_transcribe()
                    return
                if path == "/api/voice/synthesize_stream":
                    self._handle_voice_synthesize_stream()
                    return
                # 语音引擎预热（20261010_模块0_语音交互延迟优化）：会话开启时拉热
                # 三层懒加载（sidecar 进程 / MeloTTS / SenseVoice）
                if path == "/api/voice/warmup":
                    self._handle_voice_warmup()
                    return
                # 表情聊天流式版（语音低延迟）：LLM 增量 NDJSON 下发，前端句级
                # 流水线提前启动 TTS——不等完整回复
                if path == "/api/chat/message_stream":
                    self._handle_chat_message_stream()
                    return
                # 桌宠模型导入/恢复（Task 4：用户自定义 VRM 桌宠模型）——
                # 走上方统一令牌闸（无豁免），与 GET /api/pet/model 同一防线
                if path == "/api/pet/model/import":
                    self._handle_pet_model_import()
                    return
                if path == "/api/pet/model/reset":
                    self._handle_pet_model_reset()
                    return
                if path == "/api/voices/import":
                    self._handle_voices_import()
                    return
                if path == "/api/cxfc/relay/result":
                    self._handle_relay_result()
                    return
                self._send_json({"ok": False, "error": "not_found", "message": f"未找到接口 {path}"}, 404)
            except _BodyTooLarge:
                # N6：413 响应已由 _read_body_json 发出，此处直接返回
                return
            except Exception as exc:  # noqa: BLE001 - 兜底：结构化 500 而非连接中断
                self._guard_internal_error(exc)

        def do_PUT(self):
            """处理 PUT：/api/settings 更新配置；/api/agents/{id} 更新指定 Agent。"""
            if not self._check_host():
                self._deny_bad_host()
                return
            try:
                path = urlparse(self.path).path
                # N1：PUT 无豁免端点，统一在 path 解析后过启动令牌闸（与 do_GET 次序对齐）
                if not self._check_token():
                    return
                if path == "/api/settings":
                    self._handle_settings_update()
                    return
                # 记忆编辑（20261005）：PUT /api/memories/{id}
                if path.startswith("/api/memories/"):
                    raw = path[len("/api/memories/"):]
                    if raw and "/" not in raw:
                        self._handle_memory_update(raw)
                        return
                agent_id = self._extract_agents_id(path)
                if agent_id is None:
                    self._send_json({"ok": False, "error": "not_found", "message": f"未找到接口 {path}"}, 404)
                    return
                self._handle_agents_update(agent_id)
            except _BodyTooLarge:
                # N6：413 响应已由 _read_body_json 发出，此处直接返回
                return
            except Exception as exc:  # noqa: BLE001 - 兜底：结构化 500 而非连接中断
                self._guard_internal_error(exc)

        def do_DELETE(self):
            """处理 DELETE：/api/memories/{id} 软删除 / /api/agents/{id} 删除 Agent。"""
            if not self._check_host():
                self._deny_bad_host()
                return
            try:
                path = urlparse(self.path).path
                # N1：DELETE 无豁免端点，统一在 path 解析后过启动令牌闸（与 do_GET 次序对齐）
                if not self._check_token():
                    return
                prefix = "/api/memories/"
                if path.startswith(prefix):
                    self._delete_memory(path[len(prefix):])
                    return
                agent_id = self._extract_agents_id(path)
                if agent_id is not None:
                    self._handle_agents_delete(agent_id)
                    return
                # 管理面：注销实例（/api/fleet/instances/{id}）
                fleet_prefix = "/api/fleet/instances/"
                if path.startswith(fleet_prefix):
                    raw = path[len(fleet_prefix):]
                    if raw and "/" not in raw:
                        self._handle_fleet_remove(raw)
                        return
                self._send_json({"ok": False, "error": "not_found", "message": f"未找到接口 {path}"}, 404)
            except Exception as exc:  # noqa: BLE001 - 兜底：超大 int 触发 sqlite OverflowError 等畸形输入均结构化响应
                self._guard_internal_error(exc)

        @staticmethod
        def _extract_agents_id(path):
            """从 /api/agents/{id} 提取 id；非该前缀或含额外斜杠返回 None。"""
            prefix = "/api/agents/"
            if not path.startswith(prefix):
                return None
            raw = path[len(prefix):]
            if not raw or "/" in raw:
                return None
            return raw

        def _delete_memory(self, raw_id):
            """软删除单条记忆（原先 do_DELETE 的记忆分支）。"""
            if not raw_id or "/" in raw_id:
                self._send_json({"ok": False, "error": "bad_request", "message": "非法记忆 id"}, 400)
                return
            try:
                memory_id = int(raw_id)
            except ValueError:
                self._send_json({"ok": False, "error": "bad_request", "message": "id 必须是整数"}, 400)
                return
            # 低-6（第四轮体检批次B）：超出 64 位有符号整数范围的 id 直接入库会触发
            # sqlite OverflowError → 500，边界处显式 400
            if not (_INT64_MIN <= memory_id <= _INT64_MAX):
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "id 超出有效范围（64 位整数）"}, 400
                )
                return

            # M-10（第三轮体检批次3）：优先走 manager.soft_delete（软删 + 同步
            # 清理向量库孤儿向量）；manager 缺席时回落 store 直删保持旧行为
            manager = getattr(self._pipeline, "manager", None)
            if manager is not None:
                deleted = manager.soft_delete(memory_id)
            else:
                deleted = self._store.soft_delete(memory_id)
            if deleted:
                self._send_json({"ok": True, "id": memory_id})
            else:
                self._send_json({"ok": False, "error": "not_found", "message": f"记忆 {memory_id} 不存在"}, 404)

        # ------------------------------------------------------------ 各接口实现
        def _parse_limit_param(self, raw, name):
            """解析并校验 limit/top_k 类参数（M-6，第三轮体检批次2）。

            非整数回 400；负数回 400（LIMIT -1 在 SQLite 语义为无限制，与
            限制条数意图相反）；超上限钳制到 _MAX_LIST_LIMIT（防 sqlite
            OverflowError 与全量拉取）。

            :param raw: 原始字符串参数
            :param name: 参数名（用于错误消息）
            :return: 校验后的整数；校验失败时已发送 400 并返回 None
            """
            try:
                value = int(raw)
            except ValueError:
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": f"{name} 必须是整数"}, 400
                )
                return None
            if value < 0:
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": f"{name} 不能为负数"}, 400
                )
                return None
            return min(value, _MAX_LIST_LIMIT)

        def _sanitize_agent_id(self, raw):
            """规范化 agent_id（L-5）：strip + 限长，空值回落 default。"""
            return (str(raw or "").strip()[:_MAX_AGENT_ID_CHARS]) or "default"

        def _handle_list(self, query):
            """记忆列表：支持 type / agent_id / limit 过滤，默认仅返回未软删除记录。"""
            limit = None
            if query.get("limit") is not None:
                limit = self._parse_limit_param(query["limit"], "limit")
                if limit is None:
                    return
            agent_id = self._sanitize_agent_id(query.get("agent_id")) if query.get("agent_id") else None
            try:
                rows = self._store.list(
                    type=query.get("type"),
                    agent_id=agent_id,
                    limit=limit,
                    include_deleted=False,
                )
            except ValueError as exc:
                self._send_json({"ok": False, "error": "bad_request", "message": str(exc)}, 400)
                return
            self._send_json(rows)

        def _handle_search(self, query):
            """记忆检索：走 MemoryRetrievalPipeline.retrieve，返回命中的记忆与 context_text。"""
            q = query.get("q") or ""
            if not q.strip():
                self._send_json({"memories": [], "context_text": "【回忆】"})
                return

            top_k = None
            if query.get("top_k") is not None:
                top_k = self._parse_limit_param(query["top_k"], "top_k")
                if top_k is None:
                    return
            agent_id = self._sanitize_agent_id(query.get("agent_id"))
            try:
                result = self._pipeline.retrieve(q, agent_id=agent_id, top_k=top_k)
            except ValueError as exc:
                self._send_json({"ok": False, "error": "bad_request", "message": str(exc)}, 400)
                return
            self._send_json(result)

        # ------------------------------------------------------------ 记忆页完整控制（20261005，spec: align-wizard-settings-memory-pet）
        @staticmethod
        def _parse_memory_id(raw_id):
            """解析记忆 id 字符串为 int；非法返回 (None, 中文错误消息)。

            校验口径与 _delete_memory 一致：整数 + 64 位有符号范围（防 sqlite
            OverflowError 500）。
            """
            if raw_id is None or raw_id == "" or "/" in str(raw_id):
                return None, "非法记忆 id"
            try:
                memory_id = int(raw_id)
            except ValueError:
                return None, "id 必须是整数"
            if not (_INT64_MIN <= memory_id <= _INT64_MAX):
                return None, "id 超出有效范围（64 位整数）"
            return memory_id, None

        @staticmethod
        def _parse_memory_importance(raw):
            """校验 importance 入参（1~5 整数）；非法返回 None（由调用方回 400）。"""
            try:
                value = int(raw)
            except (TypeError, ValueError):
                return None
            return value if 1 <= value <= 5 else None

        @staticmethod
        def _normalize_memory_tags(raw):
            """规范化 tags 入参：list[str] -> JSON 文本；str 原样；None -> None。

            返回 (tags, error)；error 非 None 表示入参非法（非字符串/列表）。
            """
            if raw is None:
                return None, None
            if isinstance(raw, str):
                return raw, None
            if isinstance(raw, list) and all(isinstance(t, str) for t in raw):
                return json.dumps(raw, ensure_ascii=False), None
            return None, "tags 必须为字符串列表或字符串"

        def _memory_write_target(self):
            """解析记忆写入口：优先 MemoryManager（相似去重 + 向量化），回落 store.add。

            与 MEMORY_TYPES 四值域校验（schema.py）共用 store/manager 侧既有校验。
            """
            manager = getattr(self._pipeline, "manager", None)
            return manager, self._store

        def _handle_memory_create(self):
            """POST /api/memories：新建一条记忆（记忆页新建弹窗）。

            body {content, memory_type?, importance?, tags?, agent_id?}：
            - content 必填非空（str），否则 400；
            - memory_type ∈ long_term/short_term/permanent/diary（兼容键 type），
              非法 400（中文）；
            - importance 1~5 整数（缺省 3），越界 400；
            - tags 字符串列表或字符串；agent_id 经 sanitize（空值回落 default）。
            - 写入口优先 manager.add_memory（相似去重返回 None → deduplicated:true），
              回落 store.add；成功 200 {ok, id, deduplicated?}。
            """
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            content = body.get("content")
            if not isinstance(content, str) or not content.strip():
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "content 为必填字段且不能为空"}, 400
                )
                return
            mem_type = body.get("memory_type", body.get("type", "long_term"))
            if mem_type not in MEMORY_TYPES_ALLOWED:
                self._send_json(
                    {
                        "ok": False,
                        "error": "bad_request",
                        "message": f"memory_type 非法：{mem_type!r}，可选：{list(MEMORY_TYPES_ALLOWED)}",
                    },
                    400,
                )
                return
            importance_raw = body.get("importance", 3)
            importance = self._parse_memory_importance(importance_raw)
            if importance is None:
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "importance 必须为 1~5 的整数"}, 400
                )
                return
            tags, tag_err = self._normalize_memory_tags(body.get("tags"))
            if tag_err:
                self._send_json({"ok": False, "error": "bad_request", "message": tag_err}, 400)
                return
            agent_id = self._sanitize_agent_id(body.get("agent_id"))
            manager, store = self._memory_write_target()
            try:
                if manager is not None:
                    mem_id = manager.add_memory(
                        content=content,
                        type=mem_type,
                        importance=importance,
                        agent_id=agent_id,
                        tags=tags,
                    )
                    if mem_id is None:
                        # 相似去重命中：未实际写入（G-1 统一写入口语义）
                        self._send_json({"ok": True, "id": None, "deduplicated": True})
                        return
                else:
                    mem_id = store.add(
                        {
                            "content": content,
                            "type": mem_type,
                            "importance": importance,
                            "tags": tags,
                            "agent_id": agent_id,
                        }
                    )
            except ValueError as exc:
                self._send_json({"ok": False, "error": "bad_request", "message": str(exc)}, 400)
                return
            self._send_json({"ok": True, "id": mem_id})

        def _handle_memory_update(self, raw_id):
            """PUT /api/memories/{id}：编辑一条记忆（走 MemoryStore.update 既有校验）。

            body 为字段补丁：content / memory_type(兼容 type) / importance / tags /
            agent_id 任选其一或组合。id 不存在或已软删 → 404；字段非法 → 400；
            全部为可更新白名单之外的字段 → 400（store.update 全未知键返回 0）。
            """
            memory_id, err = self._parse_memory_id(raw_id)
            if err:
                self._send_json({"ok": False, "error": "bad_request", "message": err}, 400)
                return
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            fields = {}
            if "content" in body:
                content = body.get("content")
                if not isinstance(content, str) or not content.strip():
                    self._send_json(
                        {"ok": False, "error": "bad_request", "message": "content 不能为空"}, 400
                    )
                    return
                fields["content"] = content
            if "memory_type" in body or "type" in body:
                mem_type = body.get("memory_type", body.get("type"))
                if mem_type not in MEMORY_TYPES_ALLOWED:
                    self._send_json(
                        {
                            "ok": False,
                            "error": "bad_request",
                            "message": f"memory_type 非法：{mem_type!r}，可选：{list(MEMORY_TYPES_ALLOWED)}",
                        },
                        400,
                    )
                    return
                fields["type"] = mem_type
            if "importance" in body:
                importance = self._parse_memory_importance(body.get("importance"))
                if importance is None:
                    self._send_json(
                        {"ok": False, "error": "bad_request", "message": "importance 必须为 1~5 的整数"},
                        400,
                    )
                    return
                fields["importance"] = importance
            if "tags" in body:
                tags, tag_err = self._normalize_memory_tags(body.get("tags"))
                if tag_err:
                    self._send_json({"ok": False, "error": "bad_request", "message": tag_err}, 400)
                    return
                fields["tags"] = tags
            if "agent_id" in body:
                fields["agent_id"] = self._sanitize_agent_id(body.get("agent_id"))
            if not fields:
                self._send_json(
                    {
                        "ok": False,
                        "error": "bad_request",
                        "message": "请求体中没有可更新字段（可用：content/memory_type/importance/tags/agent_id）",
                    },
                    400,
                )
                return
            store = self._store
            try:
                existing = store.get(memory_id)
            except Exception:  # noqa: BLE001 - 探测异常按不存在处理
                existing = None
            if existing is None or existing.get("is_deleted"):
                self._send_json(
                    {"ok": False, "error": "not_found", "message": f"记忆 {memory_id} 不存在"}, 404
                )
                return
            try:
                updated = store.update(memory_id, fields)
            except ValueError as exc:
                self._send_json({"ok": False, "error": "bad_request", "message": str(exc)}, 400)
                return
            if updated <= 0:
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "没有生效的更新字段"}, 400
                )
                return
            self._send_json({"ok": True, "id": memory_id, "memory": store.get(memory_id)})

        def _handle_memory_batch_delete(self):
            """POST /api/memories/batch-delete：批量软删除记忆（记忆页批量模式）。

            body {ids: [...]}：id 列表（int/数字字符串均可）。逐条处理：
            - 不存在 / 已软删 → skipped（reason=not_found）；
            - permanent 类型（permanent 标记或 type=='permanent'）→ 保护跳过
              （reason=permanent_protected，spec 硬性要求：批量删除需单条确认，
              permanent 一律不删）；
            - 其余走 manager.soft_delete（清理向量孤儿）/ store.soft_delete。
            返回 {ok, deleted_count, deleted_ids, skipped, total}。
            """
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            ids = body.get("ids")
            if not isinstance(ids, list) or not ids:
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "ids 必须为非空列表"}, 400
                )
                return
            manager = getattr(self._pipeline, "manager", None)
            store = self._store
            deleted_ids, skipped = [], []
            for raw in ids[:_MAX_BATCH_MEMORY_IDS]:
                try:
                    memory_id = int(raw)
                except (TypeError, ValueError):
                    skipped.append({"id": raw, "reason": "invalid_id"})
                    continue
                try:
                    mem = store.get(memory_id)
                except Exception:  # noqa: BLE001 - 探测异常按不存在处理
                    mem = None
                if mem is None or mem.get("is_deleted"):
                    skipped.append({"id": memory_id, "reason": "not_found"})
                    continue
                if bool(mem.get("permanent")) or mem.get("type") == "permanent":
                    skipped.append({"id": memory_id, "reason": "permanent_protected"})
                    continue
                try:
                    ok = manager.soft_delete(memory_id) if manager is not None else store.soft_delete(memory_id)
                except Exception:  # noqa: BLE001 - 单条失败不阻断批量
                    ok = False
                if ok:
                    deleted_ids.append(memory_id)
                else:
                    skipped.append({"id": memory_id, "reason": "delete_failed"})
            overflow = len(ids) - _MAX_BATCH_MEMORY_IDS
            if overflow > 0:
                skipped.append({"id": f"+{overflow}条未处理", "reason": "batch_limit_exceeded"})
            self._send_json(
                {
                    "ok": True,
                    "deleted_count": len(deleted_ids),
                    "deleted_ids": deleted_ids,
                    "skipped": skipped,
                    "total": len(ids),
                }
            )

        def _handle_memory_decay_stats(self):
            """GET /api/memories/decay-stats：遗忘衰减统计（衰减面板展示）。

            走 MemoryStore.decay_stats（CX-O 遗忘曲线口径，permanent 豁免），
            返回 {ok, statistics}，statistics 含即将遗忘(fading)/已衰减(faded)分桶。
            """
            try:
                stats = self._store.decay_stats()
            except Exception as exc:  # noqa: BLE001 - 统计异常结构化 500
                LOGGER.exception("decay-stats 统计失败：%s", exc)
                self._send_json({"ok": False, "error": "internal error"}, 500)
                return
            self._send_json({"ok": True, "statistics": stats})

        def _handle_memory_sync_decay(self):
            """POST /api/memories/sync-decay：执行衰减同步，低分记忆软删归档。

            走 MemoryStore.sync_decay（阈值 SYNC_DECAY_ARCHIVE_THRESHOLD，permanent
            豁免）；deleter 优先传 manager.soft_delete（同步清理向量库孤儿向量）。
            返回 {ok, scanned, deleted_count, deleted_ids, skipped_permanent,
            archive_threshold}。
            """
            manager = getattr(self._pipeline, "manager", None)
            deleter = manager.soft_delete if manager is not None else None
            try:
                result = self._store.sync_decay(deleter=deleter)
            except Exception as exc:  # noqa: BLE001 - 同步异常结构化 500
                LOGGER.exception("sync-decay 执行失败：%s", exc)
                self._send_json({"ok": False, "error": "internal error"}, 500)
                return
            result = dict(result)
            result["ok"] = True
            self._send_json(result)

        def _handle_memory_diary(self, query):
            """GET /api/memories/diary?date=&agent_id=&type=&limit=：日记视图。

            按 created_at 本地日期（YYYY-MM-DD，取时间戳前 10 字符）分组返回记忆，
            分组按日期降序（对齐 CX-O get_diary_entries 的 diary_groups 结构，
            出自 C:\\CX-O\\CX-O-SERVER\\server\\api\\routers\\memory.py；CX-O 按
            metadata.date 分组，CX-A 按 spec 澄清改为 created_at 本地日期）。
            - date=YYYY-MM-DD：只返回该日期组（其余日期仍计入 groups 便于前端导航）；
            - type 可选过滤（缺省全类型——日记视图为「按日期分组的浏览模式」，
              与卡片/列表视图并列，浏览同一批数据；type=diary 即过滤日记类型）。
            返回 {ok, date, diary_groups: [{date, entries, count}], count}。
            """
            limit = None
            if query.get("limit") is not None:
                limit = self._parse_limit_param(query["limit"], "limit")
                if limit is None:
                    return
            mem_type = query.get("type") or None
            if mem_type is not None and mem_type not in MEMORY_TYPES_ALLOWED:
                self._send_json(
                    {
                        "ok": False,
                        "error": "bad_request",
                        "message": f"type 非法：{mem_type!r}，可选：{list(MEMORY_TYPES_ALLOWED)}",
                    },
                    400,
                )
                return
            agent_id = self._sanitize_agent_id(query.get("agent_id")) if query.get("agent_id") else None
            try:
                rows = self._store.list(type=mem_type, agent_id=agent_id, limit=limit, include_deleted=False)
            except ValueError as exc:
                self._send_json({"ok": False, "error": "bad_request", "message": str(exc)}, 400)
                return
            grouped = {}
            for mem in rows:
                created = str(mem.get("created_at") or "")
                day = created[:10] if len(created) >= 10 else created or "未知日期"
                grouped.setdefault(day, []).append(mem)
            diary_groups = [
                {"date": day, "entries": items, "count": len(items)}
                for day, items in sorted(grouped.items(), reverse=True)
            ]
            target_date = query.get("date")
            if target_date:
                diary_groups = [g for g in diary_groups if g["date"] == target_date]
            total = sum(g["count"] for g in diary_groups)
            self._send_json(
                {"ok": True, "date": target_date, "diary_groups": diary_groups, "count": total}
            )

        def _handle_memory_search_3d(self, query):
            """GET /api/memories/3d?query=&w_importance=&w_time=&w_rel=&type=&agent_id=&limit=：
            重要性/时间/相关性三维加权检索（检索面板，权重可调）。

            打分公式（对齐 CX-O MemoryRouter._score_memories 主链路，已在
            lite/memory/scoring.py 移植为 score_memories）：
                final = importance·w_i + time·w_t + relevance·w_r（上限 1.0）
            其中 time 为衰减后分数（CX-O 遗忘曲线口径，permanent 豁免），relevance
            来自既有检索链路（MemoryRetrievalPipeline 向量相似度，缺省 0.5）。
            说明：CX-O 的 /memories/3d 端点走乘性门控变体
            （final = relevance × (w_i·imp + w_t·time)/(w_i+w_t)，出自
            C:\\CX-O\\CX-O-SERVER\\server\\core\\memory\\mixins\\advanced_mixin.py），
            CX-A 贴合既有 scoring 体系（spec 要求「实现贴合既有 lite/memory SQLite
            体系」）沿用线性加权主链路口径，默认权重 0.35/0.25/0.4 与 CX-O 一致。
            """
            w_importance = _MEMORY_3D_DEFAULT_WEIGHTS["importance"]
            w_time = _MEMORY_3D_DEFAULT_WEIGHTS["time"]
            w_rel = _MEMORY_3D_DEFAULT_WEIGHTS["relevance"]
            for key, target in (
                ("w_importance", "importance"),
                ("w_time", "time"),
                ("w_rel", "relevance"),
            ):
                raw = query.get(key)
                if raw is None:
                    continue
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    self._send_json(
                        {"ok": False, "error": "bad_request", "message": f"{key} 必须为 0~1 的数值"}, 400
                    )
                    return
                if not (0.0 <= value <= 1.0):
                    self._send_json(
                        {"ok": False, "error": "bad_request", "message": f"{key} 必须在 0~1 之间"}, 400
                    )
                    return
                setattr_placeholder = value  # noqa: F841 - 仅示位，下方按 target 赋值
                if target == "importance":
                    w_importance = value
                elif target == "time":
                    w_time = value
                else:
                    w_rel = value
            limit = _MEMORY_3D_DEFAULT_LIMIT
            if query.get("limit") is not None:
                limit = self._parse_limit_param(query["limit"], "limit")
                if limit is None:
                    return
            mem_type = query.get("type") or None
            if mem_type is not None and mem_type not in MEMORY_TYPES_ALLOWED:
                self._send_json(
                    {
                        "ok": False,
                        "error": "bad_request",
                        "message": f"type 非法：{mem_type!r}，可选：{list(MEMORY_TYPES_ALLOWED)}",
                    },
                    400,
                )
                return
            agent_id = self._sanitize_agent_id(query.get("agent_id"))
            q = (query.get("query") or "").strip()
            # 相关性维度走既有检索链路：pipeline 向量检索给出候选及其相似度 score；
            # 不可用/无 query 时退化 store.list_recent（relevance 缺省 0.5，对齐
            # CX-O「向量库不可用时无损降级」口径）。
            candidates = None
            degraded = False
            pipeline = self._pipeline
            if q and pipeline is not None:
                try:
                    result = pipeline.retrieve(q, agent_id=agent_id, top_k=_MEMORY_3D_CANDIDATE_LIMIT)
                    candidates = list(result.get("memories") or [])
                except Exception as exc:  # noqa: BLE001 - 检索链异常降级（不 500）
                    LOGGER.warning("3D 检索候选预取失败，降级列表扫描：%s", exc)
                    candidates = None
            if candidates is None:
                degraded = True
                try:
                    rows = self._store.list(type=mem_type, agent_id=None if agent_id == "default" else agent_id)
                except ValueError as exc:
                    self._send_json({"ok": False, "error": "bad_request", "message": str(exc)}, 400)
                    return
                if q:
                    # 子串过滤模拟相关性（与 builtin_registry 退化检索同口径）
                    rows = [m for m in rows if q in str(m.get("content") or "")]
                candidates = rows
            scored = _memory_score_3d(
                candidates,
                query=q,
                importance_weight=w_importance,
                time_weight=w_time,
                relevance_weight=w_rel,
            )
            self._send_json(
                {
                    "ok": True,
                    "memories": scored[:limit],
                    "total": len(scored),
                    "applied_weights": {"importance": w_importance, "time": w_time, "relevance": w_rel},
                    "degraded": degraded,
                }
            )

        # ------------------------------------------------------------ 远端遥控接口
        def _map_remote_error(self, exc):
            """把远端遥控异常映射为可发送的 (payload, status)。

            Args:
                exc: 捕获的远端异常（RemoteDisabled / RemoteUnreachable /
                    RemoteError / ValueError）。

            Returns:
                tuple[dict, int]: (JSON 载荷, HTTP 状态码)。
            """
            if isinstance(exc, RemoteDisabled):
                return {"ok": False, "error": "remote_disabled", "message": str(exc)}, 503
            if isinstance(exc, RemoteUnreachable):
                return {"ok": False, "error": "remote_unreachable", "message": str(exc)}, 504
            if isinstance(exc, ValueError):
                return {"ok": False, "error": "bad_request", "message": str(exc)}, 400
            return {"ok": False, "error": "remote_error", "message": str(exc)}, 502

        def _handle_remote_status(self):
            """GET /api/remote/status：转发 get_status，异常按语义映射状态码。"""
            try:
                data = self._remote.get_status()
            except (RemoteDisabled, RemoteUnreachable, RemoteError) as exc:
                payload, status = self._map_remote_error(exc)
                self._send_json(payload, status)
                return
            self._send_json(data)

        def _handle_remote_control(self):
            """POST /api/remote/control：读取 action/agent_id 并转发 control。"""
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            action = body.get("action")
            if action not in self._remote.ACTIONS:
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": f"action 必须为 {'/'.join(self._remote.ACTIONS)}"},
                    400,
                )
                return
            agent_id = body.get("agent_id")
            try:
                data = self._remote.control(action, agent_id=agent_id)
            except (RemoteDisabled, RemoteUnreachable, RemoteError) as exc:
                payload, status = self._map_remote_error(exc)
                self._send_json(payload, status)
                return
            self._send_json(data)

        def _handle_remote_push_config(self):
            """POST /api/remote/push_config：读取非空 JSON 补丁并转发 push_config。"""
            patch = self._read_body_json()
            if patch is None:
                self._reject_bad_json()
                return
            if not isinstance(patch, dict) or not patch:
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "patch 必须为非空 JSON 对象"}, 400
                )
                return
            try:
                data = self._remote.push_config(patch)
            except (RemoteDisabled, RemoteUnreachable, RemoteError) as exc:
                payload, status = self._map_remote_error(exc)
                self._send_json(payload, status)
                return
            self._send_json(data)

        # ------------------------------------------------------------ 管理面接口（CX-A 管理 CX-O）
        @staticmethod
        def _parse_fleet_instance_route(path):
            """解析 /api/fleet/instances/{id}/{action} → (id, action)；不匹配返回 None。"""
            prefix = "/api/fleet/instances/"
            if not path.startswith(prefix):
                return None
            parts = path[len(prefix):].split("/")
            if len(parts) != 2 or not parts[0] or not parts[1]:
                return None
            return parts[0], parts[1]

        @staticmethod
        def _query_int(query, key, default):
            """查询参数转 int；缺失/非法回落默认值。"""
            try:
                return int(query.get(key))
            except (TypeError, ValueError):
                return default

        def _fleet_ready(self):
            """FleetManager 未装配（直调 make_handler 的旧测试路径）时回 503。"""
            if self._fleet is None:
                self._send_json(
                    {"ok": False, "error": "fleet_unavailable", "message": "管理面未装配"}, 503
                )
                return False
            return True

        def _map_fleet_error(self, exc):
            """管理面异常 → (payload, status)：404/400/504/远端原样透传。

            FleetRemoteError 携带结构化 status_code + payload——CX-O 的
            ADMIN_* 错误码与 HTTP 状态码**原样透传**（非 502 包装）。
            FleetNotFound（未命中 404）必须先于 FleetInvalid（400）判定——
            前者是后者的子类，顺序颠倒会把 404 退化成 400。
            """
            if isinstance(exc, FleetNotFound):
                return {"ok": False, "error": "not_found", "message": f"未找到实例 {exc.args[0]}"}, 404
            if isinstance(exc, FleetInvalid):
                return {"ok": False, "error": "bad_request", "message": str(exc)}, 400
            if isinstance(exc, RemoteUnreachable):
                return {"ok": False, "error": "fleet_unreachable", "message": str(exc)}, 504
            if isinstance(exc, FleetRemoteError):
                payload = exc.payload if isinstance(exc.payload, dict) else {"raw": exc.payload}
                return payload, exc.status_code
            return {"ok": False, "error": "internal_error", "message": str(exc)}, 500

        def _handle_fleet_list(self):
            """GET /api/fleet/instances：台账列表（脱敏视图，被动 last_seen 新鲜度）。"""
            if not self._fleet_ready():
                return
            self._send_json({"status": "success", "instances": self._fleet.list_instances()})

        def _handle_fleet_add(self):
            """POST /api/fleet/instances：手动登记实例 {name, base_url, token}。"""
            if not self._fleet_ready():
                return
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            try:
                view = self._fleet.add_manual(
                    name=body.get("name"),
                    base_url=body.get("base_url"),
                    token=body.get("token"),
                )
            except FleetInvalid as exc:
                self._send_json({"ok": False, "error": "bad_request", "message": str(exc)}, 400)
                return
            self._send_json({"status": "success", "instance": view})

        def _handle_fleet_remove(self, instance_id):
            """DELETE /api/fleet/instances/{id}：注销实例（移除记录，非阻止注册）。"""
            if not self._fleet_ready():
                return
            try:
                self._fleet.remove(instance_id)
            except FleetNotFound:
                self._send_json(
                    {"ok": False, "error": "not_found", "message": f"未找到实例 {instance_id}"}, 404
                )
                return
            self._send_json({"status": "success"})

        def _handle_fleet_instance_get(self, instance_id, action, query):
            """GET /api/fleet/instances/{id}/{manifest|status|audit|health}：只读透传。"""
            if not self._fleet_ready():
                return
            try:
                if action == "manifest":
                    data = self._fleet.get_manifest(instance_id)
                elif action == "status":
                    data = self._fleet.get_status(instance_id)
                elif action == "audit":
                    data = self._fleet.get_audit(
                        instance_id,
                        limit=self._query_int(query, "limit", 50),
                        offset=self._query_int(query, "offset", 0),
                    )
                elif action == "health":
                    data = self._fleet.get_health(instance_id)
                else:
                    self._send_json(
                        {"ok": False, "error": "not_found", "message": f"未找到接口 {action}"}, 404
                    )
                    return
            except (FleetInvalid, RemoteUnreachable, FleetRemoteError) as exc:
                payload, status = self._map_fleet_error(exc)
                self._send_json(payload, status)
                return
            self._send_json(data)

        def _handle_fleet_instance_post(self, instance_id, action):
            """POST /api/fleet/instances/{id}/{control|batch}：治理指令透传。"""
            if not self._fleet_ready():
                return
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            try:
                if action == "control":
                    data = self._fleet.control(instance_id, body)
                elif action == "batch":
                    data = self._fleet.batch(instance_id, body)
                else:
                    self._send_json(
                        {"ok": False, "error": "not_found", "message": f"未找到接口 {action}"}, 404
                    )
                    return
            except (FleetInvalid, RemoteUnreachable, FleetRemoteError) as exc:
                payload, status = self._map_fleet_error(exc)
                self._send_json(payload, status)
                return
            self._send_json(data)

        def _handle_fleet_register(self):
            """POST /api/admin/register：接收 CX-O 主动注册/心跳（豁免令牌闸）。

            安全口径（spec establish-fleet-admin-plane / GN-004 D-1 固化）：
            仅限同机实例注册上门——client_address 非回环直接 403，跨机实例走
            手动登记通道（POST /api/fleet/instances）。
            """
            if not self._fleet_ready():
                return
            client_host = str(self.client_address[0]) if self.client_address else ""
            if not is_loopback_client(client_host):
                self._send_json(
                    {"ok": False, "error": "forbidden",
                     "message": "注册仅限本机实例（回环地址）；跨机实例请在 CX-A 手动登记"},
                    403,
                )
                return
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            try:
                view = self._fleet.register(body)
            except FleetInvalid as exc:
                self._send_json({"ok": False, "error": "bad_request", "message": str(exc)}, 400)
                return
            self._send_json({"status": "success", "instance": view})

        # ------------------------------------------------------------ 电脑控制接口
        def _handle_computer_status(self):
            """GET /api/computer/status：返回授权状态与高危确认开关。"""
            self._send_json(
                {
                    "authorized": self._authorizer.is_authorized(),
                    "confirm_dangerous": bool(self._authorizer.confirm_dangerous),
                }
            )

        def _handle_computer_authorize(self):
            """POST /api/computer/authorize：body {enabled: bool}，开启/撤销授权。

            同步 authorizer 与 computer 两端授权状态，返回最新状态（ok:true + 状态字段）。
            """
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            enabled = body.get("enabled")
            if not isinstance(enabled, bool):
                self._send_json({"ok": False, "error": "bad_request", "message": "enabled 必须为布尔值"}, 400)
                return
            if enabled:
                self._authorizer.authorize()
            else:
                self._authorizer.revoke()
            self._computer.set_authorized(self._authorizer.is_authorized())
            self._send_json(
                {
                    "ok": True,
                    "authorized": self._authorizer.is_authorized(),
                    "confirm_dangerous": bool(self._authorizer.confirm_dangerous),
                }
            )

        def _handle_computer_call(self):
            """POST /api/computer/call：body {tool, arguments}，走 ToolBridge 执行。

            未授权（NotAuthorizedError）映射 403 并标注 authorized:false；
            其余 PluginError 按各自错误码映射状态码，**不误标 authorized 字段**（L3：
            仅授权类错误才携带 authorized:false，避免语义失真）。
            """
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            tool = body.get("tool")
            if not tool:
                self._send_json({"ok": False, "error": "bad_request", "message": "tool 为必填字段"}, 400)
                return
            # L-3：arguments 存在且非 dict 时显式 400（不再静默替换为 {}）
            raw_args = body.get("arguments")
            if raw_args is not None and not isinstance(raw_args, dict):
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "arguments 必须为对象"}, 400
                )
                return
            arguments = raw_args or {}
            try:
                payload = self._bridge.execute(str(tool), arguments)
            except NotAuthorizedError as exc:
                self._send_json(
                    {
                        "ok": False,
                        "authorized": False,
                        "error": "需要先授权",
                        "error_code": exc.error_code,
                    },
                    403,
                )
                return
            except PluginError as exc:
                # N7：原 isinstance(exc, NotAuthorizedError) 分支恒 False（该异常已由
                # 上一个 except 捕获），删除死分支；非授权类 PluginError 不携带 authorized 键
                self._send_json(
                    {"ok": False, "error": exc.message, "error_code": exc.error_code},
                    exc.http_status,
                )
                return
            self._send_json(payload)

        # ------------------------------------------------------------ 内置工具 / 记忆蒸馏 / 语音（批次E）
        def _handle_tools_list(self):
            """GET /api/tools：内置工具清单 + 端点用法自述（供管理 Agent 自发现）。

            按 BuiltinToolRegistry.list_tools() 实际提供的数据结构如实映射
            （id/name/description/source/category/enabled），不虚构参数 schema。
            """
            tools = [
                {
                    "id": t["id"],
                    "name": t["name"],
                    "description": t["description"],
                    "source": t["source"],
                    "category": t["category"],
                    "enabled": t["enabled"],
                }
                for t in self._registry.list_tools()
            ]
            self._send_json({"ok": True, "tools": tools, "usage": _TOOLS_USAGE})

        def _handle_tools_call(self):
            """POST /api/tools/call：body {name, arguments}，调用内置工具注册表。

            注册表 ``call`` 不抛异常、统一返回 ``{success, tool, result|error,
            authorized}`` 外壳，本端点按外壳判定映射：
            - 未知工具（list_tools 无该 id）→ 404 not_found；
            - success=False 且 authorized=False（NotAuthorizedError 包装 /
              类别开关禁用，电脑三件套已在注册表内走 授权→高危确认→审计 链）
              → 403 not_authorized；
            - 其余 success=False（执行失败）→ 400；
            - 成功 → 200 {ok:true, result}。
            """
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            name = str(body.get("name") or "").strip()
            if not name:
                self._send_json({"ok": False, "error": "bad_request", "message": "name 为必填字段"}, 400)
                return
            # L-3：arguments 存在且非 dict 时显式 400（不再静默替换为 {}）
            raw_args = body.get("arguments")
            if raw_args is not None and not isinstance(raw_args, dict):
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "arguments 必须为对象"}, 400
                )
                return
            arguments = raw_args or {}
            known_ids = {t["id"] for t in self._registry.list_tools()}
            if name not in known_ids:
                self._send_json(
                    {"ok": False, "error": "not_found", "message": f"未知内置工具：{name!r}"}, 404
                )
                return
            outcome = self._registry.call(name, arguments)
            if not outcome.get("success"):
                if outcome.get("authorized") is False:
                    self._send_json(
                        {"ok": False, "error": "not_authorized", "message": outcome.get("error")}, 403
                    )
                else:
                    # 低-5（第四轮体检批次B）：工具执行失败的 error 可能含内部异常
                    # 文本（registry 侧包装 str(exc)），对外只回固定类别摘要，
                    # 完整错误写日志；结构化 error_code（若有）非自由文本可透传
                    LOGGER.warning("内置工具 %s 执行失败：%s", name, outcome.get("error"))
                    payload = {"ok": False, "error": "tool_failed", "message": "工具执行失败"}
                    if outcome.get("error_code"):
                        payload["error_code"] = outcome.get("error_code")
                    self._send_json(payload, 400)
                return
            self._send_json({"ok": True, "result": outcome.get("result")})

        def _handle_memory_distill(self):
            """POST /api/memory/distill：body {messages, agent_id?}，触发云端记忆蒸馏。

            - messages 必须为非空列表且每项含 role/content，否则 400；
            - CloudConfigError（未配置 api_key / 未知 provider）→ 400 cloud_not_configured；
            - DistillationPaused（云端离线）→ 503 cloud_offline；
            - 成功 → 200 {ok:true, sessions}（distill_with_sessions 完整会话明细）。
            """
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            messages = body.get("messages")
            if not isinstance(messages, list) or not messages:
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "messages 必须为非空列表"}, 400
                )
                return
            if not all(isinstance(m, dict) and "role" in m and "content" in m for m in messages):
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "messages 每项必须是含 role/content 的对象"},
                    400,
                )
                return
            # H-5：条数上限——防单请求串行发起数百次云端 LLM 调用阻塞单线程服务
            if len(messages) > _MAX_DISTILL_MESSAGES:
                self._send_json(
                    {
                        "ok": False,
                        "error": "bad_request",
                        "message": f"messages 条数超过上限 {_MAX_DISTILL_MESSAGES}（收到 {len(messages)}）",
                    },
                    400,
                )
                return
            agent_id = self._sanitize_agent_id(body.get("agent_id"))
            try:
                sessions = self._distiller.distill_with_sessions(messages, agent_id=agent_id)
            except CloudConfigError as exc:
                self._send_json({"ok": False, "error": "cloud_not_configured", "message": str(exc)}, 400)
                return
            except DistillationPaused as exc:
                self._send_json({"ok": False, "error": "cloud_offline", "message": str(exc)}, 503)
                return
            self._send_json({"ok": True, "sessions": sessions})

        def _handle_voice_synthesize(self):
            """POST /api/voice/synthesize：body {text, voice?}，TTS 合成并以 base64 返回。

            调 voice.tts.synthesize(text, voice) 得 wav/pcm 字节；后端任何异常
            （含引擎未就绪）统一 503 voice_backend_unavailable，不中断服务。
            """
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            text = str(body.get("text") or "").strip()
            if not text:
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "text 为必填字段且不能为空"}, 400
                )
                return
            # M-5：文本长度上限——防超长文本分钟级合成阻塞单线程服务
            if len(text) > _MAX_SYNTH_TEXT_CHARS:
                self._send_json(
                    {
                        "ok": False,
                        "error": "bad_request",
                        "message": f"text 长度超过上限 {_MAX_SYNTH_TEXT_CHARS} 字符（收到 {len(text)}）",
                    },
                    400,
                )
                return
            # 音色热切换（Task B）：未显式指定音色时现读 config ``tts.voice``——
            # 设置页改默认音色后下一次合成即生效，无需重建后端/重启（修复
            # ``default_voice`` 构造期固化、改 config 不重启不生效的缺陷）
            voice = body.get("voice") or self._voice_config_default()
            try:
                audio = self._voice.tts.synthesize(text, voice)
            except Exception as exc:  # noqa: BLE001 - 语音后端故障统一 503 兜底
                self._send_json(
                    {"ok": False, "error": "voice_backend_unavailable", "message": str(exc)[:200]}, 503
                )
                return
            if not audio:
                self._send_json(
                    {"ok": False, "error": "voice_backend_unavailable", "message": "合成返回空音频"}, 503
                )
                return
            self._send_json(
                {
                    "ok": True,
                    "audio_base64": base64.b64encode(bytes(audio)).decode("ascii"),
                    "mime": "audio/wav",
                }
            )

        def _handle_voice_transcribe(self):
            """POST /api/voice/transcribe：body {audio_base64, sample_rate?}，ASR 转写。

            base64 解码后送 voice.asr.transcribe（sample_rate 一并透传——桥后端
            据此重采样到 SenseVoice 期望的 16kHz；缺省 None 时按 16000 处理）；
            解码失败 400；后端异常 503 voice_backend_unavailable。
            """
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            raw = body.get("audio_base64")
            if not isinstance(raw, str) or not raw.strip():
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "audio_base64 为必填字段"}, 400
                )
                return
            try:
                audio = base64.b64decode(raw, validate=True)
            except (ValueError, TypeError):
                # binascii.Error 是 ValueError 子类，统一按非法 base64 处理
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "audio_base64 不是合法的 base64 编码"}, 400
                )
                return
            sample_rate = body.get("sample_rate")
            try:
                result = self._voice.asr.transcribe(audio, sample_rate) or {}
            except Exception as exc:  # noqa: BLE001 - 语音后端故障统一 503 兜底
                self._send_json(
                    {"ok": False, "error": "voice_backend_unavailable", "message": str(exc)[:200]}, 503
                )
                return
            self._send_json({"ok": True, "text": str(result.get("text", ""))})

        def _handle_voice_warmup(self):
            """POST /api/voice/warmup：语音引擎预热（异步后台执行，立即返回）。

            背景（20261010_模块0_语音交互延迟优化）：voice_bridge sidecar 进程、
            MeloTTS 引擎、SenseVoice ASR 模型均为「首次请求才加载」——用户开启
            语音会话后的第一次对话要叠加三层冷启动。本端点在后台线程发一次微型
            合成与一次微型识别把三层拉热；前端开启语音会话时 fire-and-forget
            调用，用户说完第一句话前的几秒足够引擎完成加载。

            防重入见 _VOICE_WARMUP_LOCK 注释；预热失败仅记日志（尽力而为），
            真实请求仍走正常懒加载兜底，绝不因此 5xx。
            """
            global _VOICE_WARMUP_INFLIGHT
            with _VOICE_WARMUP_LOCK:
                if _VOICE_WARMUP_INFLIGHT:
                    self._send_json({"ok": True, "warmup": "already_inflight"})
                    return
                _VOICE_WARMUP_INFLIGHT = True

            def _run():
                global _VOICE_WARMUP_INFLIGHT
                try:
                    voice = self._voice_config_default()
                    # TTS：微型文本合成（拉起 sidecar 进程 + 构造 MeloTTS 引擎）
                    self._voice.tts.synthesize("嗯", voice)
                    # ASR：0.2s 16kHz 16bit 静音识别（构造 SenseVoice 模型）
                    self._voice.asr.transcribe(b"\x00" * 6400, 16000)
                    LOGGER.info("语音引擎预热完成（sidecar / TTS / ASR 已就绪）")
                except Exception as exc:  # noqa: BLE001 - 预热尽力而为，不阻断服务
                    LOGGER.info(
                        "语音引擎预热未完成（%s）：%s", exc.__class__.__name__, str(exc)[:120]
                    )
                finally:
                    with _VOICE_WARMUP_LOCK:
                        _VOICE_WARMUP_INFLIGHT = False

            threading.Thread(target=_run, name="voice-warmup", daemon=True).start()
            self._send_json({"ok": True, "warmup": "started"})

        def _handle_voice_synthesize_stream(self):
            """POST /api/voice/synthesize_stream：按标点切分后逐句合成，chunked NDJSON 流式下发。

            body ``{text, voice?}``：
            - 校验：text 非空、长度 <= ``_MAX_SYNTH_TEXT_CHARS``（与整段合成同口径）。
            - 切分：经 :func:`lite.audio.text_splitter.split_by_punctuation` 按标点切短句。
            - 流式：每句合成后立即以 chunked 方式下发一行 NDJSON：
              ``{"seq": N, "text": "<原文片段>", "audio_base64": "<wav base64>"}``
              末帧：``{"done": true, "total": N}``。
              单句合成失败不中断，发送 ``{"seq": N, "error": "..."}`` 后继续。
            - 与 ``/api/voice/synthesize`` 并存：原端点保持整段返回，本端点专供长文本流式。
            """
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            text = str(body.get("text") or "").strip()
            if not text:
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "text 为必填字段且不能为空"}, 400
                )
                return
            if len(text) > _MAX_SYNTH_TEXT_CHARS:
                self._send_json(
                    {
                        "ok": False,
                        "error": "bad_request",
                        "message": f"text 长度超过上限 {_MAX_SYNTH_TEXT_CHARS} 字符（收到 {len(text)}）",
                    },
                    400,
                )
                return
            # 音色热切换（Task B）：与整段合成同口径——未显式指定时现读 config
            voice = body.get("voice") or self._voice_config_default()
            segments = split_by_punctuation(text)
            if not segments:
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "文本切分后为空"}, 400
                )
                return

            # 发起 chunked 响应头（无 Content-Length，逐段 flush 推送）
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Transfer-Encoding", "chunked")
            for key, value in self._cors_headers().items():
                self.send_header(key, value)
            self.end_headers()

            def _write_ndjson(obj):
                """写一行 NDJSON chunk（带 chunked 帧头帧尾）。"""
                line = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
                self.wfile.write(f"{len(line):X}\r\n".encode("ascii"))
                self.wfile.write(line)
                self.wfile.write(b"\r\n")
                self.wfile.flush()

            total = len(segments)
            for seq, seg in enumerate(segments):
                try:
                    audio = self._voice.tts.synthesize(seg, voice)
                    if not audio:
                        _write_ndjson({"seq": seq, "text": seg, "error": "合成返回空音频"})
                        continue
                    _write_ndjson({
                        "seq": seq,
                        "text": seg,
                        "audio_base64": base64.b64encode(bytes(audio)).decode("ascii"),
                    })
                except (BrokenPipeError, ConnectionResetError):
                    # 客户端已断开（前端 stopSpeaking 触发 abort）：停止后续合成，避免无效 CPU 消耗
                    return
                except Exception as exc:  # noqa: BLE001 - 单句失败不中断整段
                    try:
                        _write_ndjson({"seq": seq, "text": seg, "error": str(exc)[:200]})
                    except (BrokenPipeError, ConnectionResetError):
                        return
            try:
                _write_ndjson({"done": True, "total": total})
                # chunked 终止帧
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        # ------------------------------------------------------------ 音色管理（Task B「音色自由选」）
        def _voice_config_default(self):
            """读当前 config 的默认音色 id（``tts.voice``；缺失回 ``cx-open``）。"""
            return str(self._config.get("tts", "voice", DEFAULT_VOICE_ID) or DEFAULT_VOICE_ID)

        def _handle_voices_list(self):
            """GET /api/voices：内置默认音色 + ``data/voices`` 目录音色包合并列表。

            返回 ``{ok, voices: [{id, path, size, is_default, builtin}]}``：
            - 内置项 ``cx-open`` 恒在列（``builtin: true``）；目录中亦有同名包时
              与内置项合并去重（保留目录项的 size/path 信息）；
            - 目录项来自 :meth:`VoiceManager.list_voices`（附 ``builtin: false``）；
            - ``is_default`` 统一以当前 config ``tts.voice`` 为口径——列表中恰好
              一项标记默认，与设置页回显一致（VoiceManager.list_voices 的
              ``cx-open 恒默认``口径在用户切换默认音色后会与 config 矛盾）。
            目录扫描失败仅告警并按空目录项处理（端点不 5xx）。
            """
            config_default = self._voice_config_default()
            dir_items = []
            try:
                dir_items = VoiceManager(config=self._config).list_voices()
            except Exception as exc:  # noqa: BLE001 - 目录扫描失败不阻断列表
                LOGGER.warning("音色目录扫描失败（%s）：%s", exc.__class__.__name__, exc)
            voices = []
            builtin_merged = False
            for item in dir_items:
                voice_id = str(item.get("id", ""))
                entry = {
                    "id": voice_id,
                    "path": item.get("path"),
                    "size": item.get("size", 0),
                    "is_default": (voice_id == config_default),
                    "builtin": False,
                }
                if voice_id == DEFAULT_VOICE_ID:
                    # 目录项与内置项合并：保留目录项 size/path，builtin 身份保留
                    entry["builtin"] = True
                    builtin_merged = True
                voices.append(entry)
            if not builtin_merged:
                voices.insert(0, {
                    "id": DEFAULT_VOICE_ID,
                    "path": None,
                    "size": 0,
                    "is_default": (config_default == DEFAULT_VOICE_ID),
                    "builtin": True,
                })
            voices.sort(key=lambda v: v["id"])
            self._send_json({"ok": True, "voices": voices})

        def _handle_voices_import(self):
            """POST /api/voices/import：把本机音色包目录复制为 ``data/voices/<name>/``。

            body ``{source_path, name?, overwrite?}``，校验链（任一失败回 400
            中文 message 且不产生任何写入副作用）：
            - source_path 必填且须为已存在目录；
            - 须含可加载音色产物（config.json / ckpt.txt / *.pth / *.ckpt 任一，
              与 TTS 加载口径 ``VoiceManager._is_loadable_voice`` 一致）；
            - name 缺省取 source_path 的 basename；为空或含路径穿越特征
              （:func:`is_unsafe_voice_id`）→ 400——防目标目录逃逸；
            - 目标已存在且 overwrite 非 true → 400；overwrite=true 先删旧目录再复制。
            本端点为本地单用户应用的受令牌保护端点：source_path 由本机用户经
            系统对话框选择，不做沙箱化；仅 name 强制过 ``is_unsafe_voice_id``。
            """
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            source_path = body.get("source_path")
            if not isinstance(source_path, str) or not source_path.strip():
                self._send_json(
                    {"ok": False, "error": "bad_request",
                     "message": "source_path 为必填字段且必须是已存在的目录"},
                    400,
                )
                return
            source_path = os.path.normpath(source_path.strip())
            if not os.path.isdir(source_path):
                self._send_json(
                    {"ok": False, "error": "bad_request",
                     "message": f"source_path 不是有效目录：{source_path}"},
                    400,
                )
                return
            if not VoiceManager._is_loadable_voice(source_path):
                self._send_json(
                    {"ok": False, "error": "bad_request",
                     "message": "所选文件夹里没有可用的音色模型文件"
                                "（需要 config.json / ckpt.txt / *.pth / *.ckpt 任一）"},
                    400,
                )
                return
            raw_name = body.get("name")
            if raw_name is None:
                name = os.path.basename(os.path.normpath(source_path))
            else:
                name = raw_name.strip() if isinstance(raw_name, str) else ""
            if not name or is_unsafe_voice_id(name):
                self._send_json(
                    {"ok": False, "error": "bad_request",
                     "message": "音色名称非法：不能为空，且不得包含路径分隔符、盘符或 ..（防目录逃逸）"},
                    400,
                )
                return
            manager = VoiceManager(config=self._config)
            target_dir = os.path.join(manager.voices_dir, name)
            if os.path.isdir(target_dir):
                if body.get("overwrite") is not True:
                    self._send_json(
                        {"ok": False, "error": "bad_request",
                         "message": f"音色 {name} 已存在；如需覆盖请传 overwrite=true"},
                        400,
                    )
                    return
                shutil.rmtree(target_dir)
            try:
                os.makedirs(manager.voices_dir, exist_ok=True)
                shutil.copytree(source_path, target_dir, dirs_exist_ok=True)
            except OSError as exc:
                # 复制失败尽力清理半成品目录，避免残留不可加载的脏音色包
                shutil.rmtree(target_dir, ignore_errors=True)
                self._send_json(
                    {"ok": False, "error": "bad_request",
                     "message": f"复制音色文件失败：{exc}"},
                    400,
                )
                return
            self._send_json({
                "ok": True,
                "voice": {
                    "id": name,
                    "path": target_dir,
                    "size": VoiceManager._dir_size(target_dir),
                    "is_default": (name == self._voice_config_default()),
                    "builtin": False,
                },
            })

        # ------------------------------------------------------------ CXFC relay 前端转接（Task H1）
        def _handle_relay_pending(self, query):
            """GET /api/cxfc/relay/pending?plugin_id=&limit=：取走待执行的 relay 调用。

            返回 {ok, calls, count, plugin_id}；calls 每项形如
            {type:"cxfc_relay_call", plugin_id, tool, arguments, request_id}。
            取走即从待执行队列移除（at-most-once，LiteCXFC 内部加锁）：前端取走
            后未回报时，调用方在 relay 超时窗口后收到 RELAY_TIMEOUT。
            """
            if self._cxfc is None:
                self._send_json(
                    {"ok": False, "error": "cxfc_unavailable", "message": "CXFC 未装配"}, 503
                )
                return
            plugin_id = query.get("plugin_id")
            limit = _RELAY_PENDING_DEFAULT_LIMIT
            if query.get("limit") is not None:
                limit = self._parse_limit_param(query["limit"], "limit")
                if limit is None:
                    return
            calls = self._cxfc.pending_relay_calls(limit=limit, plugin_id=plugin_id)
            self._send_json(
                {"ok": True, "calls": calls, "count": len(calls), "plugin_id": plugin_id}
            )

        def _handle_relay_result(self):
            """POST /api/cxfc/relay/result：前端回报一次 relay 调用结果。

            body {request_id, plugin_id, success, result|error}：request_id 与
            success 必填（缺失 / 形态非法 400）；fulfill_relay 命中等待者返回
            {"status":"ok"}；未知 request_id / 已超时清理 / 重复回报返回
            404 语义 JSON（ok:false + not_found）。
            """
            if self._cxfc is None:
                self._send_json(
                    {"ok": False, "error": "cxfc_unavailable", "message": "CXFC 未装配"}, 503
                )
                return
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            request_id = str(body.get("request_id") or "").strip()
            if not request_id:
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "request_id 为必填字段"}, 400
                )
                return
            success = body.get("success")
            if not isinstance(success, bool):
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "success 必须为布尔值"}, 400
                )
                return
            # plugin_id 为协议回报字段（CX-O §2.2），fulfill 以 request_id 定位等待者，
            # 此处仅留痕日志供排查回报归属
            plugin_id = body.get("plugin_id")
            LOGGER.debug(
                "relay 回报 plugin=%r request_id=%s success=%s", plugin_id, request_id, success
            )
            if success:
                result_or_error = body.get("result")
            else:
                result_or_error = body.get("error", body.get("result"))
            try:
                fulfilled = self._cxfc.fulfill_relay(request_id, success, result_or_error)
            except ValueError as exc:
                self._send_json({"ok": False, "error": "bad_request", "message": str(exc)}, 400)
                return
            if not fulfilled:
                self._send_json(
                    {
                        "ok": False,
                        "error": "not_found",
                        "message": "未知 request_id 或调用已超时/已被回报",
                    },
                    404,
                )
                return
            self._send_json({"status": "ok"})

        # ------------------------------------------------------------ Agent 接口
        def _read_body_json(self):
            """读取请求体并解析为 dict。

            返回值语义（L2 收口：malformed 与空 body 明确区分）：
            - 无 body（Content-Length<=0）：返回 ``{}``（空 body 语义，向后兼容）；
            - 带 body 但 Content-Type 非 application/json 开头：返回 None；
            - 非法 JSON（malformed）：返回 None，调用方统一回 400 ``bad_json``；
            - 合法 JSON 但非 dict（数组/字符串/数字等）：返回 None（结构不符契约，
              视为坏请求）。
            调用方必须将 None 视为坏请求回 400。
            """
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0:
                return {}
            if length > _MAX_BODY_BYTES:
                # N6 + H-4（第三轮体检批次2）：超大 body 直接 413，防止阻塞单线程
                # 服务（本机 DoS 面）。丢弃读取有界（最多 1MB——保证诚实客户端的
                # 真实大 body 被读完、能收到 413；恶意声明 10GB 也最多丢 1MB），
                # 随后置 close_connection 强制断开；配合 ApiHandler.timeout=30 的
                # socket 超时，慢客户端（slowloris）最坏阻塞 30s 而非永久。
                # 抛内部信号由 do_METHOD 捕获终止分发（响应已发出）
                try:
                    self.rfile.read(min(length, _MAX_BODY_BYTES))
                except OSError:
                    pass
                self.close_connection = True
                self._send_json({"ok": False, "error": "payload_too_large"}, 413)
                raise _BodyTooLarge()
            content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if not content_type.startswith("application/json"):
                return None
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except (OSError, ValueError):
                return None
            return body if isinstance(body, dict) else None

        def _handle_agents_list(self, query):
            """Agent 列表：支持 enabled 过滤（true/false），默认返回全部。"""
            enabled = None
            if query.get("enabled") is not None:
                raw = query["enabled"].strip().lower()
                if raw in ("true", "1"):
                    enabled = True
                elif raw in ("false", "0"):
                    enabled = False
                else:
                    self._send_json({"ok": False, "error": "bad_request", "message": "enabled 必须为 true/false"}, 400)
                    return
            agents = self._manager.list(enabled=enabled)
            self._send_json([a.to_dict() for a in agents])

        def _handle_agents_create(self):
            """创建 Agent：body 必须含 name 与 persona，voice 可选。"""
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            name = (body.get("name") or "").strip()
            persona = (body.get("persona") or "").strip()
            if not name or not persona:
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "name 与 persona 为必填字段"}, 400
                )
                return
            voice = body.get("voice") or None
            agent = self._manager.create(name=name, persona=persona, voice=voice)
            self._send_json(agent.to_dict(), 201)

        def _handle_agents_update(self, agent_id):
            """更新 Agent：body 为可更新字段（name/persona/voice/enabled）。

            L-4（第三轮体检批次2）：API 层先做白名单键过滤再展开——即使底层
            AgentManager.update 的白名单未来放开，接口面仍只接受这四个字段。
            """
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            allowed_keys = ("name", "persona", "voice", "enabled")
            patch = {k: body[k] for k in allowed_keys if k in body}
            try:
                agent = self._manager.update(agent_id, **patch)
            except AgentNotFound as exc:
                self._send_json({"ok": False, "error": "not_found", "message": str(exc)}, 404)
                return
            self._send_json(agent.to_dict())

        def _handle_agents_delete(self, agent_id):
            """删除 Agent：不存在时返回 404。"""
            try:
                self._manager.delete(agent_id)
            except AgentNotFound as exc:
                self._send_json({"ok": False, "error": "not_found", "message": str(exc)}, 404)
                return
            self._send_json({"ok": True, "id": agent_id})

    return ApiHandler


def create_app(data_dir=None, config_path=None):
    """创建完整应用依赖并返回 (store, pipeline, handler_class)。

    Args:
        data_dir: 数据目录（None 用项目根 data/）。
        config_path: 配置文件路径（H-3；None 用 data_dir/config.json；
            生产链传 <root>/config.json 与安装链统一真相源）。

    Returns:
        tuple: (store, pipeline, handler) -> (MemoryStore, MemoryRetrievalPipeline, ApiHandler)。
    """
    data_dir = _resolve_data_dir(data_dir)
    store, pipeline, manager, remote = build_deps(data_dir, config_path=config_path)
    computer, authorizer, bridge = build_computer_deps(data_dir)
    #: 配置实例与 remote 同源（同一 config_path），供 /api/settings 读写，
    #: 保证测试隔离（不触碰项目根运行时配置）。
    config = ConfigManager(config_path=config_path or os.path.join(data_dir, "config.json"))
    #: 批次E生产装配：语音编排 / 内置工具注册表 / 记忆蒸馏器，全量注入 handler
    voice, registry, distiller = build_runtime_deps(
        data_dir,
        config=config,
        store=store,
        pipeline=pipeline,
        computer_deps=(computer, authorizer, bridge),
    )
    #: 管理面（20261004_模块0_管理面CX-A管理CX-O）：CX-O 实例群台账 + 注册接收 + 治理透传
    fleet = FleetManager(data_dir=data_dir)
    handler = make_handler(
        store, pipeline, manager, remote, fleet=fleet,
        computer=computer, authorizer=authorizer, bridge=bridge, config=config,
        registry=registry, distiller=distiller, voice=voice,
    )
    return store, pipeline, handler


def create_server(host=DEFAULT_HOST, port=DEFAULT_PORT, data_dir=None, config_path=None):
    """构建并返回配置好的 HTTPServer（单线程串行处理）。"""
    _store, _pipeline, handler = create_app(data_dir, config_path=config_path)
    return HTTPServer((host, port), handler)


# 管理 API 令牌落盘（20261004_模块0_管理API令牌落盘）：外部管理 Agent 在应用运行时
# 读 <app_root>/logs/api_token.json 拿当次令牌，带 X-Client-Token 调管理 API。
# 安全边界与令牌本身一致（N1 防浏览器侧 CSRF→RCE，非同用户本机进程）。
TOKEN_FILE_NAME = "api_token.json"


def _token_file_path() -> str:
    """令牌文件路径：<app_root>/logs/api_token.json。"""
    return os.path.join(app_root(), "logs", TOKEN_FILE_NAME)


def write_token_file(port: int) -> None:
    """令牌模式下把当次启动令牌落盘；开放模式（未设令牌）不写。

    令牌每次启动随机轮换，文件随启动覆盖；pid 供管理 Agent 判断服务存活
    （防读到崩溃残留旧文件）。原子写，避免读到半截 JSON。
    """
    token = _env_api_token()
    if not token:
        return
    logs_dir = os.path.dirname(_token_file_path())
    os.makedirs(logs_dir, exist_ok=True)
    payload = {
        "token": token,
        "port": port,
        "pid": os.getpid(),
        "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    tmp_path = f"{_token_file_path()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp_path, _token_file_path())


def remove_token_file() -> None:
    """尽力删除令牌文件（正常退出路径收敛披露面；崩溃残留由 pid 字段辨识）。"""
    try:
        os.remove(_token_file_path())
    except OSError:
        pass


def main(argv=None):
    """命令行入口：解析 -h/--host、-p/--port 并启动服务，支持 Ctrl+C 优雅退出。"""
    parser = argparse.ArgumentParser(
        prog="api_server",
        description="CXLite 轻量记忆浏览与管理 REST 服务",
        add_help=False,  # 关闭 argparse 默认 -h（帮助），腾出 -h 给 host
    )
    parser.add_argument("-h", "--host", default=DEFAULT_HOST, help=f"监听主机（默认 {DEFAULT_HOST}）")
    parser.add_argument("-p", "--port", type=int, default=DEFAULT_PORT, help=f"监听端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--data-dir", default=None, help="数据目录（默认项目根 data/）")
    parser.add_argument(
        "--config",
        default=None,
        help="配置文件路径（H-3：生产链传 <root>/config.json 与安装链统一；默认 data_dir/config.json）",
    )
    args = parser.parse_args(argv)

    host, port = args.host, args.port
    # 中-4（第四轮体检批次B）启动安全闸：非回环监听 + 未配置 CXA_API_TOKEN 的组合
    # 意味着 LAN 内任意主机可无令牌调用 /api/computer/authorize 等端点（远程控制面
    # 完全暴露）。默认拒绝启动；CXA_ALLOW_UNSAFE=1 显式放行并打印醒目风险横幅。
    if not _is_loopback_host(host) and not _env_api_token():
        if os.environ.get("CXA_ALLOW_UNSAFE", "").strip() == "1":
            print("=" * 68)
            print("[安全警告] CXA_ALLOW_UNSAFE=1：API 服务将以【无令牌】模式绑定非回环地址")
            print(f"[安全警告] 监听 {host}:{port} —— LAN 内任意主机可调用电脑控制/授权端点！")
            print("[安全警告] 仅限临时调试使用，生产环境必须设置 CXA_API_TOKEN。")
            print("=" * 68)
        else:
            print(
                f"[ERROR] 拒绝启动：监听地址 {host} 为非回环地址且未设置 CXA_API_TOKEN，"
                "LAN 内任意主机可无令牌调用电脑控制等端点。"
            )
            print(
                "[ERROR] 请设置环境变量 CXA_API_TOKEN 启用令牌校验后重启；"
                "确有需要可临时设置 CXA_ALLOW_UNSAFE=1 强行放行（风险自负）。"
            )
            sys.exit(1)
    server = create_server(host=host, port=port, data_dir=args.data_dir, config_path=args.config)
    write_token_file(port)
    print(f"[INFO] 记忆 API 服务已启动: http://{host}:{port}/api/health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[INFO] 收到 Ctrl+C，正在优雅退出…")
    finally:
        server.server_close()
        remove_token_file()
        print("[INFO] 服务已关闭")


if __name__ == "__main__":
    main()