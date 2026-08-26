# -*- coding: utf-8 -*-
"""语音对话全链路编排（Task B2）：VAD → ASR → 判定 → 回复 → TTS。

对齐工程文档 §7.2 简化版打断逻辑：

::

    用户说话
      → 原始 VAD 判定"说完了"（结束信号）
      → 本地小 LLM / 判定器判"是否回复"（是不是在跟我说话）
      → 是 → 云端主 LLM（在线）或本地兜底（离线）生成回复 → MeloTTS 播放
      → 否（自言自语）→ 不回复

``LiteVoicePipeline`` 负责把语音三件套（LiteVAD / LiteASR / LiteTTS）、云端
（CloudAdapter）与判定器（ShouldReplyJudge）编排为一次性喂一段音频的对话循环：
- ``start_session()`` 开启新一轮会话并复位 VAD；
- ``feed_audio(chunk)`` 逐块喂音频，VAD 判结束时自动完成 转写 → 判定 → 回复 → 合成；
- 对话历史 ``messages`` 在上限（20 条）内滚动维护。

离线兜底：在线走 ``cloud.chat`` 拼接流式文本；离线且注入了 ``offline_local`` 则
走本地兜底；两者皆不可用时返回提示文案（「当前离线，开启本地模式可继续对话」）
并照常 TTS 播报，保证不哑巴。
"""

from typing import Dict, Optional

from lite.audio.judge import judge_should_reply_text

__all__ = ["LiteVoicePipeline"]

#: 对话历史滚动上限（条；超出裁掉最旧）。
MAX_HISTORY = 20

#: 离线且无本地兜底时的提示文案。
OFFLINE_HINT = "当前离线，开启本地模式可继续对话"


class LiteVoicePipeline:
    """语音对话全链路编排器。

    :param vad: 原始 VAD 实例，须暴露 ``detect_speech_end(chunk) -> bool``
    :param asr: 语音识别实例，须暴露 ``transcribe(audio) -> {text, emotion, event}``
    :param tts: 语音合成实例，须暴露 ``synthesize(text, voice) -> bytes``
    :param cloud: 云端主 LLM 实例，须暴露 ``chat(messages) -> Iterator[str]`` 与
        ``is_online(timeout) -> bool``；可为 None（完全离线模式）
    :param judge: 是否回复判定器（ShouldReplyJudge），可为 None（未注入→默认回复）
    :param offline_local: 本地兜底生成函数，签名为 ``callable(messages) -> 回复文本``
        或返回文本块迭代器；可为 None（无本地兜底）
    """

    def __init__(self, vad, asr, tts, cloud, judge=None, offline_local=None):
        """初始化全链路编排器。"""
        self.vad = vad
        self.asr = asr
        self.tts = tts
        self.cloud = cloud
        self.judge = judge
        self.offline_local = offline_local
        #: 对话历史（OpenAI 兼容 messages，role/content dict 列表，上限 20 滚动）
        self.messages: list = []

    # ------------------------------------------------------------------ #
    # 会话生命周期                                                     #
    # ------------------------------------------------------------------ #

    def start_session(self):
        """开启新一轮会话：清空对话历史并复位 VAD 内部状态。

        :return: self，便于链式调用
        """
        self.messages = []
        if self.vad is not None and hasattr(self.vad, "reset"):
            self.vad.reset()
        return self

    # ------------------------------------------------------------------ #
    # 主入口                                                           #
    # ------------------------------------------------------------------ #

    def feed_audio(self, chunk) -> Optional[Dict]:
        """逐块喂入 PCM 音频，VAD 判"说完了"时走一轮完整对话。

        VAD 未触发结束信号时返回 ``None``（仍在听）。
        VAD 判结束时自动完成：ASR 转写 → 判定是否回复 → 生成回复 → TTS 合成。

        :param chunk: PCM 音频字节块
        :return: VAD 未判结束时为 None；判结束时为结果 dict：
            - ``{"should_reply": False}``：自言自语，未生成回复（不写历史）
            - ``{"should_reply": True, "text": str, "audio": bytes}``：已回复
        """
        # 惰性开会话：首次喂音频时自动 start_session
        if self.vad is None or not self.vad.detect_speech_end(chunk):
            return None

        result = self.asr.transcribe(chunk) or {}
        text = str(result.get("text", "") or "").strip()

        if not text:
            # 无有效转写文本：视为无内容，不回复
            return {"should_reply": False}

        should_reply = judge_should_reply_text(text, self.judge)
        if not should_reply:
            # 自言自语：不回复，不计入对话历史
            return {"should_reply": False}

        # 判定应回复：记录用户消息并生成回复
        self.messages.append({"role": "user", "content": text})
        reply, logged = self._produce_reply()
        if logged:
            # 仅把真实回复（云端 / 本地兜底）记入历史；离线提示文案不入历史
            self.messages.append({"role": "assistant", "content": reply})
        self._clip_history()

        audio = self.tts.synthesize(reply)
        return {"should_reply": True, "text": reply, "audio": audio}

    # ------------------------------------------------------------------ #
    # 回复生成                                                         #
    # ------------------------------------------------------------------ #

    def _produce_reply(self):
        """按在线状态生成回复文本，返回 ``(reply, logged)``。

        优先级：在线走云端 → 离线且有本地兜底走本地 → 否则返回提示文案。
        ``logged`` 标记该回复是否计入对话历史（提示文案不计入）。
        """
        if self.cloud is not None and self.cloud.is_online():
            reply = self._run_stream(self.cloud.chat, self.messages)
            return reply, True

        if callable(self.offline_local):
            reply = self._run_stream(self.offline_local, self.messages)
            return reply, True

        return OFFLINE_HINT, False

    @staticmethod
    def _run_stream(fn, messages) -> str:
        """把生成函数返回值归一为完整文本。

        兼容多种返回：纯 ``str`` 直接使用；``bytes`` 解码为 utf-8 文本；其余
        按可迭代协议逐块拼接（适配 ``cloud.chat`` 流式生成器 return str 块）。
        """
        out = fn(messages)
        if out is None:
            return ""
        if isinstance(out, str):
            return out
        if isinstance(out, (bytes, bytearray)):
            return out.decode("utf-8", errors="replace")
        return "".join(str(c) for c in out)

    # ------------------------------------------------------------------ #
    # 历史维护                                                         #
    # ------------------------------------------------------------------ #

    def _clip_history(self):
        """对话历史滚动裁剪：超过上限时移除最旧的条目，保留最近 MAX_HISTORY 条。"""
        if len(self.messages) > MAX_HISTORY:
            self.messages = self.messages[-MAX_HISTORY:]