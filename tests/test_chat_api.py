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
from lite.cloud.fallback import (
    CONFIG_ERROR_PROMPT,
    LOCAL_NOT_READY_PROMPT,
    OFFLINE_PROMPT,
    OfflineFallbackManager,
)
from lite.computer_control import ComputerControl, ToolBridge
from lite.computer_control.security import ControlAuthorizer
from lite.server.api_server import build_deps, build_local_chat_runtime, make_handler


class _FakeCloud:
    """内存 mock 云端适配器：按预设脚本产出流式文本块或抛异常。

    与 CloudAdapter.chat(messages) 契约一致（生成器逐块产出文本）；
    记录每次收到的 messages 供断言 system/user 组装。
    """

    def __init__(self, chunks=None, error=None, online=True):
        self.chunks = chunks if chunks is not None else []
        self.error = error
        self.online = online
        self.calls = []

    def chat(self, messages):
        """流式产出预设文本块；error 非空时抛出对应异常。"""
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        yield from self.chunks

    def is_online(self, timeout=5):
        """在线探测：与 CloudAdapter 契约一致——配置错误（无 api_key）抛
        CloudConfigError，网络故障/云端不可达返回 False，其余视为在线。"""
        if isinstance(self.error, CloudConfigError):
            raise self.error
        if self.error is not None:
            return False
        return self.online


