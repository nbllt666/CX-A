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
import shutil
import types
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


def test_download_corrupt_existing_file_self_heals(tmp_path):
    """G-9：已存在文件大小校验失败 → 删除坏文件继续正常下载（不再永久短路）。"""
    good = b"\x00" * 1024
    dl = make_loader(tmp_path, data=good)
    dest = tmp_path / MODELSCOPE_FILE
    dest.write_bytes(b"corrupt-stub")  # 坏文件：与预期大小偏差远超 20%
    expected_gb = 1024 / (1024 ** 3)  # 正确内容对应的大小

    result = dl.download(MODELSCOPE_REPO, MODELSCOPE_FILE, verify_size_gb=expected_gb)

    assert result == dest
    # 坏文件已被删除并被重新下载的正确内容取代
    assert dest.read_bytes() == good
    assert dl._requests.calls, "坏文件删除后应真实发起下载"
    assert not any(p.suffix == ".tmp" for p in tmp_path.iterdir())


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
# 3b. M12：校验先于 os.replace（Content-Length 一致性 + 大小不符不落盘）
# ------------------------------------------------------------------ #

def test_download_content_length_mismatch_raises_and_cleans_tmp(tmp_path):
    """M12：Content-Length 与实际字节不符 → 删 tmp 抛 ValueError，正式位不落盘、无残留。"""
    data = b"\x03" * 512
    dl = make_loader(tmp_path, data=data, headers={"Content-Length": str(len(data) * 4)})
    with pytest.raises(ValueError, match="下载不完整"):
        dl.download(MODELSCOPE_REPO, MODELSCOPE_FILE)
    assert not (tmp_path / MODELSCOPE_FILE).exists()  # 未落盘
    assert list(tmp_path.iterdir()) == []             # .tmp 已删除


def test_download_size_mismatch_cleans_tmp_before_replace(tmp_path):
    """M12：verify_size 不符 → 删 tmp 抛 ValueError；目标位与临时文件均不存在。"""
    data = b"\x00" * 1024
    dl = make_loader(tmp_path, data=data)
    with pytest.raises(ValueError):
        dl.download(MODELSCOPE_REPO, MODELSCOPE_FILE, verify_size_gb=1.7)
    assert not (tmp_path / MODELSCOPE_FILE).exists()
    assert list(tmp_path.iterdir()) == []


def test_download_content_length_match_still_passes(tmp_path):
    """M12：Content-Length 与实际一致时不触发完整性报错，正常落盘。"""
    data = b"\x05" * 4096
    dl = make_loader(tmp_path, data=data)  # 默认头即真实长度
    result = dl.download(MODELSCOPE_REPO, MODELSCOPE_FILE)
    assert result.is_file()
    assert result.read_bytes() == data


# ------------------------------------------------------------------ #
# 3c. L10：磁盘空间预检 + 断点续传                                     #
# ------------------------------------------------------------------ #

def test_download_disk_insufficient_raises_before_any_file(tmp_path, monkeypatch):
    """verify_size_gb 推算预期大小、磁盘可用 < 预期×1.05 → ValueError 且不落任何文件。"""
    dl = make_loader(tmp_path, data=b"x" * 64)
    # 模拟磁盘仅 10 字节可用（verify_size_gb=0.001GB≈1073742 字节）
    monkeypatch.setattr(
        shutil, "disk_usage",
        lambda p: types.SimpleNamespace(free=10),
    )
    with pytest.raises(ValueError, match="磁盘空间不足"):
        dl.download(MODELSCOPE_REPO, MODELSCOPE_FILE, verify_size_gb=0.001)
    assert list(tmp_path.iterdir()) == []          # 目录内未落任何文件
    assert dl._requests.calls == []                # 未发起任何网络请求


def test_download_disk_insufficient_from_content_length(tmp_path, monkeypatch):
    """Content-Length 提前得知且磁盘不足 → 开流写盘前抛错，无 .tmp 残留。"""
    big_total = 10 * 1024 * 1024
    dl = make_loader(
        tmp_path,
        data=b"\x00" * 128,
        headers={"Content-Length": str(big_total)},
    )
    monkeypatch.setattr(
        shutil, "disk_usage",
        lambda p: types.SimpleNamespace(free=1024),
    )
    with pytest.raises(ValueError, match="磁盘空间不足"):
        dl.download(MODELSCOPE_REPO, MODELSCOPE_FILE)
    assert list(tmp_path.iterdir()) == []          # 未创建 .tmp


