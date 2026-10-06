# -*- coding: utf-8 -*-
"""Task C1 llama.cpp 运行时单元测试。

覆盖：
- 未加载时 embed / judge_should_reply / offline_chat 抛 LlamaNotReady；
- 注入 fake llama_cpp 后：加载成功、embed 形状正确、judge 解析 是/否、
  offline_chat 返回纯文本；
- 模型文件缺失时 load 返回 False 不崩溃、置未就绪并记录 warning；
- LlamaEmbeddingProvider：成功时委托 runtime.embed、未就绪时抛 LlamaNotReady；
- __init__ 从 dict / ConfigManager 正确读取配置意向（embedding.model / local_llm）。
"""

import os
import sys
import types

import pytest

from lite.config import ConfigManager
from lite.memory.embedding import EmbeddingProvider
from lite.runtime import (
    LlamaEmbeddingProvider,
    LlamaNotReady,
    LlamaRuntime,
)
from lite.runtime.llama_runtime import DEFAULT_EMBEDDING_MODEL, _estimate_prompt_tokens


class FakeLlama:
    """模拟 llama_cpp.Llama 的 fake 实现（注入 sys.modules['llama_cpp']）。

    - create_embedding：返回与输入等长的固定向量（每向量 3 维）；
    - __call__ / create_completion：返回可配置的 fixed_text（默认"是"）。
    - 模型路径不存在时构造即抛异常（用于加载失败降级验证）。
    """

    instances = []
    last_prompt = None

    def __init__(self, model_path, embedding=False, n_ctx=None, fixed_text="是", **kwargs):
        self.model_path = str(model_path)
        self.embedding = embedding
        self.n_ctx = n_ctx
        self.fixed_text = fixed_text
        # 记录额外构造参数（如 n_gpu_layers），供 GPU 开关透传断言使用
        self.init_kwargs = dict(kwargs)
        FakeLlama.instances.append(self)
        if not os.path.exists(self.model_path):
            raise FileNotFoundError(f"模型文件不存在：{self.model_path}")

    def create_embedding(self, input=None, **kwargs):
        texts = input if isinstance(input, list) else [input]
        return {"data": [{"embedding": [0.1, 0.2, 0.3]} for _ in texts]}

    def __call__(self, prompt, **kwargs):
        FakeLlama.last_prompt = prompt
        return self.fixed_text

    def create_completion(self, prompt, **kwargs):
        FakeLlama.last_prompt = prompt
        return {"choices": [{"text": self.fixed_text}]}


#: 注入 sys.modules 的 fake 模块（含 Llama 类）
_FAKE_MODULE = types.ModuleType("llama_cpp")
_FAKE_MODULE.Llama = FakeLlama


@pytest.fixture
def fake_llama_cpp(monkeypatch):
    """把 fake llama_cpp 注入 sys.modules，测试后自动还原。"""
    monkeypatch.setitem(sys.modules, "llama_cpp", _FAKE_MODULE)
    return FakeLlama


@pytest.fixture
def emb_model(tmp_path):
    """生成一个"存在"的假嵌入 GGUF 文件，返回其绝对路径。"""
    f = tmp_path / "qwen3-embedding-0.6b.gguf"
    f.write_bytes(b"fake-gguf")
    return str(f)


@pytest.fixture
def llm_model(tmp_path):
    """生成一个"存在"的假本地小 LLM GGUF 文件，返回其绝对路径。"""
    f = tmp_path / "local-llm-1.7b.gguf"
    f.write_bytes(b"fake-gguf")
    return str(f)


# ------------------------------------------------------------------ #
# 1. 未加载即调用 → LlamaNotReady                                    #
# ------------------------------------------------------------------ #

def test_not_loaded_raises():
    """未加载任何模型时，embed / judge / offline_chat 均应抛 LlamaNotReady。"""
    rt = LlamaRuntime(config=None)
    with pytest.raises(LlamaNotReady):
        rt.embed(["你好"])
    with pytest.raises(LlamaNotReady):
        rt.embed_texts(["你好"])
    with pytest.raises(LlamaNotReady):
        rt.judge_should_reply("你好，在吗")
    with pytest.raises(LlamaNotReady):
        rt.offline_chat([{"role": "user", "content": "你好"}])


def test_ready_flags_default_false():
    """初始状态：嵌入与本地 LLM 均未就绪。"""
    rt = LlamaRuntime(config=None)
    assert rt._emb_ready is False
    assert rt._llm_ready is False
    assert rt._emb_model is None
    assert rt._llm is None


# ------------------------------------------------------------------ #
# 2. fake llama_cpp：加载成功 + 嵌入形状 + 判定 + 离线回复           #
# ------------------------------------------------------------------ #

def test_load_embedding_model_and_embed_shape(emb_model, fake_llama_cpp):
    """注入 fake 后 load 成功，embed 形状正确（长度相等、维度固定）。"""
    rt = LlamaRuntime(config=None)
    assert rt.load_embedding_model(emb_model) is True
    assert rt._emb_ready is True
    vecs = rt.embed(["你好", "今天天气不错", "记得喂猫"])
    assert len(vecs) == 3
    assert all(isinstance(v, list) and len(v) == 3 for v in vecs)


def test_embed_texts_alias(emb_model, fake_llama_cpp):
    """embed_texts 为 embed 的别名，输出一致。"""
    rt = LlamaRuntime(config=None)
    rt.load_embedding_model(emb_model)
    assert rt.embed_texts(["hello"]) == rt.embed(["hello"])


def test_load_local_llm_and_judge_yes(llm_model, fake_llama_cpp):
    """加载本地 LLM 后，结果"是"→ True。"""
    rt = LlamaRuntime(config=None)
    assert rt.load_local_llm(llm_model) is True
    assert rt._llm_ready is True
    rt._llm.fixed_text = "是"
    assert rt.judge_should_reply("你好，在吗") is True


def test_judge_parses_no(llm_model, fake_llama_cpp):
    """结果"否"→ False。"""
    rt = LlamaRuntime(config=None)
    rt.load_local_llm(llm_model)
    rt._llm.fixed_text = "否"
    assert rt.judge_should_reply("今天天气不错") is False


