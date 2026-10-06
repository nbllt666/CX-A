# -*- coding: utf-8 -*-
"""捆绑 llama.cpp 升级脚本（20261004 Gemma 4 准入）：替换 installer/bundled 两目录。

背景：捆绑 build 11178（2025 中）不认识 Gemma 4 架构——本地推理（含多模态
--mmproj）必须升级到具备 Gemma 4 支持的新版预编译二进制。本脚本从
``ggml-org/llama.cpp`` GitHub release 下载官方 Windows 预编译包，替换：

- ``installer/bundled/llama_cpp``        ← ``llama-<tag>-bin-win-cuda-12.4-x64.zip``
                                            + ``cudart-llama-bin-win-cuda-12.4-x64.zip``
- ``installer/bundled/llama_cpp_vulkan`` ← ``llama-<tag>-bin-win-vulkan-x64.zip``

旧目录整体改名备份为 ``<dir>.bak-<时间戳>``（升级失败可手工改名回滚）；
替换后更新 ``installer/manifest.json`` 两个组件的体积与升级留痕。

用法（纯标准库，无第三方依赖）::

    python scripts/update_llama_bundled.py               # 最新 b 系列 release
    python scripts/update_llama_bundled.py --tag b11391  # 指定版本
    python scripts/update_llama_bundled.py --dry-run     # 只探测，不下载不替换

说明：
- GitHub latest release 现为无资产的版本占位（v0.5.0），真正的预编译二进制
  发布在 b 系列 tag 上——本脚本遍历 releases 取**第一个**含 win-cuda-12.4
  x64 资产的 release 作为最新可升级版本；
- 下载先写 ``.tmp`` 再 ``os.replace`` 原子改名，并缓存于 ``<repo>/.cache/llama-update/``
  （重跑跳过已完成的包）；解压到目录后校验 ``llama-server.exe`` 存在且非截断
  才动正式目录（失败保留备份零破坏）；
- 全程中文 ``[INFO]/[ERROR]`` + 时间戳日志。
"""

import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.request
import zipfile

#: 仓库与资产名模板（2026-10-04 对 b11391 实测）
GITHUB_RELEASES_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases"
CUDA_ZIP_TPL = "llama-{tag}-bin-win-cuda-12.4-x64.zip"
CUDART_ZIP_TPL = "cudart-llama-bin-win-cuda-12.4-x64.zip"
VULKAN_ZIP_TPL = "llama-{tag}-bin-win-vulkan-x64.zip"

#: 仓库根（scripts/ 的上一级）
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLED_DIR = os.path.join(REPO_ROOT, "installer", "bundled")
MANIFEST_PATH = os.path.join(REPO_ROOT, "installer", "manifest.json")

#: 目标目录映射：目录名 → 需解压的 zip 资产（按序合并解压）
TARGETS = [
    ("llama_cpp", [CUDA_ZIP_TPL, CUDART_ZIP_TPL]),
    ("llama_cpp_vulkan", [VULKAN_ZIP_TPL]),
]

#: llama-server.exe 最小合法体积（字节）：CUDA 构建的 server 是**薄启动器**
#: （实测 ~9KB，真实代码在 ggml-cuda.dll 等 DLL），阈值只防 0 字节/截断占位。
MIN_SERVER_BYTES = 4096

#: 下载缓存目录（仓库根 .cache/，打包与 bundled 均不涉及）：重跑时同名字节
#: 完整 zip 直接复用，避免网络抖动后重下 657MB。
CACHE_DIR = os.path.join(REPO_ROOT, ".cache", "llama-update")


def _now():
    """本地时间戳字符串。"""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(level, msg):
    """中文时间戳日志。"""
    print(f"{_now()} [{level}] {msg}", flush=True)


def _http_json(url, retries=3):
    """GET JSON（带 UA 头；GitHub API 无 UA 会 403；网络抖动重试 3 次）。"""
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "cx-a-llama-updater"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - 网络抖动（SSL EOF 等）重试
            last_exc = exc
            if attempt < retries:
                _log("WARNING", f"请求失败（第 {attempt} 次，重试中）：{exc}")
                time.sleep(2 * attempt)
    raise RuntimeError(f"GitHub API 请求失败（已重试 {retries} 次）：{last_exc}")


