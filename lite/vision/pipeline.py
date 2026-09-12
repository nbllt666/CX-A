# -*- coding: utf-8 -*-
"""主动视觉生产管线（VisionPipeline）——采样 → 队列 → 云端理解 → 记忆沉淀。

对照 CX-O ``server/core/vision/pipeline.py``（只读参考）的装配语义继承与差异：

- **继承语义**：consumer 内「理解 → 沉淀」两段均 try/except 隔离，失败仅告警
  不阻断 worker；enabled 判定不过则不触碰采样链路；组件全部可注入（测试替身）。
- **lite 差异（有意为之，文档注明）**：
  1. CX-O 在服务启动 lifespan 中幂等注册 consumer（``register_vision_pipeline``）；
     lite 管线在构造时直接完成 consumer 接线，enabled 判定前移到每次
     ``run_once``（配合 ``vision`` 热更新段，开关即时生效）；
  2. CX-O 理解组件为 async ``consume``；lite 为同步 callable 注入点。

**enabled 隐私红线**：``vision.enabled`` 为 False（默认）时 ``run_once`` 直接返回
None，且**绝不调用 ``screen_backend.capture``**——未开启视觉时不产生任何屏幕采样。

**离线暂停语义**：``cloud.is_online()`` 为 False 时跳过理解与沉淀；片段已被
worker 取出的不回队，**恢复在线后从新片段开始，离线期间的旧片段直接丢弃**
（旧片段画面状态已过期，理解价值低于新片段）。

**记忆沉淀契约**：理解产出摘要后写入 memories 表——
``{type: "short_term", content: 摘要, source: "vision", importance: 2, agent_id: "default"}``，
返回新记忆 id；沉淀失败仅告警不崩。
"""

import logging

from lite.cloud.adapter import CloudUnavailableError
from lite.vision.queue import VisionClipQueue

#: 原生日志记录器（告警统一携带 [VisionPipeline][WARN] 前缀）
LOGGER = logging.getLogger(__name__)

#: 生产默认理解提示词（中文，一句话客观描述，限 60 字内）
VISION_PROMPT = "用一句话客观描述这帧屏幕内容，不超过 60 字"

#: 亮度拓扑渲染字符集（下标随亮度递增）
_BRIGHTNESS_CHARS = " .:-=+*#@"

#: 亮度拓扑渲染的行宽（与默认降采样宽度一致）
_BRIGHTNESS_MAP_WIDTH = 64