@pytest.mark.parametrize(
    "raw,expected",
    [("是", True), ("是的，在呢", True), ("Yes", True), ("true", True), ("no", False), ("否", False)],
)
def test_judge_parse_variants(raw, expected, llm_model, fake_llama_cpp):
    """判定结果的首词/首字解析：是/yes/y/true 视为 True，其余默认 False。"""
    rt = LlamaRuntime(config=None)
    rt.load_local_llm(llm_model)
    rt._llm.fixed_text = raw
    assert rt.judge_should_reply("测试") is expected


def test_judge_prompt_is_chinese_and_single_word_answer(llm_model, fake_llama_cpp):
    """judge 提示词应要求只答 是/否，且已含用户输入。"""
    rt = LlamaRuntime(config=None)
    rt.load_local_llm(llm_model)
    rt.judge_should_reply("在干嘛")
    prompt = FakeLlama.last_prompt
    assert "是" in prompt and "否" in prompt
    assert "在干嘛" in prompt


def test_offline_chat_returns_text(llm_model, fake_llama_cpp):
    """offline_chat 返回本地 LLM 生成的纯文本回复。"""
    rt = LlamaRuntime(config=None)
    rt.load_local_llm(llm_model)
    rt._llm.fixed_text = "我在呢，有什么可以帮你？"
    out = rt.offline_chat(
        [{"role": "system", "content": "你是 CX-A 助手"}, {"role": "user", "content": "你好"}]
    )
    assert isinstance(out, str)
    assert out == "我在呢，有什么可以帮你？"


# ------------------------------------------------------------------ #
# 3. 模型文件缺失 → load 返回 False 不崩溃                           #
# ------------------------------------------------------------------ #

def test_load_embedding_model_missing_returns_false(tmp_path, fake_llama_cpp):
    """嵌入模型文件不存在时 load 返回 False，不崩溃、置未就绪并留 warning。"""
    rt = LlamaRuntime(config=None)
    missing = str(tmp_path / "no-such-embedding.gguf")
    assert rt.load_embedding_model(missing) is False
    assert rt._emb_ready is False
    assert any("不存在" in w for w in rt.warnings)


def test_load_local_llm_missing_returns_false(tmp_path, fake_llama_cpp):
    """本地小 LLM 文件不存在时 load 返回 False，不崩溃、置未就绪并留 warning。"""
    rt = LlamaRuntime(config=None)
    missing = str(tmp_path / "no-such-llm.gguf")
    assert rt.load_local_llm(missing) is False
    assert rt._llm_ready is False
    assert any("不存在" in w for w in rt.warnings)


def test_construction_exception_returns_false(tmp_path, monkeypatch):
    """模型构造抛异常（非法 GGUF）时应返回 False 而非崩溃（降级路径）。"""
    class _ExplodingLlama:
        def __init__(self, model_path, **kwargs):
            raise RuntimeError("bad gguf")

    fake = types.ModuleType("llama_cpp")
    fake.Llama = _ExplodingLlama
    monkeypatch.setitem(sys.modules, "llama_cpp", fake)

    rt = LlamaRuntime(config=None)
    model = tmp_path / "bad.gguf"
    model.write_bytes(b"not-a-model")
    assert rt.load_embedding_model(str(model)) is False
    assert rt._emb_ready is False
    assert any("加载失败" in w for w in rt.warnings)


# ------------------------------------------------------------------ #
# 4. LlamaEmbeddingProvider：委托 + 未就绪抛错                       #
# ------------------------------------------------------------------ #

def test_embedding_provider_delegates_success(emb_model, fake_llama_cpp):
    """LlamaEmbeddingProvider 成功路径委托 runtime.embed，长度/维度正确。"""
    rt = LlamaRuntime(config=None)
    rt.load_embedding_model(emb_model)
    prov = LlamaEmbeddingProvider(rt)
    assert isinstance(prov, EmbeddingProvider)
    vecs = prov.embed(["苹果", "香蕉", "橙子"])
    assert len(vecs) == 3
    assert all(len(v) == 3 for v in vecs)
    assert prov.runtime is rt


def test_embedding_provider_not_ready_raises():
    """底层未就绪时 LlamaEmbeddingProvider.embed 抛 LlamaNotReady。"""
    rt = LlamaRuntime(config=None)
    prov = LlamaEmbeddingProvider(rt)
    with pytest.raises(LlamaNotReady):
        prov.embed(["你好"])


def test_embedding_provider_none_runtime_raises():
    """runtime 为 None 时构造 LlamaEmbeddingProvider 抛 TypeError。"""
    with pytest.raises(TypeError):
        LlamaEmbeddingProvider(None)


# ------------------------------------------------------------------ #
# 5. 配置意向读取（embedding.model / local_llm）                     #
# ------------------------------------------------------------------ #

def test_init_reads_config_intent_from_dict():
    """从 dict 配置读取 embedding.model 与 local_llm 配置意向。"""
    cfg = {
        "embedding": {"model": "qwen3-embedding:0.6b", "runtime": "llama.cpp"},
        "local_llm": {"enabled": True, "model_path": "data/local_llm/m.gguf"},
    }
    rt = LlamaRuntime(config=cfg)
    assert rt._emb_model_name == DEFAULT_EMBEDDING_MODEL
    assert rt._llm_enabled is True
    assert rt._llm_path == "data/local_llm/m.gguf"


def test_init_reads_config_manager_intent(tmp_path):
    """从 ConfigManager 读取配置意向（默认值为 embedding.model / local_llm.enabled=False）。"""
    cm = ConfigManager(
        config_path=str(tmp_path / "config.json"),
        data_dir=str(tmp_path / "data"),
    )
    rt = LlamaRuntime(config=cm)
    assert rt._emb_model_name == DEFAULT_EMBEDDING_MODEL
    assert rt._llm_enabled is False
    assert rt._llm_path == ""


def test_init_defaults_when_config_none():
    """config=None 时使用默认配置意向。"""
    rt = LlamaRuntime(config=None)
    assert rt._emb_model_name == DEFAULT_EMBEDDING_MODEL
    assert rt._llm_enabled is False
    assert rt._llm_path == ""


# ------------------------------------------------------------------ #
# 6. M11：n_ctx 覆盖键 + prompt 溢出防护                               #
# ------------------------------------------------------------------ #

