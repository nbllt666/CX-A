# -*- coding: utf-8 -*-
"""一键打包编排（打包链路步骤4）——产出 Windows 便携目录 + zip。

产物形态（与 installer/bootstrap.py、manifest.json 对齐）：
    <project>/release/portable/
        CX-A.exe                     ← Electron 壳（electron-builder --dir 产物）
        resources/...                ← 壳运行时
        runtime/backend/backend.exe  ← PyInstaller 后端（installer/backend_entry.py）
        runtime/electron/...         ← （manifest 记录位；壳即根，不再重复落位）
        data/...                     ← 数据目录（bootstrap.install 初始化）
        config.json                  ← 默认配置（首启引导可改）
    <project>/release/CX-A-portable-win64.zip

流程：工具探测 → renderer 构建 → Electron 壳 → PyInstaller 后端 → 组装 → zip。
每步可用 ``--skip-*`` 跳过（增量构建）；工具缺失时给出明确安装指引后退出，
不静默失败。全中文 [INFO]/[WARN] 输出，带时间戳。
"""

import argparse
import datetime
import os
import shutil
import subprocess
import sys
import time
import zipfile

# GBK 控制台防崩：electron-builder / vite 输出常含 U+2022 等非 GBK 字符，
# print 直写 GBK 控制台会抛 UnicodeEncodeError 并掩盖真实构建失败原因。
# 统一把标准流切到 UTF-8（replace 兜底）；流不支持 reconfigure 时静默跳过。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):  # 已重定向/已关闭等场景不阻断构建
            pass

#: 本安装器目录（installer/），基于文件绝对位置推导，禁止相对路径。
_INSTALLER_DIR = os.path.dirname(os.path.abspath(__file__))
#: 项目根目录。
PROJECT_ROOT = os.path.dirname(_INSTALLER_DIR)

# CLI 直跑支持：``python installer/build.py`` 时本模块不在包上下文中，
# 基于文件位置显式注入项目根，保证下方 ``from installer import bootstrap`` 可用。
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

#: 前端目录。
FRONTEND_DIR = os.path.join(PROJECT_ROOT, "frontend")
#: 内置组件源目录（bundled/）。
BUNDLED_DIR = os.path.join(_INSTALLER_DIR, "bundled")

#: 默认产物目录。
RELEASE_DIR = os.path.join(PROJECT_ROOT, "release")
#: Electron --dir 产物目录（electron-builder 约定名）。
ELECTRON_UNPACKED = os.path.join(FRONTEND_DIR, "release", "win-unpacked")
#: 壳产物关键文件（完整性校验口径：目录存在且该文件存在才算可用壳产物）。
ELECTRON_SHELL_EXE = "CX-A.exe"
#: PyInstaller 后端产物目录名（--distpath 下的 backend/）。
BACKEND_DIST_NAME = "backend"
#: 便携根目录名。
PORTABLE_DIR_NAME = "portable"
#: 最终 zip 名。
ZIP_BASENAME = "CX-A-portable-win64"
#: zip 内固定顶层目录前缀（A-4：与便携根实际目录名解耦，解压结果恒定）。
ZIP_TOP_DIR = "CX-A-portable/"


def _log_info(message):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[INFO] {timestamp} {message}")


def _log_warn(message):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[WARN] {timestamp} {message}")


def _die(message, hint=None):
    """报错退出；hint 为可执行的修复指引。"""
    _log_warn(message)
    if hint:
        print(f"       指引：{hint}")
    raise SystemExit(1)


# ------------------------------------------------------------------ #
# 工具探测                                                            #
# ------------------------------------------------------------------ #

def check_tool(cmd_args, name, hint):
    """探测外部工具可用性；失败时报错退出并附指引。

    :param cmd_args: 探测命令（如 ["node", "--version"]）。
    :param name: 工具展示名（用于报错）。
    :param hint: 安装/修复指引。
    :return: None（不可用则 SystemExit）。
    """
    try:
        proc = subprocess.run(
            cmd_args, capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
            shell=(os.name == "nt" and cmd_args[0] in ("node", "npm")),
        )
        if proc.returncode != 0:
            _die(
                f"工具[{name}] 不可用（退出码 {proc.returncode}）",
                hint,
            )
        version = (proc.stdout or proc.stderr).strip().splitlines()
        _log_info(f"工具[{name}] 可用：{version[0] if version else 'ok'}")
    except (OSError, subprocess.SubprocessError) as exc:
        _die(f"工具[{name}] 不可用：{exc}", hint)


