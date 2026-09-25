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

另提供**安装期装配子命令**（由独立安装程序调用，与启动路径互斥）：
    backend.exe --provision-runtime [--root <安装根>]
    见 :func:`provision_main`（内置 conda + 语音 sidecar 环境一次性装完）。

路径规范：一律基于 ``sys.executable`` / ``os.path.abspath(__file__)`` 推导，
禁止相对路径与字符串斜杠拼接。
"""

import os
import socket
import sys

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

    H-3（第三轮体检批次4）：追加 ``--config <root>/config.json``——把用户配置
    真相源统一到便携根顶层（与 bootstrap.init_workplace / first_run 写入点、
    build.py zip 注释声明的"顶层 config.json 为用户配置"对齐）；
    修复前运行链读 data/config.json，首启向导写入的云端配置永远读不到。

    :param root: 安装根；缺省用 ``resolve_root()``（测试可传 tmp_path）。
    :return: list[str]，形如 ["--host", ..., "--port", ..., "--data-dir", ..., "--config", ...]。
    """
    root = root or resolve_root()
    data_dir = os.path.join(root, "data")
    return [
        "--host", BACKEND_HOST,
        "--port", str(BACKEND_PORT),
        "--data-dir", data_dir,
        "--config", os.path.join(root, "config.json"),
    ]


def check_port_bindable(host=BACKEND_HOST, port=BACKEND_PORT):
    """探测 host:port 是否可绑定（即端口未被占用）。

    以一次性 socket bind 探测：绑定成功立即关闭并返回 True；
    绑定失败（端口被占用等 OSError）返回 False。探测套接字不留痕迹。
    独立成函数便于单测注入/monkeypatch，避免测试依赖真实端口状态。

    :param host: 监听地址。
    :param port: 监听端口。
    :return: True 表示端口空闲可绑定；False 表示被占用。
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _extract_host_port(args):
    """从命令行参数列表解析 --host/--port 实际值（缺省回退固定监听参数）。

    :param args: 传给 api_server.main 的参数列表。
    :return: (host, port) 元组。
    """
    host = BACKEND_HOST
    port = BACKEND_PORT
    for idx, token in enumerate(args):
        if token == "--host" and idx + 1 < len(args):
            host = args[idx + 1]
        elif token == "--port" and idx + 1 < len(args):
            port = int(args[idx + 1])
    return host, port


def provision_main(argv=None):
    """安装期运行时装配入口（``backend.exe --provision-runtime``）。

    由**独立安装程序**在展开载荷后调用：内置 conda + 语音 sidecar 环境 +
    语音依赖（GPU 自动探测分叉）+ nltk 数据，一次性装完（人类裁决③）。
    任何一步失败不阻断安装——本函数恒返回 0，结果如实落
    ``<root>/runtime/provision.log`` 与 ``<root>/data/install_report.json``。

    :param argv: 命令行参数列表（缺省 ``sys.argv[1:]``）；支持
        ``--root <安装根>`` 覆盖推导结果（安装器可显式传入 {app}）。
    :return: int 退出码（恒 0）。
    """
    raw = list(sys.argv[1:] if argv is None else argv)
    root = None
    for index, token in enumerate(raw):
        if token == "--root" and index + 1 < len(raw):
            root = raw[index + 1]
    root = os.path.abspath(root) if root else resolve_root()

    # 延迟导入：装配依赖（conda_runtime → bootstrap → lite.*）不应影响普通启动路径
    try:
        from installer import conda_runtime
    except ImportError:  # pragma: no cover - CLI 直跑 / 冻结态包上下文差异兜底
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from installer import conda_runtime  # type: ignore[no-redef]

    conda_runtime.provision_runtime(root)
    return 0


def main(argv=None):
    """可执行入口：数据目录自愈 + 端口预检后进入 API 服务主循环。"""
    root = resolve_root()
    # 数据目录自愈：解压直跑/异常清理后缺失时自动补齐（幂等），避免起服失败
    data_dir = os.path.join(root, "data")
    os.makedirs(data_dir, exist_ok=True)
    args = argv if argv is not None else build_args(root)

    # 端口预检（A-3）：8600 被占时向 stderr 明确报错并退出，
    # 避免 backend.exe 因 bind 失败静默闪退、用户无从排查
    host, port = _extract_host_port(args)
    if not check_port_bindable(host, port):
        sys.stderr.write(
            f"[错误] 端口 {port} 已被占用：可能是上次未退出的 CX-A 后端或其它程序，"
            "请结束对应进程（如任务管理器结束 backend.exe）后重试。\n"
        )
        sys.exit(1)

    # 延迟导入：保证 build_args 等纯函数可在无服务依赖时单测
    from lite.server.api_server import main as api_main

    api_main(args)


if __name__ == "__main__":  # pragma: no cover - PyInstaller 冻结入口
    # 安装期装配子命令（独立安装程序调用）：与常规启动路径互斥
    if "--provision-runtime" in sys.argv[1:]:
        sys.exit(provision_main())
    main()
