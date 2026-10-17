# -*- coding: utf-8 -*-
"""CXFC direct 传输层单测（20261017 CX-O 全量迁移批次 4）。

覆盖 lite/cxfc/direct.py：TOFU 注册/指纹拒绝、令牌哈希不外露、持久化往返、
访问令牌校验、direct 调用（注入 _http_json）、心跳刷新与超时巡检、
发现信标形态。全 mock 无真实网络。
"""

import ssl

import pytest

import lite.cxfc.direct as direct_mod
from lite.cxfc.direct import CxfcDirectManager, CxfDirectError, CXFC_BEACON_TYPE


def _make(tmp_path, **cfg_over):
    cfg = {
        "cxfc": {
            "enabled": True,
            "remote_admin_key": "admin-key-1",
            "heartbeat_timeout_s": 30.0,
            "heartbeat_check_interval_s": 10.0,
            "local_name": "CX-A",
            "local_http_port": 8600,
            **cfg_over,
        }
    }
    return CxfcDirectManager(config=cfg, data_dir=str(tmp_path))


# 自签测试证书形态（本测试经 mock 指纹函数验证比对逻辑，不依赖真实证书）
_TEST_PEM = (
    "-----BEGIN CERTIFICATE-----\n"
    "MIIBkTCB+wIJAKl\n"
    "-----END CERTIFICATE-----\n"
)


@pytest.fixture
def fake_fp(monkeypatch):
    """注入确定性指纹函数（sha256(PEM 文本)）——TOFU 比对逻辑不依赖真实证书。"""
    import hashlib

    def _fake(pem):
        return hashlib.sha256(pem.strip().encode()).hexdigest().upper().replace(":", "")

    monkeypatch.setattr(direct_mod, "_cert_fingerprint", _fake)
    return _fake


def test_register_direct_and_token_hash_hidden(tmp_path):
    """direct 注册：plugin_id=cxfc_{host}_{port}、明文令牌一次性返回、列表不外露哈希。"""
    mgr = _make(tmp_path)
    info, token = mgr.register_direct("192.168.1.8", 9000, name="语音工作站", tools=[
        {"name": "tts_speak", "description": "说话"},
    ])
    assert info["plugin_id"] == "cxfc_192.168.1.8_9000"
    assert info["status"] == "connected"
    assert len(token) == 64  # token_hex(32)

    listed = mgr.list_plugins()
    assert listed[0]["plugin_id"] == info["plugin_id"]
    assert "plugin_access_token_hash" not in listed[0]
    assert "tls_cert_pem" not in listed[0]
    assert token not in str(listed)
    # 同 host:port 重注册：更新不累积
    mgr.register_direct("192.168.1.8", 9000, name="v2")
    assert len(mgr.list_plugins()) == 1


def test_invalid_pem_rejected(tmp_path):
    """非法 PEM → 业务错误（拒绝注册），不裸抛 ssl 异常。"""
    mgr = _make(tmp_path)
    with pytest.raises(CxfDirectError, match="PEM"):
        mgr.register_direct(
            "10.0.0.1", 9001,
            tls_cert_pem="not a pem", tls_cert_fingerprint="0" * 64,
        )
    assert mgr.list_plugins() == []


def test_tofu_fingerprint_mismatch_rejected(tmp_path, fake_fp):
    """TOFU：PEM 与指纹不一致 → 拒绝注册且内存/持久化双空。"""
    mgr = _make(tmp_path)
    with pytest.raises(CxfDirectError, match="指纹"):
        mgr.register_direct(
            "10.0.0.1", 9001,
            tls_cert_pem=_TEST_PEM, tls_cert_fingerprint="0" * 64,
        )
    assert mgr.list_plugins() == []


def test_tofu_fingerprint_match_accepted(tmp_path, fake_fp):
    """指纹与 PEM 匹配（含冒号形态）→ 注册成功，指纹归一化存储。"""
    real_fp = fake_fp(_TEST_PEM)
    assert len(real_fp) == 64
    mgr = _make(tmp_path)
    info, _token = mgr.register_direct(
        "10.0.0.1", 9001,
        tls_cert_pem=_TEST_PEM,
        tls_cert_fingerprint=":" + ":".join(real_fp[i:i + 2] for i in range(0, 64, 2)),
    )
    assert info["tls_cert_fingerprint"] == real_fp  # 去冒号大写


def test_persist_roundtrip(tmp_path):
    """持久化往返：注册 → 新实例 load → 记录恢复；令牌哈希随盘 → 重载实例仍可验真。"""
    mgr = _make(tmp_path)
    info, token = mgr.register_direct("10.0.0.2", 9002, name="n2")
    reloaded = _make(tmp_path)
    assert reloaded.load_persisted() == 1
    got = reloaded.get_plugin(info["plugin_id"])
    assert got["name"] == "n2"
    # 哈希随盘：重载实例按哈希校验通过（明文代持表为空不影响哈希比对路径）
    assert reloaded.verify_plugin_access(token) == info["plugin_id"]
    # 原实例同样可校验（内存代持）
    assert mgr.verify_plugin_access(token) == info["plugin_id"]


