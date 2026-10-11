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

import json
import logging
import re

from lite.cloud.adapter import CloudUnavailableError
from lite.vision.queue import VisionClipQueue

#: 原生日志记录器（告警统一携带 [VisionPipeline][WARN] 前缀）
LOGGER = logging.getLogger(__name__)

#: 生产默认理解提示词（20261011 视觉语音融合升级为 JSON 判定输出）：
#: ``summary`` 照旧沉淀记忆（摘要经聊天自动记忆注入进上下文）；
#: ``reply_worthy``/``proactive_text`` 驱动 AI 主动搭话（sink 入队 → 前端
#: 轮询气泡/TTS + 托盘通知）。解析失败降级：全文当 summary，不触发主动回复。
VISION_PROMPT = (
    "观察这帧屏幕内容，只输出一个 JSON 对象（不要输出其他文字）："
    '{"summary": "一句话客观描述画面内容，不超过60字", '
    '"reply_worthy": false, "proactive_text": ""}。'
    "reply_worthy 仅当画面出现用户明确在做、需要你主动回应协助的事情才为 true"
    "（普通屏幕内容一律 false）；为 true 时 proactive_text 写你要主动对用户说的"
    "一句话（口语化，不超过30字），否则留空。"
)

#: 亮度拓扑渲染字符集（下标随亮度递增）
_BRIGHTNESS_CHARS = " .:-=+*#@"

#: 亮度拓扑渲染的行宽（与默认降采样宽度一致）
_BRIGHTNESS_MAP_WIDTH = 64