def pick_release(tag=None):
    """选定目标 release：指定 tag 直取；否则遍历取首个含 win-cuda-12.4 资产的 b 系列。

    :param tag: 形如 ``b11391`` 的版本标签；None 表示自动选最新。
    :return: dict release（含 tag_name / assets）。
    :raises RuntimeError: 未找到含目标资产的 release 时抛出（中文）。
    """
    if tag:
        _log("INFO", f"按指定标签探测 release：{tag}")
        return _http_json(f"{GITHUB_RELEASES_API}/tags/{tag}")
    _log("INFO", "遍历最近 release，取首个含 win-cuda-12.4 x64 资产的版本")
    for page in range(1, 4):  # 3 页 × 30 个足够覆盖回溯窗口
        releases = _http_json(f"{GITHUB_RELEASES_API}?per_page=30&page={page}")
        for rel in releases:
            names = {a["name"] for a in rel.get("assets", [])}
            if CUDA_ZIP_TPL.format(tag=rel["tag_name"]) in names:
                return rel
    raise RuntimeError(
        "最近 90 个 release 均未发现 win-cuda-12.4 x64 预编译资产"
        "（资产命名可能已变更，请人工核对 GitHub 发布页后更新本脚本模板）。"
    )


def _asset_url(release, name):
    """取指定名称资产的下载 URL（browser_download_url 直链）。"""
    for asset in release.get("assets", []):
        if asset["name"] == name:
            return asset["browser_download_url"], int(asset.get("size") or 0)
    raise RuntimeError(f"release {release['tag_name']} 缺少资产：{name}")


def download_zip(url, total_bytes, dest):
    """流式下载 zip 到 ``dest``（先写 .tmp 再原子改名；每 10% 打印一次进度）。"""
    tmp = dest + ".tmp"
    req = urllib.request.Request(url, headers={"User-Agent": "cx-a-llama-updater"})
    done = 0
    next_mark = 10
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as fh:
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if total_bytes and done * 100 // total_bytes >= next_mark:
                _log("INFO", f"  下载进度：{next_mark}%（{done / 1048576:.0f} MB）")
                next_mark += 10
    if total_bytes and abs(done - total_bytes) > total_bytes * 0.01:
        os.remove(tmp)
        raise RuntimeError(f"下载不完整：预期 {total_bytes} 字节，实际 {done} 字节")
    os.replace(tmp, dest)
    return done


def extract_into(zip_path, dest_dir):
    """解压单个 zip 到目标目录（server 存在性按目录合并后终验，见 main 第 3 步）。"""
    with zipfile.ZipFile(zip_path) as zf:
        if not any(n.endswith("ggml.dll") or n.endswith("ggml-cpu.dll") for n in zf.namelist()):
            _log("WARNING", f"{os.path.basename(zip_path)} 未发现 ggml 核心 dll（继续，按实际内容落位）")
        zf.extractall(dest_dir)


def dir_size_mb(path):
    """目录总字节数（MB，向下取整）；目录不存在返回 0。"""
    total = 0
    for _root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(_root, name))
            except OSError:
                pass
    return total // (1024 * 1024)


