# -*- coding: utf-8 -*-
"""表情聊天端点集成测试（Task H3）：真实起服 + mock 云端注入。

对 POST /api/chat/message 全链路验证：云端成功（流式拼接 + 标签解析）、
未知标签保留、CloudConfigError / CloudUnavailableError 离线兜底、默认装配
（无 key）兜底、参数校验与既有守卫端点零回归。
"""

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import HTTPServer

from lite.cloud.adapter import CloudConfigError, CloudUnavailableError
from lite.computer_control import ComputerControl, ToolBridge
from lite.computer_control.security import ControlAuthorizer
from lite.server.api_server import CHAT_OFFLINE_TEXT, build_deps, make_handler


class _FakeCloud:
    """内存 mock 云端适配器：按预设脚本产出流式文本块或抛异常。

    与 CloudAdapter.chat(messages) 契约一致（生成器逐块产出文本）；
    记录每次收到的 messages 供断言 system/user 组装。
    """

    def __init__(self, chunks=None, error=None):
        self.chunks = chunks if chunks is not None else []
        self.error = error
        self.calls = []

    def chat(self, messages):
        """流式产出预设文本块；error 非空时抛出对应异常。"""
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        yield from self.chunks


@contextmanager
def running_server(tmp_path, cloud=None):
    """以临时数据目录起服（make_handler 注入 cloud），yield (base_url, manager)。

    computer 三件套显式构建于 tmp_path，避免 make_handler 默认构建落盘项目根 data/；
    manager 为 handler 实际绑定的 AgentManager（测试直接在其实例上造 Agent，
    避免二次 build_deps 产生不同步的实例）。
    """
    store, pipeline, manager, _remote = build_deps(data_dir=str(tmp_path))
    authorizer = ControlAuthorizer(data_dir=str(tmp_path))
    computer = ComputerControl(authorized=authorizer.is_authorized())
    bridge = ToolBridge(computer=computer, authorizer=authorizer)
    handler = make_handler(
        store, pipeline, manager,
        computer=computer, authorizer=authorizer, bridge=bridge,
        chat_cloud=cloud,
    )
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", manager
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def http_post(url, payload):
    """POST JSON 返回 (status, body_dict)；非 2xx 同样解析错误体。"""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class TestChatMessageSuccess:
    """云端成功路径：流式拼接 + EmotionTagParser 解析。"""

    def test_stream_concat_and_parse(self, tmp_path):
        """多块流式拼接后解析：clean_text 剥离标签、mood 命中、raw 保留原文。"""
        cloud = _FakeCloud(chunks=["[emotion:happy]", "今天真开心"])
        with running_server(tmp_path, cloud=cloud) as (base, _mgr):
            status, body = http_post(f"{base}/api/chat/message", {"message": "你好"})
        assert status == 200
        assert body["ok"] is True
        assert body["clean_text"] == "今天真开心"
        assert body["mood"] == "happy"
        assert body["raw"] == "[emotion:happy]今天真开心"
        # user 消息为用户原文输入
        assert cloud.calls[0][-1] == {"role": "user", "content": "你好"}

    def test_unknown_tag_kept_via_api(self, tmp_path):
        """未知情绪标签经端点原文保留（对齐 spec Scenario「未知情绪降级」）。"""
        cloud = _FakeCloud(chunks=["[emotion:confused]", "嗯？"])
        with running_server(tmp_path, cloud=cloud) as (base, _mgr):
            status, body = http_post(f"{base}/api/chat/message", {"message": "在吗"})
        assert status == 200
        assert body["clean_text"] == "[emotion:confused]嗯？"
        assert body["mood"] == "calm"

    def test_agent_persona_injected_into_system(self, tmp_path):
        """agent_id 命中本地 Agent：persona 进入 system 消息。"""
        cloud = _FakeCloud(chunks=["好"])
        with running_server(tmp_path, cloud=cloud) as (base, _mgr):
            # 造一个本地 Agent（直接用 handler 绑定的同一 AgentManager 实例）
            agent = _mgr.create(name="小伴", persona="温柔的猫咪伴侣")
            status, body = http_post(
                f"{base}/api/chat/message",
                {"message": "早上好", "agent_id": agent.id},
            )
        assert status == 200
        assert body["mood"] == "calm"
        system_msg = cloud.calls[0][0]
        assert system_msg["role"] == "system"
        assert "温柔的猫咪伴侣" in system_msg["content"]

    def test_unknown_agent_id_falls_back_default(self, tmp_path):
        """agent_id 未命中：不报错，回落默认 system 提示。"""
        cloud = _FakeCloud(chunks=["[emotion:calm]好"])
        with running_server(tmp_path, cloud=cloud) as (base, _mgr):
            status, body = http_post(
                f"{base}/api/chat/message", {"message": "hi", "agent_id": "no-such"}
            )
        assert status == 200
        assert body["clean_text"] == "好"
        assert body["mood"] == "calm"
        # system 为默认提示（不含 persona 前缀）
        assert "虚拟伴侣" in cloud.calls[0][0]["content"]


