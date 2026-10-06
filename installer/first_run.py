# -*- coding: utf-8 -*-
"""CX-A 首次启动引导——多步流程，可驱动、可注入，便于测试与前端/向导接入。

流程（Task 9 扩展后）：
  步骤1 云端提供商选择（deepseek / tongyi / openai / moonshot，默认 deepseek）
  步骤2 API Key 输入（空则跳过，提示可在设置页补填）
  步骤3 提示默认 CX-OPEN 音色已内置
  步骤4 硬件体检与推荐（CPU / 内存 / GPU / 磁盘 → 推荐档位与体积；探测失败降级跳过）
  步骤5 下载线路选择（国内（魔塔，推荐）/ 海外（HuggingFace），默认国内；写
        download.channel，并由线路派生写入 local_llm.source——模型来源不再单独提问）
  步骤6 本地小 LLM 可选下载引导（默认档 Gemma 4 E2B（多模态），存 data/local_llm/；
        downloader 注入时可在向导内真实下载，异常只告警不中断）
  步骤7 完成汇总输出（配置段一览）

写入配置键与前端首启向导共用同一套（download.channel / local_llm.source /
setup.completed），不产生第二套真相；模型来源由下载线路唯一派生
（``lite.config.download_sources.model_repo_for_channel``），不在调用点各自推导。

通过 FirstRunDriver 逐步执行，注入 input_fn / output_fn 即可在测试或向导中驱动。
新增提示一律经 ``_ask(prompt, default)`` 获取输入，该方法捕获 ``StopIteration``
（测试注入的迭代器耗尽）与 ``EOFError``（真实 CLI 在 EOF）→ 返回 ``default``，
保证输入序列较短的既有测试自动走默认值。

运行口径（MU1）：本模块含包内相对导入，推荐 ``python -m installer.first_run``；
``python installer/bootstrap.py`` 为 CLI 直跑入口（bootstrap 内部已做 sys.path 项目根注入）。
"""

import datetime
import os

from lite.cloud.adapter import PROVIDER_BASE_URLS
from lite.config.config_manager import ConfigManager
from lite.config.download_sources import model_repo_for_channel, normalize_channel
from lite.runtime.hardware_profile import (
    accel_plan,
    derive_default_mode,
    detect_profile,
    normalize_mode,
    recommend_for,
)
from lite.runtime.model_downloader import LlmDownloader

from .bootstrap import PROJECT_ROOT

#: 可选的云端提供商列表（L-8：从 adapter.PROVIDER_BASE_URLS 派生，单一真相源）。
DEFAULT_PROVIDERS = tuple(PROVIDER_BASE_URLS.keys())

#: 下载线路候选展示文案（国内魔塔 / 海外 HuggingFace）。
_CHANNEL_OPTIONS = "国内（魔塔，推荐）/ 海外（HuggingFace）"

#: 运行偏好询问文案（口语化，零术语；不出现 EP / ORT / DirectML 等词）。
_ACCEL_MODE_OPTIONS = "省电优先 / 性能优先"


def _accel_mode_label(mode):
    """把模式取值转成口语化标签（performance → 性能优先；eco → 省电优先）。"""
    return "性能优先" if normalize_mode(mode) == "performance" else "省电优先"


def _format_accel_summary(plan):
    """把 ``accel_plan`` 落点转成口语化、零术语的一句加速结论。

    禁止出现 EP / ORT / DirectML / CUDA / DML / ROCm / GPU / CPU 等术语。
    """
    tts_accel = plan.get("tts.accel")
    device = plan.get("tts.accel_device")
    if tts_accel == "cuda":
        return "检测到 NVIDIA 独立显卡，已为语音合成开启显卡加速"
    if tts_accel == "dml" and device == "dgpu":
        return "检测到独立显卡，已为语音合成开启显卡加速"
    if tts_accel == "dml" and device == "igpu":
        return "检测到核显，已为语音合成开启省电加速"
    if tts_accel == "dml":
        return "检测到可用显卡，已为语音合成开启显卡加速"
    return "没有可用的独立显卡，语音合成先用电脑本身运行（更稳更省电）"


