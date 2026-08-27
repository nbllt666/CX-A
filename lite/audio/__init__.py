# -*- coding: utf-8 -*-
"""语音对话子包（Task B）：LiteVAD / LiteASR / LiteTTS / 判定 / 全链路编排。

- ``LiteVAD``：原始 VAD，能量法 + 静音计时，只判"用户说完"（结束信号）。
- ``LiteASR``：本地语音识别，返回 ``{text, emotion, event}``。
- ``LiteTTS``：本地语音合成，默认 MeloTTS + cx-open 音色。
- ``ShouldReplyJudge`` / ``HeuristicJudge`` / ``LlmJudge``：是否回复判定器（Task B2）。
- ``judge_should_reply_text``：判定统一出口（默认态兜底，不哑巴）。
- ``LiteVoicePipeline``：语音对话全链路编排（VAD→ASR→判定→回复→TTS，Task B2）。

``build_default_pipeline(config)`` 便捷工厂：按 config 的 asr / tts / vad 段
装配默认三件套；缺少三方库（funasr / melotts）时自动回退 Mock 并打印 warning。
判定器（H4 接线）：config 的 local_llm 段 enabled=True 且配置了 model_path 时，
延迟导入 llama_runtime 构造 LlamaRuntime 并包装为 ShouldReplyJudge 适配器；
任何异常（依赖缺失 / 模型未就绪）打印 warning 回退 HeuristicJudge()；否则
返回 dict 中 judge 保持既有默认态 None。
"""

from lite.audio.vad import LiteVAD
from lite.audio.asr import (
    ASRBackend,
    SenseVoiceBackend,
    MockASRBackend,
    LiteASR,
)
from lite.audio.tts import (
    TTSBackend,
    MeloTTSBackend,
    MockTTSBackend,
    LiteTTS,
)
from lite.audio.voice_manager import VoiceManager
from lite.audio.judge import (
    ShouldReplyJudge,
    HeuristicJudge,
    LlmJudge,
    judge_should_reply_text,
)
from lite.audio.pipeline import LiteVoicePipeline

__all__ = [
    "LiteVAD",
    "ASRBackend",
    "SenseVoiceBackend",
    "MockASRBackend",
    "LiteASR",
    "TTSBackend",
    "MeloTTSBackend",
    "MockTTSBackend",
    "LiteTTS",
    "VoiceManager",
    "ShouldReplyJudge",
    "HeuristicJudge",
    "LlmJudge",
    "judge_should_reply_text",
    "LiteVoicePipeline",
    "build_default_pipeline",
]


def _resolve_section(cfg, name):
    """从 config 中取某段的 dict；兼容裸 dict 与 ConfigManager 实例。"""
    if cfg is None:
        return {}
    if isinstance(cfg, dict):
        sec = cfg.get(name, {})
        return sec if isinstance(sec, dict) else {}
    inner = getattr(cfg, "config", None)
    if isinstance(inner, dict):
        sec = inner.get(name, {})
        return sec if isinstance(sec, dict) else {}
    return {}


def _try_sensevoice(asr_cfg):
    """探测 funasr 可用性：可用返回 SenseVoiceBackend，否则返回 None（回退 Mock）。"""
    try:
        import funasr  # noqa: F401
    except ImportError:
        return None
    return SenseVoiceBackend(
        model_path=asr_cfg.get("model_path", ""),
        device=asr_cfg.get("device", "cpu"),
    )


def _try_melotts(tts_cfg):
    """探测 MeloTTS 可用性：可用返回 MeloTTSBackend，否则返回 None（回退 Mock）。"""
    try:
        # 官方 PyPI 分发名 melotts，模块导入语 melo.api
        from melo.api import TTS  # noqa: F401
    except ImportError:
        return None
    return MeloTTSBackend(
        default_voice=tts_cfg.get("voice", "cx-open"),
        device=tts_cfg.get("device", "cpu"),
    )


class _LlamaRuntimeJudgeAdapter(ShouldReplyJudge):
    """LlamaRuntime 判定时序的轻量适配器（H4 接线）。

    实现 ``ShouldReplyJudge`` 契约：``judge(text) -> bool`` 委托给
    ``LlamaRuntime.judge_should_reply(text)``。``model_ready`` 恒为 True——工厂
    仅在本地小 LLM 成功加载后才构造本适配器。运行中判定抛出的异常（如模型失效）
    由统一出口 ``judge_should_reply_text`` 兜底为默认回复（不哑巴）。
    """

    def __init__(self, runtime):
        """初始化适配器。

        :param runtime: 已完成 load_local_llm 的 LlamaRuntime 实例
        """
        self._runtime = runtime

    @property
    def model_ready(self) -> bool:
        """判定模型已就绪（工厂构造本适配器即代表加载成功）。"""
        return True

    def judge(self, user_text: str) -> bool:
        """委托 LlamaRuntime.judge_should_reply 判定是否回复。"""
        return bool(self._runtime.judge_should_reply(user_text))


