# -*- coding: utf-8 -*-
"""LlamaServerEmbedder 单测（纯替身：绝不启真进程、绝不触网）。

覆盖：冷启动 argv 精确断言 / 就绪轮询（503→200）/ 文件缺失与中途退出 /
就绪超时 / embed 保序与长度一致性 / 连接异常自动重启重试 / 重试仍失败 /
条数不匹配与空 data / dim 探针 / close 幂等 / running 状态联动。

纪律：全程注入 ``popen_factory`` 与 ``http_factory`` 两个替身，不调用默认实现
（不 subprocess.Popen、不 urllib 触网）；唯一真实动作是取空闲端口时的本机
回环 ``socket.bind``（属纯本地探测，非网络请求）。
"""

import json

import pytest

from lite.runtime.llama_server import LlamaServerEmbedder


# ------------------------------------------------------------------ #
# 替身：假子进程 / 假 popen 工厂 / 假 http 工厂                         #
# ------------------------------------------------------------------ #

class _FakeProc:
    """假子进程对象：满足 poll / terminate / kill / wait / returncode 契约。"""

    def __init__(self, returncode=None):
        """构造假进程；``returncode`` 非 None 时模拟「启动即已退出」。"""
        self.returncode = returncode
        self.terminated = 0
        self.killed = 0
        self.waited = []

    def poll(self):
        """返回当前退出码（None 表示仍在运行）。"""
        return self.returncode

    def terminate(self):
        """模拟 terminate：置退出码 -15。"""
        self.terminated += 1
        self.returncode = -15

    def kill(self):
        """模拟 kill：置退出码 -9。"""
        self.killed += 1
        self.returncode = -9

    def wait(self, timeout=None):
        """记录等待超时并返回退出码。"""
        self.waited.append(timeout)
        return self.returncode


class _PopenRecorder:
    """假 popen 工厂：记录每次 argv，按队列或每次新建返回假进程。"""

    def __init__(self, procs=None):
        """预置进程队列（可选）；队列耗尽后每次新建假进程。"""
        self.calls = []
        self._procs = list(procs or [])

    def __call__(self, argv):
        """记录 argv 并返回假进程。"""
        self.calls.append(list(argv))
        if self._procs:
            return self._procs.pop(0)
        return _FakeProc()


class _FakeHttp:
    """假 http 工厂：按 URL 分流到 health / tokenize / embed 处理函数并记录调用。"""

    def __init__(self, health=None, embed=None, tokenize=None):
        """注入 health / tokenize / embed 处理函数。

        tokenize 缺省返回"无 tokens 字段"的 200（等效端点不可用 → 走启发式回退，
        保持既有用例的语义不变）；显式注入可演练真实分词口径。
        """
        self._health = health or (lambda: (200, b'{"status":"ok"}'))
        self._embed = embed or (lambda _payload: (200, _emb_body([[0.1, 0.2]])))
        self._tokenize = tokenize or (lambda _payload: (200, b'{"no_tokens": true}'))
        self.calls = []

    def __call__(self, url, payload, timeout):
        """记录调用并返回处理函数的 ``(status, body)``（处理函数可抛异常）。"""
        self.calls.append((url, payload, timeout))
        if url.endswith("/health"):
            return self._health()
        if url.endswith("/tokenize"):
            return self._tokenize(payload)
        return self._embed(payload)


def _emb_body(vectors, with_index=False, index_order=None):
    """构造 ``/v1/embeddings`` 响应体 bytes。

    :param vectors: 向量列表
    :param with_index: 是否写入 index 字段
    :param index_order: 显式指定各条 index（用于乱序场景）
    """
    data = []
    for pos, vector in enumerate(vectors):
        item = {"embedding": list(vector)}
        if with_index:
            item["index"] = index_order[pos] if index_order else pos
        data.append(item)
    return json.dumps({"data": data}).encode("utf-8")


