# -*- coding: utf-8 -*-
"""llama.cpp 运行时子包（承载 qwen3-embedding 与本地小 LLM），Task C 实现。

对外暴露：
- LlamaRuntime：内置 llama.cpp 运行时（嵌入 / 是否回复判定 / 离线兜底）。
- LlamaEmbeddingProvider：实现 EmbeddingProvider，供记忆检索链路替换嵌入桩。
- LlamaNotReady：模型未就绪异常。
- LlmDownloader：本地小 LLM 可选下载引导（HF / 魔塔双源，GGUF）。
- get_local_llm_info：扫描本地已下载 GGUF 模型。
"""

from .llama_runtime import LlamaEmbeddingProvider, LlamaNotReady, LlamaRuntime
from .model_downloader import LlmDownloader, get_local_llm_info

__all__ = [
    "LlamaRuntime",
    "LlamaEmbeddingProvider",
    "LlamaNotReady",
    "LlmDownloader",
    "get_local_llm_info",
]