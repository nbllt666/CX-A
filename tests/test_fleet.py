# -*- coding: utf-8 -*-
"""FleetManager 单元测试（establish-fleet-admin-plane / 20261004_模块0_管理面CX-A管理CX-O）。

覆盖：台账 CRUD 与脱敏、注册/心跳 upsert（字段映射 + 时区转换 + token 保留）、
透传请求头与 request_id 注入（自带不覆盖）、health 免鉴权与超时钳制、
CX-O 非 2xx 结构化透传、不可达 504 映射、损坏隔离、分页钳制。
传输层全部注入 RecordingTransport，零真实网络。
"""

import io
import json
import urllib.error

import pytest

from lite.management.fleet import (
    FleetInvalid,
    FleetManager,
    FleetNotFound,
    FleetRemoteError,
    is_loopback_client,
)
from lite.management.remote import RemoteTransport, RemoteUnreachable


class RecordingTransport(RemoteTransport):
    """记录调用并按脚本回放的 mock transport（零真实网络）。"""

    def __init__(self, outcome=None):
        # outcome: None → 回 {"ok": True}；Exception → 抛出；callable(url) → 动态
        self.outcome = outcome
        self.calls = []

    def request(self, method, url, json_body=None, timeout=10, headers=None):
        self.calls.append(
            {"method": method, "url": url, "json_body": json_body,
             "timeout": timeout, "headers": dict(headers or {})}
        )
        if isinstance(self.outcome, Exception):
            raise self.outcome
        if callable(self.outcome):
            return self.outcome(url)
        return {"ok": True}


def _http_error(status, body):
    """构造带响应体的 HTTPError（模拟 CX-O 非 2xx）。"""
    return urllib.error.HTTPError(
        "http://cx-o.test", status, "err", {}, io.BytesIO(body.encode("utf-8"))
    )


@pytest.fixture()
def fleet(tmp_path):
    return FleetManager(data_dir=str(tmp_path), transport=RecordingTransport())


@pytest.fixture()
def manual_instance(fleet):
    return fleet.add_manual(name="家里的重实例", base_url="http://192.168.1.10:8000", token="tok-abcd1234")


# ---------------------------------------------------------------- 台账 CRUD
def test_initial_empty(tmp_path):
    fleet = FleetManager(data_dir=str(tmp_path), transport=RecordingTransport())
    assert fleet.list_instances() == []


def test_add_manual_persists_and_redacts(fleet, manual_instance, tmp_path):
    assert manual_instance["name"] == "家里的重实例"
    assert manual_instance["has_token"] is True
    assert manual_instance["token_suffix"] == "1234"
    assert "token" not in manual_instance  # 明文绝不回传
    # 落盘往返（重载后一致且仍脱敏）
    reloaded = FleetManager(data_dir=str(tmp_path), transport=RecordingTransport())
    listed = reloaded.list_instances()
    assert len(listed) == 1
    assert listed[0]["token_suffix"] == "1234"
    assert "token" not in listed[0]


def test_add_manual_missing_fields(fleet):
    # API 层会把请求体字段原样传入：缺字段即空串/None，统一 FleetInvalid（400）
    with pytest.raises(FleetInvalid):
        fleet.add_manual(name="", base_url="http://a", token="t")
    with pytest.raises(FleetInvalid):
        fleet.add_manual(name="x", base_url=None, token="t")
    with pytest.raises(FleetInvalid):
        fleet.add_manual(name="x", base_url="http://a", token="")
    with pytest.raises(FleetInvalid):
        fleet.add_manual(name="  ", base_url="http://a", token="t")  # 纯空白等同缺失


def test_add_manual_duplicate_base_url(fleet, manual_instance):
    with pytest.raises(FleetInvalid):
        fleet.add_manual(name="另一台", base_url="http://192.168.1.10:8000", token="t2")


def test_add_manual_completes_registered_token(fleet):
    """补录分支：注册上门（无 token）的实例，手动登记同 base_url 视为补录。"""
    fleet.register({"instance_id": "smoke-node", "endpoint": "http://a:8000", "role": "active"})
    view = fleet.add_manual(name="补录的", base_url="http://a:8000", token="tok-9999")
    assert view["token_suffix"] == "9999"
    assert view["source"] == "registered"  # 来源与 id 保留
    assert view["id"] == "smoke-node"
    # 已有 token 后再补录 → 恢复重复 400
    with pytest.raises(FleetInvalid):
        fleet.add_manual(name="再来", base_url="http://a:8000", token="tok-0000")


