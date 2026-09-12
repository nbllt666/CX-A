# -*- coding: utf-8 -*-
"""有界视觉片段处理队列（VisionClipQueue）——对齐 CX-O 语义的 lite 同步化版本。

对照 CX-O ``server/core/vision/clip_queue.py``（只读参考）的语义继承与差异：

- **继承语义**：
  1. 独立队列：不复用对话 worker，避免与主链路争抢；
  2. 可注入 consumer：``set_consumer`` 注册理解消费回调，未注册时仅告警跳过；
  3. 惰性启动：首次 ``submit`` 时才启动 worker 线程（CX-O 为事件循环 create_task）；
  4. consumer 异常隔离：处理异常被捕获并告警，worker 不崩溃，继续处理下一条；
  5. 丢弃计数：透出 ``dropped_count`` 供运维观测（CX-O 为 ClipQueueDropStats）。

- **lite 差异（有意为之，文档注明）**：
  1. CX-O consumer 为 async 可等待回调、worker 跑在事件循环上；lite 设计为
     **同步 callable + 单条 daemon worker 线程**（lite 主链路单线程同步化，
     队列是唯一后台线程，退出走 ``stop()``）；
  2. CX-O 队列满时**丢弃最新**（enqueue 返回 False）；lite 按需求改为
     **满时丢弃最旧**（最新片段更贴近当前画面状态，保留价值更高）；
  3. CX-O 每条目携带临时视频文件（clip_path）并在终态幂等清理；lite 片段为
     内存降采样帧，无临时文件，故无文件清理责任。

隐私红线：片段仅存在于内存队列，不落盘；consumer 处理结束后条目即释放。
"""

import logging
import queue as _queue
import threading

#: 原生日志记录器（异常隔离告警统一携带 [VisionQueue][WARN] 前缀）
LOGGER = logging.getLogger(__name__)


class VisionClipQueue:
    """有界视觉片段处理队列（内存版，单 daemon worker 线程，满时丢弃最旧）。

    Attributes:
        maxsize: 队列最大容量，超出时丢弃最旧条目腾位（默认 16）。
    """

    #: worker 轮询空队列的休眠步长（秒）——兼顾退出响应速度与空转开销
    _POLL_INTERVAL_S = 0.05

    def __init__(self, maxsize: int = 16) -> None:
        """构造队列。

        :param maxsize: 有界容量上限（默认 16，对齐配置契约键 ``vision.queue_size``）
        """
        if maxsize <= 0:
            raise ValueError(f"队列容量必须为正数，收到 maxsize={maxsize}")
        self._q: "_queue.Queue" = _queue.Queue(maxsize=maxsize)
        self._consumer = None
        self._worker: threading.Thread = None
        self._stop_event = threading.Event()
        self._dropped_count = 0

    # ------------------------------------------------------------------ #
    # 对外接口                                                            #
    # ------------------------------------------------------------------ #

    def set_consumer(self, consumer) -> None:
        """注册消费回调（同步 callable，签名 ``consumer(item: dict)``）。

        传 ``None`` 表示清空 consumer（此后条目仅告警跳过，不做任何理解动作）。
        注意：CX-O 的 consumer 为 async 可等待回调；lite 单线程同步化后此处
        为普通同步函数，内部由唯一 daemon worker 线程串行调用。
        """
        self._consumer = consumer

    def submit(self, item: dict) -> bool:
        """把一个片段条目放入队列（满时丢弃最旧腾位）。

        惰性启动：worker 线程未启动时在首次 submit 时拉起（daemon 线程）。
        ``stop()`` 之后再 submit 返回 False（拒绝已停队列的迟到投递）。

        Args:
            item: 片段条目 dict（如 sampler 产出的视觉事件）。

        Returns:
            bool: 入队成功返回 True；队列已停止返回 False。
        """
        if self._stop_event.is_set():
            LOGGER.warning("[VisionQueue][WARN] 队列已停止，拒绝条目: %r", item)
            return False
        self._ensure_worker()
        try:
            self._q.put_nowait(item)
        except _queue.Full:
            # 满时丢弃最旧（对齐需求语义；CX-O 为丢弃最新，差异已在模块头注明）
            try:
                self._q.get_nowait()
                self._dropped_count += 1
                LOGGER.warning("[VisionQueue][WARN] 队列已满，丢弃最旧条目后腾位")
            except _queue.Empty:  # pragma: no cover - 并发窗口防御
                pass
            try:
                self._q.put_nowait(item)
            except _queue.Full:  # pragma: no cover - 并发窗口防御
                self._dropped_count += 1
                LOGGER.warning("[VisionQueue][WARN] 队列已满且腾位失败，丢弃条目: %r", item)
                return False
        return True

    def pending_count(self) -> int:
        """当前队列中尚未取出的条目数。"""
        return self._q.qsize()

    def is_ready(self) -> bool:
        """是否已注册 consumer（可字节消费）。"""
        return self._consumer is not None

    @property
    def dropped_count(self) -> int:
        """累计因队列满被丢弃的最旧条目数（仅增不减，运维观测）。"""
        return self._dropped_count

    def stop(self, timeout: float = 2.0) -> None:
        """停止 worker 线程（退出前先清空并处理完队列中剩余条目）。

        :param timeout: join 等待上限秒数；超时不再阻塞（daemon 线程随后自灭）。
        """
        self._stop_event.set()
        worker = self._worker
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=timeout)

    # ------------------------------------------------------------------ #
    # 内部：worker 线程                                                   #
    # ------------------------------------------------------------------ #

    def _ensure_worker(self) -> None:
        """惰性启动 worker 线程（仅一条，daemon）。"""
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(
                target=self._run, name="vision-clip-queue-worker", daemon=True
            )
            self._worker.start()

    def _run(self) -> None:
        """worker 主循环：取条目 → 消费 → 异常隔离。

        退出条件：stop 事件置位且队列已清空（保证 stop() 后剩余条目仍被
        完整处理，测试可据此确定性断言）。
        """
        while True:
            try:
                item = self._q.get(timeout=self._POLL_INTERVAL_S)
            except _queue.Empty:
                if self._stop_event.is_set():
                    break
                continue
            self._handle(item)

    def _handle(self, item: dict) -> None:
        """处理单条条目：consumer 缺失或异常均告警隔离，worker 不崩。"""
        consumer = self._consumer
        if consumer is None:
            LOGGER.warning("[VisionQueue][WARN] 未注册 consumer，条目跳过: %r", item)
            return
        try:
            consumer(item)
        except Exception as exc:  # noqa: BLE001 —— consumer 失败不令 worker 崩溃
            LOGGER.warning(
                "[VisionQueue][WARN] consumer 处理条目失败（worker 继续运行）: %s", exc
            )
