# -*- coding: utf-8 -*-
"""是否回复判定器（Task B2）：VAD 判"说完了"后，判定用户是否在跟我说话、要不要回。

对齐工程文档 §7.2 简化版链路：``用户说话 → VAD 判结束 → 本地小 LLM 判是否回复``。
本模块把"是否回复"判定抽象为 ``ShouldReplyJudge`` 接口，并提供两个实现：

- ``HeuristicJudge``：C1 前的启发式桩（规则匹配），不依赖任何模型；模型未就绪时
  ``model_ready=False``，此时不加载任何后端，判定走默认态。
- ``LlmJudge``：站位实现，docstring 说明 C1 落地后接入 ``llama_runtime.judge_should_reply``
  （此处**不 import** 尚未实现的模块，避免在 C1 落地前产生硬依赖）。

统一出口 :func:`judge_should_reply_text`：无论判定器是 None（未注入）、未就绪
（``model_ready=False``）还是判定抛出异常，一律返回 True（**不哑巴**，规范场景
"判定模型未就绪（默认态）"），避免因判定缺失而导致用户说话却无任何回应。
"""

__all__ = [
    "ShouldReplyJudge",
    "HeuristicJudge",
    "LlmJudge",
    "judge_should_reply_text",
]

#: 启发式判定命中的关键词（任一命中 → 视为在跟我说话，应回复）
_HEURISTIC_KEYWORDS = ("你", "您", "在吗", "帮我", "请问", "能不能")

#: 疑问句尾（任一结尾 → 视为疑问，应回复）
_QUESTION_TAILS = ("吗", "呢", "？", "?")


class ShouldReplyJudge:
    """"是否回复"判定器抽象基类。

    契约：
    - :attr:`model_ready`：判定模型是否就绪。为 ``False`` 时上层应走默认态
      （默认回复 True，"不哑巴"）。
    - :meth:`judge`：对一段用户文本返回是否回复（True 回复 / False 不回）。
    """

    @property
    def model_ready(self) -> bool:
        """判定模型是否就绪（None / False 视为不可用，走默认态）。"""
        return False

    def judge(self, user_text: str) -> bool:
        """判定用户文本是否在跟我说话、要不要回复。

        :param user_text: 用户语音转写文本（可能为空串）
        :return: True 应回复；False 不回（自言自语）
        """
        raise NotImplementedError


class HeuristicJudge(ShouldReplyJudge):
    """C1 前的启发式桩实现：纯规则判定，不加载任何模型后端。

    规则：文本包含「你 / 您 / 在吗 / 帮我 / 请问 / 能不能」任一关键词，
    或以疑问句尾（吗 / 呢 / ？ / ?）结尾 → 视为在跟我说话，判 True；否则 False。

    ``judge_model_ready`` 控制模型就绪位：为 ``False``（默认）时本桩不加载任何
    ``_backend``（判定对象加载保持为空），上层据此走默认态（不哑巴）。
    """

    def __init__(self, judge_model_ready: bool = False):
        """初始化启发式桩。

        :param judge_model_ready: 判定模型是否就绪，默认 False（未就绪→不加载后端）
        """
        self._ready = bool(judge_model_ready)
        #: 判定后端。就绪位为 False 时保持 None（不加载）；为 True 时也仅占位
        #: （C1 前无真实判定模型，留扩展点，不初始化任何第三方依赖）。
        self._backend = None if not self._ready else self._load_backend()

    def _load_backend(self):
        """按需加载判定后端（桩：当前无真实后端，返回 None 占位）。

        C1 前仅作扩展点保留；C1 落地后由 ``LlmJudge`` 承接真实模型。
        """
        return None

    @property
    def model_ready(self) -> bool:
        """判定模型是否就绪（即构造时传入的 judge_model_ready 位）。"""
        return self._ready

    def judge(self, user_text: str) -> bool:
        """按启发式规则判定是否回复（不依赖模型，始终可运行）。"""
        if not self._ready:
            # 模型未就绪：默认回复（不哑巴）
            return True
        return self._heuristic(user_text)

    @staticmethod
    def _heuristic(user_text: str) -> bool:
        """纯规则命中：含关键词或疑问句尾 → True。"""
        text = (user_text or "").strip()
        if not text:
            return False
        if any(key in text for key in _HEURISTIC_KEYWORDS):
            return True
        return text[-1] in _QUESTION_TAILS


class LlmJudge(ShouldReplyJudge):
    """"是否回复"判定器站位实现。

    说明：本类为 C1 的接口骨架。C1 落地后在本地启用小 LLM 判定时，应于此接入
    ``llama_runtime.judge_should_reply(user_text)``（C1 新增的运行时时序）。
    **出于避免对未实现模块的硬依赖，本文件不做任何 ``llama_runtime`` 导入**；
    在真实模型时序接通前，仅暴露 ``model_ready`` 就绪位供上层判定默认态。

    行为：
    - ``model_ready=False``（默认）：判定走默认态，直接返回 True（不哑巴）。
    - ``model_ready=True`` 且未接通真实时序时：抛 ``NotImplementedError`` 提示待 C1 接通。
    """

    def __init__(self, model_ready: bool = False, backend=None):
        """初始化站位判定器。

        :param model_ready: 判定模型是否就绪，默认 False
        :param backend: 预留的判定后端对象（C1 后由 llama 时序注入），默认 None
        """
        self._ready = bool(model_ready)
        self._backend = backend

    @property
    def model_ready(self) -> bool:
        """判定模型是否就绪。"""
        return self._ready

    def judge(self, user_text: str) -> bool:
        """判定是否回复。

        模型未就绪时返回 True（默认态，不哑巴）；已就绪但未接通时序时抛
        ``NotImplementedError`` 提示待 C1 落地接入 ``llama_runtime.judge_should_reply``。
        """
        if not self._ready:
            return True
        if self._backend is None:
            raise NotImplementedError(
                "LlmJudge 尚未接通真实判定时序：请在 C1 落地后注入 "
                "llama_runtime.judge_should_reply 的判定后端。"
            )
        # C1 落地后：委托真实后端判定
        return bool(self._backend.judge(user_text))


def judge_should_reply_text(user_text: str, judge) -> bool:
    """统一"是否回复"判定出口（含默认态兜底）。

    判定器不可用（未注入为 None）、未就绪（``model_ready`` 为假）或判定抛出异常时，
    一律返回 True——即**默认回复**（不哑巴），避免判定缺失导致用户说话却无回应。

    :param user_text: 用户语音转写文本
    :param judge: ShouldReplyJudge 实例，可为 None
    :return: True 应回复；False 不回（自言自语）
    """
    if judge is None:
        # 未注入判定器 → 规范场景"判定模型未就绪（默认态）"→ 默认回复
        return True
    if not getattr(judge, "model_ready", False):
        # 判定器已注入但模型未就绪 → 默认回复
        return True
    try:
        return bool(judge.judge(user_text))
    except Exception:  # noqa: BLE001 - 判定异常视为不可用，不哑巴
        return True