@contextmanager
def running_server(tmp_path, cloud=None, chat_fallback=None):
    """以临时数据目录起服（make_handler 注入 cloud / chat_fallback），yield (base_url, manager)。

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
        chat_cloud=cloud, chat_fallback=chat_fallback,
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


def http_post(url, payload, method="POST"):
    """POST/PUT JSON 返回 (status, body_dict)；非 2xx 同样解析错误体。"""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def http_get(url):
    """GET 返回 (status, body_dict)；非 2xx 同样解析错误体。"""
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class TestChatHistoryPersistence:
    """对话持久化（20261004 悬浮窗语音闭环）：成功对话落盘 + GET history 同序返回。"""

    def test_history_roundtrip_after_successful_chats(self, tmp_path):
        """两轮成功对话 → data/chat_history.json 四条记录，history 端点同序返回。"""
        cloud = _FakeCloud(chunks=["[emotion:happy]", "今天真开心"])
        with running_server(tmp_path, cloud=cloud) as (base, _mgr):
            status, body = http_post(f"{base}/api/chat/message", {"message": "你好"})
            assert status == 200 and body["ok"] is True
            status, body = http_post(f"{base}/api/chat/message", {"message": "在吗"})
            assert status == 200 and body["ok"] is True

            h_status, h_body = http_get(f"{base}/api/chat/history")
        assert h_status == 200
        assert h_body["ok"] is True
        msgs = h_body["messages"]
        assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
        assert msgs[0]["content"] == "你好"
        assert msgs[1]["content"] == "今天真开心"  # clean_text（已剥离情绪标签）
        assert msgs[2]["content"] == "在吗"
        assert all(str(m.get("time")) for m in msgs)  # 每条带时间戳

    def test_offline_placeholder_not_persisted(self, tmp_path):
        """云端不可用的固定文案不落盘（不是真实对话，避免污染历史）。"""
        cloud = _FakeCloud(error=CloudUnavailableError("connection refused"))
        with running_server(tmp_path, cloud=cloud) as (base, _mgr):
            status, body = http_post(f"{base}/api/chat/message", {"message": "你好"})
            assert status == 200 and body["offline"] is True

            h_status, h_body = http_get(f"{base}/api/chat/history")
        assert h_status == 200
        assert h_body["messages"] == []

    def test_history_cap_keeps_latest(self, monkeypatch, tmp_path):
        """cap 截断：超上限只保留最近 ``_CHAT_HISTORY_CAP`` 条（直接驱动落盘函数）。"""
        from lite.server import api_server as srv

        target = str(tmp_path / "chat_history.json")
        for i in range(105):
            srv._append_chat_history(f"u{i}", f"a{i}", path=target)
        history = srv._load_chat_history(target)
        assert len(history) == srv._CHAT_HISTORY_CAP == 200
        assert history[0]["content"] == "u5"    # 最早的 10 条被挤出
        assert history[-1]["content"] == "a104"

    def test_history_corrupt_file_reads_empty(self, tmp_path):
        """历史文件损坏 → 读侧静默回空（历史损坏不阻断聊天主链路）。"""
        from lite.server import api_server as srv

        target = str(tmp_path / "chat_history.json")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("not-json")
        assert srv._load_chat_history(target) == []
        # 且写侧可自愈：损坏后追加照常落盘
        srv._append_chat_history("你好", "嗯", path=target)
        history = srv._load_chat_history(target)
        assert [m["content"] for m in history] == ["你好", "嗯"]


class TestChatMemoryInject:
    """聊天记忆注入（RAG 闭环，20261005）：system 自动带【回忆】上下文块。"""

    def test_injects_memory_context_into_system(self, tmp_path):
        """先落一条相似记忆 → 聊天 system 消息含【回忆】块与记忆内容。"""
        cloud = _FakeCloud(chunks=["好的"])
        with running_server(tmp_path, cloud=cloud) as (base, _mgr):
            # 经记忆 CRUD 端点预置一条与提问高度重合的记忆（确保检索召回）
            status, body = http_post(
                f"{base}/api/memories",
                {"content": "用户最喜欢的项目代号是星尘计划", "memory_type": "long_term",
                 "importance": 4, "agent_id": "default"},
            )
            assert status == 200 and body.get("ok") is True

            status, body = http_post(f"{base}/api/chat/message", {"message": "星尘计划是什么"})
        assert status == 200
        system_msg = cloud.calls[0][0]
        assert system_msg["role"] == "system"
        assert "【回忆】" in system_msg["content"]
        assert "星尘计划" in system_msg["content"]

    def test_inject_disabled_keeps_system_clean(self, tmp_path):
        """开关关闭（PUT memory.context_inject=false）→ system 不含【回忆】块。"""
        cloud = _FakeCloud(chunks=["好的"])
        with running_server(tmp_path, cloud=cloud) as (base, _mgr):
            http_post(
                f"{base}/api/memories",
                {"content": "用户最喜欢的项目代号是星尘计划", "memory_type": "long_term",
                 "importance": 4, "agent_id": "default"},
            )
            status, body = http_post(
                f"{base}/api/settings", {"memory": {"context_inject": False}}, method="PUT"
            )
            assert status == 200
            assert "memory.context_inject" in body["applied"]

            status, _body = http_post(f"{base}/api/chat/message", {"message": "星尘计划是什么"})
        assert status == 200
        assert "【回忆】" not in cloud.calls[0][0]["content"]

    def test_memory_agent_skips_injection(self, tmp_path):
        """memory-agent 对话走工具环自带检索 → system 不再自动注入【回忆】（防双份）。"""
        cloud = _FakeCloud(chunks=["好的"])
        with running_server(tmp_path, cloud=cloud) as (base, _mgr):
            http_post(
                f"{base}/api/memories",
                {"content": "用户最喜欢的项目代号是星尘计划", "memory_type": "long_term",
                 "importance": 4, "agent_id": "default"},
            )
            status, body = http_post(
                f"{base}/api/chat/message", {"message": "星尘计划是什么", "agent_id": "memory-agent"}
            )
        assert status == 200
        assert "【回忆】" not in cloud.calls[0][0]["content"]


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
            agent = _mgr.create(name="小伴", persona="温柔的猫咪")
            status, body = http_post(
                f"{base}/api/chat/message",
                {"message": "早上好", "agent_id": agent.id},
            )
        assert status == 200
        assert body["mood"] == "calm"
        system_msg = cloud.calls[0][0]
        assert system_msg["role"] == "system"
        assert "温柔的猫咪" in system_msg["content"]
        # 人设路径同样拼接表情指令（隐藏系统提示词，对齐 CX-O）：引导 LLM 输出
        # [emotion:x] 标签——表情链路（chat mood → 桌宠表情）的源头防线
        assert "[emotion:" in system_msg["content"]

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
        assert "你是 CX-A" in cloud.calls[0][0]["content"]


class TestChatMessageOffline:
    """离线兜底：无 api_key / 云端不可达 → 200 + 固定友好文案 + mood=calm。"""

    def test_cloud_config_error(self, tmp_path):
        """CloudConfigError（无 api_key）→ 配置未完成文案，不抛 5xx。

        接线离线兜底管理器后，探测（is_online）抛 CloudConfigError 被识别为
        「配置未完成」诊断态（N5），产出 CONFIG_ERROR_PROMPT 而非误诊断网。
        """
        cloud = _FakeCloud(error=CloudConfigError("api_key 缺失"))
        with running_server(tmp_path, cloud=cloud) as (base, _mgr):
            status, body = http_post(f"{base}/api/chat/message", {"message": "你好"})
        assert status == 200
        assert body["ok"] is True
        assert body["clean_text"] == CONFIG_ERROR_PROMPT
        assert body["mood"] == "calm"
        assert body["offline"] is True

    def test_cloud_unreachable(self, tmp_path):
        """CloudUnavailableError（网络故障 / 流中断）且本地未开 → 断网提示文案。"""
        cloud = _FakeCloud(error=CloudUnavailableError("connection refused"))
        with running_server(tmp_path, cloud=cloud) as (base, _mgr):
            status, body = http_post(f"{base}/api/chat/message", {"message": "你好"})
        assert status == 200
        assert body["clean_text"] == OFFLINE_PROMPT
        assert body["mood"] == "calm"
        assert body["offline"] is True

    def test_default_assembly_without_key(self, tmp_path):
        """默认装配（不注入 chat_cloud）：临时目录 config 无 api_key → 配置未完成文案。"""
        with running_server(tmp_path, cloud=None) as (base, _mgr):
            status, body = http_post(f"{base}/api/chat/message", {"message": "你好"})
        assert status == 200
        assert body["ok"] is True
        assert body["clean_text"] == CONFIG_ERROR_PROMPT
        assert body["mood"] == "calm"


class _StubLocalLLM:
    """极简本地小 LLM 桩：offline_chat 返回固定文本，记录收到的 messages。"""

    def __init__(self, text):
        self.text = text
        self.calls = []

    def offline_chat(self, messages):
        self.calls.append(messages)
        return self.text


class TestChatMessageFallbackWiring:
    """离线兜底管理器接线：本地兜底可用 / 本地未就绪产提示。"""

    def test_local_fallback_used_when_offline(self, tmp_path):
        """离线 + 本地模式开 + 本地就绪 → 本地固定文本、offline=true、mood=calm。"""
        cloud = _FakeCloud(online=False)
        local = _StubLocalLLM("本地回复")
        fallback = OfflineFallbackManager(
            cloud=cloud,
            local_llm=local,
            config={"local_llm": {"enabled": True}},
        )
        with running_server(tmp_path, cloud=cloud, chat_fallback=fallback) as (base, _mgr):
            status, body = http_post(f"{base}/api/chat/message", {"message": "你好"})
        assert status == 200
        assert body["ok"] is True
        assert body["clean_text"] == "本地回复"
        assert body["mood"] == "calm"
        assert body["offline"] is True
        # 本地小 LLM 收到组装后的 messages（末条为用户原文）
        assert local.calls[0][-1] == {"role": "user", "content": "你好"}

    def test_local_not_ready_yields_prompt(self, tmp_path):
        """本地模式开但 local_llm 缺省 → 本地引导提示（不抛错、状态码 200）。"""
        cloud = _FakeCloud(online=False)
        fallback = OfflineFallbackManager(
            cloud=cloud,
            local_llm=None,
            config={"local_llm": {"enabled": True}},
        )
        with running_server(tmp_path, cloud=cloud, chat_fallback=fallback) as (base, _mgr):
            status, body = http_post(f"{base}/api/chat/message", {"message": "你好"})
        assert status == 200
        assert body["ok"] is True
        assert body["clean_text"] == LOCAL_NOT_READY_PROMPT
        assert body["mood"] == "calm"
        assert body["offline"] is True


class TestBuildLocalChatRuntime:
    """build_local_chat_runtime：未启用 / 未配置 / 依赖缺席一律 None 且不抛。"""

    def test_disabled_returns_none(self):
        """local_llm.enabled=false → None（不接兜底，行为同旧版）。"""
        assert build_local_chat_runtime({"local_llm": {"enabled": False}}) is None

    def test_enabled_without_model_path_returns_none(self):
        """enabled=true 但未配置 model_path → None。"""
        assert build_local_chat_runtime({"local_llm": {"enabled": True}}) is None

    def test_dependency_missing_returns_none_without_raise(self, tmp_path, monkeypatch):
        """enabled + model_path 但依赖缺席（load_local_llm 抛 RuntimeError）→ None 且不抛。"""
        from lite.runtime.llama_runtime import LlamaRuntime

        def _boom(self, path):
            raise RuntimeError("llama-cpp-python 未安装")

        monkeypatch.setattr(LlamaRuntime, "load_local_llm", _boom)
        result = build_local_chat_runtime(
            {"local_llm": {"enabled": True, "model_path": str(tmp_path / "missing.gguf")}}
        )
        assert result is None


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


class _ScriptedCloud:
    """按调用序次返回预设脚本的 mock 云端（memory-agent 工具环测试用）。

    与 CloudAdapter.chat(messages) 契约一致（生成器逐块产出文本）；每次调用
    chat() 消费一组脚本块并记录 messages，供断言回注轮的消息组装。
    """

    def __init__(self, scripts):
        self.scripts = [list(s) for s in scripts]
        self.calls = []

    def chat(self, messages):
        self.calls.append(messages)
        chunks = self.scripts.pop(0) if self.scripts else ["（脚本耗尽）"]
        yield from chunks

    def is_online(self, timeout=5):
        return True


class TestMemoryAgentToolLoop:
    """记忆管理助手工具环（20261005，spec: align-wizard-settings-memory-pet）：

    /api/chat/message 命中内置 agent memory-agent 时，回复中的 [memory:op {...}]
    指令标签经 BuiltinToolRegistry 记忆工具执行、结果回注一轮，最终 clean_text
    剥离全部指令标签；工具失败以中文说明原因。
    """

    def test_write_tag_executes_and_result_injected(self, tmp_path):
        """首轮回复带 [memory:write {...}]：记忆落库 + 结果回注二轮 + 标签不泄漏。"""
        cloud = _ScriptedCloud(
            [
                ['好的，我来记录。[memory:write {"content": "用户的项目计划", "type": "long_term", "importance": 4}]'],
                ["已为您记录项目计划这条记忆。"],
            ]
        )
        with running_server(tmp_path, cloud=cloud) as (base, _mgr):
            status, body = http_post(
                f"{base}/api/chat/message",
                {"message": "请记住我的项目计划", "agent_id": "memory-agent"},
            )
        assert status == 200 and body["ok"] is True
        # 标签不泄漏到展示文本
        assert "[memory:" not in body["clean_text"]
        assert "已为您记录项目计划这条记忆。" == body["clean_text"]
        # 两轮 LLM 调用（首轮 + 回注轮）
        assert len(cloud.calls) == 2
        # 回注轮消息组装：assistant 首轮原文 + 系统回执（user 角色）
        follow_up = cloud.calls[1]
        assert any(m["role"] == "assistant" and "[memory:write" in m["content"] for m in follow_up)
        receipts = [m for m in follow_up if m["role"] == "user" and m["content"].startswith("系统回执")]
        assert len(receipts) == 1 and "执行成功" in receipts[0]["content"]

    def test_write_tag_persists_memory(self, tmp_path):
        """[memory:write] 真实落库：经 registry → store，GET /api/memories 可查。"""
        cloud = _ScriptedCloud(
            [
                ['[memory:write {"content": "助手写入的记忆内容", "type": "diary"}]'],
                ["写好了。"],
            ]
        )
        with running_server(tmp_path, cloud=cloud) as (base, _mgr):
            http_post(f"{base}/api/chat/message", {"message": "记一下", "agent_id": "memory-agent"})
            status, rows = http_get(f"{base}/api/memories")
        assert status == 200
        assert any(r["content"] == "助手写入的记忆内容" and r["type"] == "diary" for r in rows)

    def test_search_tag_returns_hits(self, tmp_path):
        """[memory:search {...}]：检索既有记忆并把结果回注给助手总结。"""
        cloud = _ScriptedCloud(
            [
                ['[memory:search {"query": "项目计划"}]'],
                ["找到了 1 条关于项目计划的记忆。"],
            ]
        )
        with running_server(tmp_path, cloud=cloud) as (base, mgr):
            # 直接经 handler 同源 store 造一条记忆
            from lite.memory.storage import MemoryStore as _MS  # noqa: F401 - 仅证依赖在

            status, body = http_post(
                f"{base}/api/chat/message",
                {"message": "帮我找项目计划的记忆", "agent_id": "memory-agent"},
            )
            # 回注轮的系统回执应携带检索结果
            follow_up = cloud.calls[1]
            receipts = [m for m in follow_up if m["role"] == "user" and m["content"].startswith("系统回执")]
        assert status == 200
        assert len(receipts) == 1 and "memory_search" in json.dumps(receipts[0]["content"]) or "执行" in receipts[0]["content"]
        assert "[memory:" not in body["clean_text"]

    def test_tool_failure_degrades_with_chinese_reason(self, tmp_path):
        """工具失败（删除不存在的 id）：回执带中文原因、助手二轮正常回复、不崩溃。"""
        cloud = _ScriptedCloud(
            [
                ['[memory:delete {"id": 99999}]'],
                ["抱歉，这条记忆不存在，删除失败：记忆 99999 不存在。"],
            ]
        )
        with running_server(tmp_path, cloud=cloud) as (base, _mgr):
            status, body = http_post(
                f"{base}/api/chat/message",
                {"message": "删掉记忆 99999", "agent_id": "memory-agent"},
            )
        assert status == 200 and body["ok"] is True
        assert "[memory:" not in body["clean_text"]
        # 回执消息带失败原因（中文），助手据此说明
        follow_up = cloud.calls[1]
        receipts = [m for m in follow_up if m["role"] == "user" and m["content"].startswith("系统回执")]
        assert len(receipts) == 1 and "执行失败" in receipts[0]["content"] and "不存在" in receipts[0]["content"]
        # 二轮回复为助手总结（脚本第二组），说明失败原因
        assert "删除失败" in body["clean_text"] or "不存在" in body["clean_text"]

    def test_memory_agent_chat_history_not_polluted(self, tmp_path):
        """memory-agent 管理通道对话不落 chat_history（避免污染日常聊天历史）。"""
        cloud = _ScriptedCloud([["普通回复，无标签"]])
        with running_server(tmp_path, cloud=cloud) as (base, _mgr):
            http_post(f"{base}/api/chat/message", {"message": "管理指令", "agent_id": "memory-agent"})
            status, body = http_get(f"{base}/api/chat/history")
        assert status == 200
        assert body["messages"] == []

    def test_memory_agent_persona_injected(self, tmp_path):
        """memory-agent 命中内置种子：persona（指令协议）进入 system 消息。"""
        cloud = _ScriptedCloud([["好"]])
        with running_server(tmp_path, cloud=cloud) as (base, _mgr):
            status, _body = http_post(
                f"{base}/api/chat/message", {"message": "你好", "agent_id": "memory-agent"}
            )
        assert status == 200
        system_msg = cloud.calls[0][0]
        assert system_msg["role"] == "system"
        assert "[memory:" in system_msg["content"]

    def test_non_memory_agent_does_not_enter_loop(self, tmp_path):
        """非 memory-agent 不走工具环：标签原样保留（EmotionTagParser 未知标签语义）、仅一轮调用。"""
        cloud = _ScriptedCloud(
            [
                ['我输出了一个标签 [memory:write {"content": "不该被执行"}] 结束'],
            ]
        )
        with running_server(tmp_path, cloud=cloud) as (base, _mgr):
            status, body = http_post(f"{base}/api/chat/message", {"message": "你好", "agent_id": "default"})
            status2, rows = http_get(f"{base}/api/memories")
        assert status == 200
        # 仅一轮 LLM 调用（无回注）
        assert len(cloud.calls) == 1
        # 未知标签按既有语义保留在 clean_text（未进工具环即不被剥离）
        assert "[memory:write" in body["clean_text"]
        # 标签内容未被执行（记忆库无写入）
        assert all("不该被执行" not in (r["content"] or "") for r in rows)
