# -*- coding: utf-8 -*-
"""本地小 LLM 可选下载引导（Task C2）：LlmDownloader 与 get_local_llm_info。

对齐工程文档 §4.4「可选组件下载（本地小 LLM）」：
- **下载源**：HuggingFace / 魔塔（ModelScope）双源，国内优先魔塔（modelscope 默认）；
- **格式**：GGUF（供 llama.cpp / LlamaRuntime 消费）；
- **尺寸建议**：~1.7B（判定够快 + 断网兜底够用）；
- **存储**：``data/local_llm/``。

设计要点（对齐 C2 必做清单）：
- ``dest_dir`` 默认通过 ``os.path.abspath(__file__)`` 逐级 dirname 推导到工程根再拼
  ``data/local_llm``，全程绝对路径，无相对路径/``../../`` 硬编码；
- **双源 URL 模板 + 统一入参校验**：``repo``（org/name 双段）与 ``filename``
  （强制 ``.gguf`` 后缀、拒绝 ``../`` 与路径分隔符）；
- **可选请求库降级**：优先 ``import requests`` 流式下载，未安装时以
  ``urllib.request`` 兜底；
- **流式写临时文件后改名**：写入 ``<filename>.tmp``，完成后 ``os.replace`` 原子改名，
  避免损坏一半的模型文件落在目标目录；
- **校验**：``verify_size_gb`` 给出时下载/已存在文件均做大小时长校验，
  偏差超过 20% 抛 ``ValueError``；已存在同名文件则跳过并提示；
- **全程中文 ``[INFO]``/``[ERROR]`` + 时间戳日志**，``progress_cb(downloaded,total)``
  可选进度回调（first+ 块粒度，不依赖真实网络可 mock 测试）。
- 本模块不 import 任何未实现模块。
"""

import os
import time
import urllib.request

from pathlib import Path

#: 工程根目录推导：model_downloader.py 位于 lite/runtime/ 下，上溯两层 dirname 即工程根
_LITE_RUNTIME_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_LITE_RUNTIME_DIR))
#: 默认本地小 LLM 存储目录（工程根 / data / local_llm）
DEFAULT_DOWNLOAD_DIR = os.path.normpath(os.path.join(_PROJECT_ROOT, "data", "local_llm"))

#: 支持的下载源别名 -> 规范名（国内优先魔塔）
_SOURCE_ALIASES = {
    "modelscope": "modelscope",
    "ms": "modelscope",
    "魔塔": "modelscope",
    "huggingface": "huggingface",
    "hf": "huggingface",
    "HF": "huggingface",
}