@pytest.fixture
def files(tmp_path):
    """在临时目录落盘假 exe 与假 gguf，返回 ``(exe_path, model_path)``。"""
    exe = tmp_path / "llama-server.exe"
    exe.write_bytes(b"fake-exe")
    model = tmp_path / "model.gguf"
    model.write_bytes(b"fake-gguf")
    return str(exe), str(model)


def _embedder(files, popen, http, **kwargs):
    """按注入替身构造嵌入器。"""
    exe, model = files
    return LlamaServerEmbedder(
        exe, model, popen_factory=popen, http_factory=http, **kwargs
    )


# ------------------------------------------------------------------ #
# 1. 冷启动 argv 精确断言                                              #
# ------------------------------------------------------------------ #

def test_cold_start_argv_exact(files):
    """冷启动 argv 严格为规定形状；就绪后再次 ensure_started 幂等。"""
    popen = _PopenRecorder()
    http = _FakeHttp()
    embedder = _embedder(files, popen, http)

    embedder.ensure_started()

    assert len(popen.calls) == 1
    argv = popen.calls[0]
    exe, model = files
    assert argv[:9] == [
        exe, "-m", model, "--embeddings", "--pooling", "last",
        "--host", "127.0.0.1", "--port",
    ]
    assert argv[9].isdigit() and 1 <= int(argv[9]) <= 65535
    assert argv[10:] == ["-c", "2048", "-ngl", "0", "--no-webui"]
    assert len(argv) == 15
    assert " ".join(argv).count("--embeddings") == 1

    # 幂等：进程存活时不重复拉起
    embedder.ensure_started()
    assert len(popen.calls) == 1
    assert embedder.running is True


# ------------------------------------------------------------------ #
# 2. 就绪轮询                                                          #
# ------------------------------------------------------------------ #

def test_ready_polling_loading_then_ok(files):
    """health 先 503（loading）后 200（ok）：轮询至就绪成功。"""
    state = {"n": 0}

    def health():
        state["n"] += 1
        if state["n"] == 1:
            return 503, b'{"status":"loading"}'
        return 200, b'{"status":"ok"}'

    proc = _FakeProc()
    popen = _PopenRecorder([proc])
    embedder = _embedder(files, popen, _FakeHttp(health=health))

    embedder.ensure_started()

    assert state["n"] >= 2  # 至少轮询两轮
    assert embedder.running is True
    assert proc.terminated == 0  # 就绪后未被清理


# ------------------------------------------------------------------ #
# 3. 文件缺失 / 进程中途退出                                           #
# ------------------------------------------------------------------ #

def test_missing_exe_raises_chinese_error(tmp_path):
    """exe 文件缺失 → 中文 RuntimeError，且不拉起进程。"""
    model = tmp_path / "model.gguf"
    model.write_bytes(b"gguf")
    popen = _PopenRecorder()
    embedder = LlamaServerEmbedder(
        str(tmp_path / "nope.exe"), str(model),
        popen_factory=popen, http_factory=_FakeHttp(),
    )
    with pytest.raises(RuntimeError) as ei:
        embedder.ensure_started()
    assert "不存在" in str(ei.value)
    assert popen.calls == []


def test_missing_model_raises_chinese_error(files, tmp_path):
    """model 文件缺失 → 中文 RuntimeError，且不拉起进程。"""
    exe, _model = files
    popen = _PopenRecorder()
    embedder = LlamaServerEmbedder(
        exe, str(tmp_path / "nope.gguf"),
        popen_factory=popen, http_factory=_FakeHttp(),
    )
    with pytest.raises(RuntimeError) as ei:
        embedder.ensure_started()
    assert "不存在" in str(ei.value)
    assert popen.calls == []


def test_process_exits_during_startup_cleans_reference(files):
    """进程中途退出 → RuntimeError 含 returncode，且引用被清理。"""
    dead = _FakeProc(returncode=7)
    popen = _PopenRecorder([dead])
    embedder = _embedder(files, popen, _FakeHttp())

    with pytest.raises(RuntimeError) as ei:
        embedder.ensure_started()

    assert "returncode=7" in str(ei.value)
    assert embedder._proc is None
    assert embedder.running is False