def test_init_reads_n_ctx_override():
    """local_llm.n_ctx 可覆盖默认 2048；缺失 / 非法 / 非正值回退默认。"""
    from lite.runtime.llama_runtime import DEFAULT_LLM_N_CTX as NC

    rt_ok = LlamaRuntime(config={"local_llm": {"n_ctx": 512}})
    assert rt_ok._n_ctx == 512
    rt_missing = LlamaRuntime(config=None)
    assert rt_missing._n_ctx == NC
    for bad in ("abc", None, -1, {"a": 1}):
        rt_bad = LlamaRuntime(config={"local_llm": {"n_ctx": bad}})
        assert rt_bad._n_ctx == NC, f"非法 n_ctx={bad!r} 应回退默认"


def test_fit_prompt_hard_clip_single_huge_line(llm_model, fake_llama_cpp):
    """单条超限（无行可删）时走硬截断尾部兜底，且保底最后一轮内容仍在头部预算内。"""
    rt = LlamaRuntime(config={"local_llm": {"n_ctx": 256}})  # judge 预算 = 256-8-64 = 184 token
    rt.load_local_llm(llm_model)
    rt.judge_should_reply("聊" * 20000)  # 判定模板+巨量中文输入远超 184 token
    prompt = FakeLlama.last_prompt
    assert _estimate_prompt_tokens(prompt) <= (256 - 8 - 64)


def test_estimate_prompt_tokens_cjk_vs_ascii():
    """第四轮体检批次C：token 估算按 CJK 占比折算——纯 ASCII 4 字符/token，
    纯中文约 1.3 字符/token（每字符 0.75 token），混合各按占比折算。"""
    assert _estimate_prompt_tokens("") == 0
    assert _estimate_prompt_tokens("a" * 400) == pytest.approx(100.0)
    assert _estimate_prompt_tokens("聊" * 400) == pytest.approx(300.0)
    mixed = "a" * 200 + "聊" * 200
    assert _estimate_prompt_tokens(mixed) == pytest.approx(50.0 + 150.0)


def test_cjk_prompt_budget_trims_where_ascii_budget_passed(llm_model, fake_llama_cpp):
    """第四轮体检批次C：旧"4 字符/token"口径下"合规"的长中文 prompt
    （约 726 字符 ≤ 736 字符预算，真实 token 约 545 早已溢出 184 预算），
    新 CJK 折算口径下必须被裁剪到 token 预算内。"""
    rt = LlamaRuntime(config={"local_llm": {"n_ctx": 256}})  # judge 预算 = 184 token
    rt.load_local_llm(llm_model)
    rt._llm.fixed_text = "是"
    rt.judge_should_reply("好" * 650)
    prompt = FakeLlama.last_prompt
    assert _estimate_prompt_tokens(prompt) <= (256 - 8 - 64)
    assert len(prompt) < 726, "旧字符口径下不会被裁剪的中文 prompt 未按新预算收敛"


def test_offline_chat_trims_long_history_under_budget(llm_model, fake_llama_cpp):
    """M11 + 第四轮体检批次C：超长中文历史（经 chat_window 截断仍超出 token 预算）
    被裁剪后投递 prompt 的 CJK 折算估算不超过预算，且保底 system 行 +
    最近一轮起始内容（消息级重建 → 行级兜底 → 硬截断）。"""
    rt = LlamaRuntime(config=None)  # 默认 n_ctx=2048 -> offline 预算 = 2048-128-64 = 1856 token
    assert rt.load_local_llm(llm_model) is True
    rt._llm.fixed_text = "收到"

    # 每轮 6000 中文：chat_window 取最近 6 条约 36k 字符，CJK 折算约 2.7 万 token
    # -> 消息级重建（system + 最近一轮约 4.5k token）仍超 -> 行级兜底 + 硬截断
    messages = [{"role": "system", "content": "你是 CX-A 助手"}]
    for i in range(20):
        messages.append({"role": "user", "content": f"历史第{i}轮：" + "聊" * 6000})
    out = rt.offline_chat(messages)

    assert out == "收到"
    prompt = FakeLlama.last_prompt
    budget = 2048 - 128 - 64
    assert _estimate_prompt_tokens(prompt) <= budget, (
        f"prompt token 估算 {_estimate_prompt_tokens(prompt)} 超过预算 {budget}"
    )
    # 保底 system 头部保留、最近一轮的起始内容仍在（硬截断保留前缀）
    assert prompt.startswith("system: ")
    assert "历史第19轮" in prompt


# ------------------------------------------------------------------ #
# 7. GPU 开关：device / n_gpu_layers 意向解析与构造透传               #
# ------------------------------------------------------------------ #

from lite.runtime.llama_runtime import GPU_LAYERS_ALL, GPU_LAYERS_CPU  # noqa: E402


def test_gpu_layers_default_is_cpu():
    """默认（config=None / 无设备键）嵌入与 LLM 卸载层数均为 0 = 纯 CPU。"""
    rt = LlamaRuntime(config=None)
    assert GPU_LAYERS_CPU == 0
    assert rt._emb_n_gpu_layers == GPU_LAYERS_CPU
    assert rt._llm_n_gpu_layers == GPU_LAYERS_CPU


def test_device_gpu_derives_all_layers_offload():
    """device="gpu" 且未显式配置 n_gpu_layers 时推导 -1（全层卸载）；大小写不敏感。"""
    cfg = {"embedding": {"device": "GPU"}, "local_llm": {"device": "gpu"}}
    rt = LlamaRuntime(config=cfg)
    assert rt._emb_n_gpu_layers == GPU_LAYERS_ALL == -1
    assert rt._llm_n_gpu_layers == GPU_LAYERS_ALL


def test_explicit_n_gpu_layers_overrides_device():
    """显式 n_gpu_layers 优先于 device 推导；None 视为未配置走 device 推导。"""
    cfg = {
        "embedding": {"device": "cpu", "n_gpu_layers": 8},
        "local_llm": {"device": "gpu", "n_gpu_layers": 5},
    }
    rt = LlamaRuntime(config=cfg)
    assert rt._emb_n_gpu_layers == 8  # cpu 但显式给层数 → 尊重显式值
    assert rt._llm_n_gpu_layers == 5  # 显式层数覆盖 device=gpu 的 -1 推导

    rt_none = LlamaRuntime(config={"local_llm": {"device": "gpu", "n_gpu_layers": None}})
    assert rt_none._llm_n_gpu_layers == GPU_LAYERS_ALL


