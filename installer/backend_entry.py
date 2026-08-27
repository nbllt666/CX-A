# -*- coding: utf-8 -*-
"""后端可执行入口（打包链路步骤1）——PyInstaller 冻结态/开发态双轨。

PyInstaller 打包本文件为 ``backend.exe``，落位于便携根 ``runtime/backend/``：
    <portable_root>/runtime/backend/backend.exe
    <portable_root>/data/...

- 冻结态（``sys.frozen``）：安装根 = exe 所在目录的上级（runtime/backend -> root）；
- 开发态直跑：安装根 = 项目根（本文件位于 installer/，上溯一级），
  与 ``installer.bootstrap.PROJECT_ROOT`` 同口径。

启动时组装固定参数调用 ``lite.server.api_server.main``：
    --host 127.0.0.1 --port 8600 --data-dir <root>/data
（host/port 与 frontend/src/renderer/api.ts 的 API_PORT 约定一致。）

路径规范：一律基于 ``sys.executable`` / ``os.path.abspath(__file__)`` 推导，
禁止相对路径与字符串斜杠拼接。
"""

import os
import sys

#: 冻结态下 backend.exe 相对安装根的固定层级：runtime/backend -> 上溯 2 级
_FROZEN_ROOT_LEVELS = 2

#: 后端服务固定监听参数（与前端 api.ts API_PORT / DEFAULT_HOST 对齐）
BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 8600


def resolve_root():
    """推导安装根目录（数据目录 data/ 的父目录）。

    :return: str 绝对路径。
    """
    if getattr(sys, "frozen", False):
        # 冻结态：<root>/runtime/backend/backend.exe
        # 文件路径 -> backend 目录 -> runtime 目录 -> 安装根
        exe_path = os.path.abspath(sys.executable)
        backend_dir = os.path.dirname(exe_path)
        runtime_dir = os.path.dirname(backend_dir)
        return os.path.dirname(runtime_dir)
    # 开发态：installer/backend_entry.py -> 上溯 1 级即项目根
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_args(root=None):
    """组装传给 ``api_server.main`` 的命令行参数列表。

    :param root: 安装根；缺省用 ``resolve_root()``（测试可传 tmp_path）。
    :return: list[str]，形如 ["--host", ..., "--port", ..., "--data-dir", ...]。
    """
    root = root or resolve_root()
    data_dir = os.path.join(root, "data")
    return [
        "--host", BACKEND_HOST,
        "--port", str(BACKEND_PORT),
        "--data-dir", data_dir,
    ]


def main(argv=None):
    """可执行入口：数据目录自愈（幂等建目录）后进入 API 服务主循环。"""
    root = resolve_root()
    # 数据目录自愈：解压直跑/异常清理后缺失时自动补齐（幂等），避免起服失败
    data_dir = os.path.join(root, "data")
    os.makedirs(data_dir, exist_ok=True)
    args = argv if argv is not None else build_args(root)

    # 延迟导入：保证 build_args 等纯函数可在无服务依赖时单测
    from lite.server.api_server import main as api_main

    api_main(args)


if __name__ == "__main__":  # pragma: no cover - PyInstaller 冻结入口
    main()