# ------------------------------------------------------------------ #
# 4. 就绪超时                                                          #
# ------------------------------------------------------------------ #

def test_ready_timeout_terminates_process(files):
    """ready_timeout=0.3 且 health 恒失败 → RuntimeError + 进程被 terminate。"""
    proc = _FakeProc()
    popen = _PopenRecorder([proc])
    http = _FakeHttp(health=lambda: (503, b'{"status":"loading"}'))
    embedder = _embedder(files, popen, http, ready_timeout=0.3)

    with pytest.raises(RuntimeError) as ei:
        embedder.ensure_started()

    assert "超时" in str(ei.value)
    assert proc.terminated >= 1
    assert embedder._proc is None
    assert embedder.running is False


# ------------------------------------------------------------------ #
# 5. embed 正常返回 / 保序 / 长度一致                                  #
# ------------------------------------------------------------------ #

def test_embed_returns_vectors_with_index_reorder(files):
    """响应带 index 且乱序 → 按 index 排序保序，逐条转 float，长度一致。"""
    body = _emb_body(
        [[0.5, 0.5], [1.0, 0.0], [0.0, 1.0]],
        with_index=True, index_order=[2, 0, 1],
    )
    http = _FakeHttp(embed=lambda _p: (200, body))
    embedder = _embedder(files, _PopenRecorder(), http)

    out = embedder.embed(["a", "b", "c"])

    assert out == [[1.0, 0.0], [0.0, 1.0], [0.5, 0.5]]
    assert all(isinstance(v, float) for v in out[0])
    assert len(out) == 3
    # 请求体形状：POST /v1/embeddings + {"input": [...]}，UTF-8
    url, payload, _timeout = http.calls[-1]
    assert url.endswith("/v1/embeddings")
    assert json.loads(payload.decode("utf-8")) == {"input": ["a", "b", "c"]}


def test_embed_without_index_keeps_response_order(files):
    """响应无 index → 维持原顺序（不排序）。"""
    body = _emb_body([[1.0], [2.0], [3.0]])
    http = _FakeHttp(embed=lambda _p: (200, body))
    embedder = _embedder(files, _PopenRecorder(), http)

    assert embedder.embed(["x", "y", "z"]) == [[1.0], [2.0], [3.0]]


def test_embed_rejects_empty_input(files):
    """texts 为空列表 / 非法类型 → 中文 RuntimeError，不触发进程。"""
    popen = _PopenRecorder()
    embedder = _embedder(files, popen, _FakeHttp())
    with pytest.raises(RuntimeError):
        embedder.embed([])
    with pytest.raises(RuntimeError):
        embedder.embed("not-a-list")
    assert popen.calls == []


# ------------------------------------------------------------------ #
# 6. embed 首次失败 → 自动重启重试                                     #
# ------------------------------------------------------------------ #

def test_embed_retries_after_restart_on_connection_error(files):
    """首次连接异常 → 自动重启并重试成功（popen 被调用两次）。"""
    state = {"n": 0}

    def embed(_payload):
        state["n"] += 1
        if state["n"] == 1:
            raise ConnectionError("模拟连接失败")
        return 200, _emb_body([[1.0, 2.0, 3.0]])

    popen = _PopenRecorder()
    embedder = _embedder(files, popen, _FakeHttp(embed=embed))

    out = embedder.embed(["hi"])

    assert out == [[1.0, 2.0, 3.0]]
    assert len(popen.calls) == 2  # 初次启动 + 重启一次
    assert embedder.running is True


# ------------------------------------------------------------------ #
# 7. embed 重试仍失败                                                  #
# ------------------------------------------------------------------ #

