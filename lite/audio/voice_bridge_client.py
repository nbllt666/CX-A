# -*- coding: utf-8 -*-
"""语音桥客户端（lite/audio/voice_bridge_client.py）——常驻 sidecar 进程管理 + uv1 协议。

主后端经本客户端驱动 sidecar 环境内的 :mod:`lite.audio.voice_bridge`：

- **常驻复用**：首次请求惰性拉起子进程，之后复用同一进程（MeloTTS/funasr
  模型常驻内存，避免每次合成长冷启动）；
- **帧协议**：与 bridge 对齐（单行 JSON 头 + 可选原始载荷），协议原文见
  ``lite/audio/voice_bridge.py`` 模块 docstring（唯一真相源）；
- **健壮性**：读写在同一受控线程内完成并带超时（超时判定卡死 → 杀进程重启
  一次再重试）；子进程 stderr 由读线程收集，错误路径下回传尾部诊断；
- **生命周期**：父进程退出时 stdin 关闭 → bridge 主循环遇 EOF 自然退出
  （无需父进程显式回收）；:meth:`close` 供显式收尾（发 shutdown 后兜底 terminate）。

路径口径（frozen-aware，与项目既有 ``app_root()`` 单一真相源一致）：

- 便携根：``lite.config.paths.app_root()``（冻结态 = exe 上溯 2 级）；
- sidecar Python：``<root>/runtime/voice/python.exe``；
- bridge 脚本：**仅认分发落点** ``<root>/runtime/voice_bridge/bridge.py``
  （开发态无此落点时稳定回退 Mock；联测可经构造参数 ``script_path`` 显式注入源码）；
- HF 缓存重定向：``HF_HOME=<root>/data/hf_cache``（便携原则，模型落便携根、
  不污染用户目录）；``HF_ENDPOINT`` **缺省不注入、走官方端点**（镜像 308 跨域
  重定向不被旧客户端跟随，见 :param:`hf_endpoint`）。
"""

import collections
import json
import os
import subprocess
import sys
import threading

from lite.config.paths import app_root

__all__ = ["VoiceBridgeError", "VoiceBridgeClient"]

#: 单请求默认超时（秒）：首次 TTS 需下载 MeloTTS 权重（数百 MB），放宽至 30 分钟。
DEFAULT_TIMEOUT_S = 1800
#: 卡死重启重试上限（一次）。
_RESTART_LIMIT = 1
#: 协议流容忍的非 JSON 行上限（第三方库裸打印兜底；bridge 已优先在源头隔离）。
_PROTOCOL_NOISE_LIMIT = 20
#: stderr 收集行数上限（诊断用）。
_STDERR_KEEP_LINES = 200
#: Windows CREATE_NO_WINDOW（避免桥进程弹控制台窗口）。
_CREATE_NO_WINDOW = 0x08000000


class VoiceBridgeError(RuntimeError):
    """桥请求失败（进程不可用 / 超时卡死 / 协议错误 / sidecar 返回 ok=false）。"""