def _try_build_llm_judge(local_llm_cfg):
    """按 local_llm 配置段组装真实判定器（H4 接线）。

    策略：
    - ``enabled`` 为 False，或启用但未配置 ``model_path``：返回 None（保持既有
      默认态——未注入判定器时上层 ``judge_should_reply_text`` 默认回复 True，
      不改变无配置时的行为）。
    - enabled 且配置了 model_path：**函数内延迟导入** llama_runtime（禁止模块
      顶层 import 造成硬依赖），构造 LlamaRuntime 并加载本地小 LLM；成功返回
      ``_LlamaRuntimeJudgeAdapter`` 适配器。
    - 模型文件缺失 / llama-cpp-python 未安装 / 加载异常：抛出异常，由调用方打
      warning 并回退 HeuristicJudge()。

    :param local_llm_cfg: local_llm 配置段 dict
    :return: 判定器实例或 None；None 表示走默认态
    :raises RuntimeError: 启用了真实判定但接线失败时抛出（交调用方降级处理）
    """
    if not bool(local_llm_cfg.get("enabled", False)):
        return None
    model_path = str(local_llm_cfg.get("model_path", "") or "").strip()
    if not model_path:
        # 启用位打开但未配置模型路径：无法接线，保持默认态
        return None
    # 函数内延迟导入：llama_runtime 依赖链不进入本模块顶层导入路径
    from lite.runtime.llama_runtime import LlamaRuntime

    runtime = LlamaRuntime(
        config={
            "local_llm": {
                "enabled": True,
                "model_path": model_path,
                # M11：可选 n_ctx 覆盖键透传；缺失/None 时由 LlamaRuntime 回退默认 2048
                "n_ctx": local_llm_cfg.get("n_ctx"),
                # GPU 开关透传：device（cpu/gpu）与可选 n_gpu_layers 高级覆盖，
                # 缺失/None 时由 LlamaRuntime._read_gpu_layers 按 device 推导
                "device": local_llm_cfg.get("device"),
                "n_gpu_layers": local_llm_cfg.get("n_gpu_layers"),
            }
        }
    )
    ok = runtime.load_local_llm(model_path)
    if not ok:
        raise RuntimeError("; ".join(runtime.warnings) or "本地小 LLM 加载失败")
    return _LlamaRuntimeJudgeAdapter(runtime)


def _resolve_judge(config):
    """解析 config 的 local_llm 段并组装判定器。

    真实接线失败（依赖缺失 / 模型文件缺失 / 加载异常）时打印 warning 并回退
    ``HeuristicJudge()``（默认就绪位 False → 默认回复 True，不哑巴）；无配置
    返回 None（既有默认态）。

    :param config: 完整配置来源（裸 dict 或 ConfigManager）
    :return: ShouldReplyJudge 实例或 None
    """
    try:
        judge = _try_build_llm_judge(_resolve_section(config, "local_llm"))
    except Exception as exc:  # noqa: BLE001 - 接线失败一律降级，不阻塞装配
        print(f"[LiteAudio][WARN] 本地判定 LLM 接线失败，回退启发式判定：{exc}")
        return HeuristicJudge()
    return judge


def build_default_pipeline(config=None):
    """便捷工厂：按 config 的 asr / tts / vad / local_llm 段装配语音全链路组件。

    - VAD：纯标准库实现，始终可用，参数取自 ``config["vad"]``（缺省用默认值）。
    - ASR：funasr 可用时用 SenseVoiceBackend；不可用时回退 MockASRBackend 并警告。
    - TTS：melotts 可用时用 MeloTTSBackend；不可用时回退 MockTTSBackend 并警告。
    - judge（H4 接线）：local_llm 段 enabled 且配置了 model_path 时尝试接通真实
      判定 LLM，失败打印 warning 回退 HeuristicJudge()；否则保持默认态 None
      （上层 ``judge_should_reply_text`` 默认回复，不哑巴）。

    :param config: 配置（含 asr / tts / vad / local_llm 段）；支持裸 dict 或
        ConfigManager。
    :return: dict，含 ``vad`` / ``asr`` / ``tts`` / ``judge`` 四个已装配实例。
    """
    vad_cfg = _resolve_section(config, "vad")
    asr_cfg = _resolve_section(config, "asr")
    tts_cfg = _resolve_section(config, "tts")

    vad = LiteVAD(
        sample_rate=vad_cfg.get("sample_rate", 16000),
        speech_threshold_dB=vad_cfg.get("speech_threshold_dB", -35.0),
        silence_ms=vad_cfg.get("silence_ms", 600),
    )

    asr_backend = _try_sensevoice(asr_cfg)
    if asr_backend is None:
        asr_backend = MockASRBackend()
        print("[LiteAudio][WARN] funasr 未安装，LiteASR 回退 MockASRBackend")

    tts_backend = _try_melotts(tts_cfg)
    if tts_backend is None:
        tts_backend = MockTTSBackend()
        print("[LiteAudio][WARN] melotts 未安装，LiteTTS 回退 MockTTSBackend")

    return {
        "vad": vad,
        "asr": LiteASR(backend=asr_backend, device=asr_cfg.get("device", "cpu")),
        "tts": LiteTTS(
            backend=tts_backend,
            default_voice=tts_cfg.get("voice", "cx-open"),
        ),
        # H4 接线：judge 键恒存在（增量兼容）——真实 LLM 判定接通或回退启发式，
        # 无配置时保持既有默认态 None
        "judge": _resolve_judge(config),
    }