@pytest.mark.parametrize("bad", ["abc", [1], {"a": 1}, float("nan")])
def test_invalid_n_gpu_layers_falls_back_to_device(bad):
    """非法 n_gpu_layers 按 device 推导回退：gpu→-1，其余→0。"""
    try:
        int(bad)
        pytest.skip("int() 可转换的值不属于非法样例")
    except (TypeError, ValueError):
        pass
    rt_gpu = LlamaRuntime(config={"local_llm": {"device": "gpu", "n_gpu_layers": bad}})
    assert rt_gpu._llm_n_gpu_layers == GPU_LAYERS_ALL
    rt_cpu = LlamaRuntime(config={"embedding": {"device": "cpu", "n_gpu_layers": bad}})
    assert rt_cpu._emb_n_gpu_layers == GPU_LAYERS_CPU


def test_load_passes_gpu_layers_to_constructor(emb_model, llm_model, fake_llama_cpp):
    """加载时把解析出的 n_gpu_layers 透传到 Llama 构造参数。"""
    cfg = {"embedding": {"device": "gpu"}, "local_llm": {"device": "cpu", "n_gpu_layers": 8}}
    rt = LlamaRuntime(config=cfg)
    assert rt.load_embedding_model(emb_model) is True
    assert rt.load_local_llm(llm_model) is True
    emb_inst, llm_inst = FakeLlama.instances[-2], FakeLlama.instances[-1]
    assert emb_inst.init_kwargs.get("n_gpu_layers") == GPU_LAYERS_ALL
    assert llm_inst.init_kwargs.get("n_gpu_layers") == 8


# ------------------------------------------------------------------ #
# 8. 外部解释器路径（llama.cpp 预编译二进制 + 语音桥，规划 §四-15）     #
# ------------------------------------------------------------------ #

from lite.runtime.llama_runtime import (  # noqa: E402
    EXTERNAL_CHAT_TIMEOUT_S,
    JUDGE_MAX_TOKENS,
    NO_THINK_SUFFIX,
    OFFLINE_CHAT_MAX_TOKENS,
    OFFLINE_CHAT_TEMPERATURE,
)


class _StubExternalClient:
    """替身语音桥客户端：记录请求并回预设 text（外部 llama-cli 路径）。"""

    available_value = True
    reply_text = "我在呢"
    instances = []

    def __init__(self, root=None, device="cpu", timeout=None):
        """记录构造参数（root/device/timeout 供断言）。"""
        self.root = root
        self.device = device
        self.timeout = timeout
        self.calls = []
        _StubExternalClient.instances.append(self)

    def available(self):
        """返回类级开关（默认 True）。"""
        return _StubExternalClient.available_value

    def request(self, payload, timeout=None):
        """记录请求并返回预设帧（不触真实进程）。"""
        self.calls.append({"payload": payload, "timeout": timeout})
        return {"ok": True, "text": _StubExternalClient.reply_text}, b""


@pytest.fixture
def external_env(tmp_path, monkeypatch):
    """外部路径环境：cli 落位 + 替身桥 + llama_cpp 缺席（确定性）。"""
    import lite.audio.voice_bridge_client as bridge_client_mod
    import lite.runtime.llama_runtime as llama_mod

    root = tmp_path / "portable"
    (root / "runtime" / "llama").mkdir(parents=True)
    (root / "runtime" / "llama" / "llama-cli.exe").write_bytes(b"stub-exe")
    monkeypatch.setattr(bridge_client_mod, "VoiceBridgeClient", _StubExternalClient)

    def _missing_llama():
        raise RuntimeError("llama-cpp-python 未安装：请先执行 pip install llama-cpp-python")

    monkeypatch.setattr(llama_mod, "_import_llama", _missing_llama)
    _StubExternalClient.available_value = True
    _StubExternalClient.reply_text = "我在呢"
    _StubExternalClient.instances = []
    return root


def test_external_path_load_and_offline_chat(external_env, llm_model):
    """llama_cpp 缺席时选出外部路径：加载就绪（_external_client 非空、_llm 为 None），
    offline_chat 经桥 chat 请求拿到真文本，且请求参数按配置透传。"""
    rt = LlamaRuntime(config={"local_llm": {"device": "gpu", "n_ctx": 512}}, root=str(external_env))
    assert rt.load_local_llm(llm_model) is True
    assert rt._llm_ready is True
    assert rt._external_client is not None and rt._llm is None

    out = rt.offline_chat([{"role": "user", "content": "你好"}])
    assert out == "我在呢"

    payload = rt._external_client.calls[-1]["payload"]
    assert payload["op"] == "chat"
    assert payload["model"] == llm_model
    assert "你好" in payload["prompt"]
    assert payload["max_tokens"] == OFFLINE_CHAT_MAX_TOKENS
    assert payload["n_ctx"] == 512
    assert payload["n_gpu_layers"] == GPU_LAYERS_ALL  # device=gpu 推导 -1
    assert payload["temperature"] == OFFLINE_CHAT_TEMPERATURE
    assert rt._external_client.calls[-1]["timeout"] == EXTERNAL_CHAT_TIMEOUT_S


def test_external_path_judge_uses_bridge(external_env, llm_model):
    """外部路径下的判定：同一桥 chat 通道（temperature=0.0），解析口径复用。"""
    rt = LlamaRuntime(config=None, root=str(external_env))
    assert rt.load_local_llm(llm_model) is True
    _StubExternalClient.reply_text = "是"
    assert rt.judge_should_reply("在吗") is True
    payload = rt._external_client.calls[-1]["payload"]
    assert payload["max_tokens"] == JUDGE_MAX_TOKENS
    assert payload["temperature"] == 0.0
    assert "在吗" in payload["prompt"]


def test_external_prompts_carry_no_think_suffix(external_env, llm_model):
    """思维链关闭（20260930）：offline_chat / judge 两条路径投递的提示词均以
    ``NO_THINK_SUFFIX`` 结尾（raw 补全通道下 Qwen3 软开关，防思考块混入回复）。"""
    rt = LlamaRuntime(config=None, root=str(external_env))
    assert rt.load_local_llm(llm_model) is True

    rt.offline_chat([{"role": "user", "content": "你好"}])
    chat_prompt = rt._external_client.calls[-1]["payload"]["prompt"]
    assert chat_prompt.endswith(NO_THINK_SUFFIX)

    rt.judge_should_reply("在吗")
    judge_prompt = rt._external_client.calls[-1]["payload"]["prompt"]
    assert judge_prompt.endswith(NO_THINK_SUFFIX)


