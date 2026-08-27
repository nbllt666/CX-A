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
- ``start_session()`` 开启新一轮会话，清空对话历史、话语缓冲并复位 VAD；
- ``feed_audio(chunk)`` 逐块喂音频：非 end 帧进入话语缓冲累积（H3 修复），VAD
  判结束时把整段话语缓冲一并交给 ASR 完成转写 → 判定 → 回复 → 合成；
- 对话历史 ``messages`` 在上限（20 条）内滚动维护。

离线兜底（M10 修复）：在线走 ``cloud.chat`` 拼接流式文本，任一层抛出异常自动
降级本地兜底再降级提示文案；离线且注入了 ``offline_local`` 则走本地兜底；
最终返回提示文案（「当前离线，开启本地模式可继续对话」）并照常 TTS 播报，
保证全链路任何一层故障都不中断 ``feed_audio`` 循环、始终有文本产出（不哑巴）。
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
        #: 话语缓冲（H3 修复）：自上一轮结束信号以来累积的 PCM 字节块列表，
        #: end 帧触发转写或 start_session 复位时清空
        self._utterance_buffer: list = []

    # ------------------------------------------------------------------ #
    # 会话生命周期                                                     #
    # ------------------------------------------------------------------ #

    def start_session(self):
        """开启新一轮会话：清空对话历史、话语缓冲并复位 VAD 内部状态。

        :return: self，便于链式调用
        """
        self.messages = []
        # H3：会话复位必须同时清空话语缓冲，防止上一轮残留语音跨轮污染
        self._utterance_buffer = []
        if self.vad is not None and hasattr(self.vad, "reset"):
            self.vad.reset()
        return self

    # ------------------------------------------------------------------ #
    # 主入口                                                           #
    # ------------------------------------------------------------------ #

    def feed_audio(self, chunk) -> Optional[Dict]:
        """逐块喂入 PCM 音频，VAD 判"说完了"时走一轮完整对话。

        话语缓冲（H3 修复）：VAD 未判结束时，chunk 先进入 ``_utterance_buffer``
        累积并返回 None（仍在听）；判结束时把「缓冲累积的全部音频 + 当前结束帧」
        拼接后整体交给 ASR 转写。语义选择说明：结束帧拼入转写载荷——LiteVAD 的
        触发帧为纯静音（拼入对识别无害），且若静音阈值误判提前触发可避免丢尾字。
        首帧即 end（无前文语音）保持旧行为：仅转写当前触发帧或走空文本分支。

        VAD 未触发结束信号时返回 ``None``（仍在听）。
        VAD 判结束时自动完成：ASR 转写 → 判定是否回复 → 生成回复 → TTS 合成。

        :param chunk: PCM 音频字节块
        :return: VAD 未判结束时为 None；判结束时为结果 dict：
            - ``{"should_reply": False}``：自言自语 / 转写为空 / ASR 异常兜底
            - ``{"should_reply": True, "text": str, "audio": bytes}``：已回复
              （TTS 失败时 audio 为空 bytes，返回结构保持稳定）
        """
        # VAD 异常按"未结束"处理并照常入缓冲，保证喂入循环不中断（M10 兜底）
        is_end = False
        if self.vad is not None:
            try:
                is_end = bool(self.vad.detect_speech_end(chunk))
            except Exception as exc:  # noqa: BLE001 - VAD 故障不影响循环存活
                print(f"[LiteAudio][WARN] VAD detect_speech_end 异常，按未结束处理：{exc}")
        if not is_end:
            # 非 end 帧：chunk 加入话语缓冲，返回 None（仍在听，原有逻辑不变）
            self._utterance_buffer.append(chunk)
            return None

        # end 帧：取「缓冲累积的全部音频 + 当前结束帧」整体交给 ASR（语义见 docstring）
        payload = chunk
        if self._utterance_buffer:
            parts = list(self._utterance_buffer)
            parts.append(chunk)
            try:
                payload = b"".join(parts)
            except TypeError:
                # 元素非字节类型的异常输入：退化为仅传当前帧，交由下方兜底路径处理
                print("[LiteAudio][WARN] 话语缓冲含非字节元素，退化为仅转写当前帧")
        self._utterance_buffer = []  # 无论本轮是否产生有效文本，缓冲一次性清空

        try:
            result = self.asr.transcribe(payload) or {}
        except Exception as exc:  # noqa: BLE001 - M10 兜底：ASR 失败不中断循环
            print(f"[LiteAudio][WARN] ASR 转写异常，本轮忽略该段音频：{exc}")
            return {"should_reply": False}

        text = str(result.get("text", "") or "").strip()

        if not text:
            # 无有效转写文本：视为无内容，不回复
            return {"should_reply": False}

        try:
            should_reply = judge_should_reply_text(text, self.judge)
        except Exception as exc:  # noqa: BLE001 - M10 双保险：自定义判定器越界也默认回复
            print(f"[LiteAudio][WARN] 判定器调用异常，默认回复处理：{exc}")
            should_reply = True

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

        try:
            audio = self.tts.synthesize(reply)
        except Exception as exc:  # noqa: BLE001 - M10 兜底：TTS 失败不中断回复链
            print(f"[LiteAudio][WARN] TTS 合成失败，返回空音频：{exc}")
            audio = b""
        return {"should_reply": True, "text": reply, "audio": audio}

    # ------------------------------------------------------------------ #
    # 回复生成                                                         #
    # ------------------------------------------------------------------ #

    def _produce_reply(self):
        """按在线状态生成回复文本，返回 ``(reply, logged)``。

        三级降级（M10 兜底）：云端在线走 ``cloud.chat`` 流式 → 云端异常 / 离线且
        注入 ``offline_local`` 走本地兜底 → 再失败回落提示文案（OFFLINE_HINT）。
        任何一层抛出异常都不向上传播，``logged`` 标记该回复是否计入对话历史
        （提示文案不计入）。
        """
        # 一级：云端在线流式；is_online / chat 任一异常均视为云端不可用并降级
        if self.cloud is not None:
            try:
                if self.cloud.is_online():
                    reply = self._run_stream(self.cloud.chat, self.messages)
                    return reply, True
            except Exception as exc:  # noqa: BLE001 - M10 兜底：云端故障降级本地
                print(f"[LiteAudio][WARN] 云端流式回复失败，降级本地兜底：{exc}")

        # 二级：本地兜底生成器；同样不得向上抛出
        if callable(self.offline_local):
            try:
                reply = self._run_stream(self.offline_local, self.messages)
                return reply, True
            except Exception as exc:  # noqa: BLE001 - M10 兜底：本地兜底失败回落提示
                print(f"[LiteAudio][WARN] 本地兜底生成失败，返回离线提示：{exc}")

        # 三级：无可用回复来源 → 离线提示文案（不计入历史）
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