def _now_str():
    """返回形如 2026-08-26 12:00:00 的本地时间戳字符串。"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


class LlmDownloader:
    """本地小 LLM 下载引导器（HF / 魔塔双源，GGUF）。

    职责：
    1. 校验并构造下载 URL（modelscope / huggingface 双源模板）；
    2. 校验 repo（org/name 双段）与 filename（强制 .gguf、拒绝 ../）；
    3. 流式下载模型到 ``data/local_llm/``（可选 requests，urllib 兜底）；
    4. 提供建议模型清单与本地已下载模型扫描。
    """

    def __init__(self, dest_dir=None, source="modelscope", timeout=60):
        """初始化下载器。

        Args:
            dest_dir: 模型存储目录；默认 ``工程根/data/local_llm``（基于
                ``os.path.abspath(__file__)`` 逐级推导，无相对路径）。
            source: 默认下载源，'' 时取默认 ``"modelscope"``（国内优先魔塔）。
            timeout: 网络请求超时秒数。
        """
        self.dest_dir = dest_dir or DEFAULT_DOWNLOAD_DIR
        self.source = source or "modelscope"
        self.timeout = timeout
        #: 可选 requests 库；未安装时置 None，下载走 urllib 兜底。
        self._requests = None
        self._using_requests = False
        try:
            import requests  # noqa: PLC0415 - 可选依赖，未装时降级 urllib
            self._requests = requests
            self._using_requests = True
        except ImportError:
            self._using_requests = False

    # ------------------------------------------------------------------ #
    # 日志                                                               #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _log(level, msg):
        """中文时间戳日志：``YYYY-MM-DD HH:MM:SS [INFO/ERROR] 内容``。"""
        print(f"{_now_str()} [{level}] {msg}")

    # ------------------------------------------------------------------ #
    # 入参校验                                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _validate_repo(repo):
        """校验仓库名：须为 org/name 双段，且不允许危险路径（``..`` / ``/`` 开头 / 反斜杠）。

        Args:
            repo: 仓库标识，形如 ``Qwen/Qwen1.5-1.8B-Chat-GGUF``。
        Raises:
            ValueError: 仓库名为空、非双段、或含危险路径时抛出。
        """
        if not isinstance(repo, str) or not repo.strip():
            raise ValueError("仓库名不能为空，格式应为 org/name（如 Qwen/Qwen1.5-1.8B-Chat-GGUF）。")
        if repo.startswith("/") or "\\" in repo:
            raise ValueError(f"非法仓库名：{repo!r}（不允许以 / 开头或含反斜杠）。")
        parts = repo.split("/")
        if len(parts) < 2 or any((not seg) or seg in (".", "..") for seg in parts):
            raise ValueError(f"非法仓库名：{repo!r}（需为 org/name 双段且不含危险路径）。")

    @staticmethod
    def _validate_filename(filename):
        """校验文件名：非空、强制 ``.gguf`` 后缀、拒绝路径分隔符与 ``../``。

        Args:
            filename: 模型文件名，形如 ``qwen1.5-1_8b-chat-q4_k_m.gguf``。
        Raises:
            ValueError: 文件名为空、非 .gguf、或含危险路径时抛出。
        """
        if not isinstance(filename, str) or not filename.strip():
            raise ValueError("文件名不能为空。")
        if "/" in filename or "\\" in filename or ".." in filename:
            raise ValueError(f"非法文件名：{filename!r}（不允许含路径分隔符或危险路径）。")
        if not filename.endswith(".gguf"):
            raise ValueError(f"非法文件名：{filename!r}（必须为 .gguf 后缀）。")

    # ------------------------------------------------------------------ #
    # URL 构造                                                           #
    # ------------------------------------------------------------------ #

    def build_url(self, source, repo, filename) -> str:
        """按下载源模板构造模型文件下载 URL，并校验入参。

        Args:
            source: 下载源（modelscope / huggingface 或其别名）。
            repo: 仓库标识（org/name 双段）。
            filename: 模型文件名（强制 .gguf）。
        Returns:
            str: 可直接请求的下载 URL。
        Raises:
            ValueError: 下载源未知、repo / filename 非法时抛出。
        """
        src = (source or self.source).lower()
        if src not in _SOURCE_ALIASES:
            raise ValueError(f"未知下载源：{source!r}，仅支持 modelscope / huggingface。")
        src = _SOURCE_ALIASES[src]
        self._validate_repo(repo)
        self._validate_filename(filename)
        if src == "modelscope":
            return f"https://modelscope.cn/api/v1/models/{repo}/repo?FilePath={filename}"
        return f"https://huggingface.co/{repo}/resolve/main/{filename}"

    # ------------------------------------------------------------------ #
    # 大小校验                                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _check_size(file_size_bytes, verify_size_gb, filename):
        """对文件大小做大时长校验（相对 20% 容差）。

        Args:
            file_size_bytes: 实际文件字节数。
            verify_size_gb: 预期文件大小（GB），须 > 0。
            filename: 模型文件名，用于报错提示。
        Raises:
            ValueError: verify_size_gb 非法，或实际大小偏差超 20% 时抛出。
        """
        expected_gb = float(verify_size_gb)
        if expected_gb <= 0:
            raise ValueError(f"verify_size_gb 必须为正数，收到：{verify_size_gb!r}。")
        expected = int(expected_gb * (1024 ** 3))
        actual = int(file_size_bytes)
        ratio = abs(actual - expected) / expected if expected else 1.0
        if ratio > 0.20:
            raise ValueError(
                f"模型文件 {filename} 大小与预期偏差超过 20%："
                f"实际 {actual} 字节（约 {actual / (1024 ** 3):.2f} GB），"
                f"预期 {verify_size_gb} GB。"
            )

    @classmethod
    def _stream_from_response(cls, read_chunks, total, tmp_path, progress_cb):
        """把分块迭代器流式写入临时文件，返回累计字节数。

        Args:
            read_chunks: 块迭代器（每次产出 bytes 或 None）。
            total: 响应声明总长度（Content-Length，无则 0）。
            tmp_path: 临时文件路径。
            progress_cb: 可选回调 ``cb(downloaded, total)``。
        Returns:
            int: 实际累计下载字节数。
        """
        done = 0
        with open(tmp_path, "wb") as f:
            for chunk in read_chunks:
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if progress_cb is not None:
                    progress_cb(done, total)
        return done

    def _download_requests(self, url, tmp_path, progress_cb):
        """使用 requests 流式下载（分块写临时文件），返回 ``(downloaded, total)``。"""
        resp = self._requests.get(url, stream=True, timeout=self.timeout)
        resp.raise_for_status()
        total = int((resp.headers or {}).get("Content-Length", 0) or 0)
        downloaded = self._stream_from_response(
            resp.iter_content(chunk_size=64 * 1024), total, tmp_path, progress_cb
        )
        return downloaded, total

    def _download_urllib(self, url, tmp_path, progress_cb):
        """使用 urllib.request 兜底流式下载，返回 ``(downloaded, total)``（未装 requests 时）。"""
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - 下载源为固定白名单双源
            total = int((resp.headers or {}).get("Content-Length", 0) or 0)

            def _iterator():
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk

            downloaded = self._stream_from_response(_iterator(), total, tmp_path, progress_cb)
            return downloaded, total

    def download(self, repo, filename, source=None, progress_cb=None, verify_size_gb=None) -> Path:
        """流式下载 GGUF 模型到存储目录（临时文件 + 原子改名）。

        Args:
            repo: 仓库标识（org/name 双段）。
            filename: 模型文件名（强制 .gguf）。
            source: 下载源；None 时用实例的默认 source。
            progress_cb: 可选进度回调 ``cb(downloaded, total)``。
            verify_size_gb: 可选预期大小（GB）；给出时下载/已存在均做 20% 容差校验。
        Returns:
            Path: 下载/已存在模型的绝对路径。
        Raises:
            ValueError: 入参非法、未知下载源、或大小校验失败时抛出。
        """
        self._validate_repo(repo)
        self._validate_filename(filename)
        src = (source or self.source).lower()
        if src not in _SOURCE_ALIASES:
            raise ValueError(f"未知下载源：{source!r}，仅支持 modelscope / huggingface。")
        url = self.build_url(src, repo, filename)

        os.makedirs(self.dest_dir, exist_ok=True)
        dest = os.path.join(self.dest_dir, filename)

        # 已存在同名文件 → 跳过下载（若给出 verify_size 则校验大小）
        if os.path.exists(dest) and os.path.isfile(dest):
            self._log("INFO", f"模型文件已存在，跳过下载：{dest}")
            if verify_size_gb is not None:
                self._check_size(os.path.getsize(dest), verify_size_gb, filename)
            return Path(dest)

        if self._using_requests and self._requests is not None:
            self._log("INFO", f"开始下载：{url}")
            tmp = dest + ".tmp"
            try:
                downloaded, total = self._download_requests(url, tmp, progress_cb)
            except Exception as exc:  # noqa: BLE001 - 网络/写入异常统一清理临时文件后上抛
                if os.path.exists(tmp):
                    os.remove(tmp)
                self._log("ERROR", f"下载失败：{filename}（{exc}）")
                raise
        else:
            self._log("INFO", f"开始下载（urllib 兜底）：{url}")
            tmp = dest + ".tmp"
            try:
                downloaded, total = self._download_urllib(url, tmp, progress_cb)
            except Exception as exc:  # noqa: BLE001
                if os.path.exists(tmp):
                    os.remove(tmp)
                self._log("ERROR", f"下载失败：{filename}（{exc}）")
                raise

        # M12 修复：所有校验前置于 os.replace——损坏/伪造大小的模型不得落到正式位。
        # 1) Content-Length 可得时校验 done == total，不符删 tmp 抛 ValueError
        if total and int(downloaded) != int(total):
            if os.path.exists(tmp):
                os.remove(tmp)
            self._log(
                "ERROR",
                f"下载不完整：{filename}（预期 {total} 字节，实际 {downloaded} 字节），已删除临时文件",
            )
            raise ValueError(
                f"模型文件 {filename} 下载不完整：预期 {total} 字节，实际 {downloaded} 字节。"
            )
        # 2) 大小校验（相对预期 20% 容差），失败删 tmp 并抛 ValueError
        if verify_size_gb is not None:
            try:
                self._check_size(downloaded, verify_size_gb, filename)
            except ValueError as exc:
                if os.path.exists(tmp):
                    os.remove(tmp)
                self._log("ERROR", f"大小校验失败（已删除临时文件）：{filename}（{exc}）")
                raise

        os.replace(tmp, dest)
        self._log("INFO", f"下载完成：{dest}（{downloaded} 字节）")
        return Path(dest)

    # ------------------------------------------------------------------ #
    # 建议模型                                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def suggest_model(source="modelscope") -> dict:
        """返回指定下载源的推荐小 LLM 信息（~1.7B GGUF，Qwen 系）。

        Args:
            source: 下载源（modelscope / huggingface）。
        Returns:
            dict: 含 source、repo、filename、approximate_size_gb、disclaimer 等。
                大小为近似估算，实际以仓库为准。
        """
        src = (source or "modelscope").lower()
        if src not in _SOURCE_ALIASES:
            src = "modelscope"
        src = _SOURCE_ALIASES[src]
        baseline = {
            "repo": "Qwen/Qwen1.5-1.8B-Chat-GGUF",
            "filename": "qwen1.5-1_8b-chat-q4_k_m.gguf",
            "family": "Qwen1.5-1.8B-Chat",
            "quant": "Q4_K_M",
            "approximate_size_gb": 1.7,
            "reason": "1.7B 判定够快 + 断网兜底够用（工程文档 §4.4）",
            "disclaimer": "approximate_size_gb 为近似估算，实际大小以下载仓库对应文件为准。",
        }
        if src == "modelscope":
            baseline.update(
                source="modelscope",
                url_hint="https://modelscope.cn/models/Qwen/Qwen1.5-1.8B-Chat-GGUF",
                note="国内优先建议使用魔塔（ModelScope）下载。",
            )
        else:
            baseline.update(
                source="huggingface",
                url_hint="https://huggingface.co/Qwen/Qwen1.5-1.8B-Chat-GGUF",
                note="HuggingFace 源备选；国内网络若受限建议切回 modelscope。",
            )
        return baseline


def get_local_llm_info(dest_dir) -> dict:
    """扫描本地小 LLM 存储目录，返回已存在的 GGUF 模型信息。

    Args:
        dest_dir: 模型存储目录（默认工程根 data/local_llm）。
    Returns:
        Optional[dict]: 目录下存在 .gguf 时返回
        ``{"count": n, "models": [{"path", "size_bytes", "size_gb"}, ...]}``；
        目录不存在或无 .gguf 时返回 None。
    """
    if not dest_dir or not os.path.isdir(dest_dir):
        return None
    models = []
    try:
        entries = sorted(os.listdir(dest_dir))
    except OSError:
        return None
    for entry in entries:
        if not entry.endswith(".gguf"):
            continue
        path = os.path.join(dest_dir, entry)
        if not os.path.isfile(path):
            continue
        size = os.path.getsize(path)
        models.append(
            {
                "path": path,
                "size_bytes": size,
                "size_gb": round(size / (1024 ** 3), 3),
            }
        )
    if not models:
        return None
    return {"count": len(models), "models": models}