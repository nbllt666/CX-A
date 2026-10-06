# -*- coding: utf-8 -*-
"""本地小 LLM 后台下载管理器（Task 6）——线程 / 状态锁 / 取消逻辑的唯一归属。

对齐 spec「向导内下载（进度 / 取消 / 不阻塞服务）」：

- **不阻塞服务**：真正的下载在 ``threading.Thread(daemon=True)`` 内执行；请求线程
  只做「启动 / 读快照 / 置取消」三类 O(1) 操作，``snapshot()`` 加锁读取后立即返回，
  绝不等待下载线程结束（``api_server`` 为单线程 HTTPServer，一旦阻塞即整站挂起）；
- **进度**：下载器 ``progress_cb(downloaded, total)`` 回调内更新已下载 / 总字节数，
  百分比由 :meth:`ModelDownloadManager.snapshot` 按需计算；
- **取消**：``cancel()`` 置 ``threading.Event``；下载线程在下一次 ``progress_cb``
  回调时检查该标记并抛出内部哨兵 :class:`_DownloadCanceled` 中断下载。
  ``LlmDownloader`` 的异常路径**保留 ``.tmp``**（既有 L10 行为），下次重入自动续传；
- **配置写入串行化**：本模块内所有 ``config.save()`` 与 ``api_server`` 请求线程的
  ``config.save()`` 共用**同一把模块级写锁**（``api_server._CONFIG_WRITE_LOCK``，
  经 ``write_lock`` 参数注入；未注入时本模块自建一把，语义等价）。该锁的归属为
  **调用方（api_server 模块）**，本模块只持有引用——请求线程与下载线程共用同一
  锁对象，防止并发落盘导致 ``config.json`` 字段丢失 / 截断；
- **不静默换源**：失败时 ``error`` 为中文，明确含所用**下载通道 / 端点**并提示可
  切换通道或仓库后重试，**绝不自动改换下载来源**；
- **可注入**：``downloader_factory`` 供测试注入替身（绝不触网）；生产缺省即
  ``LlmDownloader``。

本模块不读写任何 handler 实例状态：后台线程只经本管理器的锁访问自身字段。
"""

import os
import threading
import time

from lite.config.download_sources import (
    CHANNEL_MIRROR,
    CHANNEL_OFFICIAL,
    DEFAULT_CHANNEL,
    HF_OFFICIAL,
    MODEL_REPOS,
    normalize_channel,
)
from lite.runtime.model_downloader import LlmDownloader

__all__ = [
    "ModelDownloadManager",
    "STATE_IDLE",
    "STATE_DOWNLOADING",
    "STATE_DONE",
    "STATE_FAILED",
    "STATE_CANCELED",
]

#: 下载状态机取值（与前端冻结契约 ``GET /api/setup/model/progress`` 一致）
STATE_IDLE = "idle"
STATE_DOWNLOADING = "downloading"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CANCELED = "canceled"

#: 通道中文说明（失败提示用）
_CHANNEL_LABELS = {
    CHANNEL_MIRROR: "国内路线（魔塔）",
    CHANNEL_OFFICIAL: "海外路线（HuggingFace）",
}

#: 魔塔站点（``modelscope`` 源本身即国内站，下载 URL 与通道无关）
_MODELSCOPE_SITE = "https://modelscope.cn（魔塔，本身即国内站，与通道无关）"


class _DownloadCanceled(Exception):
    """内部哨兵：取消标记命中时由 ``progress_cb`` 抛出，用于中断下载（不对外暴露）。"""


