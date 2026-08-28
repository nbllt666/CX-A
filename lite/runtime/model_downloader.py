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
import re
import shutil
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

    # ------------------------------------------------------------------ #
    # 磁盘预检与 Range 续传辅助（L10）                                    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _disk_free_bytes(path):
        """返回 path（或其最近存在的祖先目录）所在磁盘的可用字节数。

        Args:
            path: 目标目录；允许不存在（逐级向上找最近存在祖先）。
        Returns:
            int: 可用字节数；探测失败时返回 float("inf") 以不阻断下载。
        """
        probe = path
        while probe and not os.path.exists(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        try:
            return shutil.disk_usage(probe or os.getcwd()).free
        except OSError:
            return float("inf")

    @classmethod
    def _ensure_disk_space(cls, path, expected_bytes, filename=""):
        """磁盘空间预检（L10）：可用空间 < 预期×1.05 时抛 ValueError，且不落任何文件。

        Args:
            path: 预检目标目录。
            expected_bytes: 预期文件大小（字节），须 > 0 才生效。
            filename: 文件名（仅用于日志/报错提示）。
        Raises:
            ValueError: 磁盘可用空间不足时抛出。
        """
        expected = int(expected_bytes)
        if expected <= 0:
            return
        needed = int(expected * 1.05)
        free = cls._disk_free_bytes(path)
        if free < needed:
            raise ValueError(
                f"磁盘空间不足：{filename or '模型文件'}需约 {expected / (1024 ** 3):.2f} GB"
                f"（含 5% 余量需 {needed / (1024 ** 3):.2f} GB），"
                f"当前可用 {free / (1024 ** 3):.2f} GB。"
            )

    @staticmethod
    def _content_range_total(headers, already_have):
        """从 206 响应头推算整文件总字节数（断点续传用）。

        优先解析 ``Content-Range: bytes start-end/total`` 的 total；
        缺失时以 ``Content-Length(剩余长度) + already_have`` 兜底；仍不可得返回 0。
        """
        try:
            content_range = headers.get("Content-Range") or ""
        except AttributeError:
            content_range = ""
        match = re.search(r"/\s*(\d+)\s*$", str(content_range))
        if match:
            return int(match.group(1))
        try:
            rest = int(headers.get("Content-Length", 0) or 0)
        except (AttributeError, ValueError, TypeError):
            rest = 0
        return already_have + rest

    @classmethod
    def _stream_from_response(cls, read_chunks, total, tmp_path, progress_cb, start_offset=0):
        """把分块迭代器流式写入临时文件，返回累计字节数。

        Args:
            read_chunks: 块迭代器（每次产出 bytes 或 None）。
            total: 响应声明总长度（Content-Length 或续传时的整文件总长，无则 0）。
            tmp_path: 临时文件路径。
            progress_cb: 可选回调 ``cb(downloaded, total)``。
            start_offset: 续传起点字节数（>0 时以追加模式写入，进度从该偏移量续计）。
        Returns:
            int: 实际累计下载字节数（含 start_offset）。
        """
        done = int(start_offset)
        mode = "ab" if done > 0 else "wb"
        with open(tmp_path, mode) as f:
            for chunk in read_chunks:
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if progress_cb is not None:
                    progress_cb(done, total)
        return done

    def _download_requests(self, url, tmp_path, progress_cb, resume_size=0):
        """使用 requests 流式下载（分块写临时文件），返回 ``(downloaded, total)``。

        L10：resume_size > 0 时携带 ``Range: bytes=<resume_size>-`` 请求头；
        服务器响应 206 则以追加模式断点续传，响应非 206（忽略 Range 返回 200）
        则从头重写覆盖 tmp。开流写盘前按 Content-Length 做磁盘预检。
        """
        kwargs = {"stream": True, "timeout": self.timeout}
        seeking = False
        if resume_size and resume_size > 0:
            kwargs["headers"] = {"Range": f"bytes={int(resume_size)}-"}
            seeking = True
        # L-13（第三轮体检批次5）：with 确定性关闭响应——修复前 _ensure_disk_space
        # 抛出 / 写盘 IO 异常时连接只能等 GC 回收
        with self._requests.get(url, **kwargs) as resp:
            resp.raise_for_status()
            headers = resp.headers or {}
            if seeking and getattr(resp, "status_code", None) == 206:
                start = int(resume_size)
                total = self._content_range_total(headers, start)
            else:
                start = 0
                total = int(headers.get("Content-Length", 0) or 0)
            self._ensure_disk_space(self.dest_dir, total, os.path.basename(tmp_path))
            downloaded = self._stream_from_response(
                resp.iter_content(chunk_size=64 * 1024), total, tmp_path, progress_cb,
                start_offset=start,
            )
        return downloaded, total

    def _download_urllib(self, url, tmp_path, progress_cb, resume_size=0):
        """使用 urllib.request 兜底流式下载，返回 ``(downloaded, total)``（未装 requests 时）。

        L10：resume_size > 0 时携带 ``Range`` 头；resp.status==206 追加续传，
        否则从头重写覆盖。开流写盘前按 Content-Length 做磁盘预检。
        """
        req = urllib.request.Request(url)
        seeking = False
        if resume_size and resume_size > 0:
            req.add_header("Range", f"bytes={int(resume_size)}-")
            seeking = True
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - 下载源为固定白名单双源
            headers = resp.headers or {}
            if seeking and getattr(resp, "status", None) == 206:
                start = int(resume_size)
                total = self._content_range_total(headers, start)
            else:
                start = 0
                total = int(headers.get("Content-Length", 0) or 0)

            def _iterator():
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk

            downloaded = self._stream_from_response(
                _iterator(), total, tmp_path, progress_cb, start_offset=start
            )
            return downloaded, total

    def download(self, repo, filename, source=None, progress_cb=None, verify_size_gb=None) -> Path:
        """流式下载 GGUF 模型到存储目录（临时文件 + 原子改名 + 断点续传）。

        Args:
            repo: 仓库标识（org/name 双段）。
            filename: 模型文件名（强制 .gguf）。
            source: 下载源；None 时用实例的默认 source。
            progress_cb: 可选进度回调 ``cb(downloaded, total)``。
            verify_size_gb: 可选预期大小（GB）；给出时下载/已存在均做 20% 容差校验。
        Returns:
            Path: 下载/已存在模型的绝对路径。
        Raises:
            ValueError: 入参非法、未知下载源、磁盘空间不足或大小校验失败时抛出。

        L10 行为：
        - 下载前磁盘预检：verify_size_gb 可推算预期大小时，可用空间 < 预期×1.05
          直接抛 ValueError，不落任何文件（响应头 Content-Length 提前得知时，
          开流写盘前按同一口径二次预检）；
        - 断点续传：网络中断保留 ``<filename>.tmp``；重入时若 tmp 存在则携带
          Range 头请求，206 追加续传、200 从头重写覆盖；
        - 最终校验逻辑（done==total / verify_size 容差 / 失败删 tmp）不变。
        """
        self._validate_repo(repo)
        self._validate_filename(filename)
        src = (source or self.source).lower()
        if src not in _SOURCE_ALIASES:
            raise ValueError(f"未知下载源：{source!r}，仅支持 modelscope / huggingface。")
        url = self.build_url(src, repo, filename)

        # L10：预期大小可由 verify_size_gb 推算时，先做磁盘预检再创建目录/写文件
        if verify_size_gb is not None and float(verify_size_gb) > 0:
            expected_bytes = int(float(verify_size_gb) * (1024 ** 3))
            self._ensure_disk_space(self.dest_dir, expected_bytes, filename)

        os.makedirs(self.dest_dir, exist_ok=True)
        dest = os.path.join(self.dest_dir, filename)

        # 已存在同名文件 → 大小校验通过则跳过下载（G-9 自愈：校验失败视为坏文件，
        # 删除后继续走正常下载流程，不再永久短路下载）
        if os.path.exists(dest) and os.path.isfile(dest):
            if verify_size_gb is not None:
                try:
                    self._check_size(os.path.getsize(dest), verify_size_gb, filename)
                except ValueError as exc:
                    # G-9：已存在文件大小校验失败 → 删除坏文件重新下载（校验失败不 return）
                    self._log("WARNING", f"已存在文件大小校验失败，删除坏文件后重新下载：{filename}（{exc}）")
                    try:
                        os.remove(dest)
                    except OSError as rm_exc:
                        self._log("ERROR", f"删除坏文件失败，无法继续下载：{dest}（{rm_exc}）")
                        raise
                else:
                    self._log("INFO", f"模型文件已存在，跳过下载：{dest}")
                    return Path(dest)
            else:
                self._log("INFO", f"模型文件已存在，跳过下载：{dest}")
                return Path(dest)

        # L10：断点重入检测——已有 .tmp 时从其大小处请求续传
        tmp = dest + ".tmp"
        resume_size = 0
        if os.path.isfile(tmp):
            try:
                resume_size = max(0, os.path.getsize(tmp))
            except OSError:
                resume_size = 0
        if resume_size > 0:
            self._log("INFO", f"发现未完成的临时文件（{resume_size} 字节），尝试断点续传：{filename}")

        if self._using_requests and self._requests is not None:
            self._log("INFO", f"开始下载：{url}")
            # 异常时不再删除 .tmp（L10）：保留供下次断点续传
            try:
                downloaded, total = self._download_requests(url, tmp, progress_cb, resume_size=resume_size)
            except Exception as exc:  # noqa: BLE001 - 网络/写入异常统一记录后上抛
                self._log("ERROR", f"下载失败（已保留 .tmp 供断点续传）：{filename}（{exc}）")
                raise
        else:
            self._log("INFO", f"开始下载（urllib 兜底）：{url}")
            try:
                downloaded, total = self._download_urllib(url, tmp, progress_cb, resume_size=resume_size)
            except Exception as exc:  # noqa: BLE001
                self._log("ERROR", f"下载失败（已保留 .tmp 供断点续传）：{filename}（{exc}）")
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