def test_get_instance(fleet, manual_instance):
    """get_instance：命中返回脱敏视图；未命中抛 FleetNotFound（FleetInvalid 子类，404 档）。"""
    view = fleet.get_instance(manual_instance["id"])
    assert view["id"] == manual_instance["id"]
    assert view["name"] == "家里的重实例"
    assert view["has_token"] is True and view["token_suffix"] == "1234"
    assert "token" not in view  # 脱敏：无 token 明文

    with pytest.raises(FleetNotFound) as excinfo:
        fleet.get_instance("no-such")
    # 兼容契约：按 docstring 宽口径捕 FleetInvalid 仍能命中（子类关系）
    assert isinstance(excinfo.value, FleetInvalid)


def test_remove(fleet, manual_instance):
    fleet.remove(manual_instance["id"])
    assert fleet.list_instances() == []
    with pytest.raises(FleetNotFound):
        fleet.remove(manual_instance["id"])


# ---------------------------------------------------------------- 损坏隔离
def test_corrupt_file_isolated_and_restarted(tmp_path):
    path = tmp_path / "fleet.json"
    path.write_text("{不是json", encoding="utf-8")
    fleet = FleetManager(data_dir=str(tmp_path), transport=RecordingTransport())
    assert fleet.list_instances() == []  # 空台账启动，不崩溃
    assert list(tmp_path.glob("fleet.json.corrupt-*"))  # 原文件隔离保留


def test_non_list_toplevel_isolated(tmp_path):
    path = tmp_path / "fleet.json"
    path.write_text('{"a": 1}', encoding="utf-8")
    fleet = FleetManager(data_dir=str(tmp_path), transport=RecordingTransport())
    assert fleet.list_instances() == []
    assert list(tmp_path.glob("fleet.json.corrupt-*"))


# ---------------------------------------------------------------- 注册/心跳
def test_register_new_instance(fleet):
    view = fleet.register(
        {"instance_id": "cx-o-node", "endpoint": "http://192.168.1.20:8000",
         "role": "active", "timestamp": "2026-10-04T05:00:00+00:00"}
    )
    assert view["id"] == "cx-o-node"
    assert view["name"] == "cx-o-node"  # 注册来源 name 以 instance_id 充当
    assert view["base_url"] == "http://192.168.1.20:8000"  # endpoint→base_url 映射
    assert view["role"] == "active"
    assert view["source"] == "registered"
    assert view["last_seen"].startswith("2026-10-04T1")  # UTC 05:00 → 本地 13:00 (UTC+8)


def test_register_upsert_refreshes_and_keeps_manual_token(fleet, tmp_path):
    # 预置手动登记（自定义 id），再以同 id 注册上门：token 保留、last_seen/role 刷新
    (tmp_path / "fleet.json").write_text(
        json.dumps([{"id": "inst-1", "name": "手动登记", "base_url": "http://old:8000",
                     "token": "tok-keep", "role": None, "source": "manual",
                     "registered_at": "2026-10-04T12:00:00", "last_seen": "2026-10-04T12:00:00"}],
                   ensure_ascii=False),
        encoding="utf-8",
    )
    fleet2 = FleetManager(data_dir=str(tmp_path), transport=RecordingTransport())
    view = fleet2.register({"instance_id": "inst-1", "endpoint": "http://new:8000",
                            "role": "active", "timestamp": "2026-10-04T06:00:00+00:00"})
    assert view["has_token"] is True and view["token_suffix"] == "keep"
    assert view["base_url"] == "http://new:8000"
    assert view["source"] == "manual"  # 来源不因心跳改写


def test_register_missing_fields(fleet):
    with pytest.raises(FleetInvalid):
        fleet.register({"instance_id": "x"})
    with pytest.raises(FleetInvalid):
        fleet.register({"endpoint": "http://a"})
    with pytest.raises(FleetInvalid):
        fleet.register("不是对象")


def test_register_bad_timestamp_falls_back(fleet):
    view = fleet.register({"instance_id": "n1", "endpoint": "http://a:8000",
                           "timestamp": "not-a-time"})
    assert view["last_seen"]  # 回落当前本地时间，不抛


# ---------------------------------------------------------------- 透传
def test_manifest_sends_bearer(fleet, manual_instance):
    fleet.get_manifest(manual_instance["id"])
    call = fleet._transport.calls[-1]
    assert call["url"] == "http://192.168.1.10:8000/api/admin/manifest"
    assert call["method"] == "GET"
    assert call["headers"]["Authorization"] == "Bearer tok-abcd1234"


