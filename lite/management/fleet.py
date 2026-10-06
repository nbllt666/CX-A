# -*- coding: utf-8 -*-
"""CX-A 管理面——CX-O 实例群（Fleet）管理器（establish-fleet-admin-plane）。

管理面语义（用户既定方向）：**CX-A 是管理面，管理 CX-O 实例群**；前端刻意不放
管理界面，本模块只以 API 形态存在（经 lite/server/api_server.py 暴露 /api/fleet/*）。

三层职责：

1. **实例台账**：``<data_dir>/fleet.json`` 持久化（原子写 + N3 损坏隔离重建，
   口径对齐 local_agents.py）。字段：id / name / base_url / token / role /
   source(manual|registered) / registered_at / last_seen（统一 naive 本地时间，
   与 agents.json 惯例一致）。台账列表/详情对外**不回传 token 明文**（仅尾 4 位
   标识）；落盘 token 的威胁模型与 logs/api_token.json 一致（同用户本机可读，
   防的是浏览器侧 CSRF→RCE，非同用户本机进程）。

2. **注册/心跳接收**：接收 CX-O ``InstanceRegistry`` 主动上报（源码实证格式
   ``{"instance_id", "endpoint", "role", "timestamp"}``，无鉴权头；endpoint 为
   CX-O 的 LAN 地址，timestamp 为 UTC aware ISO 串）。endpoint→base_url 映射、
   timestamp→本地 naive last_seen 转换、按 instance_id upsert。
   注册方向拓扑约束：仅限同机实例注册上门（回环来源校验由 API 层执行）；
   跨机实例走手动登记通道（add_manual）。

3. **透传治理**：以实例令牌（``Authorization: Bearer``）透传 CX-O 控制平面端点
   （c:\\CX-O\\docs\\CX-A管理接口文档.md）：
   - GET  /api/admin/manifest | status | audit —— 只读；
   - POST /api/admin/control | batch —— 请求体缺 ``request_id`` 时自动生成
     UUID，调用方自带时不覆盖（防重放边界：自带 → CX-O 防重放缓存生效；
     自动生成 → 每次为新请求，CX-A 层不做防重放缓存）；
   - GET  /api/admin/health —— CX-O 侧免鉴权，转发不带 Bearer，超时钳制 1-2s。
   错误语义：不可达 → FleetUnreachable（API 层 504）；CX-O 非 2xx →
   FleetRemoteError 携带结构化 status_code + payload（API 层**原样透传**状态码
   与 ADMIN_* 错误码，非 502 包装）；参数缺失 → ValueError（API 层 400）。

路径规范：本文件路径推导仅基于注入的 data_dir（os.path.join），禁止相对路径；
传输复用 lite/management/remote.py 的 RemoteTransport 抽象（已扩展 headers 注入
与结构化错误状态码，向后兼容）。
"""

import json
import os
import uuid
import urllib.error
from datetime import datetime

from lite.management.remote import (
    HTTPRemoteTransport,
    RemoteTransport,
    RemoteUnreachable,
)

__all__ = [
    "FleetInvalid",
    "FleetNotFound",
    "FleetRemoteError",
    "FleetManager",
]


class FleetInvalid(ValueError):
    """参数缺失 / 非法（API 层转 400）。继承 ValueError 保持口径统一。"""


class FleetNotFound(FleetInvalid):
    """实例 id 未命中台账（资源不存在，API 层转 404）。

    继承 FleetInvalid：调用方按「未命中属参数类错误」宽口径捕获（except
    FleetInvalid）仍能命中；需要区分 404 / 400 时再按本类精确分流
    （api_server._map_fleet_error 首位判定，先于 FleetInvalid 的 400 分支）。
    """