def preflight(skip_frontend, skip_electron, skip_backend):
    """按需探测全链路外部工具。"""
    if skip_frontend and skip_electron:
        _log_warn("前端与壳均跳过：跳过 node/npm 探测")
    else:
        check_tool(["node", "--version"], "node", "安装 Node.js >= 18（https://nodejs.org/）")
        check_tool(["npm", "--version"], "npm", "随 Node.js 一起安装")
    if not skip_backend:
        check_tool(
            [sys.executable, "-m", "PyInstaller", "--version"], "PyInstaller",
            "pip install pyinstaller（需 PyInstaller >= 支持当前 Python 的版本）",
        )


# ------------------------------------------------------------------ #
# 构建步骤                                                            #
# ------------------------------------------------------------------ #

def build_renderer():
    """步骤1：vite 构建 renderer（frontend/dist）。"""
    _log_info("步骤1：构建 renderer（npm run build:renderer）…")
    npm = shutil.which("npm") or "npm"
    proc = subprocess.run(
        [npm, "run", "build:renderer"], cwd=FRONTEND_DIR,
        capture_output=True, text=True, timeout=600,
        encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:])
        _die("renderer 构建失败", "在 frontend/ 下单独执行 npm run build 复现定位")
    dist_index = os.path.join(FRONTEND_DIR, "dist", "index.html")
    if not os.path.isfile(dist_index):
        _die(f"构建产物缺失：{dist_index}")
    _log_info("renderer 构建完成（frontend/dist）")


def build_electron_shell():
    """步骤2：electron-builder --dir 产出壳，返回实际使用的产物目录。"""
    _log_info("步骤2：打包 Electron 壳（npm run dist:electron）…")
    npm = shutil.which("npm") or "npm"
    env = dict(os.environ)
    env.setdefault("ELECTRON_MIRROR", "https://npmmirror.com/mirrors/electron/")

    # 壳产物目录预清理：Trae IDE 索引/杀毒等会长期独占 app.asar 句柄，
    # electron-builder 自身 remove 失败即 ERR_ELECTRON_BUILDER_CANNOT_EXECUTE。
    # 清理失败时切换全新输出父目录（与 portable 根自愈同策略）。
    unpacked = ELECTRON_UNPACKED
    extra = []
    if os.path.isdir(unpacked):
        try:
            _rmtree_retry(unpacked, attempts=3, delay=1.0)
        except PermissionError:
            # electron-builder 对 --dir 目标固定在 <directories.output>/win-unpacked
            # 下落位，故覆盖父目录、产物实际落在 <alt>/win-unpacked
            alt_parent = os.path.join(
                FRONTEND_DIR, "release", f"shell-{time.strftime('%Y%m%d_%H%M%S')}"
            )
            extra = ["--", f"-c.directories.output={alt_parent}"]
            unpacked = os.path.join(alt_parent, "win-unpacked")
            _log_warn(f"旧壳产物目录被占用，改用全新输出目录：{unpacked}")
    proc = subprocess.run(
        [npm, "run", "dist:electron", *extra], cwd=FRONTEND_DIR,
        capture_output=True, text=True, timeout=1800, env=env,
        encoding="utf-8", errors="replace",
        shell=(os.name == "nt"),
    )
    if proc.returncode != 0:
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:])
        _die(
            "Electron 壳打包失败",
            "确认已 npm install（electron/electron-builder 二进制经 npmmirror 拉取）",
        )
    if not os.path.isfile(os.path.join(unpacked, ELECTRON_SHELL_EXE)):
        _die(f"壳产物缺失：{os.path.join(unpacked, ELECTRON_SHELL_EXE)}")
    _log_info(f"Electron 壳打包完成：{unpacked}")
    return unpacked