class _FakeRangedURLResponse:
    """模拟 urllib 响应：携带 status，支持上下文管理器与 read。"""

    def __init__(self, payload, status=None, headers=None):
        self._payload = payload
        self._off = 0
        self.status = status  # 真实 HTTPResponse 有 status；None 表示不携带
        self.headers = headers if headers is not None else {"Content-Length": str(len(payload))}

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


def _make_urllib_dl(tmp_path):
    dl = LlmDownloader(dest_dir=str(tmp_path))
    dl._using_requests = False
    dl._requests = None
    return dl


def test_download_urllib_resume_206_appends_from_offset(tmp_path, monkeypatch):
    """tmp 存在 + 服务器响应 206 → 从断点追加续传，最终 done==total 落盘完整文件。"""
    full = b"HELLOPART2"
    partial = full[:6]
    (tmp_path / (MODELSCOPE_FILE + ".tmp")).write_bytes(partial)
    remaining = full[len(partial):]

    resp = _FakeRangedURLResponse(
        remaining,
        status=206,
        headers={
            "Content-Length": str(len(remaining)),
            "Content-Range": f"bytes {len(partial)}-{len(full) - 1}/{len(full)}",
        },
    )
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return resp

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    dl = _make_urllib_dl(tmp_path)
    result = dl.download(MODELSCOPE_REPO, MODELSCOPE_FILE, source="modelscope")

    dest = tmp_path / MODELSCOPE_FILE
    assert result == dest
    assert dest.read_bytes() == full                     # 断点前后拼接完整
    assert not any(p.suffix == ".tmp" for p in tmp_path.iterdir())
    # 请求头带 Range，起点为 tmp 当前大小
    assert captured["req"].headers.get("Range") == f"bytes={len(partial)}-"


def test_download_urllib_restart_overwrites_tmp_on_200(tmp_path, monkeypatch):
    """tmp 存在但服务器忽略 Range 返回 200 → 从头重写覆盖 tmp（不留旧断点残留）。"""
    stale = b"STALE-OLD-PARTIAL-DATA"
    tmp_file = tmp_path / (MODELSCOPE_FILE + ".tmp")
    tmp_file.write_bytes(stale)

    fresh = b"FRESH"
    resp = _FakeRangedURLResponse(fresh)  # 无 status 属性 → 视作 200

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: resp)
    dl = _make_urllib_dl(tmp_path)
    result = dl.download(MODELSCOPE_REPO, MODELSCOPE_FILE, source="modelscope")

    dest = tmp_path / MODELSCOPE_FILE
    assert dest.read_bytes() == fresh                    # 干净重写，无旧内容
    assert result == dest


class _FakeRangedStreamResponse:
    """模拟 requests 流式响应：记录状态码与 Range 相关头。"""

    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload or b""
        self.status_code = status_code
        self.headers = headers if headers is not None else {"Content-Length": str(len(self._payload))}
        self._offset = 0

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=8192):
        while self._offset < len(self._payload):
            chunk = self._payload[self._offset : self._offset + chunk_size]
            self._offset += len(chunk)
            yield chunk


class _FakeRangedRequests:
    """模拟支持 Range 的 requests.get：206 走截断体，200 走全量体。"""

    def __init__(self, data):
        self.data = data
        self.calls = []

    def get(self, url, stream=False, timeout=None, headers=None):
        headers = headers or {}
        self.calls.append(headers)
        rng = headers.get("Range")
        if rng:
            start = int(rng.split("=", 1)[1].rstrip("-"))
            body = self.data[start:]
            return _FakeRangedStreamResponse(
                body,
                status_code=206,
                headers={
                    "Content-Length": str(len(body)),
                    "Content-Range": f"bytes {start}-{len(self.data) - 1}/{len(self.data)}",
                },
            )
        return _FakeRangedStreamResponse(self.data, status_code=200)


def test_download_requests_resume_206_appends(tmp_path):
    """requests 路径：tmp 存在 + 206 → 追加续传并完整落盘。"""
    full = bytes(range(10))
    partial = full[:5]
    (tmp_path / (MODELSCOPE_FILE + ".tmp")).write_bytes(partial)

    dl = LlmDownloader(dest_dir=str(tmp_path))
    dl._requests = _FakeRangedRequests(full)
    dl._using_requests = True

    result = dl.download(MODELSCOPE_REPO, MODELSCOPE_FILE, source="modelscope")

    dest = tmp_path / MODELSCOPE_FILE
    assert result == dest
    assert dest.read_bytes() == full
    assert dl._requests.calls[0].get("Range") == f"bytes={len(partial)}-"


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