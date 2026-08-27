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
from lite.runtime.llama_runtime import DEFAULT_EMBEDDING_MODEL


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
    rt = LlamaRuntime(config={"local_llm": {"n_ctx": 256}})  # judge 预算=(256-8-64)*4=736
    rt.load_local_llm(llm_model)
    rt.judge_should_reply("聊" * 20000)  # 判定模板+巨量输入远超 736
    prompt = FakeLlama.last_prompt
    assert len(prompt) <= (256 - 8 - 64) * 4


def test_offline_chat_trims_long_history_under_budget(llm_model, fake_llama_cpp):
    """M11 要求用例：超长历史（经 chat_window 截断仍超出预算）被裁剪后调用成功，
    投递 prompt 长度不超过预算上限，且重建时保底 system + 最近一轮。"""
    rt = LlamaRuntime(config=None)  # 默认 n_ctx=2048 -> offline 预算=(2048-128-64)*4=7424
    assert rt.load_local_llm(llm_model) is True
    rt._llm.fixed_text = "收到"

    # 每轮 6000 字符：chat_window 取最近 6 条约 36k 字符，必超 7424 预算 -> 触发消息级删减
    messages = [{"role": "system", "content": "你是 CX-A 助手"}]
    for i in range(20):
        messages.append({"role": "user", "content": f"历史第{i}轮：" + "聊" * 6000})
    out = rt.offline_chat(messages)

    assert out == "收到"
    prompt = FakeLlama.last_prompt
    limit = (2048 - 128 - 64) * 4
    assert len(prompt) <= limit, f"prompt 长度 {len(prompt)} 超过预算 {limit}"
    # 保底 system + 最近一轮（消息级重建绕开 chat_window 对 system 的截断）
    assert prompt.startswith("system: ")
    assert messages[-1]["content"] in prompt


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