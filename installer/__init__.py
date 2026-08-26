# -*- coding: utf-8 -*-
"""CX-A 安装程序包。

负责 Windows 一键安装与会首次启动引导：
- bootstrap：目录初始化 / 组件校验 / 内置组件落位 / 数据目录初始化
- first_run：首次启动五步引导流程（可注入 input，便于测试与前端/向导接入）
- manifest.json：组件清单（内置 / 可选组件）
"""