def test_inprocess_prompts_carry_no_think_suffix(llm_model, fake_llama_cpp):
    """in-process 路径同口径（GN-004 O-1）：offline_chat / judge 投递的提示词
    均以 ``NO_THINK_SUFFIX`` 结尾（与外部桥路径结构一致，独立断言）。"""
    rt = LlamaRuntime(config=None)
    assert rt.load_local_llm(llm_model) is True

    rt.offline_chat([{"role": "user", "content": "你好"}])
    assert FakeLlama.last_prompt.endswith(NO_THINK_SUFFIX)

    rt.judge_should_reply("在吗")
    assert FakeLlama.last_prompt.endswith(NO_THINK_SUFFIX)


# ------------------------------------------------------------------ #
# 9. 常驻 chat 服务路径（20260930 常驻化改造）                          #
# ------------------------------------------------------------------ #

from lite.runtime.llama_runtime import CHAT_DEFAULT_SEED  # noqa: E402
from lite.runtime.llama_server import CHAT_SERVER_N_CTX  # noqa: E402


class _StubChatServer:
    """替身常驻 chat 服务：记录构造参数与 chat 调用，不启进程、不触网。"""

    instances = []
    reply_text = "我在呢"

    def __init__(self, exe_path, model_path, n_gpu_layers=0, n_ctx=2048, **kwargs):
        """记录构造参数（exe/model/n_gpu_layers/n_ctx 供断言）。"""
        self.exe_path = exe_path
        self.model_path = model_path
        self.n_gpu_layers = n_gpu_layers
        self.n_ctx = n_ctx
        self.calls = []
        self.started = 0
        self.closed = 0
        _StubChatServer.instances.append(self)

    def ensure_started(self):
        """记录预热调用（替身无真实进程）。"""
        self.started += 1

    def chat(self, messages, max_tokens=128, temperature=0.7, seed=None, timeout=None):
        """记录请求并返回预设文本。"""
        self.calls.append({
            "messages": [dict(m) for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "seed": seed,
        })
        return _StubChatServer.reply_text

    def close(self):
        """记录回收调用。"""
        self.closed += 1


@pytest.fixture
def chat_server_env(external_env, monkeypatch):
    """常驻 chat 服务环境：在 external_env 基础上补 llama-server.exe + 替身 chat 服务。

    ``_try_load_chat_server`` 用函数内 ``from lite.runtime.llama_server import
    LlamaServerChat``（调用时读模块属性），故 patch 模块属性即可拦截。
    """
    import lite.runtime.llama_server as llama_server_mod

    (external_env / "runtime" / "llama" / "llama-server.exe").write_bytes(b"stub-server")
    monkeypatch.setattr(llama_server_mod, "LlamaServerChat", _StubChatServer)
    _StubChatServer.instances = []
    _StubChatServer.reply_text = "我在呢"
    return external_env


def test_external_prefers_chat_server_over_bridge(chat_server_env, llm_model):
    """llama-server 就位时优先选常驻 chat 服务（而非桥）；n_ctx 固定 8192（Gemma 4 KV 封顶）。"""
    rt = LlamaRuntime(
        config={"local_llm": {"device": "gpu", "n_ctx": 512}}, root=str(chat_server_env)
    )
    assert rt.load_local_llm(llm_model) is True
    assert rt._external_chat is not None
    assert rt._external_client is None  # 桥路径未被选中
    assert rt._llm is None

    stub = rt._external_chat
    assert stub.n_gpu_layers == GPU_LAYERS_ALL  # device=gpu 推导 -1
    # 20261004 Gemma 4：chat 服务上下文不再沿用配置 n_ctx，固定 CHAT_SERVER_N_CTX
    assert stub.n_ctx == CHAT_SERVER_N_CTX == 8192

    out = rt.offline_chat(
        [{"role": "system", "content": "人设"}, {"role": "user", "content": "你好"}]
    )
    assert out == "我在呢"
    call = stub.calls[-1]
    assert call["messages"][0]["role"] == "system"
    assert call["messages"][-1] == {"role": "user", "content": "你好"}
    assert call["max_tokens"] == OFFLINE_CHAT_MAX_TOKENS
    assert call["temperature"] == OFFLINE_CHAT_TEMPERATURE
    assert call["seed"] == CHAT_DEFAULT_SEED  # 与桥路径 -s 42 同口径


def test_chat_server_judge_uses_single_user_message(chat_server_env, llm_model):
    """判定经常驻服务：单条 user 消息投递（走 chat template），temperature=0。"""
    rt = LlamaRuntime(config=None, root=str(chat_server_env))
    assert rt.load_local_llm(llm_model) is True
    _StubChatServer.reply_text = "是"

    assert rt.judge_should_reply("在吗") is True
    call = rt._external_chat.calls[-1]
    assert len(call["messages"]) == 1
    assert call["messages"][0]["role"] == "user"
    assert "是否回复" in call["messages"][0]["content"]
    assert call["max_tokens"] == JUDGE_MAX_TOKENS
    assert call["temperature"] == 0.0


def test_chat_server_prompt_has_no_no_think_suffix(chat_server_env, llm_model):
    """常驻服务路径关闭思考靠 ``enable_thinking=false``（请求体参数），
    投递的 messages 内不再追加 ``NO_THINK_SUFFIX``（那是桥路径的手段）。"""
    rt = LlamaRuntime(config=None, root=str(chat_server_env))
    assert rt.load_local_llm(llm_model) is True

    rt.offline_chat([{"role": "user", "content": "你好"}])
    content = rt._external_chat.calls[-1]["messages"][-1]["content"]
    assert not content.endswith(NO_THINK_SUFFIX)


def test_warm_local_llm_prewarms_chat_server(chat_server_env, llm_model):
    """预热：常驻路径调 ensure_started；桥路径为 no-op 且不抛。"""
    rt = LlamaRuntime(config=None, root=str(chat_server_env))
    assert rt.load_local_llm(llm_model) is True
    assert rt.warm_local_llm() is True
    assert rt._external_chat.started == 1


def test_warm_local_llm_noop_without_chat_server(external_env, llm_model):
    """无常驻服务（桥路径）：预热为 no-op（True，不触碰桥）。"""
    rt = LlamaRuntime(config=None, root=str(external_env))
    assert rt.load_local_llm(llm_model) is True
    assert rt._external_chat is None
    assert rt.warm_local_llm() is True


def test_close_releases_chat_server(chat_server_env, llm_model):
    """close 回收常驻 chat 服务（幂等；桥路径不受影响）。"""
    rt = LlamaRuntime(config=None, root=str(chat_server_env))
    assert rt.load_local_llm(llm_model) is True
    stub = rt._external_chat

    rt.close()
    assert stub.closed == 1

    rt.close()  # 已置 None，二次调用安全
    assert stub.closed == 1


def test_fit_messages_trims_history_and_clips(llm_model, fake_llama_cpp):
    """_fit_messages：超预算时从最旧侧删非 system 消息，仍超限则硬截断最后一条。"""
    from lite.runtime.llama_runtime import _estimate_prompt_tokens

    rt = LlamaRuntime(config={"local_llm": {"n_ctx": 512}})
    assert rt.load_local_llm(llm_model) is True
    budget = rt._token_budget(OFFLINE_CHAT_MAX_TOKENS)

    history = [{"role": "system", "content": "人设"}] + [
        {"role": "user", "content": f"第{i}轮：" + "闲聊内容" * 40} for i in range(8)
    ] + [{"role": "user", "content": "最后一句"}]
    fitted = rt._fit_messages(history, OFFLINE_CHAT_MAX_TOKENS)

    assert fitted[0]["role"] == "system"           # system 保底
    assert fitted[-1]["content"] == "最后一句"      # 最近一条恒留
    assert len(fitted) < len(history)              # 中间轮次被删减
    assert _estimate_prompt_tokens(
        "\n".join(str(m["content"]) for m in fitted)
    ) <= budget

    # 极端：单条即爆（无历史可删）→ 内容被硬截断到预算内
    huge = rt._fit_messages([{"role": "user", "content": "长" * 5000}], OFFLINE_CHAT_MAX_TOKENS)
    assert _estimate_prompt_tokens(huge[0]["content"]) <= budget

    # 极端二：system 自身即爆 → 整表兜底（截最近一条后仍超限，再截 system；GN-004 E12）
    two = rt._fit_messages(
        [{"role": "system", "content": "设" * 6000}, {"role": "user", "content": "你好"}],
        OFFLINE_CHAT_MAX_TOKENS,
    )
    assert _estimate_prompt_tokens(
        "\n".join(str(m["content"]) for m in two)
    ) <= budget


def test_fit_messages_passes_multimodal_arrays_untouched(llm_model, fake_llama_cpp):
    """content 为数组（text + image_url，Gemma 4 多模态）→ 整体跳过裁剪原样透传。

    base64 数据 URL 不参与 token 估算，任何截断都会损坏数据 URL（服务端 400）；
    上下文预算由常驻服务 -c（CHAT_SERVER_N_CTX=8192）兜底。
    """
    rt = LlamaRuntime(config={"local_llm": {"n_ctx": 512}})
    assert rt.load_local_llm(llm_model) is True

    huge_b64 = "A" * 100000  # 远超 n_ctx=512 预算的 base64 图片
    multimodal = [
        {"role": "system", "content": "人设"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "我屏幕上是什么？"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + huge_b64}},
            ],
        },
    ]
    fitted = rt._fit_messages(multimodal, OFFLINE_CHAT_MAX_TOKENS)

    assert fitted == multimodal  # 原样透传：无删减、无截断
    assert fitted[-1]["content"][1]["image_url"]["url"].endswith(huge_b64)


