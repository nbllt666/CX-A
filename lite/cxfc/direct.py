# -*- coding: utf-8 -*-
"""CXFC direct 传输 + UDP 发现 + 持久化 + 心跳（20261017 CX-O 全量迁移批次 4）。

对齐 CX-O ``server/core/cxfc/manager.py`` 的 direct 子集与 ``discovery.py``
（recon_cxfc.md §3/§4，行号佐证），改写为纯标准库线程版：

- **direct 注册**：``plugin_id = cxfc_{host}_{port}``；TOFU——同时上报 PEM 与
  指纹时 DER SHA-256 比对，不一致拒绝注册；签发插件访问令牌（token_hex(32)
  明文仅注册响应一次性返回，库中仅存 SHA-256 哈希）；
- **direct 调用**：``POST http(s)://{host}:{port}/call``（30s 超时），带 token
  插件附加 ``Authorization: Bearer`` + uuid4 request_id；无 token 旧插件兼容；
- **心跳**：daemon 线程每 10s 巡检，**仅 direct**；last_seen 超 30s →
  disconnected（复活靠插件心跳重上，重上时重拉 /tools /skills）；
- **UDP 发现**：信标 ``type=CXFC_BEACON``；**广播双发 9996/9997、监听双收
  9996/9997**——CX-O 事实端口不对称（其广播发 9997、监听 9996），双收双发
  同时覆盖「CX-A 发现 CX-O」与「CX-O 发现 CX-A」两个方向；
- **持久化**：``<data_dir>/cxfc/plugins.json``（CX-A JSON 主力存储口径；
  ``plugin_access_token_hash`` 哈希落盘、明文不落盘）。

本模块只使用标准库；与 :mod:`lite.cxfc.lite_cxfc`（embedded/relay）并存。
"""

import hashlib
import json
import logging
import os
import secrets
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid

LOGGER = logging.getLogger(__name__)

#: CXFC 发现信标类型（对齐 CX-O discovery.py）
CXFC_BEACON_TYPE = "CXFC_BEACON"
#: CX-O 事实端口（不对称）：其广播发 9997、监听 9996（recon_cxfc.md §3.1）
CXFC_BROADCAST_PORTS = (9996, 9997)
CXFC_LISTEN_PORTS = (9996, 9997)
#: 直连调用默认超时（对齐 CX-O call 30s）
DIRECT_CALL_TIMEOUT_S = 30.0
#: /health 探活超时（对齐 CX-O 5s）
HEALTH_PROBE_TIMEOUT_S = 5.0
#: 心跳巡检默认参数（对齐 CX-O manager L54-55）
DEFAULT_HEARTBEAT_TIMEOUT_S = 30.0
DEFAULT_HEARTBEAT_CHECK_INTERVAL_S = 10.0


class CxfDirectError(Exception):
    """direct 传输错误（注册校验失败 / 连接不可达等）。"""


def _cert_fingerprint(pem):
    """PEM 证书的 DER SHA-256 指纹（去冒号大写，对齐 CX-O 归一化口径）。

    :raises CxfDirectError: PEM 非法（base64 解码失败等）
    """
    try:
        der = ssl.PEM_cert_to_DER_cert(pem.strip())
    except Exception as exc:  # noqa: BLE001 - 非法 PEM 归一为业务错误
        raise CxfDirectError(f"TLS 证书 PEM 非法：{exc}") from exc
    return hashlib.sha256(der).hexdigest().upper().replace(":", "")