class FleetRemoteError(Exception):
    """CX-O 管理端点返回非 2xx（API 层原样透传状态码与 ADMIN_* 错误码）。

    Attributes:
        status_code: 远端原始 HTTP 状态码（结构化，非消息字符串）。
        payload: 远端响应体（已解析 JSON；解析失败为 {"status":..,"reason":..} 兜底）。
    """

    def __init__(self, message, status_code, payload=None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload if payload is not None else {}


def _now_naive():
    """当前本地时间（naive，与 agents.json 时间戳惯例一致）。"""
    return datetime.now().isoformat()


def _to_local_naive(value):
    """把 CX-O 上报的 ISO 时间戳统一转本地 naive；解析失败回落当前本地时间。

    CX-O InstanceRegistry 上报 UTC aware ISO 串（源码实证）；直接混存会与
    naive 本地时间产生时区偏差，故先 astimezone 再剥 tzinfo。
    """
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone().replace(tzinfo=None)
            return parsed.isoformat()
        except ValueError:
            pass
    return _now_naive()


def is_loopback_client(host):
    """注册来源回环校验（spec establish-fleet-admin-plane / GN-004 D-1 固化）。

    拓扑约束固化为代码：CX-O→CX-A 注册方向仅限同机实例；跨机实例必须走
    手动登记通道。接受 127.0.0.0/8、::1、localhost 与 IPv4 映射回环。
    """
    host = str(host or "").strip().lower()
    if host in ("127.0.0.1", "::1", "localhost"):
        return True
    return host.startswith("127.") or host.startswith("::ffff:127.")


class FleetManager:
    """CX-O 实例群台账 + 注册接收 + 治理指令透传。

    Args:
        data_dir: 数据目录（``fleet.json`` 落在 <data_dir>/fleet.json）。
        transport: 传输对象（RemoteTransport 子类，提供
            request(method, url, json_body, timeout, headers)）；None 时用默认
            HTTPRemoteTransport。测试注入 mock transport 隔离网络。
    """

    #: 注册上报 payload 必填字段（源码实证：registry.py 周期上报该四字段）
    REGISTER_REQUIRED_FIELDS = ("instance_id", "endpoint")

    def __init__(self, data_dir, transport=None):
        self._data_dir = data_dir
        self._path = os.path.join(data_dir, "fleet.json")
        self._transport = (
            transport if transport is not None else HTTPRemoteTransport()
        )
        self._instances = []
        self._load()

    # ------------------------------------------------------------------ #
    # 台账持久化（原子写 + N3 损坏隔离）                                    #
    # ------------------------------------------------------------------ #

    def _load(self):
        """读台账；文件缺失视为空表，非法 JSON 按 N3 口径隔离后空表启动。"""
        if not os.path.exists(self._path):
            self._instances = []
            return
        raw = None
        try:
            with open(self._path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            isolated = self._isolate_corrupt_file()
            print(
                f"[WARN] fleet.json 读取失败（{exc}），原文件已"
                f"{'隔离至 ' + str(isolated) if isolated else '隔离失败（保留原位）'}，以空台账启动"
            )
            raw = None
        if not isinstance(raw, list):
            if raw is not None:
                isolated = self._isolate_corrupt_file()
                print(
                    f"[WARN] fleet.json 顶层结构非法（应为列表，实际 "
                    f"{type(raw).__name__}），原文件已"
                    f"{'隔离至 ' + str(isolated) if isolated else '隔离失败（保留原位）'}，以空台账启动"
                )
            raw = []
        self._instances = [item for item in raw if isinstance(item, dict)]

    def _isolate_corrupt_file(self):
        """损坏文件隔离：改名保留原位（fleet.json.corrupt-<时间戳>），失败返回 None。"""
        try:
            target = f"{self._path}.corrupt-{datetime.now().strftime('%Y%m%d%H%M%S')}"
            os.replace(self._path, target)
            return target
        except OSError:
            return None

    def _save(self):
        """全量原子写回（临时文件 + os.replace），失败清理 tmp 并抛出。"""
        tmp_path = f"{self._path}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._instances, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._path)
        except OSError:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------ #
    # 台账 CRUD（对外视图一律脱敏）                                        #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _public_view(inst):
        """对外视图：不含 token 明文，仅尾 4 位标识。"""
        token = inst.get("token") or ""
        return {
            "id": inst["id"],
            "name": inst.get("name", ""),
            "base_url": inst.get("base_url", ""),
            "role": inst.get("role"),
            "source": inst.get("source", "manual"),
            "registered_at": inst.get("registered_at"),
            "last_seen": inst.get("last_seen"),
            "has_token": bool(token),
            "token_suffix": token[-4:] if token else None,
        }

    def _find(self, instance_id):
        for inst in self._instances:
            if inst.get("id") == instance_id:
                return inst
        return None

    def list_instances(self):
        """台账列表（脱敏视图；仅被动 last_seen 新鲜度，不做在线探测）。"""
        return [self._public_view(inst) for inst in self._instances]

    def get_instance(self, instance_id):
        """按 id 取脱敏视图；未命中抛 FleetNotFound（API 层按 404 处理）。

        注意：与 add_manual 的 FleetInvalid(400) 不同，本方法未命中属
        「资源不存在」——FleetNotFound 为 FleetInvalid 子类，API 层
        _map_fleet_error 首位按 404 分流。
        """
        inst = self._find(instance_id)
        if inst is None:
            raise FleetNotFound(instance_id)
        return self._public_view(inst)

    def add_manual(self, name, base_url, token):
        """手动登记实例（跨机实例的唯一通道）。

        Args:
            name: 实例显示名（非空）。
            base_url: CX-O 管理面基础地址（非空；全台账唯一）。
            token: 该实例 admin.tokens 登记的 Bearer 令牌（非空）。

        Returns:
            dict: 新实例的脱敏视图。

        Raises:
            FleetInvalid: 必填字段缺失 / base_url 重复。
        """
        name = str(name or "").strip()
        base_url = str(base_url or "").strip().rstrip("/")
        token = str(token or "").strip()
        if not name or not base_url or not token:
            raise FleetInvalid("name / base_url / token 均为必填")
        existing = next(
            (inst for inst in self._instances if inst.get("base_url") == base_url), None
        )
        if existing is not None:
            if existing.get("token"):
                raise FleetInvalid(f"base_url 已登记：{base_url}")
            # 补录分支：注册上门的实例无令牌，手动登记同 base_url 视为补录
            # token（否则注册实例永久不可治理）；来源与 id 保留。
            existing["token"] = token
            existing["name"] = name
            existing["last_seen"] = _now_naive()
            self._save()
            return self._public_view(existing)
        inst = {
            "id": f"fleet-{uuid.uuid4().hex[:12]}",
            "name": name,
            "base_url": base_url,
            "token": token,
            "role": None,
            "source": "manual",
            "registered_at": _now_naive(),
            "last_seen": _now_naive(),
        }
        self._instances.append(inst)
        self._save()
        return self._public_view(inst)

    def remove(self, instance_id):
        """注销实例（移除记录；CX-O 侧心跳仍会重新上门——语义为移除非阻止）。

        Raises:
            FleetNotFound: instance_id 未命中台账（API 层按 404 处理）。
        """
        inst = self._find(instance_id)
        if inst is None:
            raise FleetNotFound(instance_id)
        self._instances.remove(inst)
        self._save()

    # ------------------------------------------------------------------ #
    # 注册/心跳接收（CX-O InstanceRegistry 主动上报）                       #
    # ------------------------------------------------------------------ #

    def register(self, payload):
        """接收 CX-O 注册/心跳：按 instance_id upsert，刷新 last_seen。

        Args:
            payload: 源码实证格式 ``{"instance_id", "endpoint", "role",
                "timestamp"}``（无鉴权头）；endpoint→base_url 映射，
                timestamp（UTC aware）→ 本地 naive last_seen。

        Returns:
            dict: upsert 后实例的脱敏视图。

        Raises:
            FleetInvalid: instance_id / endpoint 缺失。
        """
        if not isinstance(payload, dict):
            raise FleetInvalid("注册体应为 JSON 对象")
        instance_id = str(payload.get("instance_id") or "").strip()
        endpoint = str(payload.get("endpoint") or "").strip().rstrip("/")
        for field, value in zip(self.REGISTER_REQUIRED_FIELDS, (instance_id, endpoint)):
            if not value:
                raise FleetInvalid(f"注册体缺少必填字段：{field}")
        role = payload.get("role")
        last_seen = _to_local_naive(payload.get("timestamp"))

        existing = self._find(instance_id)
        if existing is not None:
            # 心跳/重注册：刷新 last_seen 与 role/base_url；手动登记的 token 保留
            existing["base_url"] = endpoint
            existing["role"] = role
            existing["last_seen"] = last_seen
            inst = existing
        else:
            inst = {
                "id": instance_id,
                "name": instance_id,
                "base_url": endpoint,
                "token": None,
                "role": role,
                "source": "registered",
                "registered_at": _now_naive(),
                "last_seen": last_seen,
            }
            self._instances.append(inst)
        self._save()
        return self._public_view(inst)

    # ------------------------------------------------------------------ #
    # 治理指令透传（CX-O 控制平面）                                        #
    # ------------------------------------------------------------------ #

    def _request_instance(self, instance_id, method, path, json_body=None,
                          timeout=10, with_auth=True):
        """对指定实例发起一次管理端点请求，统一异常映射。

        Raises:
            FleetNotFound: 实例 id 未命中（API 层 404）。
            FleetInvalid: 实例已登记但无令牌却要求鉴权透传。
            RemoteUnreachable: 连接失败 / 超时（API 层 504）。
            FleetRemoteError: 远端非 2xx（API 层原样透传 status_code/payload）。
        """
        inst = self._find(instance_id)
        if inst is None:
            raise FleetNotFound(instance_id)
        token = inst.get("token") or ""
        headers = {}
        if with_auth:
            if not token:
                raise FleetInvalid(
                    f"实例 {instance_id} 为注册来源（无令牌），先经手动登记补录 token"
                )
            headers["Authorization"] = f"Bearer {token}"
        url = f"{inst['base_url']}{path}"
        try:
            return self._transport.request(
                method, url, json_body=json_body, headers=headers, timeout=timeout
            )
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {"status": status, "reason": exc.reason}
            raise FleetRemoteError(
                f"CX-O 实例 {method} {url} 返回非 2xx（{status}）",
                status_code=status,
                payload=payload,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RemoteUnreachable(f"CX-O 实例 {url} 不可达: {exc}") from exc

    def get_manifest(self, instance_id, timeout=10):
        """透传 GET /api/admin/manifest（自描述能力清单，readonly）。"""
        return self._request_instance(instance_id, "GET", "/api/admin/manifest", timeout=timeout)

    def get_status(self, instance_id, timeout=10):
        """透传 GET /api/admin/status（实例状态快照，readonly）。"""
        return self._request_instance(instance_id, "GET", "/api/admin/status", timeout=timeout)

    def get_audit(self, instance_id, limit=50, offset=0, timeout=10):
        """透传 GET /api/admin/audit（管理审计，readonly；分页参数钳制后拼接）。"""
        limit = max(1, min(int(limit), 1000))
        offset = max(0, int(offset))
        return self._request_instance(
            instance_id, "GET", f"/api/admin/audit?limit={limit}&offset={offset}",
            timeout=timeout,
        )

    def get_health(self, instance_id, timeout=1.5):
        """透传 GET /api/admin/health（CX-O 侧免鉴权：转发不带 Bearer）。

        超时钳制 [1, 2] 秒——health 用于快速探测，防长超时拖慢单线程服务。
        """
        clamped = min(max(float(timeout), 1.0), 2.0)
        return self._request_instance(
            instance_id, "GET", "/api/admin/health", timeout=clamped, with_auth=False
        )

    def control(self, instance_id, body, timeout=10):
        """透传 POST /api/admin/control（operator；缺 request_id 自动补 UUID）。"""
        body = self._prepare_control_body(body)
        return self._request_instance(
            instance_id, "POST", "/api/admin/control", json_body=body, timeout=timeout
        )

    def batch(self, instance_id, body, timeout=10):
        """透传 POST /api/admin/batch（operator；缺 request_id 自动补 UUID）。"""
        body = self._prepare_control_body(body)
        return self._request_instance(
            instance_id, "POST", "/api/admin/batch", json_body=body, timeout=timeout
        )

    @staticmethod
    def _prepare_control_body(body):
        """控制/编排请求体校验 + request_id 注入（自带不覆盖）。"""
        if not isinstance(body, dict) or not body:
            raise FleetInvalid("请求体应为非空 JSON 对象")
        prepared = dict(body)
        prepared.setdefault("request_id", uuid.uuid4().hex)
        return prepared