def test_external_not_ready_raises_llama_not_ready(external_env, llm_model):
    """外部路径就绪前（未 load）调用仍抛 LlamaNotReady（双路径统一门禁）。"""
    rt = LlamaRuntime(config=None, root=str(external_env))
    with pytest.raises(LlamaNotReady):
        rt.offline_chat([{"role": "user", "content": "你好"}])
    with pytest.raises(LlamaNotReady):
        rt.judge_should_reply("在吗")


def test_external_missing_binary_raises_runtime_error(tmp_path, monkeypatch, llm_model):
    """llama_cpp 缺席且 cli 不在位：维持 RuntimeError（并提示外部路径条件）。"""
    import lite.runtime.llama_runtime as llama_mod

    def _missing_llama():
        raise RuntimeError("llama-cpp-python 未安装")

    monkeypatch.setattr(llama_mod, "_import_llama", _missing_llama)
    rt = LlamaRuntime(config=None, root=str(tmp_path))  # 无 runtime/llama
    with pytest.raises(RuntimeError) as ei:
        rt.load_local_llm(llm_model)
    assert "外部 llama-cli 路径亦不可用" in str(ei.value)
    assert rt._llm_ready is False


def test_external_missing_sidecar_raises_runtime_error(external_env, llm_model, monkeypatch):
    """cli 在位但桥不可用（available=False）：同样维持 RuntimeError 且不误置就绪。"""
    import lite.runtime.llama_runtime as llama_mod

    def _missing_llama():
        raise RuntimeError("llama-cpp-python 未安装")

    monkeypatch.setattr(llama_mod, "_import_llama", _missing_llama)
    _StubExternalClient.available_value = False
    rt = LlamaRuntime(config=None, root=str(external_env))
    with pytest.raises(RuntimeError) as ei:
        rt.load_local_llm(llm_model)
    assert "外部 llama-cli 路径亦不可用" in str(ei.value)
    assert rt._external_client is None and rt._llm_ready is False


def test_external_priority_only_when_llama_cpp_missing(tmp_path, monkeypatch, llm_model, fake_llama_cpp):
    """llama_cpp 可用时优先 in-process（外部路径不抢占）：既有行为不受宿主二进制影响。"""
    import lite.audio.voice_bridge_client as bridge_client_mod

    root = tmp_path / "portable"
    (root / "runtime" / "llama").mkdir(parents=True)
    (root / "runtime" / "llama" / "llama-cli.exe").write_bytes(b"stub-exe")
    monkeypatch.setattr(bridge_client_mod, "VoiceBridgeClient", _StubExternalClient)

    rt = LlamaRuntime(config=None, root=str(root))
    assert rt.load_local_llm(llm_model) is True
    assert rt._llm is not None and rt._external_client is None


# ------------------------------------------------------------------ #
# 6. 外部嵌入路径（20260926_模块0_真实嵌入与向量持久化）               #
# ------------------------------------------------------------------ #

from lite.runtime.llama_runtime import EMBEDDING_SERVER_N_CTX  # noqa: E402