class VoiceBridgeClient:
    """sidecar 语音桥客户端（进程管理 + uv1 协议，线程安全）。"""

    def __init__(
        self,
        root=None,
        python_exe=None,
        script_path=None,
        device="cpu",
        timeout=DEFAULT_TIMEOUT_S,
        hf_endpoint="",
        hf_home=None,
    ):
        """初始化客户端（不启动进程；进程在首次 :meth:`request` 时惰性拉起）。

        :param root: 便携根；缺省经 ``app_root()`` 推导（frozen-aware）。
        :param python_exe: sidecar 解释器显式路径；缺省 ``<root>/runtime/voice/python.exe``。
        :param script_path: bridge 脚本显式路径；缺省按"分发落点 → 源码兜底"解析。
        :param device: 推理设备串（cpu / cuda / cuda:0）。
        :param timeout: 单请求超时秒数（读写整体）。
        :param hf_endpoint: HF 镜像端点（注入子进程 ``HF_ENDPOINT``）；**缺省空串 =
            不注入、走官方端点**。实测（2026-09-25）：hf-mirror 对未缓存文件返回
            308 重定向回 huggingface.co，而 transformers 4.27 / hub 客户端**不跟随
            跨域重定向** → 必然失败；本机官方端点直连可用，故缺省不用镜像
            （用户环境如需镜像可显式覆盖本参数）。
        :param hf_home: HF 缓存目录；缺省 ``<root>/data/hf_cache``。
        """
        self.root = root or app_root()
        self.python_exe = python_exe or os.path.join(self.root, "runtime", "voice", "python.exe")
        self.script_path = script_path or self._resolve_script()
        self.device = self._normalize_device(device)
        self.timeout = timeout
        self.hf_endpoint = hf_endpoint
        self.hf_home = hf_home or os.path.join(self.root, "data", "hf_cache")
        self._proc = None
        self._lock = threading.Lock()
        self._next_id = 1
        self._stderr_lines = collections.deque(maxlen=_STDERR_KEEP_LINES)
        self._stderr_thread = None

    @staticmethod
    def _normalize_device(device):
        """设备意图归一：``"gpu"`` → ``"auto"``（由 sidecar 侧 torch 判定可用性）。

        其余取值（``cpu`` / ``cuda`` / ``cuda:0`` / ``auto``）原样透传；
        空值回 ``cpu``。主进程无 torch，不做本地可用性判断。
        """
        value = str(device or "cpu").strip().lower() or "cpu"
        return "auto" if value == "gpu" else value

    def _resolve_script(self):
        """解析 bridge 脚本路径（**仅认分发落点**）。

        分发落点：``<root>/runtime/voice_bridge/bridge.py``（安装链落位口径）。
        源码路径**不作为就位依据**——避免"开发机器上源码偶然可用"导致环境相关
        行为差异（无 sidecar 的宿主必须稳定回退 Mock）；联测/调试可经构造参数
        ``script_path`` 显式注入源码路径。

        :return: 分发落点脚本绝对路径（存在与否由 :meth:`available` 判定）。
        """
        return os.path.join(self.root, "runtime", "voice_bridge", "bridge.py")

    def available(self):
        """sidecar 解释器与**分发落点**的 bridge 脚本均存在时返回 True（不启动进程）。"""
        return os.path.isfile(self.python_exe) and os.path.isfile(self.script_path)

    # ------------------------------------------------------------------ #
    # 进程管理                                                            #
    # ------------------------------------------------------------------ #

    def _build_env(self):
        """构造子进程环境：HF 缓存 / NLTK 数据 / 临时目录均落便携根（不污染用户目录）。

        - ``HF_HOME`` → ``<root>/data/hf_cache``（MeloTTS/BERT 权重缓存）；
        - ``HF_HUB_DISABLE_SYMLINKS_WARNING`` → 抑制 Windows 无符号链接权限的噪音告警
          （降级为复制模式，功能不受影响）；
        - ``HF_ENDPOINT`` 仅在显式给出 hf_endpoint 时注入（缺省走官方端点）；
        - ``NLTK_DATA`` → ``<root>/data/nltk_data``（g2p_en 依赖的 nltk 语料）；
        - ``TEMP`` / ``TMP`` → ``<root>/data/tmp``（jieba 词频缓存等临时产物；
          仅影响 sidecar 进程及其子进程，系统级 TEMP 不变）。
        """
        env = dict(os.environ)
        tmp_dir = os.path.join(self.root, "data", "tmp")
        if self.hf_home:
            env.setdefault("HF_HOME", self.hf_home)
            env.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        if self.hf_endpoint:
            env.setdefault("HF_ENDPOINT", self.hf_endpoint)
        env.setdefault("NLTK_DATA", os.path.join(self.root, "data", "nltk_data"))
        # TEMP/TMP 强制覆盖（系统必有该变量，setdefault 无法实现重定向目的）
        env["TEMP"] = tmp_dir
        env["TMP"] = tmp_dir
        return env

    def _ensure_process(self):
        """确保常驻桥进程已启动（已存活则直接返回）。

        :raises VoiceBridgeError: 解释器/脚本缺失或进程启动失败。
        """
        if self._proc is not None and self._proc.poll() is None:
            return
        if not os.path.isfile(self.python_exe):
            raise VoiceBridgeError(f"sidecar 解释器不存在：{self.python_exe}")
        if not os.path.isfile(self.script_path):
            raise VoiceBridgeError(f"bridge 脚本不存在：{self.script_path}")
        # 便携临时目录须先存在（子进程 TEMP/TMP 指向此处）
        os.makedirs(os.path.join(self.root, "data", "tmp"), exist_ok=True)
        creationflags = _CREATE_NO_WINDOW if sys.platform == "win32" else 0
        proc = subprocess.Popen(
            [
                self.python_exe,
                self.script_path,
                "--root", self.root,
                "--device", self.device,
                *(["--hf-endpoint", self.hf_endpoint] if self.hf_endpoint else []),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._build_env(),
            creationflags=creationflags,
        )
        self._proc = proc
        self._stderr_lines.clear()
        self._stderr_thread = threading.Thread(
            target=self._pump_stderr, args=(proc,), daemon=True
        )
        self._stderr_thread.start()

    def _pump_stderr(self, proc):
        """后台线程：持续收集子进程 stderr 尾部（诊断用），进程退出即结束。"""
        try:
            for raw in proc.stderr:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    self._stderr_lines.append(line)
        except Exception:  # noqa: BLE001 - 诊断线程不允许影响主流程
            pass

    def _kill_process(self):
        """终止桥进程并等待回收（幂等）。"""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 - 回收失败不抛（进程随系统回收）
            pass
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------------ #
    # 协议交互                                                            #
    # ------------------------------------------------------------------ #

    def _read_frame(self):
        """读一帧响应：单行 JSON 头 + 可选原始载荷。

        容忍前缀噪声：第三方库若向 stdout 裸打印（bridge 已优先在源头隔离，
        此处为兜底），跳过不以 ``{`` 开头的行并记入 stderr 诊断（最多
        :data:`_PROTOCOL_NOISE_LIMIT` 行）。

        :return: ``(header_dict, payload_bytes)``
        :raises VoiceBridgeError: 流关闭 / 噪声超限 / 头部非 JSON 时。
        """
        attempts = 0
        while True:
            header_line = self._proc.stdout.readline()
            if not header_line:
                raise VoiceBridgeError("桥进程输出流已关闭（进程可能已退出）")
            if header_line.lstrip().startswith(b"{"):
                break
            attempts += 1
            self._stderr_lines.append(f"[非协议输出] {header_line.strip()[:200]!r}")
            if attempts > _PROTOCOL_NOISE_LIMIT:
                raise VoiceBridgeError(
                    f"桥协议流被非 JSON 输出持续污染（已跳过 {_PROTOCOL_NOISE_LIMIT} 行）"
                )
        try:
            header = json.loads(header_line.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - 协议帧损坏
            raise VoiceBridgeError(f"桥响应头解析失败：{exc}") from exc
        length = int(header.get("length") or 0)
        payload = b""
        while length > 0:
            chunk = self._proc.stdout.read(length)
            if not chunk:
                raise VoiceBridgeError("桥载荷读取中断（进程可能已退出）")
            payload += chunk
            length -= len(chunk)
        return header, payload

    def _exchange(self, request, timeout):
        """在受控线程内完成"写请求 + 读响应"，主线程按 timeout 判定卡死。

        :return: ``(header, payload)``
        :raises VoiceBridgeError: 超时 / 读写异常 / 帧损坏。
        """
        outcome = {}

        def _worker():
            """在线程内执行协议交互，结果写入 outcome（不改主流程状态）。"""
            try:
                line = json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n"
                self._proc.stdin.write(line)
                self._proc.stdin.flush()
                outcome["frame"] = self._read_frame()
            except Exception as exc:  # noqa: BLE001 - 统一归为桥错误
                outcome["error"] = exc

        worker = threading.Thread(target=_worker, daemon=True)
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            raise VoiceBridgeError(f"桥请求超时（>{timeout}s），已判定进程卡死")
        if "error" in outcome:
            raise VoiceBridgeError(f"桥交互失败：{outcome['error']}")
        return outcome["frame"]

    def request(self, payload, timeout=None):
        """发送一次请求并返回 ``(header, payload_bytes)``（线程安全）。

        进程崩溃/卡死时自动重建进程并重试一次（``_RESTART_LIMIT``）。

        :param payload: 请求 dict（``op`` 等；``id`` 由客户端分配，调用方无需填）。
        :param timeout: 本次请求超时秒数；缺省用构造时的 ``timeout``。
        :return: ``(header_dict, payload_bytes)``
        :raises VoiceBridgeError: 请求最终失败时（含 sidecar 返回的 ok=false）。
        """
        effective_timeout = self.timeout if timeout is None else timeout
        with self._lock:
            last_error = None
            for attempt in range(_RESTART_LIMIT + 1):
                try:
                    self._ensure_process()
                    request = dict(payload)
                    request["id"] = self._next_id
                    self._next_id += 1
                    header, body = self._exchange(request, effective_timeout)
                except VoiceBridgeError as exc:
                    last_error = exc
                    self._kill_process()
                    if attempt < _RESTART_LIMIT:
                        continue
                    raise VoiceBridgeError(
                        f"桥请求失败（已重试 {_RESTART_LIMIT} 次）：{exc}；"
                        f"stderr 尾部：{self.stderr_tail()}"
                    ) from exc
                if not header.get("ok"):
                    raise VoiceBridgeError(
                        f"sidecar 返回错误：{header.get('error') or '未知错误'}"
                    )
                return header, body
            raise VoiceBridgeError(f"桥请求失败：{last_error}")  # pragma: no cover

    def stderr_tail(self, lines=10):
        """返回桥进程 stderr 的最后若干行（诊断用，单行拼接）。"""
        return " | ".join(list(self._stderr_lines)[-lines:])

    def close(self):
        """显式收尾：尝试发 shutdown，随后兜底终止进程（幂等、不抛错）。"""
        with self._lock:
            proc = self._proc
            if proc is not None and proc.poll() is None:
                try:
                    self._exchange({"op": "shutdown", "id": self._next_id}, timeout=5)
                    self._next_id += 1
                except Exception:  # noqa: BLE001 - 关闭失败走兜底 terminate
                    pass
            self._kill_process()