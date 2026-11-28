"""DreamTalk LLM 适配器：确定性离线 Mock 与 CX-A LLM 通道兼容层。

本模块只做端口适配，不导入 CX-A 的聊天管线（``_build_chat_messages``）——梦境
messages 完全由 ``core.dialogue`` 构造。回合失败、返回空内容或命中 CX-A 的离线引导
文案时降级为确定性离线文本，绝不向梦境编排层抛出异常。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)

_DIRECTOR_LINES = (
    "潮水从天花板上退下去，露出一排倒挂的窗。窗里没有房间，只有缓慢移动的森林。"
    "远处有人叫你的名字，声音却像从水底传来。",
    "走廊尽头的灯忽然全部转向你。墙纸下面有细小的翅膀在扑动，"
    "一张没有地址的明信片从门缝滑进来，落在地板上，慢慢洇出蓝墨水。",
    "站台开始向上升，像被谁提起来的一块布。铁轨之间长出一片麦田，麦穗上挂着钥匙。"
    "风从相反的方向吹，把所有的钟表拨慢了一分钟。",
    "雨从地面往天上落，街角的邮筒慢慢长高。你听见有人在你背后翻动一本很厚的书，"
    "纸页间夹着细小的雷声。",
    "所有的门同时打开，门后是同一个月台。月台上站着一排撑伞的人，伞面上印着不同的天气。",
    "房间开始像抽屉一样被拉开，每一层都装着不同年份的下午。"
    "灰尘在光里悬停，像一群不肯落地的鸟。",
)

_DREAMER_LINES = (
    "我把那张明信片捡起来，纸面像皮肤一样温热。字迹在指尖下重新排列，"
    "变成一条我从未走过的街。我沿着街走下去，路灯一盏盏低头看我。",
    "我伸手碰了碰麦穗上的钥匙，整片田野忽然安静下来。"
    "远处有一扇门在麦浪里开合，我朝它跑去，脚下的土却像水一样漫过脚踝。",
    "我推开那扇没有把手的门，里面是白天的厨房，只是所有东西都浮在半空。"
    "我抓住一只杯子，杯底映出另一片天空。",
    "我走进那排撑伞的人中间，伞面上的天气开始下雨。"
    "我伸手接住一滴，它在我掌心里变成一枚小小的钥匙。",
    "我拉开最底层的抽屉，里面躺着一条没有尽头的河。"
    "我俯身去看，河面映出一个正在醒来的我。",
    "我把雷声揣进口袋，继续沿着月台走。"
    "身后的门一扇接一扇合上，脚步声却始终比我快半步。",
)

_MOCK_SUMMARIES = (
    {
        "title": "倒挂的窗",
        "summary": "潮水退去后，窗里长出一片森林。我一直走到麦田尽头，钥匙在口袋里发烫，像一句还没说出口的话。",
        "emotion_residue": {"valence": 0.2, "arousal": 0.4},
    },
    {
        "title": "月台上的雨",
        "summary": "所有的门同时打开，门后站着撑伞的人。我接住一滴雨，它变成钥匙；再抬头时，月台已经开往另一个年份。",
        "emotion_residue": {"valence": 0.1, "arousal": 0.5},
    },
    {
        "title": "抽屉里的河",
        "summary": "房间像抽屉一样被一层层拉开，最底层躺着一条河。我俯身去看，河面映出一个正在醒来的人。",
        "emotion_residue": {"valence": -0.1, "arousal": 0.3},
    },
)

_SUMMARY_MARKER = "梦醒后的记录者"
_DIRECTOR_MARKER = "【你的身份：潜意识导演】"


class MockLLMClient:
    """确定性离线 LLM：无网络也能完整体验双 LLM 梦境。

    Args:
        seed: 文本选择偏移量；相同 seed 与相同调用序列必然产生相同输出。
    """

    def __init__(self, seed: int = 0):
        self.calls = 0
        self._seed = int(seed)

    async def chat(self, messages: Sequence[Any], *, temperature: float = 0.0) -> str:
        """按 system 提示判定角色，返回确定性的中文梦境文本。"""
        self.calls += 1
        system = _system_content(messages)
        if _SUMMARY_MARKER in system:
            summary = _MOCK_SUMMARIES[(self.calls + self._seed) % len(_MOCK_SUMMARIES)]
            return json.dumps(summary, ensure_ascii=False)
        index = (self.calls + self._seed) % len(_DIRECTOR_LINES)
        if _DIRECTOR_MARKER in system:
            return _DIRECTOR_LINES[index]
        return _DREAMER_LINES[index]


class CXAChatLLMClient:
    """把 CX-A ``OfflineFallbackManager.chat(messages)`` 包装成 DreamTalk LLM 端口。

    CX-A 的兜底管理器是**同步生成器**（云端流式 / 本地小 LLM 单块），且在没有
    API Key 或本地未就绪时会产出引导文案而不是抛异常。这些文案绝不能写入梦境
    回合——命中即视为通道不可用，改用确定性 Mock 文本。

    Args:
        fallback: ``OfflineFallbackManager`` 实例（None 时直接走 Mock）
        mock: 降级客户端，缺省为 ``MockLLMClient()``
    """

    def __init__(self, fallback: Any = None, mock: Optional[MockLLMClient] = None):
        self._fallback = fallback
        self._mock = mock if mock is not None else MockLLMClient()

    async def chat(self, messages: Sequence[Any], *, temperature: float = 0.0) -> str:
        """调用 CX-A 通道；失败 / 空文本 / 离线提示一律降级为 Mock 文本。"""
        if self._fallback is None:
            return await self._mock.chat(messages, temperature=temperature)
        try:
            text = await asyncio.to_thread(self._collect, messages)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单回合失败降级，不阻断梦境
            logger.warning("CX-A 梦境 LLM 调用失败，回退离线文本: %s", exc)
            return await self._mock.chat(messages, temperature=temperature)
        content = str(text or "").strip()
        if not content or content in _offline_prompts():
            if content:
                logger.warning("CX-A 通道返回离线引导文案，梦境回合改用离线文本")
            return await self._mock.chat(messages, temperature=temperature)
        return content

    def _collect(self, messages: Sequence[Any]) -> str:
        """把同步流式生成器拼接为整段文本（在线程池中执行，避免阻塞事件循环）。"""
        payload = [
            {
                "role": str(_message_field(message, "role", "user")),
                "content": str(_message_field(message, "content", "")),
            }
            for message in messages
        ]
        return "".join(self._fallback.chat(payload))


# ------------------------------------------------------------------ 内部工具
def _offline_prompts() -> frozenset:
    """CX-A 三条离线引导文案集合（延迟导入防导入环，失败退回字面量）。"""
    try:
        from lite.cloud.fallback import (  # noqa: PLC0415
            CONFIG_ERROR_PROMPT,
            LOCAL_NOT_READY_PROMPT,
            OFFLINE_PROMPT,
        )

        return frozenset({OFFLINE_PROMPT, LOCAL_NOT_READY_PROMPT, CONFIG_ERROR_PROMPT})
    except Exception:  # noqa: BLE001 - 导入失败按字面量兜底
        return frozenset(
            {
                "当前离线，开启本地模式可继续对话",
                "云端配置未完成，请在设置中填写服务商与 API Key",
                "本地大脑还没准备好，先去下载好它（设置页或新手引导里可以下）",
            }
        )


def _system_content(messages: Sequence[Any]) -> str:
    """取第一条 system 消息内容；缺失时返回空串。"""
    for message in messages:
        if str(_message_field(message, "role", "")) == "system":
            return str(_message_field(message, "content", "") or "")
    return ""


def _message_field(message: Any, name: str, default: Any) -> Any:
    """兼容对象属性与字典两种消息形态。"""
    if isinstance(message, dict):
        return message.get(name, default)
    return getattr(message, name, default)
