# -*- coding: utf-8 -*-
"""Task C2 本地小 LLM 下载引导单元测试（全 mock 网络，不真实下载）。

覆盖：
- build_url：modelscope / huggingface 两源 URL 模板正确；
- build_url / download：非法 repo（单段）、非法文件名（无 .gguf、含 ../、含 / 与 \\）
  均抛 ValueError；
- download（注入 fake requests 或 mock urllib.urlopen）：
  写入成功、目标路径正确、verify_size 通过、偏差超 20% 报错、已存在跳过、进度回调；
- suggest_model：返回含 source 与 approximate_size_gb；
- get_local_llm_info：tmp_path 放 .gguf 假文件扫描正确；空/不存在目录返回 None。
"""

import os
import urllib.request

import pytest

from lite.runtime.model_downloader import (
    LlmDownloader,
    get_local_llm_info,
)

MODELSCOPE_REPO = "Qwen/Qwen1.5-1.8B-Chat-GGUF"
MODELSCOPE_FILE = "qwen1.5-1_8b-chat-q4_k_m.gguf"


class _FakeResponse:
    """模拟 requests 流式响应：记录状态码/头，按块产出内容。"""

    def __init__(self, data, status=200, headers=None):
        self._data = data or b""
        self._status = status
        self.headers = headers if headers is not None else {"Content-Length": str(len(self._data))}
        self._offset = 0

    def raise_for_status(self):
        if self._status >= 400:
            raise RuntimeError(f"HTTP {self._status}")

    def iter_content(self, chunk_size=8192):
        while self._offset < len(self._data):
            chunk = self._data[self._offset : self._offset + chunk_size]
            self._offset += len(chunk)
            yield chunk


class _FakeRequests:
    """模拟 requests 模块的 get()，记录调用 URL。"""

    def __init__(self, data=b"", status=200, headers=None):
        self.data = data
        self.status = status
        self.headers = headers
        self.calls = []

    def get(self, url, stream=False, timeout=None):
        self.calls.append(url)
        return _FakeResponse(self.data, self.status, self.headers)


def make_loader(tmp_path, data=b"", status=200, headers=None):
    """构造 LlmDownloader 并注入 fake requests（测试不真实发网络请求）。"""
    dl = LlmDownloader(dest_dir=str(tmp_path))
    dl._requests = _FakeRequests(data=data, status=status, headers=headers)
    dl._using_requests = True
    return dl


# ------------------------------------------------------------------ #
# 0. 默认 dest_dir 推导（必须挂在工程根下 data/local_llm）           #
# ------------------------------------------------------------------ #

def test_default_dest_dir_derives_project_root():
    """默认 dest_dir 应为工程根 data/local_llm 的绝对路径（非顶层根）。"""
    dl = LlmDownloader()
    parts = os.path.normpath(dl.dest_dir).split(os.sep)
    assert os.path.isabs(dl.dest_dir)
    assert parts[-1] == "local_llm"
    assert parts[-2] == "data"
    assert len(parts) >= 4, f"默认 dest_dir 未挂在本工程根下，received={dl.dest_dir!r}"


# ------------------------------------------------------------------ #
# 1. build_url：两源 URL 模板正确                                    #
# ------------------------------------------------------------------ #

def test_build_url_modelscope():
    """魔塔 source → modelscope URL 模板。"""
    dl = LlmDownloader(dest_dir="/tmp")
    url = dl.build_url("modelscope", MODELSCOPE_REPO, MODELSCOPE_FILE)
    assert url == (
        f"https://modelscope.cn/api/v1/models/{MODELSCOPE_REPO}/repo?FilePath={MODELSCOPE_FILE}"
    )


def test_build_url_huggingface():
    """HuggingFace source → hf URL 模板。"""
    dl = LlmDownloader(dest_dir="/tmp")
    url = dl.build_url("huggingface", "Qwen/Qwen1.5-1.8B-Chat-GGUF", "qwen1.5-1_8b.gguf")
    assert url == "https://huggingface.co/Qwen/Qwen1.5-1.8B-Chat-GGUF/resolve/main/qwen1.5-1_8b.gguf"