def test_embed_retry_still_fails_raises_chinese_error(files):
    """HTTP 非 200 且重试仍失败 → 中文 RuntimeError（含「已重启重试」）。"""
    http = _FakeHttp(embed=lambda _p: (500, b'{"error":"boom"}'))
    popen = _PopenRecorder()
    embedder = _embedder(files, popen, http)

    with pytest.raises(RuntimeError) as ei:
        embedder.embed(["hi"])

    message = str(ei.value)
    assert "已重启重试" in message
    assert "HTTP 500" in message
    assert "boom" in message
    assert len(popen.calls) == 2
    assert embedder._proc is None  # 最终失败后已清理


# ------------------------------------------------------------------ #
# 8. 长度不匹配 / 空 data                                              #
# ------------------------------------------------------------------ #

def test_embed_length_mismatch_raises(files):
    """返回条数与输入不等长 → RuntimeError（含「不一致」）。"""
    http = _FakeHttp(embed=lambda _p: (200, _emb_body([[1.0]])))
    embedder = _embedder(files, _PopenRecorder(), http)

    with pytest.raises(RuntimeError) as ei:
        embedder.embed(["a", "b"])
    assert "不一致" in str(ei.value)


def test_embed_empty_data_raises(files):
    """data 为空数组 → RuntimeError（含「data」）。"""
    http = _FakeHttp(embed=lambda _p: (200, b'{"data": []}'))
    embedder = _embedder(files, _PopenRecorder(), http)

    with pytest.raises(RuntimeError) as ei:
        embedder.embed(["a"])
    assert "data" in str(ei.value)


def test_embed_empty_embedding_entry_raises(files):
    """条目 embedding 为空列表 → RuntimeError（含「空条目」）。"""
    http = _FakeHttp(embed=lambda _p: (200, _emb_body([[]])))
    embedder = _embedder(files, _PopenRecorder(), http)

    with pytest.raises(RuntimeError) as ei:
        embedder.embed(["a"])
    assert "空条目" in str(ei.value)


# ------------------------------------------------------------------ #
# 9. dim 探针                                                          #
# ------------------------------------------------------------------ #

def test_dim_probe_returns_length(files):
    """dim() 经探针文本取首条向量长度；探针文本透传请求体。"""
    http = _FakeHttp(embed=lambda _p: (200, _emb_body([[0.0] * 1024])))
    embedder = _embedder(files, _PopenRecorder(), http)

    assert embedder.dim() == 1024
    assert json.loads(http.calls[-1][1].decode("utf-8"))["input"] == ["ping"]

    assert embedder.dim("你好") == 1024
    assert json.loads(http.calls[-1][1].decode("utf-8"))["input"] == ["你好"]


# ------------------------------------------------------------------ #
# 10. close 幂等                                                       #
# ------------------------------------------------------------------ #

def test_close_idempotent_and_terminates(files):
    """close：terminate 存活进程；重复 close 与未启动 close 均不报错。"""
    proc = _FakeProc()
    embedder = _embedder(files, _PopenRecorder([proc]), _FakeHttp())

    embedder.close()  # 未启动：无副作用
    assert embedder._proc is None

    embedder.ensure_started()
    embedder.close()
    assert proc.terminated >= 1
    assert embedder._proc is None

    embedder.close()  # 重复关闭
    embedder.close()
    assert embedder._proc is None


def test_close_with_already_exited_process(files):
    """close 时进程已退出：仅清理引用，不抛错。"""
    proc = _FakeProc(returncode=0)
    embedder = _embedder(files, _PopenRecorder(), _FakeHttp())
    embedder._proc = proc  # 注入已退出进程引用

    embedder.close()

    assert embedder._proc is None
    assert proc.terminated == 0  # 已退出不再 terminate


# ------------------------------------------------------------------ #
# 11. running 属性随进程状态变化                                        #
# ------------------------------------------------------------------ #

