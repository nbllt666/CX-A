# -*- coding: utf-8 -*-
"""原始 VAD（语音活动检测）：只判"用户说完"（结束信号），不做打断判定。

对齐工程文档 §7.3 / §7.2：LiteVAD 仅判定语音结束（返回一次"结束信号"），
由上层在 VAD 结束后再接本地小 LLM 判定是否回复；本模块不关心被打断、
抢插话等全双工打断逻辑（该职责属于 CX-O 重度版，不在轻量版范围）。

实现方式：纯标准库能量检测（wave 格式 PCM 16bit little-endian 单声道）。
逻辑 = 曾检测到说话 → 静音持续时长超过 ``silence_ms``（默认 600ms）
即返回 True 表示检测到一次结束；逐帧把状态内部维护（call 逐帧喂入）。
"""

import math

__all__ = ["LiteVAD"]

#: int16 满量程幅值，作为相对满刻度（dBFS）的参考。
_FULL_SCALE = 32768.0


class LiteVAD:
    """原始语音活动检测器（能量法 + 静音计时）。

    以 ``detect_speech_end`` 逐帧喂入 PCM 字节：有声帧标记"曾说话"并清零
    静音计时；静音帧在"曾说话"前提下累计静音时长；累计超过 ``silence_ms``
    返回 True 触发一次结束信号，随后自动复位进入下一轮。
    """

    def __init__(self, sample_rate=16000, sample_width=2,
                 speech_threshold_dB=-35.0, silence_ms=600):
        """初始化 LiteVAD。

        :param sample_rate: 采样率，默认 16000
        :param sample_width: 单样本字节数（16bit 为 2），默认 2
        :param speech_threshold_dB: 说话/静音判决阈值（相对满刻度 dB，
            默认 -35，可调）
        :param silence_ms: 触发结束所需的静音持续毫秒数，默认 600
        """
        self.sample_rate = int(sample_rate)
        self.sample_width = int(sample_width)
        self.speech_threshold_dB = float(speech_threshold_dB)
        self.silence_ms = float(silence_ms)
        self.reset()

    def reset(self):
        """复位内部状态（说话标记与静音累计时长），开始新的一轮检测。"""
        self._has_speaking = False
        self._silence_elapsed_ms = 0.0

    # ------------------------------------------------------------------ #
    # 内部：能量 / 时长                                                  #
    # ------------------------------------------------------------------ #

    def _frame_duration_ms(self, n_bytes):
        """由该帧字节数推算其时长（毫秒）。

        :param n_bytes: PCM 帧字节数
        :return: 帧时长（毫秒），参数非法时返回 0
        """
        if self.sample_rate <= 0 or self.sample_width <= 0:
            return 0.0
        return n_bytes * 1000.0 / (self.sample_rate * self.sample_width)

    def _db_of(self, audio_chunk):
        """计算 PCM 16bit 音频的 RMS 幅值（相对满刻度，单位 dB）。

        空片段 / 不足一个样本时返回 ``-inf``（等价于纯静音）。
        """
        if not audio_chunk or self.sample_width <= 0 \
                or len(audio_chunk) < self.sample_width:
            return float("-inf")
        n = len(audio_chunk) // self.sample_width
        rms_sq = 0.0
        for i in range(n):
            start = i * self.sample_width
            sample = int.from_bytes(
                audio_chunk[start:start + self.sample_width],
                "little", signed=True,
            )
            rms_sq += sample * sample
        rms = math.sqrt(rms_sq / n)
        if rms <= 0:
            return float("-inf")
        return 20.0 * math.log10(rms / _FULL_SCALE)

    # ------------------------------------------------------------------ #
    # 对外接口                                                           #
    # ------------------------------------------------------------------ #

    def detect_speech_end(self, audio_chunk):
        """逐帧喂入 PCM 音频，评测是否检测到"用户说完"。

        :param audio_chunk: wave 格式 PCM 16bit little-endian 单声道音频字节
        :return: bool——True 表示检测到一次语音结束（曾说话→静音超过
            silence_ms），随后内部复位进入下一轮；False 表示仍在说话或
            静音未达标。
        """
        if not audio_chunk:
            return False

        frame_ms = self._frame_duration_ms(len(audio_chunk))
        if self._db_of(audio_chunk) >= self.speech_threshold_dB:
            # 有声帧：标记曾说话，静音计时清零
            self._has_speaking = True
            self._silence_elapsed_ms = 0.0
            return False

        # 静音帧：仅在"曾说话"前提下累计静音时长
        if not self._has_speaking:
            return False
        self._silence_elapsed_ms += frame_ms
        if self._silence_elapsed_ms >= self.silence_ms:
            self.reset()  # 结束信号，自动复位
            return True
        return False