def update_manifest(tag):
    """更新 installer/manifest.json：两组件体积按实测目录重算 + 升级留痕。"""
    with open(MANIFEST_PATH, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    stamp = datetime.datetime.now().strftime("%Y%m%d")
    for comp in manifest.get("components", []):
        key = comp.get("key")
        if key == "llama_cpp":
            comp["size_estimate"] = f"约 {dir_size_mb(os.path.join(BUNDLED_DIR, 'llama_cpp'))} MB"
            comp["notes"] += f"；{stamp} 经 scripts/update_llama_bundled.py 升级至 {tag}（Gemma 4 架构支持 + 多模态 --mmproj，含 CUDA 12.4 运行时 cudart）"
        elif key == "llama_cpp_vulkan":
            comp["size_estimate"] = f"约 {dir_size_mb(os.path.join(BUNDLED_DIR, 'llama_cpp_vulkan'))} MB"
            comp["notes"] = re.sub(
                r"版本 [^\s，,]+ build \d+( commit [0-9a-f]+)?",
                f"版本 {tag}",
                comp.get("notes", ""),
            )
            comp["notes"] += f"；{stamp} 经 scripts/update_llama_bundled.py 升级至 {tag}（与 llama_cpp 的 CUDA 构建同版本）"
    manifest["generated_at"] = datetime.datetime.now().strftime("%Y-%m-%d")
    with open(MANIFEST_PATH, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    _log("INFO", "installer/manifest.json 已更新（体积 + 升级留痕）")


def main():
    """入口：探测 → 下载 → 校验 → 备份替换 → manifest 留痕。"""
    parser = argparse.ArgumentParser(description="升级捆绑 llama.cpp 预编译二进制")
    parser.add_argument("--tag", default=None, help="指定版本标签（如 b11391）；缺省取最新")
    parser.add_argument("--dry-run", action="store_true", help="只探测版本与资产，不下载不替换")
    args = parser.parse_args()

    release = pick_release(args.tag)
    tag = release["tag_name"]
    if not re.fullmatch(r"b\d+", tag):
        raise RuntimeError(f"版本标签非 b 系列预期格式：{tag!r}")
    _log("INFO", f"目标版本：{tag}")

    plan = []
    for dir_name, templates in TARGETS:
        zips = []
        for tpl in templates:
            name = tpl.format(tag=tag)
            url, size = _asset_url(release, name)
            zips.append((name, url, size))
        plan.append((dir_name, zips))
    for dir_name, zips in plan:
        for name, _url, size in zips:
            _log("INFO", f"计划：{dir_name} ← {name}（{size / 1048576:.0f} MB）")
    if args.dry_run:
        _log("INFO", "--dry-run：探测完成，未下载未替换")
        return

    # 1) 下载到缓存目录（同名字节完整 zip 直接复用；任一失败即中止，不触碰正式目录）
    os.makedirs(CACHE_DIR, exist_ok=True)
    downloaded = []
    for dir_name, zips in plan:
        for name, url, size in zips:
            dest = os.path.join(CACHE_DIR, name)
            if os.path.isfile(dest) and size and abs(os.path.getsize(dest) - size) <= size * 0.01:
                try:
                    with zipfile.ZipFile(dest) as _zf:
                        _zf.namelist()
                    _log("INFO", f"缓存命中，跳过下载：{name}")
                    downloaded.append((dir_name, dest))
                    continue
                except zipfile.BadZipFile:
                    _log("WARNING", f"缓存 zip 损坏，重新下载：{name}")
            _log("INFO", f"开始下载：{name}")
            download_zip(url, size, dest)
            downloaded.append((dir_name, dest))

    # 2) 校验：zip 完整可读（注意 cudart 包只含 CUDA 运行时 DLL、不含
    # llama-server.exe——server 存在性按目录合并后终验，见第 3 步）
    _log("INFO", "全部 zip 下载完成，开始校验")
    for _dir_name, path in downloaded:
        try:
            with zipfile.ZipFile(path) as zf:
                zf.testzip()
        except zipfile.BadZipFile as exc:
            raise RuntimeError(f"zip 损坏（缓存文件可删除后重跑）：{path}（{exc}）")
    _log("INFO", "zip 内容校验通过")

    # 3) 逐目录：备份旧目录 → 新建 → 解压 → 终验（任一失败旧目录保留 .bak-* 可回滚）
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    for dir_name, zips in plan:
        target = os.path.join(BUNDLED_DIR, dir_name)
        if os.path.isdir(target):
            backup = target + f".bak-{stamp}"
            os.rename(target, backup)
            _log("INFO", f"旧目录已备份：{backup}")
        try:
            os.makedirs(target, exist_ok=True)
            for name, _url, _size in zips:
                extract_into(os.path.join(CACHE_DIR, name), target)
            server_exe = os.path.join(target, "llama-server.exe")
            if not os.path.isfile(server_exe) or os.path.getsize(server_exe) < MIN_SERVER_BYTES:
                raise RuntimeError(f"落位后 llama-server.exe 校验失败：{server_exe}")
            _log("INFO", f"{dir_name} 落位完成（约 {dir_size_mb(target)} MB，版本 {tag}）")
        except Exception:
            _log("ERROR", f"{dir_name} 落位失败；旧目录保留为 .bak-{stamp}，可手工改名回滚")
            raise

    update_manifest(tag)
    _log("INFO", f"升级完成：bundled 两目录已更新至 {tag}；请跑一次后端测试与打包冒烟验证")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - 顶层统一中文报错
        _log("ERROR", f"{exc.__class__.__name__}: {exc}")
        sys.exit(1)