def build_backend(work_dir):
    """步骤3：PyInstaller 打后端（onedir），产物 work_dir/dist/backend。

    使用正式 spec（installer/backend.spec，基于 SPECPATH 推导路径、跨环境可移植），
    不再用命令行参数生成一次性 spec，避免绝对路径固化。

    :param work_dir: PyInstaller workpath/distpath 的父目录。
    :return: str 后端产物目录绝对路径。
    """
    _log_info("步骤3：PyInstaller 打包后端（installer/backend.spec）…")
    dist_path = os.path.join(work_dir, "dist")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--distpath", dist_path,
        "--workpath", os.path.join(work_dir, "work"),
        os.path.join(_INSTALLER_DIR, "backend.spec"),
    ]
    proc = subprocess.run(
        cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=1800,
        encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:])
        _die("后端打包失败", "确认 PyInstaller 已安装且支持当前 Python 版本")
    backend_out = os.path.join(dist_path, BACKEND_DIST_NAME)
    if not os.path.isdir(backend_out):
        _die(f"后端产物缺失：{backend_out}")
    _log_info(f"后端打包完成：{backend_out}")
    return backend_out


# ------------------------------------------------------------------ #
# 组装与压缩                                                          #
# ------------------------------------------------------------------ #

def _rmtree_retry(path, attempts=6, delay=1.0):
    """带重试的目录删除（Windows 健壮性）。

    杀毒/索引服务等会瞬态持有文件句柄（如 app.asar），直接 rmtree 偶发
    ``PermissionError(WinError 32)``；此处按 1s 间隔最多重试 attempts 次，
    重试耗尽仍失败才抛出。

    :param path: 待删除目录。
    :param attempts: 最大尝试次数。
    :param delay: 每次重试间隔秒数。
    """
    for i in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except PermissionError as exc:
            if i == attempts - 1:
                raise
            _log_warn(f"删除被占用（第{i + 1}次重试）：{exc.filename or path}")
            time.sleep(delay)


def _copytree_contents(src, dst):
    """把 src 目录内容（不含 src 本身）拷到 dst；dst 不存在则创建。"""
    os.makedirs(dst, exist_ok=True)
    for item in os.listdir(src):
        s = os.path.join(src, item)
        d = os.path.join(dst, item)
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            shutil.copy2(s, d)


def _prepare_portable_root(output_dir):
    """确定本次组装的便携根目录，规避 Windows 文件锁。

    IDE 索引 / 杀毒服务等可能对上一次产物中的文件（如 app.asar）持长期独占
    句柄，导致清理重建失败。策略：优先清理复用固定目录；清理失败则自动改用
    带时间戳的新目录，保证主流程不被环境锁阻断（旧目录留给用户手动删）。

    :param output_dir: 产物输出目录。
    :return: str 可用的便携根绝对路径。
    """
    base = os.path.join(output_dir, PORTABLE_DIR_NAME)
    if not os.path.isdir(base):
        return base
    try:
        _rmtree_retry(base, attempts=3, delay=1.0)
        return base
    except PermissionError:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        alt = os.path.join(output_dir, f"{PORTABLE_DIR_NAME}-{ts}")
        _log_warn(f"旧目录被其它进程占用且清理失败：{base}")
        _log_warn(f"改用全新目录组装：{alt}（旧目录可稍后手动删除）")
        return alt


def assemble(electron_dist, backend_dist, portable_root):
    """组装便携根：壳产物为根 + runtime/backend + 数据目录初始化。

    纯文件操作，便于单测（tmp_path 注入）。幂等：已存在则重建。

    :param electron_dist: electron-builder 产物目录（win-unpacked）。
    :param backend_dist: PyInstaller 后端产物目录。
    :param portable_root: 便携根输出目录。
    :return: str portable_root。
    """
    _log_info(f"步骤4：组装便携根 -> {portable_root}")
    if os.path.isdir(portable_root):
        # 调用方已通过 _prepare_portable_root 保证可写；此处防御性兜底
        _rmtree_retry(portable_root)
    os.makedirs(portable_root, exist_ok=True)

    # 壳产物内容平铺为便携根（CX-A.exe 位于根）
    _copytree_contents(electron_dist, portable_root)

    # 后端 -> runtime/backend/
    backend_target = os.path.join(portable_root, "runtime", "backend")
    _copytree_contents(backend_dist, backend_target)

    # 数据目录 + 内置模型组件落位 + 默认 config（复用 bootstrap，幂等）
    from installer import bootstrap

    bootstrap.BUNDLED_DIR = BUNDLED_DIR
    bootstrap.ensure_dirs(portable_root)
    bootstrap.install_builtin_assets(portable_root)
    bootstrap.init_workplace(portable_root)

    _log_info("便携根组装完成。")
    return portable_root


