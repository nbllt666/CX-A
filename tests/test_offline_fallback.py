# -*- coding: utf-8 -*-
"""Task C3 断网兜底机制单元测试（OfflineFallbackManager）。

覆盖（全 mock，无网络）：
- 本地优先路由：本地模式开启 → 完全不探测网络、不连云端（refresh_online /
  cloud.chat 零调用），就绪走本地、未就绪产 LOCAL_NOT_READY_PROMPT 引导提示；
- resolver 形态：local_llm 传零参可调用对象，运行时从 None 变可用动态生效；
- 在线 + 本地模式未开启 → cloud 流式透传，mode 保持 "cloud"（既有行为不变）；
- 离线 + 本地模式开（实例形态）→ 切 "local"、offline_chat 文本单块产出；
- 离线 + 未开本地 → 提示文案、mode 保持 "cloud"（不切换）、status=offline_hint；
- 云端调用抛 CloudUnavailableError → 自动降级（切本地 / 提示）；
- 恢复：本地模式关闭后网络恢复，下次 chat 自动回 cloud 并通知；
- 节流：多次 refresh_online 只在超间隔 / force 时真正调 cloud.is_online；
- 本地未就绪：offline_chat 抛 LlamaNotReady → 产引导提示文案不崩；
- 导出：OfflineFallbackManager / OFFLINE_PROMPT 可由 lite.cloud 导入；
- G-3：云端路径流中途 CloudUnavailableError 不再追加离线提示（本地模式未开启）；
- N5：CloudConfigError 置配置错误诊断态，提示"配置未完成"而非误诊断网。
"""

from unittest.mock import Mock

import pytest

from lite.cloud.adapter import CloudConfigError, CloudUnavailableError
from lite.cloud.fallback import (
    CONFIG_ERROR_PROMPT,
    LOCAL_NOT_READY_PROMPT,
    OFFLINE_PROMPT,
    OfflineFallbackManager,
)
from lite.runtime import LlamaNotReady


# ------------------------------------------------------------------ #
# 构造辅助                                                           #
# ------------------------------------------------------------------ #

def _make_manager(
    *,
    online=True,
    local_enabled=True,
    local_llm=None,
    cloud_chunks=None,
    cloud_raises=None,
    interval=30.0,
):
    """构造一个全 mock 的 OfflineFallbackManager。

    - cloud：Mock，is_online 返回 MutableMock，chat 为返回可配置块的生成器。
    - config：dict 嵌套，控制 local_llm.enabled。
    """
    cloud = Mock()
    cloud.is_online.return_value = online
    if cloud_raises is not None:
        def _raising_chat(messages):
            raise cloud_raises
            yield  # pragma: no cover - 使函数成为生成器
        cloud.chat.side_effect = _raising_chat
    else:
        chunks = cloud_chunks if cloud_chunks is not None else ["你", "好", "世界"]
        def _streaming_chat(messages):
            yield from list(chunks)
        cloud.chat.side_effect = _streaming_chat

    cfg = {"local_llm": {"enabled": local_enabled}}
    mgr = OfflineFallbackManager(
        cloud=cloud,
        local_llm=local_llm,
        config=cfg,
        online_probe_interval=interval,
    )
    return mgr, cloud


class StubLocalLLM:
    """极简本地小 LLM 桩：可配置返回值或抛 LlamaNotReady。"""

    def __init__(self, text="我在呢", raise_not_ready=False):
        self.text = text
        self.raise_not_ready = raise_not_ready

    def offline_chat(self, messages):
        if self.raise_not_ready:
            raise LlamaNotReady("本地小 LLM 未就绪")
        return self.text


# ------------------------------------------------------------------ #
# 1. 本地优先路由：本地模式开启 → 不探测、不连云                       #
# ------------------------------------------------------------------ #

def test_local_mode_ready_bypasses_probe_and_cloud():
    """本地模式开 + 就绪（resolver 形态）→ 走本地，refresh_online / cloud.chat 零调用。"""
    local = StubLocalLLM(text="本地优先回复")
    mgr, cloud = _make_manager(online=True, local_enabled=True, local_llm=lambda: local)
    events = []
    mgr.add_listener(lambda m: events.append(m))

    out = list(mgr.chat([{"role": "user", "content": "hi"}]))
    assert out == ["本地优先回复"]
    assert mgr.mode == "local"
    assert mgr.status == "local"
    assert events == ["local"]
    # 隐私红线：本地模式下既不探测网络也不连云端
    assert cloud.is_online.call_count == 0
    assert cloud.chat.call_count == 0