def _parse_vision_json(raw):
    """解析视觉理解产出的 JSON（容错：剥围栏 + 提取首个平衡对象；非法返回 None）。

    :param raw: 理解产出的原始文本（模型可能混入说明文字 / markdown 围栏）
    :return: dict（合法 JSON 对象）或 None（无 JSON / 解析失败）
    """
    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start:i + 1])
                except ValueError:
                    return None
                return obj if isinstance(obj, dict) else None
    return None


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
                 memory_store=None, config=None, understanding=None,
                 local_understanding=None, proactive_sink=None):
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
            缺省用 :meth:`_default_understanding`（本地多模态优先 → 云端拓扑）
        :param local_understanding: 本地多模态理解回调，callable(messages) -> str；
            缺省 None 时直接走云端路径。注入后（20261004 Gemma 4 本地视觉）
            默认理解优先把截图帧（image_url base64）/ 灰度拓扑交给本地小 LLM
            （llama-server ``--mmproj``）理解——数据不出本机；回调未就绪（返回
            None）/ 抛异常时自动回落云端。
        :param proactive_sink: 主动搭话回调（20261011 视觉语音融合），
            callable(payload: dict) -> None；理解产出 JSON 且
            ``reply_worthy=true`` 时以
            ``{"summary", "proactive_text", "timestamp"}`` 调用——缺省 None 时
            判定通过也只沉淀记忆不搭话。回调异常仅告警不影响记忆沉淀主链路。
        """
        self._sampler = sampler
        self._cloud = cloud
        self._memory_store = memory_store
        self._config = config
        self._understanding = understanding if understanding is not None else self._default_understanding
        self._local_understanding = local_understanding
        self._proactive_sink = proactive_sink
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
        raw_text = str(summary).strip()
        # JSON 解析分流（20261011 视觉语音融合）：summary 照旧沉淀记忆；
        # reply_worthy=true 且 proactive_text 非空 → proactive_sink 主动搭话。
        # 解析失败降级：全文当 summary（不触发主动回复）——模型输出不可靠时
        # 行为与旧版逐字等价。
        parsed = _parse_vision_json(raw_text)
        if parsed is not None and str(parsed.get("summary") or "").strip():
            summary_text = str(parsed.get("summary")).strip()
        else:
            summary_text = raw_text
            parsed = None
        try:
            memory_id = self._memory_store.add({
                "type": "short_term",
                "content": summary_text,
                "source": "vision",
                "importance": 2,
                "agent_id": "default",
            })
        except Exception as exc:  # noqa: BLE001 —— 沉淀失败不崩 worker
            LOGGER.warning("[VisionPipeline][WARN] 视觉记忆沉淀失败（不阻断 worker）: %s", exc)
            return None
        # 主动搭话分流（在记忆沉淀成功之后——sink 异常只告警，不影响返回值）
        if (
            parsed is not None
            and parsed.get("reply_worthy") is True
            and self._proactive_sink is not None
        ):
            proactive_text = str(parsed.get("proactive_text") or "").strip()
            if proactive_text:
                try:
                    self._proactive_sink({
                        "summary": summary_text,
                        "proactive_text": proactive_text,
                        "timestamp": item.get("timestamp"),
                    })
                except Exception as exc:  # noqa: BLE001 —— sink 失败不影响沉淀
                    LOGGER.warning(
                        "[VisionPipeline][WARN] 主动搭话回调失败（不影响记忆沉淀）: %s", exc
                    )
            else:
                LOGGER.warning(
                    "[VisionPipeline][WARN] reply_worthy=true 但 proactive_text 为空，跳过主动搭话"
                )
        return memory_id

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
        """生产默认理解实现：本地多模态优先 → 云端灰度拓扑降级。

        双通道语义（20261004 Gemma 4 本地视觉）：
        - **本地优先**：注入 ``local_understanding`` 回调时，把截图帧（事件含
          ``image_b64`` → image_url 数据 URL）或灰度拓扑文本组装为多模态消息，
          交本地小 LLM（llama-server ``--mmproj``）理解——数据不出本机；回调
          返回 None / 抛异常时告警并自动回落云端；
        - **云端**：维持既有隐私红线——**仅用灰度拓扑文本，不传输截图 base64**
          （原始帧不出本机；事件携带的 image_b64 只供本地通道消费）。

        :raises CloudUnavailableError: 本地未就绪且未注入 CloudAdapter / 云端调用失败
        """
        if self._local_understanding is not None:
            try:
                summary = self._local_understanding(self._build_local_messages(item))
                if summary is not None and str(summary).strip():
                    return str(summary).strip()
                LOGGER.warning(
                    "[VisionPipeline][WARN] 本地视觉理解未就绪（返回空），回落云端理解"
                )
            except Exception as exc:  # noqa: BLE001 —— 本地失败不阻断，回落云端
                LOGGER.warning("[VisionPipeline][WARN] 本地视觉理解失败，回落云端：%s", exc)
        # 云端路径：灰度拓扑（隐私红线——原始帧不出本机，截图 base64 不上云）
        if self._cloud is None:
            raise CloudUnavailableError("未注入 CloudAdapter，无法执行云端视觉理解")
        topology = _render_brightness_map(item.get("frame") or [])
        content = [
            {"type": "text", "text": VISION_PROMPT},
            {
                "type": "text",
                "text": "画面亮度拓扑（64x36 灰度降采样，字符越靠后越亮）：\n" + topology,
            },
        ]
        messages = [{"role": "user", "content": content}]
        chunks = [chunk for chunk in self._cloud.chat(messages)]
        return "".join(chunks).strip()

    def _build_local_messages(self, item: dict) -> list:
        """组装本地多模态理解消息（截图帧 image_url 优先，无截图回落灰度拓扑）。

        :param item: 视觉事件 dict（可含 ``image_b64`` PNG base64 截图）。
        :return: OpenAI 兼容消息列表（content 为数组，llama-server 多模态直收）。
        """
        content = [{"type": "text", "text": VISION_PROMPT}]
        image_b64 = item.get("image_b64")
        if image_b64:
            # 本地多模态路径：截图帧以 image_url 数据 URL 直投本地推理服务
            data_url = "data:image/png;base64," + str(image_b64)
            content.append({"type": "image_url", "image_url": {"url": data_url}})
        else:
            topology = _render_brightness_map(item.get("frame") or [])
            content.append({
                "type": "text",
                "text": "画面亮度拓扑（64x36 灰度降采样，字符越靠后越亮）：\n" + topology,
            })
        return [{"role": "user", "content": content}]


#: 导出提示词常量供下游复用（提示词属于理解契约的一部分）
__all__ = ["VisionPipeline", "VISION_PROMPT", "_render_brightness_map"]