class TestChatMessageOffline:
    """离线兜底：无 api_key / 云端不可达 → 200 + 固定友好文案 + mood=calm。"""

    def test_cloud_config_error(self, tmp_path):
        """CloudConfigError（无 api_key）→ 离线文案，不抛 5xx。"""
        cloud = _FakeCloud(error=CloudConfigError("api_key 缺失"))
        with running_server(tmp_path, cloud=cloud) as (base, _mgr):
            status, body = http_post(f"{base}/api/chat/message", {"message": "你好"})
        assert status == 200
        assert body["ok"] is True
        assert body["clean_text"] == CHAT_OFFLINE_TEXT
        assert body["mood"] == "calm"
        assert body["offline"] is True

    def test_cloud_unreachable(self, tmp_path):
        """CloudUnavailableError（网络故障 / 流中断）→ 离线文案。"""
        cloud = _FakeCloud(error=CloudUnavailableError("connection refused"))
        with running_server(tmp_path, cloud=cloud) as (base, _mgr):
            status, body = http_post(f"{base}/api/chat/message", {"message": "你好"})
        assert status == 200
        assert body["clean_text"] == CHAT_OFFLINE_TEXT
        assert body["mood"] == "calm"
        assert body["offline"] is True

    def test_default_assembly_without_key(self, tmp_path):
        """默认装配（不注入 chat_cloud）：临时目录 config 无 api_key → 离线兜底。"""
        with running_server(tmp_path, cloud=None) as (base, _mgr):
            status, body = http_post(f"{base}/api/chat/message", {"message": "你好"})
        assert status == 200
        assert body["ok"] is True
        assert body["clean_text"] == CHAT_OFFLINE_TEXT
        assert body["mood"] == "calm"


class TestChatMessageValidation:
    """参数校验与既有端点零回归。"""

    def test_empty_message_400(self, tmp_path):
        """空 message → 400。"""
        with running_server(tmp_path, cloud=_FakeCloud()) as (base, _mgr):
            status, body = http_post(f"{base}/api/chat/message", {"message": "   "})
        assert status == 400
        assert body["ok"] is False

    def test_bad_json_400(self, tmp_path):
        """非 JSON 请求体 → 400 bad_json。"""
        import urllib.request

        with running_server(tmp_path, cloud=_FakeCloud()) as (base, _mgr):
            req = urllib.request.Request(
                f"{base}/api/chat/message",
                data=b"not-json",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status = resp.status
                    body = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                status = exc.code
                body = json.loads(exc.read().decode("utf-8"))
        assert status == 400
        assert body["error"] == "bad_json"

    def test_guard_endpoint_unchanged(self, tmp_path):
        """既有 /api/chat/messages（复数）守卫端点行为零回归。"""
        with running_server(tmp_path, cloud=_FakeCloud()) as (base, _mgr):
            status, body = http_post(f"{base}/api/chat/messages", {"content": "你好"})
        assert status == 200
        assert body["ok"] is False
        assert body["error"] == "chat_service_disabled"