def test_local_mode_not_ready_yields_local_prompt_without_cloud():
    """本地模式开 + 未就绪（resolver 返回 None）→ 引导提示、mode 不切换、云端零调用。"""
    mgr, cloud = _make_manager(online=True, local_enabled=True, local_llm=lambda: None)
    events = []
    mgr.add_listener(lambda m: events.append(m))

    out = list(mgr.chat([{"role": "user", "content": "hi"}]))
    assert out == [LOCAL_NOT_READY_PROMPT]
    assert LOCAL_NOT_READY_PROMPT == "本地大脑还没准备好，先去下载好它（设置页或新手引导里可以下）"
    assert mgr.mode == "cloud"                   # 未真正承接，不切换状态机
    assert mgr.status == "offline_hint"
    assert events == []                          # 无模式变化
    # 隐私红线：未就绪也绝不静默回落云端
    assert cloud.is_online.call_count == 0
    assert cloud.chat.call_count == 0


def test_local_mode_ready_but_chat_fails_yields_local_prompt():
    """就绪判定通过但本地调用中途失败 → 产引导提示（不抛、不回落云端）。"""
    local = StubLocalLLM(raise_not_ready=True)
    mgr, cloud = _make_manager(online=True, local_enabled=True, local_llm=lambda: local)
    out = list(mgr.chat([{"role": "user", "content": "hi"}]))
    assert out == [LOCAL_NOT_READY_PROMPT]
    assert mgr.mode == "local"                   # 已切本地承接
    assert mgr.status == "offline_hint"
    assert cloud.is_online.call_count == 0
    assert cloud.chat.call_count == 0


def test_resolver_runtime_becomes_available_midway():
    """resolver 形态：运行时从 None 变可用 → 两次 chat 结果不同，动态生效。"""
    state = {"runtime": None}
    mgr, cloud = _make_manager(
        online=True, local_enabled=True, local_llm=lambda: state["runtime"]
    )
    # 第一次：运行时未加载 → 引导提示
    out1 = list(mgr.chat([{"role": "user", "content": "你好"}]))
    assert out1 == [LOCAL_NOT_READY_PROMPT]
    assert mgr.mode == "cloud"

    # 模型后台加载完成（模拟运行中变为可用）
    state["runtime"] = StubLocalLLM(text="本地大脑上线了")

    # 第二次：无需重建管理器即走本地
    out2 = list(mgr.chat([{"role": "user", "content": "你好"}]))
    assert out2 == ["本地大脑上线了"]
    assert mgr.mode == "local"
    assert mgr.status == "local"
    assert cloud.is_online.call_count == 0
    assert cloud.chat.call_count == 0


def test_local_llm_ready_flag_variants():
    """local_llm_ready：暴露 is_ready / model_ready 属性时须为真；resolver 异常视为未就绪。"""

    class _Flagged:
        def __init__(self, **flags):
            for k, v in flags.items():
                setattr(self, k, v)

        def offline_chat(self, messages):
            return "ok"

    # 无就绪属性：非 None 即就绪
    assert _make_manager(local_enabled=True, local_llm=lambda: _Flagged())[0].local_llm_ready() is True
    # is_ready=False / model_ready=False → 未就绪
    assert _make_manager(local_enabled=True, local_llm=lambda: _Flagged(is_ready=False))[0].local_llm_ready() is False
    assert _make_manager(local_enabled=True, local_llm=lambda: _Flagged(model_ready=False))[0].local_llm_ready() is False
    # is_ready=True → 就绪
    assert _make_manager(local_enabled=True, local_llm=lambda: _Flagged(is_ready=True))[0].local_llm_ready() is True

    def _boom():
        raise RuntimeError("resolver 炸了")

    assert _make_manager(local_enabled=True, local_llm=_boom)[0].local_llm_ready() is False


# ------------------------------------------------------------------ #
# 1b. 本地模式未开启 → 既有云端路径行为不变                            #
# ------------------------------------------------------------------ #

def test_online_streams_cloud_and_mode_cloud():
    """本地模式未开 + 在线 → 流式透传 cloud 各文本块，mode 保持 "cloud"。"""
    mgr, cloud = _make_manager(online=True, local_enabled=False,
                                cloud_chunks=["你", "好", "世界"])
    out = "".join(mgr.chat([{"role": "user", "content": "hi"}]))
    assert out == "你好世界"
    assert mgr.mode == "cloud"
    assert mgr.status == "cloud"
    assert cloud.chat.called