def _now_str():
    """返回形如 ``2026-09-19 10:30:00`` 的本地时间戳字符串。"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _log(level, msg):
    """中文时间戳日志：``YYYY-MM-DD HH:MM:SS [INFO/ERROR] 内容``。"""
    print(f"{_now_str()} [{level}] {msg}")


class ModelDownloadManager:
    """本地小 LLM 后台下载管理器（线程安全）。

    三个对外方法构成最小接口面：:meth:`start`（幂等启动）、:meth:`snapshot`
    （即时读进度）、:meth:`cancel`（置取消标记）。所有共享字段均受 ``self._lock``
    保护，后台线程不触碰任何 handler 实例状态。
    """

    def __init__(self, dest_dir=None, config_manager=None, downloader_factory=None, write_lock=None):
        """初始化下载管理器。

        :param dest_dir: 模型落盘目录；``None`` 时由 ``LlmDownloader`` 用其默认值
            （工程根 ``data/local_llm``）。
        :param config_manager: 任意提供 ``get/set/save`` 的配置对象；下载成功后在
            写锁内写回 ``local_llm.model_path`` 并落盘。``None`` 时仅下载不写配置。
        :param downloader_factory: 下载器构造器（测试注入替身，绝不触网）。调用形态
            ``factory(dest_dir=..., source=...)``；返回对象须实现
            ``download(repo, filename, source=None, progress_cb=None,
            verify_size_gb=None)``。``None`` 时用 ``LlmDownloader``。
        :param write_lock: 配置写入锁；应与 ``api_server`` 请求线程**共用同一把**
            （``api_server._CONFIG_WRITE_LOCK``）。未注入时本模块自建一把（等价语义，
            仅适用于单一管理器场景）——锁的归属为调用方，本模块只持有引用。
        """
        self.dest_dir = dest_dir
        self.config_manager = config_manager
        self._downloader_factory = downloader_factory
        #: 配置写锁：请求线程与下载线程共用（见模块 docstring）
        self._write_lock = write_lock if write_lock is not None else threading.Lock()
        #: 状态锁：保护下方全部共享字段
        self._lock = threading.Lock()
        #: 取消标记（每次 start 重新创建，避免上一轮取消污染下一轮）
        self._cancel_event = threading.Event()
        self._state = STATE_IDLE
        self._downloaded = 0
        self._total = 0
        self._file = ""
        self._error = None
        self._model = None
        self._thread = None

    # ------------------------------------------------------------------ #
    # 对外只读属性 / 内部辅助                                             #
    # ------------------------------------------------------------------ #

    @property
    def cancel_event(self):
        """当前轮的取消标记（供测试与下载替身观察；生产由 :meth:`cancel` 置位）。"""
        return self._cancel_event

    def _config_get(self, section, key, default=None):
        """经鸭子类型读取配置；配置对象缺席或读取失败时返回默认值。"""
        getter = getattr(self.config_manager, "get", None)
        if not callable(getter):
            return default
        try:
            return getter(section, key, default)
        except Exception:  # noqa: BLE001 - 配置读取异常不应阻断下载
            return default

    def _resolve_source(self, source):
        """归一化下载源：请求值 → 配置 ``local_llm.source`` → ``modelscope``。

        请求值与配置值均不在 ``MODEL_REPOS`` 白名单内时回落 ``modelscope`` 并给出
        一条中文提示（实际使用的源会随响应 ``model.source`` 回显，非静默）。
        """
        for candidate in (source, self._config_get("local_llm", "source", None)):
            if isinstance(candidate, str) and candidate.strip().lower() in MODEL_REPOS:
                return candidate.strip().lower()
        if source is not None:
            _log("INFO", f"下载源 {source!r} 不在支持列表 {list(MODEL_REPOS)}，回落 modelscope（魔塔）")
        return "modelscope"

    def _resolve_channel(self):
        """归一化当前下载通道（``download.channel``，非法值按默认通道处理）。"""
        return normalize_channel(self._config_get("download", "channel", DEFAULT_CHANNEL))

    def _make_downloader(self, source):
        """构造下载器（缺省 ``LlmDownloader``，测试可注入替身）。

        端点不再作为构造参数（HuggingFace 恒用官方端点，modelscope 恒用魔塔站点）。
        """
        factory = self._downloader_factory
        if factory is None:
            factory = lambda **kwargs: LlmDownloader(**kwargs)  # noqa: E731 - 缺省构造器
        return factory(dest_dir=self.dest_dir, source=source)

    @staticmethod
    def _endpoint_display(source):
        """取「实际生效的下载站点」展示文本（失败提示用）。

        HuggingFace 恒用官方端点；魔塔本身即独立国内站，与通道无关。
        """
        if source == "huggingface":
            return HF_OFFICIAL
        return _MODELSCOPE_SITE

    def _failure_message(self, exc, source, channel):
        """组装失败中文说明：含所用通道 / 下载站点 + 「切换」提示（不自动换源）。"""
        normalized = normalize_channel(channel)
        label = _CHANNEL_LABELS.get(normalized, normalized)
        return (
            f"下载失败：{str(exc)[:200]}。本次使用下载通道「{normalized}」（{label}），"
            f"模型下载端点：{self._endpoint_display(source)}。"
            "请检查网络后重试，或切换下载通道（国内（魔塔） / 海外（HuggingFace））"
            "或模型仓库（魔塔 / HuggingFace）后重试；"
            "系统不会在您不知情的情况下自动改换下载来源。"
        )

    # ------------------------------------------------------------------ #
    # 对外接口                                                            #
    # ------------------------------------------------------------------ #

    def start(self, source=None, tier=None) -> dict:
        """启动一次后台下载（幂等：进行中时不启动第二个线程）。

        :param source: 下载源；``None`` 时取配置 ``local_llm.source``（再回落魔塔）。
        :param tier: 模型档位（``E2B-Q4`` / ``E2B-Q6`` / ``E4B-Q4`` / ``E4B-Q6``）；
            ``None`` 时回落默认档 ``E2B-Q4``。
        :return: ``{"ok": True, "state": "downloading", "already_running": bool,
            "model": {...}}``；``already_running=True`` 表示复用进行中的任务。
        :raises ValueError: 档位不在 ``MODEL_TIERS`` 中（中文错误，由调用方映射 400）。
        """
        with self._lock:
            if self._state == STATE_DOWNLOADING and self._thread is not None and self._thread.is_alive():
                return {
                    "ok": True,
                    "state": STATE_DOWNLOADING,
                    "already_running": True,
                    "model": self._model,
                }

        resolved_source = self._resolve_source(source)
        # suggest_model 对未知档位抛中文 ValueError——不静默下载错误文件
        model_info = LlmDownloader.suggest_model(resolved_source, tier)
        # 通道仍用于失败提示 / 日志（展示本次下载路线）；下载 URL 不再由通道派生端点
        channel = self._resolve_channel()
        downloader = self._make_downloader(resolved_source)

        cancel_event = threading.Event()
        with self._lock:
            self._cancel_event = cancel_event
            self._state = STATE_DOWNLOADING
            self._downloaded = 0
            self._total = 0
            self._file = model_info.get("filename") or ""
            self._error = None
            self._model = model_info
            thread = threading.Thread(
                target=self._run,
                args=(downloader, model_info, resolved_source, channel, cancel_event),
                name="cxa-model-download",
                daemon=True,
            )
            self._thread = thread
        thread.start()
        _log(
            "INFO",
            f"已启动本地模型后台下载：{model_info.get('repo')} / {model_info.get('filename')}"
            f"（通道 {channel}，仓库 {resolved_source}）",
        )
        return {
            "ok": True,
            "state": STATE_DOWNLOADING,
            "already_running": False,
            "model": model_info,
        }

    def snapshot(self) -> dict:
        """加锁即时返回下载快照（绝不等待下载线程）。

        :return: ``{"state", "downloaded", "total", "percent", "file", "error",
            "model"}``——字段与前端冻结契约一致；``state`` 为 ``idle`` /
            ``downloading`` / ``done`` / ``failed`` / ``canceled`` 之一。
        """
        with self._lock:
            total = int(self._total or 0)
            downloaded = int(self._downloaded or 0)
            percent = round(downloaded * 100.0 / total, 2) if total > 0 else 0.0
            return {
                "state": self._state,
                "downloaded": downloaded,
                "total": total,
                "percent": percent,
                "file": self._file,
                "error": self._error,
                "model": self._model,
            }

    def cancel(self) -> dict:
        """置取消标记并立即返回（不等待下载线程退出）。

        下载线程在下一次 ``progress_cb`` 回调时经内部哨兵中断下载；``.tmp`` 由
        ``LlmDownloader`` 的异常路径保留，供下次断点续传。

        :return: ``{"ok": True, "state": "canceled"}``。
        """
        self._cancel_event.set()
        with self._lock:
            self._state = STATE_CANCELED
        _log("INFO", "已请求取消本地模型下载（保留 .tmp 供断点续传）")
        return {"ok": True, "state": STATE_CANCELED}

    # ------------------------------------------------------------------ #
    # 后台线程主体                                                        #
    # ------------------------------------------------------------------ #

    def _run(self, downloader, model_info, source, channel, cancel_event):
        """后台下载线程主体：下载 → 写回配置 / 记录取消 / 记录失败。

        仅访问本管理器自身的加锁字段，**不触碰任何 handler 实例状态**。
        """
        repo = model_info.get("repo")
        filename = model_info.get("filename")
        verify_size_gb = model_info.get("approximate_size_gb")

        def _progress(downloaded, total):
            """进度回调：先查取消标记（抛内部哨兵中断下载），再更新加锁状态。"""
            if cancel_event.is_set():
                raise _DownloadCanceled()
            with self._lock:
                self._downloaded = int(downloaded or 0)
                self._total = int(total or 0)

        try:
            path = downloader.download(
                repo,
                filename,
                source=source,
                progress_cb=_progress,
                verify_size_gb=verify_size_gb,
            )
            # 多模态档位（20261004 Gemma 4）：主模型就位后同仓库下载视觉投影
            # 文件（mmproj，落同目录；llama-server 经 --mmproj 挂载后具备看图
            # 能力）。失败与主模型同口径落 failed（文本聊天不可缺视觉组件的
            # 语义一致性由档位表保证——四档均多模态）。
            mmproj_name = model_info.get("mmproj_filename")
            if mmproj_name:
                mmproj_path = downloader.download(
                    repo,
                    mmproj_name,
                    source=source,
                    progress_cb=_progress,
                    verify_size_gb=model_info.get("mmproj_size_gb"),
                )
                _log("INFO", f"视觉组件下载完成：{mmproj_path}")
        except _DownloadCanceled:
            self._mark_canceled(filename)
            return
        except Exception as exc:  # noqa: BLE001 - 网络/写入/校验异常统一落为 failed
            if cancel_event.is_set():
                self._mark_canceled(filename)
                return
            message = self._failure_message(exc, source, channel)
            with self._lock:
                self._state = STATE_FAILED
                self._error = message
            _log("ERROR", message)
            return

        # 取消后即便下载器已跑完也不写回配置（保持 canceled 语义一致）
        if cancel_event.is_set():
            self._mark_canceled(filename)
            return

        model_path = str(path)
        save_error = None
        if self.config_manager is not None:
            # 配置写入串行化：与 api_server 请求线程共用同一把模块级写锁
            with self._write_lock:
                try:
                    self.config_manager.set("local_llm", "model_path", model_path)
                    self.config_manager.save()
                except Exception as exc:  # noqa: BLE001 - 落盘失败不得损坏既有文件
                    save_error = exc
        with self._lock:
            if save_error is None:
                self._state = STATE_DONE
                self._file = os.path.basename(model_path) or filename
                self._error = None
            else:
                self._state = STATE_FAILED
                self._error = (
                    f"模型已下载到 {model_path}，但配置写入失败（{str(save_error)[:200]}）；"
                    "请检查配置目录的写入权限后重试。"
                )
        if save_error is None:
            _log("INFO", f"本地模型下载完成：{model_path}（已写入 local_llm.model_path）")
        else:
            _log("ERROR", f"本地模型下载完成但配置写入失败：{model_path}（{save_error}）")

    def _mark_canceled(self, filename):
        """把状态置为 ``canceled`` 并记录中文日志（取消路径统一出口）。"""
        with self._lock:
            self._state = STATE_CANCELED
        _log("INFO", f"下载已取消（保留 .tmp 供断点续传）：{filename}")