# ------------------------------------------------------------------ #
# 2. 非法 repo / 文件名 → ValueError                                 #
# ------------------------------------------------------------------ #

def test_build_url_reject_single_segment_repo():
    """repo 非双段（无 /）→ ValueError。"""
    dl = LlmDownloader(dest_dir="/tmp")
    with pytest.raises(ValueError):
        dl.build_url("modelscope", "QwenOnly", MODELSCOPE_FILE)


@pytest.mark.parametrize(
    "bad_repo",
    ["", "Qwen/../evil", "Qwen/.", "Qwen//Name", "Org/Name\\bad"],
)
def test_build_url_reject_dangerous_repo(bad_repo):
    """含危险路径或空段的 repo → ValueError。"""
    dl = LlmDownloader(dest_dir="/tmp")
    with pytest.raises(ValueError):
        dl.build_url("modelscope", bad_repo, MODELSCOPE_FILE)


def test_build_url_reject_non_gguf_filename():
    """文件名无 .gguf 后缀 → ValueError。"""
    dl = LlmDownloader(dest_dir="/tmp")
    with pytest.raises(ValueError):
        dl.build_url("modelscope", MODELSCOPE_REPO, "model.bin")


def test_build_url_reject_dangerous_filename():
    """文件名含 ../、/ 、\\ → ValueError。"""
    dl = LlmDownloader(dest_dir="/tmp")
    for bad in ["../../etc/passwd.gguf", "a/b.gguf", "a\\b.gguf", "..model.gguf"]:
        with pytest.raises(ValueError):
            dl.build_url("modelscope", MODELSCOPE_REPO, bad)


def test_build_url_reject_unknown_source():
    """未知下载源 → ValueError。"""
    dl = LlmDownloader(dest_dir="/tmp")
    with pytest.raises(ValueError):
        dl.build_url("unknown-source", MODELSCOPE_REPO, MODELSCOPE_FILE)


# ------------------------------------------------------------------ #
# 3. download：requests 流式写入 + 目标路径 + 校验                    #
# ------------------------------------------------------------------ #

def test_download_writes_file_and_returns_path(tmp_path):
    """下载成功：临时文件改名到目标路径，内容与源一致，URL 正确。"""
    data = b"\x00" * 2048
    dl = make_loader(tmp_path, data=data)
    result = dl.download(MODELSCOPE_REPO, MODELSCOPE_FILE, source="modelscope")
    dest = tmp_path / MODELSCOPE_FILE
    assert result == dest
    assert dest.is_file()
    assert dest.read_bytes() == data
    assert dl._requests.calls[0].startswith("https://modelscope.cn/api/v1/models/")
    # 不应残留 .tmp 临时文件
    assert not any(p.suffix == ".tmp" for p in tmp_path.iterdir())


def test_download_verify_size_passes(tmp_path):
    """verify_size_gb 与实际下载大小一致（容差内）→ 不报错。"""
    data = b"\x00" * 1024
    dl = make_loader(tmp_path, data=data)
    expected_gb = 1024 / (1024 ** 3)  # 与 data 大小完全一致
    result = dl.download(MODELSCOPE_REPO, MODELSCOPE_FILE, verify_size_gb=expected_gb)
    assert result.is_file()


def test_download_verify_size_mismatch_raises(tmp_path):
    """verify_size_gb 与其实大小偏差超 20% → ValueError。"""
    data = b"\x00" * 1024
    dl = make_loader(tmp_path, data=data)
    with pytest.raises(ValueError):
        dl.download(MODELSCOPE_REPO, MODELSCOPE_FILE, verify_size_gb=1.7)


def test_download_skip_when_exists(tmp_path):
    """已存在同名文件 → 跳过下载（不调用网络）、返回既有路径并提示已存在。"""
    dest = tmp_path / MODELSCOPE_FILE
    dest.write_bytes(b"already-there")
    dl = make_loader(tmp_path, data=b"should-not-overwrite")
    result = dl.download(MODELSCOPE_REPO, MODELSCOPE_FILE)
    assert result == dest
    assert dest.read_bytes() == b"already-there"  # 未覆盖
    assert dl._requests.calls == []  # 未发起任何下载