# ------------------------------------------------------------------ #
# 2. 离线 + 本地模式开：切 local、单块产出、listener 收到变化        #
# ------------------------------------------------------------------ #

def test_offline_with_local_enabled_switches_to_local():
    """离线 + 本地模式开 → 切 local、offline_chat 文本单块产出。"""
    local = StubLocalLLM(text="欢迎来到离线模式")
    mgr, _ = _make_manager(online=False, local_enabled=True, local_llm=local)
    events = []
    mgr.add_listener(lambda m: events.append(m))

    out = list(mgr.chat([{"role": "user", "content": "你好"}]))
    assert out == ["欢迎来到离线模式"]          # 单块产出
    assert mgr.mode == "local"
    assert mgr.status == "local"
    assert events == ["local"]                   # listener 收到模式变化
    assert mgr.last_mode_event == "local"
    assert mgr.mode_history[-1] == "local"


# ------------------------------------------------------------------ #
# 3. 离线 + 未开本地：提示文案、mode 保持 cloud                      #
# ------------------------------------------------------------------ #

def test_offline_without_local_mode_yields_hint():
    """离线 + 未开本地 → 提示文案、mode 保持 "cloud"、status=offline_hint。"""
    mgr, _ = _make_manager(online=False, local_enabled=False)
    events = []
    mgr.add_listener(lambda m: events.append(m))

    out = list(mgr.chat([{"role": "user", "content": "你好"}]))
    assert out == [OFFLINE_PROMPT]               # 提示文案
    assert OFFLINE_PROMPT == "当前离线，开启本地模式可继续对话"
    assert mgr.mode == "cloud"                   # 不切换状态机
    assert mgr.status == "offline_hint"
    assert events == []                          # 无模式变化
    assert mgr.last_mode_event is None


# ------------------------------------------------------------------ #
# 4. 在线但云端调用失败 → 自动降级                                    #
# ------------------------------------------------------------------ #

def test_cloud_unavailable_auto_degrades_to_local():
    """探测在线但 cloud.chat 抛 CloudUnavailableError → 自动切本地兜底。"""
    local = StubLocalLLM(text="云端暂不可用，先由本地接话")
    mgr, _ = _make_manager(online=True, local_enabled=True, local_llm=local,
                           cloud_raises=CloudUnavailableError("断连"))
    out = list(mgr.chat([{"role": "user", "content": "hi"}]))
    assert out == ["云端暂不可用，先由本地接话"]
    assert mgr.mode == "local"


def test_cloud_unavailable_no_local_yields_hint():
    """在线但 cloud 故障 + 未开本地 → 提示文案，模式保持 "cloud"。"""
    mgr, _ = _make_manager(online=True, local_enabled=False,
                           cloud_raises=CloudUnavailableError("断连"))
    out = list(mgr.chat([{"role": "user", "content": "hi"}]))
    assert out == [OFFLINE_PROMPT]
    assert mgr.mode == "cloud"


# ------------------------------------------------------------------ #
# 5. 恢复：本地模式关闭 + 网络恢复 → 下次 chat 自动回 cloud 并通知   #
# ------------------------------------------------------------------ #

def test_auto_recover_back_to_cloud():
    """本地模式开启时走本地；用户关闭本地模式且网络恢复 → 下次 chat 自动回 cloud 并通知。"""
    local = StubLocalLLM(text="离线应答")
    mgr, cloud = _make_manager(online=False, local_enabled=True, local_llm=local,
                               interval=0.0)  # 关闭节流，每次重探

    # 第一次：本地模式开启 → 本地优先直连本地（不探测）
    events = []
    mgr.add_listener(lambda m: events.append(m))
    assert list(mgr.chat([{"role": "user", "content": "你好"}])) == ["离线应答"]
    assert mgr.mode == "local"
    assert events == ["local"]

    # 用户关闭本地模式 + 网络恢复（config dict 原地翻转，模拟设置页热更）
    mgr._config["local_llm"]["enabled"] = False
    cloud.is_online.return_value = True

    # 第二次：云端路径 → 自动切回 cloud 并通知
    out = "".join(mgr.chat([{"role": "user", "content": "在吗"}]))
    assert out == "你好世界"
    assert mgr.mode == "cloud"
    assert events == ["local", "cloud"]          # 收到了恢复通知
    assert mgr.last_mode_event == "cloud"