def _format_profile(profile):
    """把硬件画像格式化为一行中文摘要（缺项显示「未知」，不抛错）。"""
    ram = profile.get("ram_gb")
    vram = profile.get("vram_gb")
    disk = profile.get("disk_free_gb")
    parts = [
        f"CPU {profile.get('cpu_cores') or '未知'} 核",
        f"内存 {ram:.1f} GB" if isinstance(ram, (int, float)) else "内存未知",
        f"显卡 {profile.get('gpu_vendor') or '未知'}",
    ]
    if isinstance(vram, (int, float)):
        parts.append(f"显存 {vram:.1f} GB")
    parts.append(
        f"磁盘可用 {disk:.1f} GB" if isinstance(disk, (int, float)) else "磁盘可用未知"
    )
    return " / ".join(parts)


class FirstRunDriver:
    """首次启动引导驱动器。

    每次引导按步骤执行，input 与 output 均可注入，便于测试确定性驱动。
    """

    def __init__(self, root, input_fn=None, output_fn=None, config_manager=None, downloader=None):
        """初始化引导驱动器。

        :param root: 安装根目录（决定 config.json / data 落点）。
        :param input_fn: 读取用户输入的可调用对象；缺省使用内建 input。
        :param output_fn: 输出提示的可调用对象；缺省使用内建 print。
        :param config_manager: 已构造的 ConfigManager；缺省按 root 新建。
        :param downloader: 本地小 LLM 下载执行器（须提供
            ``download(repo, filename, source=..., progress_cb=..., verify_size_gb=...)``）；
            缺省 ``None`` 表示不实际下载，步骤7 仅打印引导（既有行为不变，测试不触网）。
        """
        self.root = root or PROJECT_ROOT
        self._input = input_fn if input_fn is not None else input
        self._output = output_fn if output_fn is not None else print
        self.cm = config_manager or ConfigManager(
            config_path=os.path.join(self.root, "config.json"),
            data_dir=os.path.join(self.root, "data"),
        )
        self._downloader = downloader
        #: 硬件体检推荐结果（step_hardware_recommend 填充；探测降级时为 None）。
        self.recommendation = None
        #: 硬件画像（step_hardware_recommend 填充；探测降级时为 None）——供模式询问复用。
        self.profile = None
        #: 加速方案落点（step_choose_accel_mode 填充；未询问时为 None）。
        self.accel_plan_result = None

    # ------------------------------------------------------------------ #
    # 输入辅助                                                           #
    # ------------------------------------------------------------------ #

    def _ask(self, prompt, default=""):
        """安全读取用户输入，输入不可得时回落 ``default``。

        捕获 ``StopIteration``（测试注入的固定长度迭代器耗尽）与 ``EOFError``
        （真实 CLI 在 EOF / 非交互环境），返回 ``default``——既有以短输入序列
        驱动的用例因此自动走默认值，无需修改既有断言。

        :param prompt: 提示语（原样传给 input_fn）。
        :param default: 无输入 / 空输入时的回落值。
        :return: 去空白后的输入字符串；无输入或空输入时为 ``default``。
        """
        try:
            raw = self._input(prompt)
        except (StopIteration, EOFError):
            return default
        if raw is None:
            return default
        text = str(raw).strip()
        return text if text else default

    # ------------------------------------------------------------------ #
    # 步骤1：云端提供商选择                                            #
    # ------------------------------------------------------------------ #

    def step_choose_provider(self):
        """选择云端提供商（默认 deepseek），写入 config.cloud.provider。"""
        options = " / ".join(DEFAULT_PROVIDERS)
        prompt = f"[引导] 选择云端提供商（默认 deepseek）：{options} > "
        raw = self._input(prompt).strip().lower()
        provider = raw if raw in DEFAULT_PROVIDERS else "deepseek"
        self.cm.set("cloud", "provider", provider)
        self._output(f"[引导] 已选择云端提供商：{provider}")
        return provider

    # ------------------------------------------------------------------ #
    # 步骤2：API Key 输入                                              #
    # ------------------------------------------------------------------ #

    def step_input_api_key(self):
        """输入云端 API Key；留空则跳过（可在设置页补填）。

        API Key 通过 ConfigManager.set 写入内存，保存时由 save() 统一走 Fernet 加密。
        """
        prompt = "[引导] 请输入云端 API Key（留空跳过，可在设置页补填）> "
        raw = self._input(prompt).strip()
        if raw:
            self.cm.set("cloud", "api_key", raw)
            self._output("[引导] 已写入 API Key（将在保存时加密存储）")
        else:
            self._output("[引导] 未填写 API Key，可在设置页补填")
        return raw

    # ------------------------------------------------------------------ #
    # 步骤3：默认音色提示                                              #
    # ------------------------------------------------------------------ #

    def step_notice_voice(self):
        """提示默认 CX-OPEN 音色已内置，开箱即用。"""
        self._output("[引导] 默认已内置 CX-OPEN 音色，开箱即用。")
        return None

    # ------------------------------------------------------------------ #
    # 步骤4：硬件体检与推荐                                             #
    # ------------------------------------------------------------------ #

    def step_hardware_recommend(self):
        """硬件体检与推荐（探测失败只降级为「跳过推荐」，绝不中断流程）。

        经 ``lite.runtime.hardware_profile`` 探测 CPU 核数 / 内存 / GPU / 显存 /
        磁盘可用空间，再产出推荐档位、体积与中文理由；结果同时保存在
        ``self.recommendation`` 供步骤7 选取下载档位。

        :return: ``recommend_for`` 的推荐 dict；探测异常时返回 None（降级跳过）。
        """
        self._output("[引导] 正在做硬件体检（CPU / 内存 / 显卡 / 磁盘）...")
        try:
            profile = detect_profile(root=self.root)
            recommendation = recommend_for(
                profile, disk_free_gb=profile.get("disk_free_gb")
            )
        except Exception as exc:  # noqa: BLE001 - 探测失败必须降级为跳过推荐
            self.recommendation = None
            self._output(
                f"[引导] 硬件体检不可用（{type(exc).__name__}: {exc}），已跳过推荐，"
                "不影响后续步骤，可直接按默认档位继续"
            )
            return None

        self.recommendation = recommendation
        self.profile = profile
        self._output(f"[引导] 硬件体检结果：{_format_profile(profile)}")
        if recommendation.get("use_local"):
            model = recommendation.get("model") or {}
            size = model.get("approximate_size_gb")
            size_text = f"约 {size} GB" if size else "体积未知"
            self._output(
                f"[引导] 推荐档位与体积：{recommendation.get('tier')}"
                f"（{size_text}），建议运行设备：{recommendation.get('device')}"
            )
        else:
            self._output("[引导] 推荐档位与体积：暂不下载本地小 LLM，先走云端（可稍后自行下载）")
        for reason in recommendation.get("reasons") or []:
            self._output(f"[引导] 推荐理由：{reason}")
        # 运行偏好询问（省电优先 / 性能优先，默认按画像推导）——本步骤内完成
        try:
            self.step_choose_accel_mode(profile)
        except Exception as exc:  # noqa: BLE001 - 模式询问失败不阻断向导
            self._output(
                f"[引导] 运行偏好设置失败（{type(exc).__name__}: {exc}），"
                "已跳过，不影响后续步骤"
            )
        return recommendation

    def step_choose_accel_mode(self, profile=None):
        """询问运行偏好（省电优先 / 性能优先），默认按画像推导并落盘全部落点。

        选择结果经唯一真相源 :func:`lite.runtime.hardware_profile.accel_plan`
        展开为各组件落点（``accel.mode`` / ``tts.accel`` / ``tts.accel_device`` /
        ``asr.device`` / ``local_llm.device`` / ``embedding.device``）并写入配置，
        与前端 ``/api/settings`` 共用同一键（无第二套真相）。

        :param profile: 硬件画像（缺省用 ``self.profile``）；缺失时保守按空画像推导。
        :return: 归一后的模式字符串（``"performance"`` / ``"eco"``）。
        """
        hardware = profile if isinstance(profile, dict) else (self.profile or {})
        default_mode = derive_default_mode(hardware)
        default_label = _accel_mode_label(default_mode)
        raw = self._ask(
            f"[引导] 平时更看重哪一点？{_ACCEL_MODE_OPTIONS}（默认{default_label}）> ",
            default_mode,
        )
        mode = self._parse_accel_mode(raw, default_mode)
        plan = accel_plan(hardware, mode)
        # 落盘：模式 + accel_plan 全部组件落点（单一真相源，禁止在本处另写判断）
        self.cm.set("accel", "mode", plan["accel.mode"])
        self.cm.set("tts", "accel", plan["tts.accel"])
        self.cm.set("tts", "accel_device", plan["tts.accel_device"])
        self.cm.set("asr", "device", plan["asr.device"])
        self.cm.set("local_llm", "device", plan["local_llm.device"])
        self.cm.set("embedding", "device", plan["embedding.device"])
        self.accel_plan_result = plan
        self._output(f"[引导] 已选择：{_accel_mode_label(plan['accel.mode'])}")
        self._output(f"[引导] 加速方案：{_format_accel_summary(plan)}")
        return plan["accel.mode"]

    @staticmethod
    def _parse_accel_mode(raw, default_mode):
        """解析用户输入为模式取值；无法识别时回落默认（非法输入不报错）。"""
        text = str(raw or "").strip().lower()
        if text in ("performance", "性能", "性能优先", "2", "gpu", "独显"):
            return "performance"
        if text in ("eco", "省电", "省电优先", "节能", "1", "cpu", "核显"):
            return "eco"
        return normalize_mode(default_mode)

    # ------------------------------------------------------------------ #
    # 步骤5：下载线路选择（唯一用户选择，兼管模型来源）                  #
    # ------------------------------------------------------------------ #

    def step_download_channel(self):
        """选择下载线路（国内（魔塔）/ 海外（HuggingFace）），承担全部下载源信息。

        写入 ``download.channel``，并由线路经
        ``lite.config.download_sources.model_repo_for_channel`` **派生写入**
        ``local_llm.source``（国内 → ``modelscope`` 魔塔，海外 → ``huggingface``）；
        模型来源不再单独提问，也不允许在调用点各自推导。
        与前端首启向导、``/api/setup/status`` 共用同一配置键（无第二套真相）。

        :return: 归一化后的通道字符串（``"mirror"`` / ``"official"``）。
        """
        raw = self._ask(
            f"[引导] 下载走哪条线路？{_CHANNEL_OPTIONS}（默认国内）> ",
            "mirror",
        )
        channel = normalize_channel(raw)
        source = model_repo_for_channel(channel)
        self.cm.set("download", "channel", channel)
        self.cm.set("local_llm", "source", source)
        label = "国内（魔塔）" if channel == "mirror" else "海外（HuggingFace）"
        self._output(f"[引导] 下载线路：{label}（{channel}）；模型来源：{source}")
        return channel

    # ------------------------------------------------------------------ #
    # 步骤6：本地小 LLM 可选下载                                        #
    # ------------------------------------------------------------------ #

    def step_local_llm(self):
        """本地小 LLM 可选下载引导（模型来源由线路派生、默认档 Gemma 4 E2B、存 data/local_llm/）。

        - ``downloader`` 未注入（缺省）：仅打印引导，不做任何网络请求（既有行为）；
        - ``downloader`` 已注入：询问是否现在下载（默认不下载），同意则调用
          ``downloader.download(...)``（repo / 文件名 / 体积经
          ``LlmDownloader.suggest_model`` 解析），异常只告警不中断引导。

        :return: 当前模型来源（``local_llm.source``，由下载线路派生）。
        """
        source = self.cm.get("local_llm", "source", "modelscope")
        channel = normalize_channel(self.cm.get("download", "channel", "mirror"))
        self._output("[引导] 可选：本地小 LLM 下载引导")
        self._output("   - 模型来源：由下载线路派生（国内=魔塔 Modelscope / 海外=HuggingFace）")
        self._output("   - 默认规格：Gemma 4 E2B 多模态 GGUF 模型（约 2.9GB + 视觉组件）")
        self._output("   - 安装位置：data/local_llm/")
        self._output(f"   - 当前模型来源：{source}；当前下载线路：{channel}")
        if self._downloader is None:
            self._output("   - 未注入下载执行器，本步骤仅做引导（可稍后在设置页下载）")
            return source

        answer = self._ask("[引导] 现在下载本地小 LLM 吗？（y/N，默认不下载）> ", "n").lower()
        if answer not in ("y", "yes", "是"):
            self._output("[引导] 已跳过下载，可稍后在设置页或向导中下载")
            return source

        tier = None
        if self.recommendation and self.recommendation.get("use_local"):
            tier = self.recommendation.get("tier")
        try:
            info = LlmDownloader.suggest_model(source, tier)
            self._output(
                f"[引导] 开始下载：{info['repo']} / {info['filename']}"
                f"（档位 {info['tier']}，约 {info['approximate_size_gb']} GB）"
            )
            path = self._downloader.download(
                info["repo"],
                info["filename"],
                source=source,
                progress_cb=self._make_progress_cb(),
                verify_size_gb=info["approximate_size_gb"],
            )
            self.cm.set("local_llm", "model_path", str(path))
            self._output(f"[引导] 下载完成：{path}")
            # 多模态档位（20261004 Gemma 4）：主模型就位后同仓库下载视觉组件
            # （落同目录；llama-server --mmproj 挂载后具备看图能力），失败不阻断
            mmproj_name = info.get("mmproj_filename")
            if mmproj_name:
                self._output(f"[引导] 开始下载视觉组件：{mmproj_name}")
                mmproj_path = self._downloader.download(
                    info["repo"],
                    mmproj_name,
                    source=source,
                    progress_cb=self._make_progress_cb(),
                    verify_size_gb=info.get("mmproj_size_gb"),
                )
                self._output(f"[引导] 视觉组件下载完成：{mmproj_path}")
        except Exception as exc:  # noqa: BLE001 - 下载失败不阻断引导完成
            self._output(
                f"[引导] 下载失败（不影响引导完成，可稍后重试）：{type(exc).__name__}: {exc}"
            )
        return source

    def _make_progress_cb(self):
        """构造按 25% 步进打印的中文进度回调（不依赖真实网络，替身下载器可直接调用）。"""
        state = {"percent": 0}

        def _progress(done, total):
            """下载进度回调：``done / total`` 字节，跨过 25% 台阶时打印一次。"""
            if not total:
                return
            percent = int(done * 100 / total)
            if percent - state["percent"] >= 25:
                state["percent"] = percent
                self._output(f"[引导] 下载进度：{percent}%")

        return _progress

    # ------------------------------------------------------------------ #
    # 步骤7：完成汇总 + 编排                                            #
    # ------------------------------------------------------------------ #

    def summary(self):
        """输出首次启动配置汇总（配置段一览 + 下载线路与派生的模型来源）。"""
        api_key = self.cm.get("cloud", "api_key", "")
        channel = normalize_channel(self.cm.get("download", "channel", "mirror"))
        route_label = "国内（魔塔）" if channel == "mirror" else "海外（HuggingFace）"
        lines = [
            "===== 首次启动配置汇总 =====",
            f"云端提供商: {self.cm.get('cloud', 'provider')}",
            f"API Key: {'已填写（加密存储）' if api_key else '未填写（可在设置页补填）'}",
            f"TTS 引擎/音色: {self.cm.get('tts', 'engine')} / {self.cm.get('tts', 'voice', 'cx-open')}",
            f"运行偏好: {_accel_mode_label(self.cm.get('accel', 'mode', 'performance'))}",
            f"嵌入模型: {self.cm.get('embedding', 'model')}",
            f"向量库: {self.cm.get('vector', 'backend')}",
            f"下载线路: {route_label}（{channel}），模型来源: {self.cm.get('local_llm', 'source')}",
            f"本地小 LLM: source={self.cm.get('local_llm', 'source')}, "
            f"enabled={self.cm.get('local_llm', 'enabled')}",
            "==============================",
        ]
        for line in lines:
            self._output(line)

    def run(self):
        """依次执行各步引导，保存配置（含 setup.completed）并输出汇总。

        :return: dict，含 provider / api_key / local_llm_source /
            download_channel / model_repo / setup_completed，供调用方断言；
            ``model_repo`` 即由下载线路派生的模型来源（``local_llm.source``）。
        """
        self.step_choose_provider()
        self.step_input_api_key()
        self.step_notice_voice()
        self.step_hardware_recommend()
        download_channel = self.step_download_channel()
        local_llm_source = self.step_local_llm()
        # 向导完成状态（与前端首启向导同一配置键，老用户升级不受打扰）
        self.cm.set("setup", "completed", True)
        self.cm.set(
            "setup", "completed_at", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        self.cm.save()
        self.summary()
        return {
            "provider": self.cm.get("cloud", "provider"),
            "api_key": self.cm.get("cloud", "api_key", ""),
            "local_llm_source": local_llm_source,
            "download_channel": download_channel,
            "model_repo": self.cm.get("local_llm", "source"),
            "setup_completed": self.cm.get("setup", "completed"),
            "accel_mode": self.cm.get("accel", "mode", "performance"),
        }


def run_first_run(root, input_fn=None, output_fn=None):
    """便捷入口：在 root 上一次性完成首次启动引导。

    :param root: 安装根目录。
    :param input_fn: 输入注入（测试传固定返回的 callable）。
    :param output_fn: 输出注入。
    :return: FirstRunDriver.run() 的 dict 结果。
    """
    return FirstRunDriver(root, input_fn=input_fn, output_fn=output_fn).run()