class _StubServerEmbedder:
    """假 llama-server 嵌入器：记录构造参数与调用，可切"启动失败"模式。"""

    instances = []
    start_value = True

    def __init__(self, exe_path, model_path, **kwargs):
        self.exe_path = exe_path
        self.model_path = model_path
        self.kwargs = kwargs
        self.closed = False
        self.embed_calls = []
        _StubServerEmbedder.instances.append(self)

    def ensure_started(self):
        """按类级开关决定就绪或抛错（不触真实进程）。"""
        if not _StubServerEmbedder.start_value:
            raise RuntimeError("就绪超时（测试替身）")

    def dim(self, probe_text="ping"):
        """固定维度探针。"""
        return 4

    def embed(self, texts):
        """返回固定 4 维向量并按输入保序。"""
        self.embed_calls.append(list(texts))
        return [[1.0, 0.5, 0.25, 0.0] for _ in texts]

    def close(self):
        """记录关闭（幂等断言用）。"""
        self.closed = True


@pytest.fixture
def external_embed_env(tmp_path, monkeypatch):
    """外部嵌入环境：llama-server 落位 + 替身嵌入器 + llama_cpp 缺席（确定性）。"""
    import lite.runtime.llama_runtime as llama_mod
    import lite.runtime.llama_server as llama_server_mod

    root = tmp_path / "portable"
    (root / "runtime" / "llama").mkdir(parents=True)
    (root / "runtime" / "llama" / "llama-server.exe").write_bytes(b"stub-server")
    model = root / "embedding.gguf"
    model.write_bytes(b"stub-model")

    monkeypatch.setattr(llama_server_mod, "LlamaServerEmbedder", _StubServerEmbedder)

    def _missing_llama():
        raise RuntimeError("llama-cpp-python 未安装：请先执行 pip install llama-cpp-python")

    monkeypatch.setattr(llama_mod, "_import_llama", _missing_llama)
    _StubServerEmbedder.instances = []
    _StubServerEmbedder.start_value = True
    return root, model


def test_external_embedding_path_loads_and_embeds(external_embed_env):
    """llama_cpp 缺席时嵌入走外部 llama-server：就绪置位、dim 探针、embed 保序透传。"""
    root, model = external_embed_env
    rt = LlamaRuntime(config={"embedding": {"device": "gpu"}}, root=str(root))
    assert rt.load_embedding_model(str(model)) is True
    assert rt._external_emb is not None and rt._emb_model is None
    assert rt.emb_dim == 4

    vecs = rt.embed(["你好", "世界"])
    assert len(vecs) == 2 and all(len(v) == 4 for v in vecs)
    embedder = rt._external_emb
    assert embedder.embed_calls[-1] == ["你好", "世界"]
    assert embedder.exe_path.endswith("llama-server.exe")
    assert embedder.model_path == str(model)
    assert embedder.kwargs["n_gpu_layers"] == GPU_LAYERS_ALL  # device=gpu 推导 -1
    assert embedder.kwargs["n_ctx"] == EMBEDDING_SERVER_N_CTX


def test_external_embedding_close_releases_server(external_embed_env):
    """close 回收外部嵌入服务（幂等：重复调用不抛错）。"""
    root, model = external_embed_env
    rt = LlamaRuntime(config=None, root=str(root))
    assert rt.load_embedding_model(str(model)) is True
    embedder = rt._external_emb
    rt.close()
    assert embedder.closed is True
    assert rt._external_emb is None
    rt.close()  # 幂等


def test_external_embedding_start_failure_raises_runtime_error(external_embed_env):
    """外部服务启动失败且 in-process 不可用：维持 RuntimeError 且不误置就绪。"""
    root, model = external_embed_env
    _StubServerEmbedder.start_value = False
    rt = LlamaRuntime(config=None, root=str(root))
    with pytest.raises(RuntimeError) as ei:
        rt.load_embedding_model(str(model))
    assert "外部 llama-server 嵌入路径亦不可用" in str(ei.value)
    assert rt._external_emb is None and rt._emb_ready is False
    assert any("外部 llama-server 嵌入路径不可用" in w for w in rt.warnings)


def test_external_embedding_missing_binary_raises_runtime_error(tmp_path, monkeypatch, external_embed_env):
    """llama_cpp 缺席且 llama-server 不在位：维持 RuntimeError（不静默降级）。"""
    _root, model = external_embed_env
    empty_root = tmp_path / "no-binaries"
    empty_root.mkdir()
    rt = LlamaRuntime(config=None, root=str(empty_root))
    with pytest.raises(RuntimeError) as ei:
        rt.load_embedding_model(str(model))
    assert "外部 llama-server 嵌入路径亦不可用" in str(ei.value)


def test_external_embedding_priority_only_when_llama_cpp_missing(tmp_path, monkeypatch, emb_model, fake_llama_cpp):
    """llama_cpp 可用时优先 in-process（外部嵌入路径不抢占）：既有行为不受宿主二进制影响。"""
    import lite.runtime.llama_server as llama_server_mod

    root = tmp_path / "portable"
    (root / "runtime" / "llama").mkdir(parents=True)
    (root / "runtime" / "llama" / "llama-server.exe").write_bytes(b"stub-server")
    monkeypatch.setattr(llama_server_mod, "LlamaServerEmbedder", _StubServerEmbedder)

    rt = LlamaRuntime(config=None, root=str(root))
    assert rt.load_embedding_model(emb_model) is True
    assert rt._emb_model is not None and rt._external_emb is None


# ------------------------------------------------------------------ #
# 7. 嵌入模型路径解析（resolve_embedding_model_path）                  #
# ------------------------------------------------------------------ #


def test_resolve_embedding_model_path_config_wins(tmp_path):
    """配置 model_path 优先：绝对路径原样、相对路径按根拼接。"""
    from lite.runtime.llama_runtime import resolve_embedding_model_path

    root = tmp_path / "root"
    root.mkdir()
    abs_target = tmp_path / "abs-model.gguf"
    assert resolve_embedding_model_path(
        {"embedding": {"model_path": str(abs_target)}}, root=str(root)
    ) == str(abs_target)
    assert resolve_embedding_model_path(
        {"embedding": {"model_path": "installer/bundled/model.gguf"}}, root=str(root)
    ) == str(root / "installer" / "bundled" / "model.gguf")