def _http_json(url, payload=None, token=None, timeout=HEALTH_PROBE_TIMEOUT_S,
               ca_pem=None, extra_headers=None):
    """HTTP JSON 请求（urllib；ca_pem 提供时走 HTTPS 证书固定）。返回 (status, dict)。"""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra_headers:
        headers.update(extra_headers)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    if url.startswith("https://") and ca_pem:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.load_verify_locations(cadata=ca_pem)
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    else:
        opener = urllib.request.build_opener()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with opener.open(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    try:
        return int(resp.status), (json.loads(body) if body else {})
    except ValueError:
        return int(resp.status), {}


class CxfcDiscovery:
    """CXFC UDP 发现（线程版）：双端口广播 + 双端口监听，发现即回调。

    协议事实（recon_cxfc.md §3）：CX-O 广播发 9997、监听 9996（同构互不相闻）；
    本实现广播**双发 9996/9997**、监听**双收 9996/9997**，两个互通方向均覆盖。
    """

    def __init__(self, local_name, local_port, on_discovered=None,
                 broadcast_address="255.255.255.255", socket_factory=None):
        """构造发现器。

        :param local_name: 本机节点名（信标 name 字段）
        :param local_port: 本机主服务端口（信标 port 字段——对端据此拉插件清单）
        :param on_discovered: 可选回调 ``cb({"name","host","port","version"}, addr)``
        :param socket_factory: 测试注入；缺省真实 socket
        """
        self._name = str(local_name or "CX-A")
        self._local_port = int(local_port or 0)
        self._on_discovered = on_discovered
        self._broadcast_address = broadcast_address
        self._socket_factory = socket_factory or socket.socket
        self._send_sock = None
        self._recv_socks = []
        self._threads = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        #: 已发现节点：{(host, port): info dict}（去重，刷新 version/name）
        self.discovered = {}

    def start(self):
        """启动：建发送 socket + 绑定全部监听端口并开收包线程（幂等）。"""
        if self._send_sock is not None:
            return
        try:
            sock = self._socket_factory(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._send_sock = sock
        except OSError as exc:
            raise CxfDirectError(f"CXFC 发现发送 socket 创建失败：{exc}") from exc
        for port in CXFC_LISTEN_PORTS:
            try:
                rsock = self._socket_factory(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                rsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                rsock.bind(("0.0.0.0", port))
                rsock.settimeout(0.5)
                self._recv_socks.append(rsock)
                thread = threading.Thread(
                    target=self._recv_loop, args=(rsock, port),
                    name=f"cxfc-disc-{port}", daemon=True,
                )
                self._threads.append(thread)
                thread.start()
            except OSError as exc:
                LOGGER.warning("CXFC 发现监听端口 %s 绑定失败（跳过）：%s", port, exc)

    def stop(self):
        """停止并回收全部 socket 与线程（幂等）。"""
        self._stop.set()
        for sock in [self._send_sock, *self._recv_socks]:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self._send_sock = None
        self._recv_socks = []
        for thread in self._threads:
            if thread.is_alive():
                thread.join(timeout=2.0)
        self._threads = []

    def beacon(self):
        """构造本机信标 dict（五字段，对齐 CX-O §3.2）。"""
        return {
            "type": CXFC_BEACON_TYPE,
            "name": self._name,
            "port": self._local_port,
            "capabilities": ["cxfc"],
            "version": "1.0.0",
        }

    def broadcast(self):
        """向双端口广播本机信标（任一失败仅告警，不阻断另一端口）。"""
        if self._send_sock is None:
            raise CxfDirectError("CXFC 发现未启动（先 start()）")
        payload = json.dumps(self.beacon()).encode("utf-8")
        for port in CXFC_BROADCAST_PORTS:
            try:
                self._send_sock.sendto(payload, (self._broadcast_address, port))
            except OSError as exc:
                LOGGER.warning("CXFC 信标广播失败（port=%s）：%s", port, exc)

    def discover_once(self, window_s=2.0):
        """主动发现：广播后在窗口内收包，返回已发现节点快照（去重表副本）。"""
        self.broadcast()
        deadline = time.monotonic() + max(0.0, float(window_s))
        while time.monotonic() < deadline:
            time.sleep(0.1)
        with self._lock:
            return dict(self.discovered)

    def _recv_loop(self, sock, port):
        """单端口收包循环：合法 CXFC_BEACON → 去重登记 + 回调。"""
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = json.loads(data.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if not isinstance(msg, dict) or msg.get("type") != CXFC_BEACON_TYPE:
                continue
            name = str(msg.get("name") or "")
            try:
                beacon_port = int(msg.get("port") or 0)
            except (TypeError, ValueError):
                beacon_port = 0
            host = str(addr[0] or "")
            if not host or beacon_port <= 0:
                continue
            info = {
                "name": name,
                "host": host,
                "port": beacon_port,
                "version": str(msg.get("version") or "1.0.0"),
            }
            with self._lock:
                self.discovered[(host, beacon_port)] = info
            if self._on_discovered is not None:
                try:
                    self._on_discovered(info, addr)
                except Exception as exc:  # noqa: BLE001 - 回调失败不影响接收
                    LOGGER.warning("CXFC 发现回调失败：%s", exc)


class CxfcDirectManager:
    """direct 传输管理（注册/调用/心跳/持久化/发现），线程安全。

    与 :class:`lite.cxfc.lite_cxfc.LiteCXFC` 的 embedded/relay 并存；插件目录
    （tools/skills）由调用方按需登记进宿主工具表（CX-A 由 api_server 装配）。
    """

    def __init__(self, config=None, data_dir=None, local_name="CX-A", local_port=0):
        """构造。

        :param config: ConfigManager / dict（读 ``cxfc`` 段参数）
        :param data_dir: 持久化目录（plugins.json 落 ``<data_dir>/cxfc/``）
        :param local_name / local_port: 本机信标字段
        """
        self.enabled = bool(self._get(config, "cxfc", "enabled", False))
        self.heartbeat_timeout_s = float(
            self._get(config, "cxfc", "heartbeat_timeout_s", DEFAULT_HEARTBEAT_TIMEOUT_S)
        )
        self.heartbeat_check_interval_s = float(
            self._get(config, "cxfc", "heartbeat_check_interval_s", DEFAULT_HEARTBEAT_CHECK_INTERVAL_S)
        )
        self._config = config
        self._data_dir = os.path.join(data_dir or os.getcwd(), "cxfc")
        #: 插件表：plugin_id -> info dict（对齐 CX-O CXFCPluginInfo 字段）
        self._plugins = {}
        #: 插件访问令牌明文（内存代持；库中仅存哈希）
        self._tokens = {}
        self._lock = threading.RLock()
        self._hb_thread = None
        self._hb_stop = threading.Event()
        self._discovery = None

    @staticmethod
    def _get(config, section, key, default):
        """ConfigManager / dict 双形态配置读取（与 LiteCXFC._get_config 同口径）。"""
        if isinstance(config, dict):
            sec = config.get(section)
            return sec.get(key, default) if isinstance(sec, dict) else default
        try:
            return config.get(section, key, default)
        except (TypeError, AttributeError):
            return default

    # ---------------- 注册 / 目录 ---------------- #

    def register_direct(self, host, port, name="", tools=None, capabilities=None,
                        token=None, tls_cert_fingerprint=None, tls_cert_pem=None):
        """direct 注册（对齐 CX-O register_plugin：TOFU + 令牌签发 + 持久化）。

        :return: (plugin_info dict, plugin_access_token 明文或 None)
        :raises CxfDirectError: host/port 非法 / TLS 指纹与 PEM 不一致
        """
        host = str(host or "").strip()
        try:
            port = int(port or 0)
        except (TypeError, ValueError):
            port = 0
        if not host or not (1 <= port <= 65535):
            raise CxfDirectError("direct 注册要求合法 host 与 port（1~65535）")
        if tls_cert_pem and tls_cert_fingerprint:
            actual = _cert_fingerprint(tls_cert_pem)
            expected = str(tls_cert_fingerprint).replace(":", "").upper()
            if actual != expected:
                raise CxfDirectError("TLS 证书指纹与 PEM 不一致，拒绝注册（TOFU）")
        plugin_id = f"cxfc_{host}_{port}"
        access_token = secrets.token_hex(32)
        info = {
            "plugin_id": plugin_id,
            "host": host,
            "port": port,
            "name": str(name or plugin_id),
            "version": "1.0.0",
            "capabilities": list(capabilities or []),
            "status": "connected",
            "transport": "direct",
            "last_seen": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "tools": list(tools or []),
            "skills": [],
            "token": str(token) if token else None,
            "tls_cert_fingerprint": (
                str(tls_cert_fingerprint).replace(":", "").upper()
                if tls_cert_fingerprint else None
            ),
            "tls_cert_pem": tls_cert_pem,
            "plugin_access_token_hash": hashlib.sha256(access_token.encode()).hexdigest(),
        }
        with self._lock:
            if plugin_id in self._plugins:
                LOGGER.warning("direct 插件 %s 重注册，覆盖旧记录（更新不累积）", plugin_id)
            self._plugins[plugin_id] = info
            self._tokens[plugin_id] = access_token
            self._persist_locked()
        return dict(info), access_token

    def list_plugins(self):
        """插件列表（副本；哈希与令牌明文不出现在响应）。"""
        with self._lock:
            out = []
            for info in self._plugins.values():
                copy = dict(info)
                copy.pop("plugin_access_token_hash", None)
                copy.pop("tls_cert_pem", None)
                out.append(copy)
            return out

    def get_plugin(self, plugin_id):
        """按 id 取插件（副本，剥哈希/PEM）；不存在返回 None。"""
        with self._lock:
            info = self._plugins.get(plugin_id)
        if info is None:
            return None
        copy = dict(info)
        copy.pop("plugin_access_token_hash", None)
        copy.pop("tls_cert_pem", None)
        return copy

    def verify_plugin_access(self, bearer, plugin_id=None):
        """校验插件访问令牌（sha256 恒时比对）；命中返回 plugin_id，否则 None。"""
        if not bearer:
            return None
        digest = hashlib.sha256(bearer.encode()).hexdigest()
        with self._lock:
            if plugin_id:
                info = self._plugins.get(plugin_id)
                if info and secrets.compare_digest(
                    digest, str(info.get("plugin_access_token_hash") or "")
                ):
                    return plugin_id
                return None
            for pid, info in self._plugins.items():
                if secrets.compare_digest(digest, str(info.get("plugin_access_token_hash") or "")):
                    return pid
        return None

    def issue_admin_token(self, admin_key):
        """运维旁路判定：X-API-Key 与传入 admin_key 恒时一致（对齐 CX-O admin 旁路）。"""
        if not admin_key:
            return False
        return secrets.compare_digest(str(admin_key), str(self._get(self._config, "cxfc", "remote_admin_key", "") or ""))

    # ---------------- 连接 / 调用 ---------------- #

    def connect_plugin(self, host, port):
        """主动连接：GET /health 探活 → 拉 /tools /skills → 注册（对齐 CX-O connect）。"""
        host, port = str(host or "").strip(), int(port or 0)
        base = f"http://{host}:{port}"
        try:
            status, health = _http_json(f"{base}/health", timeout=HEALTH_PROBE_TIMEOUT_S)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise CxfDirectError(f"插件不可达（{base}/health）：{exc}") from exc
        if status != 200:
            raise CxfDirectError(f"插件 /health 返回 HTTP {status}")
        name = str(health.get("name") or f"cxfc_{host}_{port}")
        version = str(health.get("version") or "1.0.0")
        tools = []
        try:
            _s, tools_body = _http_json(f"{base}/tools")
            tools = list(tools_body.get("tools") or [])
        except (urllib.error.URLError, OSError, ValueError) as exc:
            LOGGER.warning("拉取插件工具清单失败（%s）：%s", base, exc)
        info, _token = self.register_direct(host, port, name=f"{name}@{version}", tools=tools)
        return info

    def call_plugin(self, plugin_id, tool, arguments=None, timeout_s=DIRECT_CALL_TIMEOUT_S):
        """direct 调用：POST /call（Bearer + uuid4 request_id；返回外壳不抛异常）。"""
        with self._lock:
            info = self._plugins.get(plugin_id)
            token = self._tokens.get(plugin_id)
        if info is None or info.get("status") != "connected":
            return {"success": False, "error": f"插件 {plugin_id} 不可用"}
        request_id = uuid.uuid4().hex
        scheme = "https" if info.get("tls_cert_pem") else "http"
        url = f"{scheme}://{info['host']}:{info['port']}/call"
        try:
            _status, body = _http_json(
                url,
                payload={"tool": tool, "arguments": arguments or {}, "request_id": request_id},
                token=str(token) if (token or info.get("token")) else None,
                timeout=float(timeout_s),
                ca_pem=info.get("tls_cert_pem"),
            )
            return body if isinstance(body, dict) else {"success": True, "result": body}
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return {"success": False, "error": str(exc)}

    def heartbeat(self, plugin_id=None, port=None):
        """心跳上行：刷新 last_seen；DISCONNECTED → 复位 connected（重拉清单由调用方做）。"""
        with self._lock:
            target = None
            if plugin_id and plugin_id in self._plugins:
                target = plugin_id
            elif port:
                for pid, info in self._plugins.items():
                    if int(info.get("port") or 0) == int(port):
                        target = pid
                        break
            if target is None:
                return False
            info = self._plugins[target]
            info["last_seen"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            if info.get("status") != "connected":
                info["status"] = "connected"
                LOGGER.info("CXFC 插件 %s 心跳复活，已复位 connected", target)
            self._persist_locked()
            return True

    # ---------------- 心跳巡检 ---------------- #

    def start_heartbeat(self):
        """启动心跳巡检线程（daemon；仅 direct——embedded/relay 无网络不巡检）。"""
        if self._hb_thread is not None and self._hb_thread.is_alive():
            return True
        self._hb_stop.clear()
        thread = threading.Thread(target=self._hb_loop, name="cxfc-heartbeat", daemon=True)
        self._hb_thread = thread
        thread.start()
        return True

    def stop_heartbeat(self):
        """停止巡检线程（幂等）。"""
        self._hb_stop.set()
        thread = self._hb_thread
        self._hb_thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)

    def _hb_loop(self):
        """巡检循环：每 check_interval 秒判定一次超时（对齐 CX-O 仅 direct）。"""
        while not self._hb_stop.wait(max(1.0, self.heartbeat_check_interval_s)):
            try:
                self._hb_sweep()
            except Exception as exc:  # noqa: BLE001 - 巡检失败不 kill 线程
                LOGGER.warning("CXFC 心跳巡检失败：%s", exc)

    def _hb_sweep(self):
        """超时判定：last_seen 超 heartbeat_timeout_s 的 connected 插件置 disconnected。"""
        now = time.time()
        with self._lock:
            for pid, info in list(self._plugins.items()):
                if info.get("transport") != "direct":
                    continue  # 心跳仅对 direct 生效（CX-O L820-824）
                if info.get("status") != "connected":
                    continue
                try:
                    from datetime import datetime

                    last = datetime.fromisoformat(str(info.get("last_seen"))).timestamp()
                except (ValueError, TypeError):
                    last = 0.0
                if now - last > self.heartbeat_timeout_s:
                    info["status"] = "disconnected"
                    LOGGER.info("CXFC direct 插件 %s 心跳超时，置 disconnected", pid)
                    self._persist_locked()

    # ---------------- 发现 ---------------- #

    def start_discovery(self, on_discovered=None):
        """启动 CXFC UDP 发现（双端口广播/监听；幂等）。"""
        if self._discovery is not None:
            return self._discovery
        self._discovery = CxfcDiscovery(
            local_name=str(self._get(self._config, "cxfc", "local_name", "CX-A") or "CX-A"),
            local_port=int(self._get(self._config, "cxfc", "local_http_port", 0) or 0),
            on_discovered=on_discovered,
        )
        self._discovery.start()
        return self._discovery

    def stop_discovery(self):
        """停止发现（幂等）。"""
        if self._discovery is not None:
            self._discovery.stop()
            self._discovery = None

    def discovered_nodes(self):
        """已发现节点快照（副本列表）。"""
        if self._discovery is None:
            return []
        return list(self._discovery.discovered.values())

    # ---------------- 持久化 ---------------- #

    def _persist_locked(self):
        """原子写 plugins.json（须持 _lock；仅存令牌哈希不存明文）。"""
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            target = os.path.join(self._data_dir, "plugins.json")
            tmp = target + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._plugins, fh, ensure_ascii=False)
            os.replace(tmp, target)
        except OSError as exc:
            LOGGER.warning("CXFC 插件持久化失败：%s", exc)

    def load_persisted(self):
        """启动加载持久化插件（明文令牌丢失——重注册/心跳复活时重新签发流程处理）。"""
        target = os.path.join(self._data_dir, "plugins.json")
        try:
            with open(target, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return 0
        if not isinstance(data, dict):
            return 0
        with self._lock:
            self._plugins.update(data)
        return len(data)


class CxfcRemoteClient:
    """远端 CX-O 节点客户端（20261017 批次 5）：跨机插件调用链。

    用户裁决的核心场景：CX-A 发现/手动添加局域网 CX-O 主机 → 拉取其注册的
    插件清单（``GET /cxfc/plugins``，CX-O 该端点无鉴权）→ 跨机调用（
    ``POST /cxfc/plugins/{id}/call`` + ``X-API-Key`` 管理密钥——CX-O 远程调用
    唯一入口，recon_cxfc.md §9 #10）。节点表持久化 ``remote_nodes.json``。

    与 direct 传输的区别：direct 是「插件注册到 CX-A 宿主」（CX-A 为服务端）；
    本类是「CX-A 作为客户端调 CX-O 宿主上的插件」（CX-A 为调用方）。
    """

    def __init__(self, config=None, data_dir=None):
        """构造（admin key 读 ``cxfc.remote_admin_key``；节点表落 ``<data_dir>/cxfc/``）。"""
        self._admin_key = str(CxfcDirectManager._get(config, "cxfc", "remote_admin_key", "") or "")
        self._data_dir = os.path.join(data_dir or os.getcwd(), "cxfc")
        self._nodes = {}
        self._lock = threading.RLock()

    # ---------------- 节点表 ---------------- #

    def add_node(self, host, port, name=""):
        """添加/更新远端 CX-O 节点。返回节点 dict。"""
        host = str(host or "").strip()
        try:
            port = int(port or 0)
        except (TypeError, ValueError):
            port = 0
        if not host or not (1 <= port <= 65535):
            raise CxfDirectError("远端节点要求合法 host 与 port（1~65535）")
        node_id = f"remote_{host}_{port}"
        node = {
            "id": node_id,
            "host": host,
            "port": port,
            "name": str(name or f"CX-O@{host}:{port}"),
            "added_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "plugins": [],          # 最近一次拉取的插件清单缓存
            "plugins_fetched_at": "",
        }
        with self._lock:
            if node_id in self._nodes:
                node["plugins"] = self._nodes[node_id].get("plugins") or []
                node["plugins_fetched_at"] = self._nodes[node_id].get("plugins_fetched_at") or ""
            self._nodes[node_id] = node
            self._persist_locked()
        return dict(node)

    def remove_node(self, node_id):
        """移除节点（幂等）。"""
        with self._lock:
            existed = self._nodes.pop(node_id, None) is not None
            if existed:
                self._persist_locked()
            return existed

    def list_nodes(self):
        """节点列表（副本）。"""
        with self._lock:
            return [dict(n) for n in self._nodes.values()]

    def get_node(self, node_id):
        """按 id 取节点（副本）；不存在 None。"""
        with self._lock:
            node = self._nodes.get(node_id)
        return dict(node) if node else None

    # ---------------- 跨机调用链 ---------------- #

    def _base(self, node):
        """节点基础 URL。"""
        return f"http://{node['host']}:{node['port']}"

    def fetch_plugins(self, node_id):
        """拉取远端节点插件清单（GET /cxfc/plugins；清单缓存进节点条目）。"""
        node = self.get_node(node_id)
        if node is None:
            raise CxfDirectError(f"远端节点不存在：{node_id}")
        try:
            status, body = _http_json(f"{self._base(node)}/cxfc/plugins", timeout=HEALTH_PROBE_TIMEOUT_S)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise CxfDirectError(f"拉取插件清单失败（{node_id}）：{exc}") from exc
        if status != 200:
            raise CxfDirectError(f"远端 /cxfc/plugins 返回 HTTP {status}")
        plugins = list(body.get("plugins") or [])
        with self._lock:
            stored = self._nodes.get(node_id)
            if stored is not None:
                stored["plugins"] = plugins
                stored["plugins_fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                self._persist_locked()
                return list(plugins), dict(stored)
        return plugins, node

    def call_plugin(self, node_id, plugin_id, tool, arguments=None, timeout_s=DIRECT_CALL_TIMEOUT_S):
        """跨机调用远端插件（POST /cxfc/plugins/{id}/call + X-API-Key 管理密钥）。

        :return: CX-O 返回外壳原样 ``{status:"ok", result:{...call_tool 外壳}}``；
            失败以 ``{"success": False, "error": ...}`` 表达（不抛异常）。
        """
        node = self.get_node(node_id)
        if node is None:
            return {"success": False, "error": f"远端节点不存在：{node_id}"}
        if not self._admin_key:
            return {"success": False, "error": "未配置管理密钥（cxfc.remote_admin_key），无法跨机调用"}
        url = f"{self._base(node)}/cxfc/plugins/{urllib.request.quote(str(plugin_id), safe='')}/call"
        try:
            status, body = _http_json(
                url,
                payload={"tool": tool, "arguments": arguments or {}},
                timeout=float(timeout_s),
                extra_headers={"X-API-Key": self._admin_key},
            )
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return {"success": False, "error": f"跨机调用失败：{exc}"}
        if status != 200:
            return {"success": False, "error": f"远端返回 HTTP {status}"}
        return body if isinstance(body, dict) else {"success": True, "result": body}

    # ---------------- 持久化 ---------------- #

    def _persist_locked(self):
        """原子写 remote_nodes.json（须持 _lock）。"""
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            target = os.path.join(self._data_dir, "remote_nodes.json")
            tmp = target + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._nodes, fh, ensure_ascii=False)
            os.replace(tmp, target)
        except OSError as exc:
            LOGGER.warning("CXFC 远端节点持久化失败：%s", exc)

    def load_persisted(self):
        """启动加载远端节点表。"""
        target = os.path.join(self._data_dir, "remote_nodes.json")
        try:
            with open(target, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return 0
        if isinstance(data, dict):
            with self._lock:
                self._nodes.update(data)
            return len(data)
        return 0