# ------------------------------------------------------------------ #
# 6. 节流：多次 refresh_online 只在超间隔 / force 时真正探测          #
# ------------------------------------------------------------------ #

def test_refresh_online_throttle_count():
    """节流：间隔内复用缓存，仅当超间隔或 force 才真正调 cloud.is_online。"""
    mgr, cloud = _make_manager(online=True, interval=30.0)
    assert mgr.refresh_online() is True
    assert mgr.refresh_online() is True            # 缓存命中，不打网络
    assert mgr.refresh_online() is True
    assert cloud.is_online.call_count == 1

    assert mgr.refresh_online(force=True) is True  # force 强制重探
    assert cloud.is_online.call_count == 2


def test_refresh_online_force_updates_cache():
    """force 强制重探并更新缓存结果（离线→在线反转）。"""
    mgr, cloud = _make_manager(online=False, interval=30.0)
    assert mgr.refresh_online() is False
    cloud.is_online.return_value = True
    assert mgr.refresh_online() is False          # 间隔内缓存仍为 False
    assert mgr.refresh_online(force=True) is True  # force 后更新为 True


def test_refresh_online_probe_interval_elapsed_resets():
    """超间隔后 refresh_online 会重新真实探测。"""
    mgr, cloud = _make_manager(online=True, interval=0.0)
    assert mgr.refresh_online() is True
    assert mgr.refresh_online() is True           # interval=0 每次都重探
    assert cloud.is_online.call_count == 2


# ------------------------------------------------------------------ #
# 7. 本地未就绪：offline_chat 抛 LlamaNotReady → 引导提示不崩          #
# ------------------------------------------------------------------ #

def test_local_not_ready_yields_hint():
    """本地模式开但 offline_chat 抛 LlamaNotReady → 产本地引导提示且不崩溃。"""
    local = StubLocalLLM(raise_not_ready=True)
    mgr, _ = _make_manager(online=False, local_enabled=True, local_llm=local)
    out = list(mgr.chat([{"role": "user", "content": "你好"}]))
    assert out == [LOCAL_NOT_READY_PROMPT]
    assert mgr.status == "offline_hint"


def test_local_llm_none_yields_hint():
    """开启本地模式但未注入 local_llm → 产本地引导提示不崩。"""
    mgr, _ = _make_manager(online=False, local_enabled=True, local_llm=None)
    out = list(mgr.chat([{"role": "user", "content": "你好"}]))
    assert out == [LOCAL_NOT_READY_PROMPT]


# ------------------------------------------------------------------ #
# 8. 其它：配置读取 / 非法模式 / 导出                                  #
# ------------------------------------------------------------------ #

def test_local_mode_enabled_default_false():
    """config 缺失时 local_mode_enabled 默认 False。"""
    cloud = Mock()
    cloud.is_online.return_value = False
    mgr = OfflineFallbackManager(cloud=cloud, config=None)
    assert mgr.local_mode_enabled() is False


def test_change_mode_invalid_raises():
    """change_mode 传入非法模式抛 ValueError。"""
    mgr, _ = _make_manager()
    with pytest.raises(ValueError):
        mgr.change_mode("local_offline")


def test_change_mode_noop_when_same():
    """变化到相同模式不通知监听者、不写事件。"""
    mgr, _ = _make_manager()
    events = []
    mgr.add_listener(lambda m: events.append(m))
    mgr.change_mode("cloud")                      # 已是 cloud，无变化
    assert events == []
    assert mgr.last_mode_event is None


def test_cloud_required():
    """cloud 为 None 时构造抛 TypeError。"""
    with pytest.raises(TypeError):
        OfflineFallbackManager(cloud=None)


def test_package_exports():
    """OfflineFallbackManager / OFFLINE_PROMPT 可由 lite.cloud 包导入。"""
    from lite.cloud import OFFLINE_PROMPT as PkgPrompt
    from lite.cloud import OfflineFallbackManager as PkgMgr

    assert PkgMgr is OfflineFallbackManager
    assert PkgPrompt == OFFLINE_PROMPT


# ------------------------------------------------------------------ #
# G-3：流中途降级不再追加离线提示                                      #
# ------------------------------------------------------------------ #

def _make_midstream_cloud(chunks_before_error):
    """构造"先产出若干 chunk 再抛 CloudUnavailableError"的云端 mock。"""
    cloud = Mock()
    cloud.is_online.return_value = True

    def _chat(messages):
        for chunk in chunks_before_error:
            yield chunk
        raise CloudUnavailableError("流中途断连")

    cloud.chat.side_effect = _chat
    return cloud