def test_resolve_embedding_model_path_convention_dir_prefers_q8(tmp_path):
    """约定目录扫描：多个 gguf 时优先含 q8_0 的文件。"""
    from lite.runtime.llama_runtime import resolve_embedding_model_path

    model_dir = tmp_path / "data" / "local_llm" / "qwen3-embedding-0.6b"
    model_dir.mkdir(parents=True)
    (model_dir / "Qwen3-Embedding-0.6B-f16.gguf").write_bytes(b"stub")
    (model_dir / "Qwen3-Embedding-0.6B-Q8_0.gguf").write_bytes(b"stub")
    resolved = resolve_embedding_model_path(None, root=str(tmp_path))
    assert resolved.endswith("Qwen3-Embedding-0.6B-Q8_0.gguf")


def test_resolve_embedding_model_path_missing_returns_empty(tmp_path):
    """约定目录不存在 / 无 gguf：返回空串（调用方按不可用降级）。"""
    from lite.runtime.llama_runtime import resolve_embedding_model_path

    assert resolve_embedding_model_path(None, root=str(tmp_path)) == ""


# ------------------------------------------------------------------ #
# embedding.backend == "onnx"：嵌入桥路径（20261002 批 B）              #
# ------------------------------------------------------------------ #


class _StubBridgeEmbedder:
    """VoiceBridgeEmbedder 替身：类级开关控制 ensure_started 是否失败。"""

    instances = []
    fail = False

    def __init__(self, root=None, device="cpu", timeout=None):
        self.root = root
        self.device = device
        self.closed = False
        type(self).instances.append(self)

    def ensure_started(self):
        if type(self).fail:
            raise RuntimeError("嵌入 ONNX 资产缺失（模拟）")

    def embed(self, texts):
        return [[0.1, 0.2, 0.3] for _ in texts]

    def dim(self, probe_text="ping"):
        return 3

    def close(self):
        self.closed = True


def test_embedding_backend_onnx_prefers_bridge_path(emb_model, tmp_path, monkeypatch):
    """backend="onnx" 且桥就绪 → 嵌入装配 VoiceBridgeEmbedder（emb_dim 探针确定）。"""
    from lite.runtime import llama_runtime as lr

    _StubBridgeEmbedder.instances.clear()
    _StubBridgeEmbedder.fail = False
    monkeypatch.setattr(lr, "VoiceBridgeEmbedder", _StubBridgeEmbedder)
    rt = LlamaRuntime(config={"embedding": {"backend": "onnx"}}, root=str(tmp_path))
    assert rt.load_embedding_model(emb_model) is True
    assert rt._emb_ready is True
    assert isinstance(rt._external_emb, _StubBridgeEmbedder)
    assert rt.emb_dim == 3
    assert _StubBridgeEmbedder.instances[-1].root == str(tmp_path)
    vecs = rt.embed(["你好"])
    assert len(vecs) == 1 and len(vecs[0]) == 3


def test_embedding_backend_onnx_falls_back_to_llama_cpp(emb_model, fake_llama_cpp, tmp_path, monkeypatch):
    """backend="onnx" 但桥路径失败 → 中文告警后回落 llama.cpp 既有路径（不中断）。"""
    from lite.runtime import llama_runtime as lr

    _StubBridgeEmbedder.instances.clear()
    _StubBridgeEmbedder.fail = True
    monkeypatch.setattr(lr, "VoiceBridgeEmbedder", _StubBridgeEmbedder)
    rt = LlamaRuntime(config={"embedding": {"backend": "onnx"}}, root=str(tmp_path))
    assert rt.load_embedding_model(emb_model) is True
    assert any("嵌入 ONNX 桥路径不可用" in w for w in rt.warnings)
    # 回退后走 in-process fake llama 路径（向量维度 3 与桥替身同形，属既有口径）
    vecs = rt.embed(["你好"])
    assert len(vecs[0]) == 3


def test_embedding_backend_default_skips_bridge(emb_model, fake_llama_cpp, monkeypatch):
    """缺省（backend=""）不触碰桥路径（embedder 构造即视为违例）。"""

    def _boom(*args, **kwargs):
        raise AssertionError("缺省 backend 不应构造 VoiceBridgeEmbedder")

    from lite.runtime import llama_runtime as lr

    monkeypatch.setattr(lr, "VoiceBridgeEmbedder", _boom)
    rt = LlamaRuntime(config=None)
    assert rt.load_embedding_model(emb_model) is True
    vecs = rt.embed(["你好"])
    assert len(vecs[0]) == 3


def test_bridge_embedder_validates_texts(tmp_path, monkeypatch):
    """VoiceBridgeEmbedder.embed 输入校验：非列表 / 空列表抛中文 RuntimeError。"""
    from lite.runtime.llama_runtime import VoiceBridgeEmbedder

    class _ClientStub:
        def __init__(self, **kwargs):
            self.requests = []

        def request(self, payload, timeout=None):
            self.requests.append((dict(payload), timeout))
            if payload.get("op") == "embed":
                return {"ok": True, "dim": 3, "vectors": [[0.1, 0.2, 0.3]]}, b""
            return {"ok": True, "pong": True}, b""

        def close(self):
            pass

    import lite.audio.voice_bridge_client as vbc

    monkeypatch.setattr(vbc, "VoiceBridgeClient", _ClientStub)
    embedder = VoiceBridgeEmbedder(root=str(tmp_path))
    with pytest.raises(RuntimeError):
        embedder.embed("not-a-list")
    with pytest.raises(RuntimeError):
        embedder.embed([])
    vectors = embedder.embed(["你好"])
    assert vectors == [[0.1, 0.2, 0.3]]


def test_bridge_embedder_close_is_idempotent_and_silent(tmp_path, monkeypatch):
    """close 委托 client.close 且吞回收异常（幂等不抛）。"""
    from lite.runtime.llama_runtime import VoiceBridgeEmbedder

    class _ClientStub:
        def __init__(self, **kwargs):
            pass

        def request(self, payload, timeout=None):
            return {"ok": True, "dim": 3, "vectors": [[0.1, 0.2, 0.3]]}, b""

        def close(self):
            raise RuntimeError("模拟回收异常")

    import lite.audio.voice_bridge_client as vbc

    monkeypatch.setattr(vbc, "VoiceBridgeClient", _ClientStub)
    embedder = VoiceBridgeEmbedder(root=str(tmp_path))
    embedder.close()  # 不抛即通过
    embedder.close()