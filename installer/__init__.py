# -*- coding: utf-8 -*-
"""CX-A 安装程序包。

负责 Windows 一键安装与首次启动引导：
- bootstrap：目录初始化 / 组件校验 / 内置组件落位 / 数据目录初始化 /
  下载通道解析（镜像 / 官方）与 GPU 依赖命令装配（CLI：``--channel mirror|official``）
- first_run：首次启动引导流程（云端提供商 → API Key → 音色提示 → 硬件体检与推荐
  → 下载线路（国内魔塔 / 海外 HuggingFace，模型来源由线路派生） → 本地小 LLM
  可选下载 → 汇总），可注入 input / 输出 / 下载执行器，便于测试与前端/向导接入；
  配置键与前端向导同源
- manifest.json：组件清单（内置 / 可选组件）
"""