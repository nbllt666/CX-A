# -*- coding: utf-8 -*-
"""CX-A 首次启动引导——五步流程，可驱动、可注入，便于测试与前端/向导接入。

流程：
  步骤1 云端提供商选择（deepseek / tongyi / openai / moonshot，默认 deepseek）
  步骤2 API Key 输入（空则跳过，提示可在设置页补填）
  步骤3 提示默认 CX-OPEN 音色已内置
  步骤4 本地小 LLM 可选下载引导（HF / 魔塔双源，建议 1.7B，存 data/local_llm/，
        下载逻辑由 C2 提供，此处仅引导 + 置 local_llm.source=modelscope）
  步骤5 完成汇总输出（配置段一览）

通过 FirstRunDriver 逐步执行，注入 input_fn / output_fn 即可在测试或向导中驱动。
"""

import os

from lite.config.config_manager import ConfigManager

from .bootstrap import PROJECT_ROOT

#: 可选的云端提供商列表（对齐 config.cloud.provider 可选值）。
DEFAULT_PROVIDERS = ("deepseek", "tongyi", "openai", "moonshot")


class FirstRunDriver:
    """首次启动引导驱动器。

    每次引导按步骤执行，input 与 output 均可注入，便于测试确定性驱动。
    """

    def __init__(self, root, input_fn=None, output_fn=None, config_manager=None):
        """初始化引导驱动器。

        :param root: 安装根目录（决定 config.json / data 落点）。
        :param input_fn: 读取用户输入的可调用对象；缺省使用内建 input。
        :param output_fn: 输出提示的可调用对象；缺省使用内建 print。
        :param config_manager: 已构造的 ConfigManager；缺省按 root 新建。
        """
        self.root = root or PROJECT_ROOT
        self._input = input_fn if input_fn is not None else input
        self._output = output_fn if output_fn is not None else print
        self.cm = config_manager or ConfigManager(
            config_path=os.path.join(self.root, "config.json"),
            data_dir=os.path.join(self.root, "data"),
        )

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
    # 步骤4：本地小 LLM 可选下载引导                                    #
    # ------------------------------------------------------------------ #

    def step_local_llm(self):
        """本地小 LLM 可选下载引导。

        仅打印说明（HF / 魔塔双源、建议 1.7B、存 data/local_llm/）并置
        local_llm.source=modelscope；实际下载逻辑由 C2 后续提供。
        """
        self._output("[引导] 可选：本地小 LLM 下载引导")
        self._output("   - 双源：HuggingFace / 魔塔（Modelscope），国内优先魔塔")
        self._output("   - 建议规格：约 1.7B 参数 GGUF 模型")
        self._output("   - 安装位置：data/local_llm/")
        self._output("   - 下载逻辑由 C2 后续提供，本步骤仅做引导")
        self.cm.set("local_llm", "source", "modelscope")
        return "modelscope"

    # ------------------------------------------------------------------ #
    # 步骤5：完成汇总 + 编排                                            #
    # ------------------------------------------------------------------ #

    def summary(self):
        """输出首次启动配置汇总（配置段一览）。"""
        api_key = self.cm.get("cloud", "api_key", "")
        lines = [
            "===== 首次启动配置汇总 =====",
            f"云端提供商: {self.cm.get('cloud', 'provider')}",
            f"API Key: {'已填写（加密存储）' if api_key else '未填写（可在设置页补填）'}",
            f"TTS 引擎/音色: {self.cm.get('tts', 'engine')} / {self.cm.get('tts', 'voice', 'cx-open')}",
            f"嵌入模型: {self.cm.get('embedding', 'model')}",
            f"向量库: {self.cm.get('vector', 'backend')}",
            f"本地小 LLM: source={self.cm.get('local_llm', 'source')}, "
            f"enabled={self.cm.get('local_llm', 'enabled')}",
            "==============================",
        ]
        for line in lines:
            self._output(line)

    def run(self):
        """依次执行五步引导，保存配置并输出汇总。

        :return: dict，含 provider / api_key / local_llm_source 供调用方断言。
        """
        self.step_choose_provider()
        api_key = self.step_input_api_key()
        self.step_notice_voice()
        local_llm_source = self.step_local_llm()
        self.cm.save()
        self.summary()
        return {
            "provider": self.cm.get("cloud", "provider"),
            "api_key": self.cm.get("cloud", "api_key", ""),
            "local_llm_source": local_llm_source,
        }


def run_first_run(root, input_fn=None, output_fn=None):
    """便捷入口：在 root 上一次性完成首次启动引导。

    :param root: 安装根目录。
    :param input_fn: 输入注入（测试传固定返回的 callable）。
    :param output_fn: 输出注入。
    :return: FirstRunDriver.run() 的 dict 结果。
    """
    return FirstRunDriver(root, input_fn=input_fn, output_fn=output_fn).run()