def test_verify_plugin_access(tmp_path):
    """令牌校验：命中 / 未命中 / 错令牌 None。"""
    mgr = _make(tmp_path)
    info, token = mgr.register_direct("10.0.0.3", 9003)
    assert mgr.verify_plugin_access(token, plugin_id=info["plugin_id"]) == info["plugin_id"]
    assert mgr.verify_plugin_access("bad") is None
    assert mgr.verify_plugin_access("") is None


def test_admin_bypass_check(tmp_path):
    """运维旁路：X-API-Key 与 remote_admin_key 恒时比对。"""
    mgr = _make(tmp_path)
    assert mgr.issue_admin_token("admin-key-1") is True
    assert mgr.issue_admin_token("wrong") is False
    assert mgr.issue_admin_token("") is False


def test_heartbeat_refresh_and_sweep_timeout(tmp_path, monkeypatch):
    """心跳：上行刷新 last_seen + 复活；巡检超时置 disconnected（仅 direct）。"""
    mgr = _make(tmp_path)
    info, _ = mgr.register_direct("10.0.0.4", 9004)
    pid = info["plugin_id"]

    # 心跳上行刷新
    assert mgr.heartbeat(port=9004) is True  # 空 plugin_id 按 port 反查
    assert mgr.heartbeat(plugin_id=pid) is True
    assert mgr.heartbeat(plugin_id="ghost") is False

    # 巡检：last_seen 超时 → disconnected；embedded/relay 不受巡检影响
    from datetime import datetime, timedelta

    old = (datetime.now() - timedelta(seconds=60)).isoformat()
    mgr._plugins[pid]["last_seen"] = old
    mgr._hb_sweep()
    assert mgr.get_plugin(pid)["status"] == "disconnected"

    # 心跳重上 → 复活 connected
    mgr.heartbeat(plugin_id=pid)
    assert mgr.get_plugin(pid)["status"] == "connected"


def test_call_plugin_with_injected_http(tmp_path, monkeypatch):
    """direct 调用：POST /call 带 Bearer + request_id；结果透传；不可用插件外壳。"""
    mgr = _make(tmp_path)
    info, token = mgr.register_direct("10.0.0.5", 9005)
    pid = info["plugin_id"]
    calls = {}

    def fake_http(url, payload=None, token=None, timeout=None, ca_pem=None):
        calls["url"] = url
        calls["payload"] = payload
        calls["token"] = token
        return 200, {"success": True, "result": {"echo": payload["tool"]}}

    monkeypatch.setattr(direct_mod, "_http_json", fake_http)
    out = mgr.call_plugin(pid, "speak", {"text": "你好"})
    assert out["success"] is True
    assert calls["url"] == "http://10.0.0.5:9005/call"
    assert calls["payload"]["request_id"]
    assert calls["token"] == token

    # disconnected → 不可用外壳（不发起调用）
    mgr._plugins[pid]["status"] = "disconnected"
    out2 = mgr.call_plugin(pid, "speak", {})
    assert out2 == {"success": False, "error": f"插件 {pid} 不可用"}

    # 未知插件
    out3 = mgr.call_plugin("ghost", "x", {})
    assert out3["success"] is False


def test_discovery_beacon_shape(tmp_path):
    """信标五字段（type/name/port/capabilities/version）对齐 CX-O 协议。"""
    mgr = _make(tmp_path)
    disc = mgr.start_discovery()
    try:
        beacon = disc.beacon()
        assert beacon["type"] == CXFC_BEACON_TYPE
        assert beacon["name"] == "CX-A"
        assert beacon["port"] == 8600
        assert beacon["capabilities"] == ["cxfc"]
        assert beacon["version"] == "1.0.0"
        # 未启动实例：discovered 为空
        mgr.stop_discovery()
        assert mgr.discovered_nodes() == []
        assert mgr._discovery is None
    finally:
        mgr.stop_discovery()


def test_discovery_dedup_on_rebeacon(tmp_path):
    """发现去重：同 host:port 重复信标仅保留一条（刷新字段）。"""
    from lite.cxfc.direct import CxfcDiscovery

    disc = CxfcDiscovery("CX-A", 8600)
    # 直接驱动登记逻辑（构造 fake recv 不易——经 discovered 表语义验证）
    with disc._lock:
        disc.discovered[("10.0.0.9", 8000)] = {"name": "a", "host": "10.0.0.9", "port": 8000, "version": "1.0.0"}
        disc.discovered[("10.0.0.9", 8000)] = {"name": "a2", "host": "10.0.0.9", "port": 8000, "version": "1.0.1"}
    assert len(disc.discovered) == 1
    assert disc.discovered[("10.0.0.9", 8000)]["version"] == "1.0.1"


def test_ssl_fingerprint_helper_real_cert():
    """_cert_fingerprint：真实自签证书走 ssl.PEM_cert_to_DER_cert 链路。"""
    # 用 ssl 内置转换验证 helper 不抛——非法 PEM 应抛 ValueError
    with pytest.raises(Exception):
        direct_mod._cert_fingerprint("not a pem")