def test_download_progress_callback_called(tmp_path):
    """progress_cb 被调用且参数为 (downloaded, total)。"""
    data = b"\x01" * 20000  # 跨多个 64KB 块（此处单块也至少回调一次）
    dl = make_loader(tmp_path, data=data)
    calls = []
    dl.download(MODELSCOPE_REPO, MODELSCOPE_FILE, progress_cb=lambda d, t: calls.append((d, t)))
    assert len(calls) >= 1
    downloaded, total = calls[-1]
    assert downloaded == total == len(data)


def test_download_rejects_bad_filename_in_primary(tmp_path):
    """download 入口同样做 .gguf / 危险路径校验。"""
    dl = make_loader(tmp_path)
    with pytest.raises(ValueError):
        dl.download(MODELSCOPE_REPO, "../../x.gguf")
    with pytest.raises(ValueError):
        dl.download(MODELSCOPE_REPO, "model.bin")


# ------------------------------------------------------------------ #
# 4. download：urllib 兜底路径                                       #
# ------------------------------------------------------------------ #

def test_download_urllib_fallback(tmp_path, monkeypatch):
    """未装 requests（_using_requests=False）时走 urllib.request.urlopen 兜底。"""
    data = b"\x02" * 1024

    class _FakeURLError(RuntimeError):
        pass

    class _FakeURLResponse:
        def __init__(self, payload):
            self._payload = payload
            self._off = 0
            self.headers = {"Content-Length": str(len(payload))}

        def read(self, n=-1):
            if n is None or n < 0:
                n = len(self._payload) - self._off
            chunk = self._payload[self._off : self._off + n]
            self._off += len(chunk)
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    captured = {}
    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return _FakeURLResponse(data)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    dl = LlmDownloader(dest_dir=str(tmp_path))
    dl._using_requests = False  # 强制走 urllib 兜底
    dl._requests = None
    result = dl.download(MODELSCOPE_REPO, MODELSCOPE_FILE, source="modelscope")
    dest = tmp_path / MODELSCOPE_FILE
    assert result == dest
    assert dest.read_bytes() == data
    assert captured["req"].full_url.startswith("https://modelscope.cn/api/v1/models/")


# ------------------------------------------------------------------ #
# 5. suggest_model                                                   #
# ------------------------------------------------------------------ #

@pytest.mark.parametrize("source", ["modelscope", "huggingface"])
def test_suggest_model_contains_source_and_size(source):
    """suggest_model 返回含 source 与 approximate_size_gb，且为建议 ~1.7B。"""
    info = LlmDownloader.suggest_model(source=source)
    assert isinstance(info, dict)
    assert info["source"] == source
    assert "approximate_size_gb" in info
    assert abs(float(info["approximate_size_gb"]) - 1.7) < 0.5  # 建议 ~1.7B
    assert "repo" in info and "filename" in info
    assert "disclaimer" in info


def test_suggest_model_default_modelscope():
    """suggest_model 默认源为 modelscope（国内优先）。"""
    info = LlmDownloader.suggest_model()
    assert info["source"] == "modelscope"


# ------------------------------------------------------------------ #
# 6. get_local_llm_info                                              #
# ------------------------------------------------------------------ #

def test_get_local_llm_info_scans_gguf(tmp_path):
    """目录下 .gguf 假文件被识别，返回其路径与大小；非 gguf 忽略。"""
    gguf = tmp_path / "local-llm-1.7b.gguf"
    gguf.write_bytes(b"fake-model")
    (tmp_path / "readme.txt").write_text("ignore me")
    info = get_local_llm_info(str(tmp_path))
    assert info is not None
    assert info["count"] == 1
    assert info["models"][0]["path"] == str(gguf)
    assert info["models"][0]["size_bytes"] == len(b"fake-model")


def test_get_local_llm_info_none_when_missing(tmp_path):
    """目录不存在或目录下无 .gguf → None。"""
    assert get_local_llm_info(str(tmp_path / "no-such-dir")) is None
    empty = tmp_path / "empty"
    empty.mkdir()
    assert get_local_llm_info(str(empty)) is None