def test_midstream_error_keeps_partial_reply_without_hint():
    """G-3（云端路径）：本地模式未开时已产出 chunk 后流中途故障 → 保留半截真实回复即止，不追加离线提示。"""
    cloud = _make_midstream_cloud(["部分", "回复"])
    local = StubLocalLLM(text="本地兜底")
    mgr = OfflineFallbackManager(
        cloud=cloud, local_llm=local, config={"local_llm": {"enabled": False}}
    )
    out = list(mgr.chat([{"role": "user", "content": "hi"}]))

    # 仅保留已产出的半截真实回复，不混入离线提示
    assert out == ["部分", "回复"]
    assert OFFLINE_PROMPT not in out


def test_pre_stream_error_still_degrades():
    """G-3：未产出任何 chunk 即故障 → 仍走降级分支（切本地）。"""
    cloud = _make_midstream_cloud([])  # 一块未产出就断连
    local = StubLocalLLM(text="本地兜底")
    mgr = OfflineFallbackManager(
        cloud=cloud, local_llm=local, config={"local_llm": {"enabled": True}}
    )
    out = list(mgr.chat([{"role": "user", "content": "hi"}]))

    assert out == ["本地兜底"]
    assert mgr.mode == "local"


# ------------------------------------------------------------------ #
# N5：CloudConfigError 探测路径诊断态                                  #
# ------------------------------------------------------------------ #

def test_refresh_online_config_error_sets_diagnostic_state():
    """N5：探测抛 CloudConfigError → online=False 且 _config_error 记录诊断文本。"""
    cloud = Mock()
    cloud.is_online.side_effect = CloudConfigError("未配置 api_key")
    mgr = OfflineFallbackManager(
        cloud=cloud, config={"local_llm": {"enabled": False}}
    )
    assert mgr.refresh_online(force=True) is False
    assert mgr._config_error is not None
    assert "api_key" in mgr._config_error


def test_config_error_yields_config_prompt_not_offline():
    """N5：配置错误诊断态下离线产出"云端配置未完成"文案，不再误诊为断网。"""
    cloud = Mock()
    cloud.is_online.side_effect = CloudConfigError("未配置 api_key")
    mgr = OfflineFallbackManager(
        cloud=cloud, config={"local_llm": {"enabled": False}}
    )
    out = list(mgr.chat([{"role": "user", "content": "hi"}]))

    assert out == [CONFIG_ERROR_PROMPT]
    assert OFFLINE_PROMPT not in out
    assert mgr.status == "offline_hint"


def test_connection_error_clears_diagnostic_state():
    """N5：连接层探测异常（非配置错误）→ _config_error 清 None，仍产断网提示。"""
    cloud = Mock()
    cloud.is_online.return_value = False
    mgr = OfflineFallbackManager(
        cloud=cloud, config={"local_llm": {"enabled": False}}
    )
    # 预置一个历史配置错误诊断态
    mgr._config_error = "旧配置错误残留"

    out = list(mgr.chat([{"role": "user", "content": "hi"}]))
    assert mgr._config_error is None
    assert out == [OFFLINE_PROMPT]


def test_successful_probe_clears_diagnostic_state():
    """N5：探测恢复成功 → 配置错误诊断态被清除。"""
    cloud = Mock()
    cloud.is_online.return_value = True
    mgr = OfflineFallbackManager(
        cloud=cloud, config={"local_llm": {"enabled": False}}
    )
    mgr._config_error = "历史配置错误"
    assert mgr.refresh_online(force=True) is True
    assert mgr._config_error is None


# ------------------------------------------------------------------ #
# 第四轮体检批次C：mode_history 尾部 100 条有界                          #
# ------------------------------------------------------------------ #

def test_mode_history_bounded_to_tail_100():
    """mode_history 无界追加改为保留尾部 100 条（长驻进程防内存缓慢增长）。"""
    from lite.cloud.fallback import _MAX_MODE_HISTORY

    mgr, _ = _make_manager(online=True)
    for i in range(_MAX_MODE_HISTORY + 50):
        mgr.change_mode("local" if i % 2 == 0 else "cloud")

    assert len(mgr.mode_history) == _MAX_MODE_HISTORY
    # 保留尾部：首个条目对应第 51 次切换（i=50，偶数 -> "local"）
    assert mgr.mode_history[0] == "local"
    assert mgr.mode_history[-1] == "cloud"