def zip_portable(portable_root, release_dir):
    """把便携根压成 zip（步骤5）。

    用户数据排除（A-1）：便携根顶层 config.json 与 data/ 整棵目录不写入 zip，
    避免升级解压覆盖用户配置（含 Fernet 加密 Key）与记忆库；首启由
    backend_entry/启动链 auto-init 按默认值重新生成。
    顶层目录固定（A-4）：arcname 恒以 CX-A-portable/ 前缀开头，与便携根
    实际目录名解耦（清理失败回退的 portable-<时间戳> 目录解压结果一致）。

    :param portable_root: 便携根目录。
    :param release_dir: zip 输出目录。
    :return: str zip 绝对路径。
    """
    os.makedirs(release_dir, exist_ok=True)
    zip_path = os.path.join(release_dir, f"{ZIP_BASENAME}.zip")
    if os.path.exists(zip_path):
        os.remove(zip_path)
    _log_info(f"步骤5：压缩 -> {zip_path}")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for base, _dirs, files in os.walk(portable_root):
            rel_base = os.path.relpath(base, portable_root)
            # A-1：data/ 整棵为用户运行数据，walk 层直接剪枝不入包
            if rel_base.split(os.sep)[0] == "data":
                continue
            for name in files:
                # A-1：便携根顶层的 config.json 为用户配置，不入包
                if rel_base == "." and name == "config.json":
                    continue
                full = os.path.join(base, name)
                # A-4：arcname 固定 CX-A-portable/ 前缀，与实际目录名解耦
                rel = ZIP_TOP_DIR + os.path.relpath(full, portable_root)
                zf.write(full, rel)
    size_mb = os.path.getsize(zip_path) / (1024 * 1024)
    _log_info(f"zip 完成：{size_mb:.1f} MB")
    return zip_path


# ------------------------------------------------------------------ #
# CLI 编排                                                            #
# ------------------------------------------------------------------ #

def main(argv=None):
    """CLI 入口：解析 --skip-* 与 --output，顺序执行全流程。"""
    parser = argparse.ArgumentParser(prog="installer.build", description="CX-A 便携包一键打包编排")
    parser.add_argument("--skip-frontend", action="store_true", help="跳过 renderer 构建（复用 frontend/dist）")
    parser.add_argument("--skip-electron", action="store_true", help="跳过 Electron 壳打包（复用 win-unpacked）")
    parser.add_argument("--skip-backend", action="store_true", help="跳过后端打包（复用已有 runtime/backend）")
    parser.add_argument("--skip-zip", action="store_true", help="只组装便携目录，不压缩 zip")
    parser.add_argument("--output", default=RELEASE_DIR, help="产物输出目录（默认 <项目>/release）")
    args = parser.parse_args(argv)

    preflight(args.skip_frontend and args.skip_electron, args.skip_electron, args.skip_backend)

    if not args.skip_frontend:
        build_renderer()
    electron_dist = ELECTRON_UNPACKED
    if args.skip_electron:
        # 跳过壳构建 = 复用已有产物：目录与关键文件必须齐备（残缺产物不得静默组装）
        if not os.path.isdir(ELECTRON_UNPACKED) or not os.path.isfile(
            os.path.join(ELECTRON_UNPACKED, ELECTRON_SHELL_EXE)
        ):
            _die(
                f"壳产物不存在或残缺且已跳过构建：{ELECTRON_UNPACKED}",
                "去掉 --skip-electron 重新打包",
            )
    else:
        electron_dist = build_electron_shell()

    work_dir = os.path.join(args.output, "_work")
    if args.skip_backend:
        backend_dist = os.path.join(work_dir, "dist", BACKEND_DIST_NAME)
        if not os.path.isdir(backend_dist):
            _die(f"后端产物不存在且已跳过打包：{backend_dist}", "去掉 --skip-backend 重新打包")
    else:
        backend_dist = build_backend(work_dir)

    portable_root = _prepare_portable_root(args.output)
    assemble(electron_dist, backend_dist, portable_root)

    if not args.skip_zip:
        zip_portable(portable_root, args.output)

    _log_info(f"全流程完成。便携根：{portable_root}")
    return portable_root


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    main()