def _render_brightness_map(frame, width: int = _BRIGHTNESS_MAP_WIDTH) -> str:
    """把降采样灰度帧渲染为文本亮度拓扑（纯文本降级描述，供云端 LLM 理解）。

    字符越靠后表示越亮（空格=全黑，@=全白）。lite 的帧为一维降采样灰度序列，
    不做 PNG 编码（隐私红线：原始帧不出本机，仅降采样亮度拓扑参与理解）；
    当 backend 未来提供 base64 编码帧时，走 item["image_b64"] 的 image_url 路径。
    """
    if not frame:
        return "(空帧)"
    chars = []
    n_chars = len(_BRIGHTNESS_CHARS)
    for i, px in enumerate(frame):
        if i and i % width == 0:
            chars.append("\n")
        idx = min(n_chars - 1, max(0, int(px) * n_chars // 256))
        chars.append(_BRIGHTNESS_CHARS[idx])
    return "".join(chars)


class VisionPipeline:
    """主动视觉生产管线：自适应采样 → 有界队列 → 云端理解 → 记忆沉淀。"""

    def __init__(self, sampler=None, queue: VisionClipQueue = None, cloud=None,
                 memory_store=None, config=None, understanding=None):
        """构造管线（全部组件可注入，缺省仅队列可自建）。

        :param sampler: :class:`~lite.vision.sampler.AdaptiveSampler` 实例；
            缺省 None 时 ``run_once`` 告警空转（lite 无内置屏幕采集实现，
            生产装配时注入携带真实 screen_backend 的采样器）
        :param queue: 视觉片段队列；缺省新建 :class:`VisionClipQueue`（容量 16）
        :param cloud: CloudAdapter 实例（须有 ``chat``/``is_online``）；
            缺省 None 时默认理解路径报 CloudUnavailableError（告警隔离）
        :param memory_store: MemoryStore 实例；缺省 None 时理解结果无处沉淀（告警跳过）
        :param config: 配置载体——ConfigManager 或含 ``vision`` 段的 dict；
            enabled 取 ``vision.enabled``（默认 False）
        :param understanding: 理解注入点，callable(item) -> str 摘要文本；
            缺省用 :meth:`_default_understanding`（CloudAdapter 多模态消息）
        """
        self._sampler = sampler
        self._cloud = cloud
        self._memory_store = memory_store
        self._config = config
        self._understanding = understanding if understanding is not None else self._default_understanding
        self._queue = queue if queue is not None else VisionClipQueue()
        # consumer 接线（对齐 CX-O register_vision_pipeline 的装配职责）
        self._queue.set_consumer(self._consume)

    # ------------------------------------------------------------------ #
    # 对外接口                                                            #
    # ------------------------------------------------------------------ #

    def run_once(self, now: float = None):
        """驱动一次视觉管线循环：到期采样 → 变化达标则入队待理解。

        enabled 为 False 时直接返回 None 且绝不调用 ``screen_backend.capture``
        （隐私红线：未开启视觉时不产生任何屏幕采样）。

        Args:
            now: 当前时间戳（秒）；透传给 sampler.tick（测试注入脚本化时间）。

        Returns:
            dict | None: 采样的视觉事件（已入队）；未采样 / 未启用返回 None。
        """
        if not self._enabled():
            return None
        if self._sampler is None:
            LOGGER.warning("[VisionPipeline][WARN] 未注入 sampler，管线空转（生产装配时注入 AdaptiveSampler）")
            return None
        event = self._sampler.tick(now=now)
        if event is None:
            return None
        self._queue.submit(event)
        return event

    def stop(self) -> None:
        """停止管线：停掉队列 worker（退出前清空处理完剩余条目）。"""
        self._queue.stop()

    @property
    def queue(self) -> VisionClipQueue:
        """内部队列实例（运维观测 pending/dropped 计数用）。"""
        return self._queue

    # ------------------------------------------------------------------ #
    # 内部：consumer（理解 → 沉淀）                                       #
    # ------------------------------------------------------------------ #

    def _consume(self, item: dict):
        """队列 consumer：云端理解片段 → 摘要沉淀为 source='vision' 记忆。

        异常契约：理解失败 / 云端离线（CloudUnavailableError）/ 沉淀失败一律
        告警并返回 None，不向 worker 抛异常（对齐 CX-O consumer 异常隔离语义）。

        Args:
            item: 视觉事件 dict（含 frame / change_ratio / timestamp）。

        Returns:
            int | None: 沉淀成功返回新记忆 id；其余情况返回 None。
        """
        if self._memory_store is None:
            LOGGER.warning("[VisionPipeline][WARN] 未注入 memory_store，跳过沉淀: %r", item.get("timestamp"))
            return None
        if self._cloud is not None and not self._cloud.is_online():
            # 离线暂停：跳过理解与沉淀；恢复在线后从新片段开始，旧片段丢弃
            LOGGER.warning("[VisionPipeline][WARN] 云端离线，暂停视觉理解与沉淀（恢复后从新片段开始，旧片段丢弃）")
            return None
        try:
            summary = self._understanding(item)
        except CloudUnavailableError as exc:
            LOGGER.warning("[VisionPipeline][WARN] 云端不可用，视觉理解失败（不阻断 worker）: %s", exc)
            return None
        except Exception as exc:  # noqa: BLE001 —— 理解失败不崩 worker
            LOGGER.warning("[VisionPipeline][WARN] 视觉理解失败（不阻断 worker）: %s", exc)
            return None
        if summary is None or not str(summary).strip():
            LOGGER.warning("[VisionPipeline][WARN] 理解产出为空，跳过沉淀")
            return None
        try:
            return self._memory_store.add({
                "type": "short_term",
                "content": str(summary).strip(),
                "source": "vision",
                "importance": 2,
                "agent_id": "default",
            })
        except Exception as exc:  # noqa: BLE001 —— 沉淀失败不崩 worker
            LOGGER.warning("[VisionPipeline][WARN] 视觉记忆沉淀失败（不阻断 worker）: %s", exc)
            return None

    # ------------------------------------------------------------------ #
    # 内部：enabled 判定与默认理解实现                                    #
    # ------------------------------------------------------------------ #

    def _enabled(self) -> bool:
        """读取 ``vision.enabled``（默认 False；支持 ConfigManager 与 dict 两种载体）。"""
        cfg = self._config
        if cfg is None:
            return False
        get = getattr(cfg, "get", None)
        if callable(get):
            try:
                # ConfigManager.get(section, key, default) 签名（vision 为热更新段，逐次读取即时生效）
                return bool(cfg.get("vision", "enabled", False))
            except TypeError:
                pass  # 非 ConfigManager 签名（如 dict.get），走下方 dict 分支
        if isinstance(cfg, dict):
            return bool((cfg.get("vision") or {}).get("enabled", False))
        return False

    def _default_understanding(self, item: dict) -> str:
        """生产默认理解实现：CloudAdapter.chat 多模态消息 → 流式摘要拼接。

        消息 content 为数组：提示词 + 视觉描述。视觉描述两条路径——
        - item 含 ``image_b64``（base64 编码帧，预留）→ image_url 数据 URL；
        - 否则 → 纯文本降级描述（降采样亮度拓扑，隐私红线：原始帧不出本机）。

        :raises CloudUnavailableError: 未注入 CloudAdapter 或云端调用失败
        """
        if self._cloud is None:
            raise CloudUnavailableError("未注入 CloudAdapter，无法执行云端视觉理解")
        content = [{"type": "text", "text": VISION_PROMPT}]
        image_b64 = item.get("image_b64")
        if image_b64:
            # 预留路径：backend 提供 base64 编码帧时走多模态 image_url
            data_url = "data:image/png;base64," + str(image_b64)
            content.append({"type": "image_url", "image_url": {"url": data_url}})
        else:
            topology = _render_brightness_map(item.get("frame") or [])
            content.append({
                "type": "text",
                "text": "画面亮度拓扑（64x36 灰度降采样，字符越靠后越亮）：\n" + topology,
            })
        messages = [{"role": "user", "content": content}]
        chunks = [chunk for chunk in self._cloud.chat(messages)]
        return "".join(chunks).strip()


#: 导出提示词常量供下游复用（提示词属于理解契约的一部分）
__all__ = ["VisionPipeline", "VISION_PROMPT", "_render_brightness_map"]
