# -*- coding: utf-8 -*-
"""Task C3 断网兜底机制单元测试（OfflineFallbackManager）。

覆盖（全 mock，无网络）：
- 在线 → cloud 流式透传，mode 保持 "cloud"；
- 离线 + 本地模式开 → 切 "local"、offline_chat 文本单块产出、listener 收到变化；
- 离线 + 未开本地 → 提示文案、mode 保持 "cloud"（不切换）、status=offline_hint；
- 云端调用抛 CloudUnavailableError → 自动降级（切本地 / 提示）；
- 恢复：先离线切 local，网络恢复后下次 chat 自动回 cloud 并通知；
- 节流：多次 refresh_online 只在超间隔 / force 时真正调 cloud.is_online；
- 本地未就绪：offline_chat 抛 LlamaNotReady → 产提示文案不崩；
- 导出：OfflineFallbackManager / OFFLINE_PROMPT 可由 lite.cloud 导入。
"""

from unittest.mock import Mock

import pytest

from lite.cloud.adapter import CloudUnavailableError
from lite.cloud.fallback import OFFLINE_PROMPT, OfflineFallbackManager
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
# 1. 在线 → cloud 流式透传，mode=cloud                               #
# ------------------------------------------------------------------ #

def test_online_streams_cloud_and_mode_cloud():
    """在线时 chat 应流式透传 cloud 各文本块，mode 保持 "cloud"。"""
    mgr, cloud = _make_manager(online=True, local_enabled=True,
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
# 5. 恢复：先离线切 local，网络恢复后下次 chat 自动回 cloud 并通知   #
# ------------------------------------------------------------------ #

def test_auto_recover_back_to_cloud():
    """先离线切 local，网络恢复后下次 chat 自动回 cloud 并通知监听者。"""
    local = StubLocalLLM(text="离线应答")
    mgr, cloud = _make_manager(online=False, local_enabled=True, local_llm=local,
                               interval=0.0)  # 关闭节流，每次重探

    # 第一次：离线 → 切 local
    events = []
    mgr.add_listener(lambda m: events.append(m))
    assert list(mgr.chat([{"role": "user", "content": "你好"}])) == ["离线应答"]
    assert mgr.mode == "local"
    assert events == ["local"]

    # 网络恢复
    cloud.is_online.return_value = True

    # 第二次：恢复在线 → 自动切回 cloud 并通知
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
# 7. 本地未就绪：offline_chat 抛 LlamaNotReady → 提示不崩             #
# ------------------------------------------------------------------ #

def test_local_not_ready_yields_hint():
    """本地模式开但 offline_chat 抛 LlamaNotReady → 产提示文案且不崩溃。"""
    local = StubLocalLLM(raise_not_ready=True)
    mgr, _ = _make_manager(online=False, local_enabled=True, local_llm=local)
    out = list(mgr.chat([{"role": "user", "content": "你好"}]))
    assert out == [OFFLINE_PROMPT]
    assert mgr.status == "offline_hint"


def test_local_llm_none_yields_hint():
    """开启本地模式但未注入 local_llm → 产提示文案不崩。"""
    mgr, _ = _make_manager(online=False, local_enabled=True, local_llm=None)
    out = list(mgr.chat([{"role": "user", "content": "你好"}]))
    assert out == [OFFLINE_PROMPT]


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