def test_running_reflects_process_state(files):
    """running：未启动 False → 启动后 True → 进程退出后 False。"""
    proc = _FakeProc()
    embedder = _embedder(files, _PopenRecorder([proc]), _FakeHttp())

    assert embedder.running is False
    embedder.ensure_started()
    assert embedder.running is True

    proc.returncode = 1  # 模拟进程退出
    assert embedder.running is False


# ------------------------------------------------------------------ #
# 12. 超长输入截断 + 失败分流（20260926 深挖）                          #
# ------------------------------------------------------------------ #

def test_embed_clips_oversize_text_to_token_budget(files):
    """超长文本按 token 预算截断后送入（不再触发服务端 400）；短文本原样透传。

    20260926 复审 W-2：截断改为**首尾各取一半**——文末关键信息必须保留。
    """
    from lite.runtime.llama_server import MAX_INPUT_TOKENS, _estimate_tokens

    captured = {}

    def embed(payload):
        captured["input"] = json.loads(payload.decode("utf-8"))["input"]
        return 200, _emb_body([[0.1]] * len(captured["input"]))

    embedder = _embedder(files, _PopenRecorder(), _FakeHttp(embed=embed))

    long_cjk = "长文" * 5000 + " 尾部关键锚点：蓝色贝壳潮汐"  # 约 1 万字中文
    out = embedder.embed([long_cjk, "短文本"])

    assert len(out) == 2
    sent_long, sent_short = captured["input"]
    assert sent_short == "短文本"
    assert len(sent_long) < len(long_cjk)
    assert _estimate_tokens(sent_long) <= MAX_INPUT_TOKENS
    # 首尾采样：头部与文末关键锚点都在送入文本中（W-2 修复断言）
    assert "长文" in sent_long
    assert "尾部关键锚点：蓝色贝壳潮汐" in sent_long


def test_embed_clips_emoji_heavy_text(files):
    """emoji / 星界字符按保守上界折算：7200 个 emoji 必被截断（W-3 修复断言）。

    旧口径按 0.25 token/字符折算时，此文本估算恰为 1800 而不触发截断（实际远超上下文）。
    """
    from lite.runtime.llama_server import MAX_INPUT_TOKENS, _estimate_tokens

    captured = {}

    def embed(payload):
        captured["input"] = json.loads(payload.decode("utf-8"))["input"]
        return 200, _emb_body([[0.1]] * len(captured["input"]))

    embedder = _embedder(files, _PopenRecorder(), _FakeHttp(embed=embed))

    emoji_blob = "\U0001F600" * 7200
    embedder.embed([emoji_blob])

    sent = captured["input"][0]
    assert len(sent) < len(emoji_blob)
    assert _estimate_tokens(sent) <= MAX_INPUT_TOKENS


def test_embed_clips_high_entropy_ascii(files):
    """高熵 ASCII（base64 样）按 0.4/字符保守折算必须被截断（W-A2 修复断言）。

    旧口径 0.25/字符下 `'A'*7200` 估算恰 1800 而不截断，实际可达 2~3 字符/token（>2048 token）。
    """
    from lite.runtime.llama_server import MAX_INPUT_TOKENS, _estimate_tokens

    captured = {}

    def embed(payload):
        captured["input"] = json.loads(payload.decode("utf-8"))["input"]
        return 200, _emb_body([[0.1]] * len(captured["input"]))

    embedder = _embedder(files, _PopenRecorder(), _FakeHttp(embed=embed))

    ascii_blob = "A" * 7200
    embedder.embed([ascii_blob])

    sent = captured["input"][0]
    assert len(sent) < len(ascii_blob)
    assert _estimate_tokens(sent) <= MAX_INPUT_TOKENS