def test_status_and_audit_passthrough(fleet, manual_instance):
    fleet.get_status(manual_instance["id"])
    assert fleet._transport.calls[-1]["url"].endswith("/api/admin/status")
    fleet.get_audit(manual_instance["id"], limit=5000, offset=-3)
    assert fleet._transport.calls[-1]["url"].endswith("/api/admin/audit?limit=1000&offset=0")


def test_health_no_auth_and_timeout_clamped(fleet, manual_instance):
    fleet.get_health(manual_instance["id"], timeout=5)
    call = fleet._transport.calls[-1]
    assert call["url"].endswith("/api/admin/health")
    assert "Authorization" not in call["headers"]  # CX-O 侧免鉴权 → 不带 Bearer
    assert call["timeout"] == 2.0  # 钳制上界
    fleet.get_health(manual_instance["id"], timeout=0.1)
    assert fleet._transport.calls[-1]["timeout"] == 1.0  # 钳制下界


def test_control_request_id_injected(fleet, manual_instance):
    fleet.control(manual_instance["id"], {"target": "config", "action": "reload"})
    body = fleet._transport.calls[-1]["json_body"]
    assert body["target"] == "config"
    assert isinstance(body["request_id"], str) and len(body["request_id"]) >= 8


def test_control_request_id_not_overridden(fleet, manual_instance):
    fleet.control(manual_instance["id"], {"target": "config", "action": "reload",
                                          "request_id": "op-001"})
    assert fleet._transport.calls[-1]["json_body"]["request_id"] == "op-001"


def test_batch_request_id_injected(fleet, manual_instance):
    fleet.batch(manual_instance["id"], {"mode": "sequential", "steps": [{"target": "config", "action": "reload"}]})
    assert "request_id" in fleet._transport.calls[-1]["json_body"]


def test_control_invalid_body(fleet, manual_instance):
    for bad in (None, {}, "x", []):
        with pytest.raises(FleetInvalid):
            fleet.control(manual_instance["id"], bad)


def test_unknown_instance_fleet_not_found(fleet):
    """透传家族 id 未命中 → FleetNotFound（API 层 404 档；非裸 KeyError）。"""
    with pytest.raises(FleetNotFound):
        fleet.get_manifest("no-such")
    with pytest.raises(FleetNotFound):
        fleet.control("no-such", {"target": "config", "action": "reload"})


def test_registered_instance_without_token_rejected(fleet):
    fleet.register({"instance_id": "cx-o-node", "endpoint": "http://192.168.1.20:8000",
                    "role": "active", "timestamp": "2026-10-04T05:00:00+00:00"})
    with pytest.raises(FleetInvalid):
        fleet.get_manifest("cx-o-node")  # 注册来源无 token → 要求先补录


# ---------------------------------------------------------------- 异常映射
def test_remote_error_structured_passthrough(fleet, manual_instance):
    fleet._transport.outcome = _http_error(401, '{"ok":false,"error":"ADMIN_AUTH_FAILED"}')
    with pytest.raises(FleetRemoteError) as exc_info:
        fleet.get_manifest(manual_instance["id"])
    assert exc_info.value.status_code == 401  # 结构化状态码（非消息字符串）
    assert exc_info.value.payload["error"] == "ADMIN_AUTH_FAILED"


def test_remote_error_non_json_body(fleet, manual_instance):
    fleet._transport.outcome = _http_error(503, "<html>gateway</html>")
    with pytest.raises(FleetRemoteError) as exc_info:
        fleet.get_status(manual_instance["id"])
    assert exc_info.value.status_code == 503
    assert exc_info.value.payload  # 兜底 {"status":..,"reason":..}


def test_unreachable_mapped(fleet, manual_instance):
    fleet._transport.outcome = urllib.error.URLError("connection refused")
    with pytest.raises(RemoteUnreachable):
        fleet.get_manifest(manual_instance["id"])


# ---------------------------------------------------------------- 回环来源校验
def test_is_loopback_client():
    """注册拓扑约束：回环放行，LAN/公网来源拒绝（GN-004 D-1 固化）。"""
    for host in ("127.0.0.1", "127.5.5.5", "::1", "localhost", "::ffff:127.0.0.1"):
        assert is_loopback_client(host) is True, host
    for host in ("192.168.1.10", "10.0.0.3", "::ffff:192.168.1.10", "fe80::1", "", None):
        assert is_loopback_client(host) is False, host