def test_embed_keeps_chinese_with_fullwidth_punct_unclipped(files):
    """中文含全角标点：按低档折算后不应被过度截断（W-A3 修复断言）。

    构造约 1360 字（87.5% CJK + 12.5% 全角标点）：旧 3.0 折算估算 ≈2438 会被误截；
    新口径（标点 1.0）估算 ≈1063 <= 1800，必须**原样透传**。
    """
    from lite.runtime.llama_server import MAX_INPUT_TOKENS, _estimate_tokens

    captured = {}

    def embed(payload):
        captured["input"] = json.loads(payload.decode("utf-8"))["input"]
        return 200, _emb_body([[0.1]] * len(captured["input"]))

    embedder = _embedder(files, _PopenRecorder(), _FakeHttp(embed=embed))

    text = ("读书笔记与批注，" * 170)  # 每段 7 CJK + 1 全角逗号
    assert len(text) < 1500
    assert _estimate_tokens(text) <= MAX_INPUT_TOKENS  # 前置条件：本就无需截断
    embedder.embed([text])

    assert captured["input"][0] == text


# ------------------------------------------------------------------ #
# 13. 真实分词口径（/tokenize；GN-004 四审 W-1~W-3 根治）              #
# ------------------------------------------------------------------ #

def _tokenize_one_per_char(payload):
    """假 /tokenize：1 token/字符（用于演练"真实分词口径"的预算收敛）。"""
    content = json.loads(payload.decode("utf-8"))["content"]
    return 200, json.dumps({"tokens": [1] * len(content)}).encode("utf-8")


def test_embed_uses_real_tokenizer_budget(files):
    """真实分词口径：重复 CJK（四审 W-2 反例）必须按**真实 token** 收敛，且保留文末信息。"""
    from lite.runtime.llama_server import MAX_INPUT_TOKENS

    captured = {}

    def embed(payload):
        captured["input"] = json.loads(payload.decode("utf-8"))["input"]
        return 200, _emb_body([[0.1]] * len(captured["input"]))

    http = _FakeHttp(embed=embed, tokenize=_tokenize_one_per_char)
    embedder = _embedder(files, _PopenRecorder(), http)

    text = "中" * 2400 + " 尾部标记甲"
    embedder.embed([text])

    sent = captured["input"][0]
    assert len(sent) <= MAX_INPUT_TOKENS   # 1 token/字符 → 字符数即真实 token 数
    assert "尾部标记甲" in sent

    captured.clear()
    embedder.embed(["短文本"])            # 短文本真实口径下原样透传
    assert captured["input"] == ["短文本"]


def test_embed_falls_back_to_heuristic_when_tokenize_unavailable(files):
    """分词端点不可用（4xx）→ 回退启发式近似，且仍在启发式预算内（回退不阻断）。"""
    from lite.runtime.llama_server import MAX_INPUT_TOKENS, _estimate_tokens

    captured = {}

    def embed(payload):
        captured["input"] = json.loads(payload.decode("utf-8"))["input"]
        return 200, _emb_body([[0.1]] * len(captured["input"]))

    http = _FakeHttp(
        embed=embed,
        tokenize=lambda _p: (404, b'{"error":"no such endpoint"}'),
    )
    embedder = _embedder(files, _PopenRecorder(), http)

    embedder.embed(["长文" * 5000])

    sent = captured["input"][0]
    assert len(sent) < 10000
    assert _estimate_tokens(sent) <= MAX_INPUT_TOKENS


def test_embed_4xx_does_not_restart_server(files):
    """HTTP 4xx（业务性错误）→ 不重启服务、不重试，直接抛中文 RuntimeError。"""
    http = _FakeHttp(
        embed=lambda _p: (400, b'{"error":{"message":"exceed_context_size_error"}}')
    )
    popen = _PopenRecorder()
    embedder = _embedder(files, popen, http)

    with pytest.raises(RuntimeError) as ei:
        embedder.embed(["hi"])

    message = str(ei.value)
    assert "HTTP 400" in message
    assert "已重启重试" not in message
    assert "exceed_context_size_error" in message
    assert len(popen.calls) == 1  # 仅初次启动，无重启抖动
    assert embedder.running is True  # 4xx 不代表服务不健康，进程保留复用