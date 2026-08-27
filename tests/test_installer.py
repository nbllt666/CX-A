# -*- coding: utf-8 -*-
"""Task A10 安装程序单元测试（pytest，tmp_path 临时根目录）。

覆盖：
- ensure_dirs 幂等且目录齐全
- init_workplace 后 config.json 存在、memories.db 建表成功
- verify_components 在缺组件时返回警告不抛错
- first_run 全流程驱动：注入 input → cloud.provider=deepseek、
  api_key 写入且加密往返、本地小 LLM 提示输出
"""

import json
import os
import sqlite3
import sys

import pytest

from installer import bootstrap
from installer.first_run import FirstRunDriver
from lite.config.config_manager import ConfigManager


# ------------------------------------------------------------------ #
# ensure_dirs：幂等 + 目录齐全                                        #
# ------------------------------------------------------------------ #

def test_ensure_dirs_idempotent_and_complete(tmp_path):
    """ensure_dirs 幂等，且 data/ 系列与 logs/ 目录齐全，memories.db 占位存在。"""
    root = str(tmp_path)

    dirs = bootstrap.ensure_dirs(root)
    bootstrap.ensure_dirs(root)  # 二次调用应幂等，不报错

    for rel in ["data", os.path.join("data", "lancedb"),
                os.path.join("data", "local_llm"), os.path.join("data", "voices"),
                "logs"]:
        assert os.path.isdir(os.path.join(root, rel)), f"缺少目录 {rel}"
    assert os.path.exists(os.path.join(root, "data", "memories.db"))
    assert len(dirs) >= 5


# ------------------------------------------------------------------ #
# init_workplace：config.json + memories.db 建表                      #
# ------------------------------------------------------------------ #

def test_init_workplace_config_and_db(tmp_path):
    """init_workplace 后 config.json 生成、memories.db 完成建表。"""
    root = str(tmp_path)
    bootstrap.ensure_dirs(root)

    cfg = bootstrap.init_workplace(root)

    # config.json 存在且含默认云端提供商
    assert os.path.exists(os.path.join(root, "config.json"))
    assert cfg.get("cloud", "provider") == "deepseek"

    # memories.db 已建表
    db_path = os.path.join(root, "data", "memories.db")
    assert os.path.exists(db_path)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memories'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None


# ------------------------------------------------------------------ #
# verify_components：缺组件时返回警告不抛错                            #
# ------------------------------------------------------------------ #

def test_verify_components_returns_warnings_not_raise(tmp_path):
    """空白根目录缺少必需组件时，返回警告列表而不抛错。"""
    root = str(tmp_path)  # 全新空目录：目录缺失 + 组件待装

    problems = bootstrap.verify_components(root)

    assert isinstance(problems, list)
    assert len(problems) > 0
    # 提示文案包含"待装态"或"缺少"等中文警告
    joined = "\n".join(problems)
    assert ("待装态" in joined) or ("缺少" in joined)


def test_verify_components_after_ensure_reduces_warnings(tmp_path):
    """ensure_dirs 后再校验，数据目录相关警告应消除（组件待装仅为开发态提示）。"""
    root = str(tmp_path)
    bootstrap.ensure_dirs(root)

    problems = bootstrap.verify_components(root)

    # 不再出现"缺少必需数据目录"类问题
    assert not any("缺少必需数据目录" in p for p in problems)


def test_verify_components_requires_nonempty_dir(tmp_path):
    """HP1：目录型组件须非空才算已安装——空占位目录不应误判为已装。"""
    root = str(tmp_path)
    bootstrap.ensure_dirs(root)

    # data/lancedb 为空目录：LanceDB 组件应报"待装态"
    problems = bootstrap.verify_components(root)
    assert any("lancedb" in p and "待装态" in p for p in problems)

    # 放入一个文件后视为已安装，该组件不再报待装态
    with open(os.path.join(root, "data", "lancedb", "vectors-0001.lance"), "w", encoding="utf-8") as fh:
        fh.write("user-vector-data")
    problems_after = bootstrap.verify_components(root)
    assert not any("data/lancedb" in p for p in problems_after)


# ------------------------------------------------------------------ #
# HP1：install() 不得擦除既有运行数据                                  #
# ------------------------------------------------------------------ #

def test_install_preserves_existing_nonempty_lancedb(tmp_path, monkeypatch, capsys):
    """预置非空 data/lancedb 后重跑 install()：既有向量库数据不被清空、内置源不覆盖进去。"""
    root = str(tmp_path / "instroot")
    os.makedirs(root)
    bootstrap.ensure_dirs(root)

    lancedb_dir = os.path.join(root, "data", "lancedb")
    user_table = os.path.join(lancedb_dir, "vectors-0001.lance")
    with open(user_table, "w", encoding="utf-8") as fh:
        fh.write("user-vector-data")

    # 构造假的内置组件源并替换 BUNDLED_DIR：验证"有源可拷"时同样跳过覆盖
    bundled_root = tmp_path / "bundled"
    fake_lancedb_src = bundled_root / "lancedb"
    fake_lancedb_src.mkdir(parents=True)
    (fake_lancedb_src / "lancedb.bin").write_text("builtin-payload", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "BUNDLED_DIR", str(bundled_root))

    bootstrap.install(root)

    # 用户数据原样保留，内置载荷未被写入
    assert os.path.exists(user_table)
    with open(user_table, encoding="utf-8") as fh:
        assert fh.read() == "user-vector-data"
    assert not os.path.exists(os.path.join(lancedb_dir, "lancedb.bin"))
    # 告警提示已输出
    assert "检测到已有运行数据" in capsys.readouterr().out


def test_install_fresh_empty_data_dir_still_receives_assets(tmp_path, monkeypatch):
    """普通全新落位行为不变：空 data/lancedb 重跑 install() 后内置组件正常落位。"""
    root = str(tmp_path / "freshroot")
    os.makedirs(root)
    bootstrap.ensure_dirs(root)  # 预建空的 data/lancedb 占位

    bundled_root = tmp_path / "bundled"
    fake_src = bundled_root / "lancedb"
    fake_src.mkdir(parents=True)
    (fake_src / "lancedb.bin").write_text("builtin-payload", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "BUNDLED_DIR", str(bundled_root))

    bootstrap.install(root)

    assert os.path.isfile(os.path.join(root, "data", "lancedb", "lancedb.bin"))
    assert not any(p.suffix == ".tmp" for p in (tmp_path / "freshroot").rglob("*"))


# ------------------------------------------------------------------ #
# first_run：全流程驱动                                              #
# ------------------------------------------------------------------ #

def test_first_run_full_flow(tmp_path):
    """注入默认输入，验证 provider=deepseek、api_key 加密往返、本地小 LLM 提示。"""
    root = str(tmp_path)
    bootstrap.ensure_dirs(root)

    # 注入序列：提供商留空→默认 deepseek；API Key；本地小 LLM 无输入
    responses = iter(["", "sk-super-secret", ""])
    output_lines = []
    driver = FirstRunDriver(
        root,
        input_fn=lambda _prompt: next(responses),
        output_fn=output_lines.append,
    )

    result = driver.run()

    # 步骤1：默认 deepseek
    assert result["provider"] == "deepseek"
    assert driver.cm.get("cloud", "provider") == "deepseek"

    # 步骤2：api_key 写入并加密往返
    assert result["api_key"] == "sk-super-secret"
    raw = json.loads(open(os.path.join(root, "config.json"), encoding="utf-8").read())
    assert raw["cloud"]["api_key"].startswith("cxa_enc:")
    assert "sk-super-secret" not in raw["cloud"]["api_key"]
    # 重新加载还原明文
    cfg2 = ConfigManager(
        config_path=os.path.join(root, "config.json"),
        data_dir=os.path.join(root, "data"),
    )
    assert cfg2.get("cloud", "api_key") == "sk-super-secret"

    # 步骤4：本地小 LLM 引导提示输出 + source=modelscope
    joined = "\n".join(output_lines)
    assert "本地小 LLM" in joined
    assert "1.7B" in joined
    assert "data/local_llm/" in joined
    assert driver.cm.get("local_llm", "source") == "modelscope"

    # 步骤3：CX-OPEN 音色提示
    assert "CX-OPEN" in joined


def test_first_run_api_key_blank_skips(tmp_path):
    """API Key 留空时跳过，不写入，且提示可在设置页补填。"""
    root = str(tmp_path)
    bootstrap.ensure_dirs(root)

    responses = iter(["tongyi", "", ""])
    output_lines = []
    driver = FirstRunDriver(
        root, input_fn=lambda _p: next(responses), output_fn=output_lines.append
    )

    result = driver.run()

    assert result["provider"] == "tongyi"
    assert result["api_key"] == ""
    joined = "\n".join(output_lines)
    assert "可在设置页补填" in joined
    # 未填写则不写入 key
    raw = json.loads(open(os.path.join(root, "config.json"), encoding="utf-8").read())
    assert raw["cloud"]["api_key"] == ""


# ------------------------------------------------------------------ #
# backend_entry：后端可执行入口（打包链路步骤1）                       #
# ------------------------------------------------------------------ #

def test_backend_entry_dev_root_and_args(tmp_path, monkeypatch):
    """开发态：root=项目根（installer 上级），build_args 指向 <root>/data，端口 8600。"""
    from installer import backend_entry

    monkeypatch.delattr(sys, "frozen", raising=False)
    assert backend_entry.resolve_root() == os.path.dirname(os.path.dirname(os.path.abspath(backend_entry.__file__)))

    args = backend_entry.build_args(str(tmp_path))
    assert args == [
        "--host", "127.0.0.1",
        "--port", "8600",
        "--data-dir", os.path.join(str(tmp_path), "data"),
    ]


def test_backend_entry_frozen_root(tmp_path, monkeypatch):
    """冻结态：root = exe 目录上溯 2 级（<root>/runtime/backend/backend.exe）。"""
    from installer import backend_entry

    fake_exe = tmp_path / "runtime" / "backend" / "backend.exe"
    fake_exe.parent.mkdir(parents=True)
    fake_exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe), raising=False)

    assert backend_entry.resolve_root() == str(tmp_path)
    args = backend_entry.build_args()
    assert args[5] == os.path.join(str(tmp_path), "data")


def test_backend_entry_main_creates_data_dir(tmp_path, monkeypatch):
    """main() 起服前自愈数据目录；并以组装参数进入 api_server.main（注入 fake 验证）。"""
    from installer import backend_entry

    root = str(tmp_path / "portable")
    os.makedirs(os.path.join(root, "runtime", "backend"))
    fake_exe = os.path.join(root, "runtime", "backend", "backend.exe")
    with open(fake_exe, "wb") as fh:
        fh.write(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe), raising=False)

    captured = {}

    def fake_api_main(argv):
        captured["argv"] = list(argv)

    import lite.server.api_server as api_server_mod
    monkeypatch.setattr(api_server_mod, "main", fake_api_main)

    backend_entry.main()  # 正常返回即未进入 serve_forever

    assert os.path.isdir(os.path.join(root, "data"))
    assert captured["argv"][0] == "--host"
    assert captured["argv"][2] == "--port"
    assert captured["argv"][5] == os.path.join(root, "data")


# ------------------------------------------------------------------ #
# build：便携包组装与压缩（打包链路步骤4）                             #
# ------------------------------------------------------------------ #

def test_build_assemble_and_zip(tmp_path):
    """assemble 平铺壳产物 + 落位后端 + bootstrap 初始化；zip 含关键条目。"""
    import zipfile

    from installer import build as build_mod

    # 伪造 Electron 壳产物（CX-A.exe 位于根）
    electron_dist = tmp_path / "win-unpacked"
    (electron_dist / "resources").mkdir(parents=True)
    (electron_dist / "CX-A.exe").write_bytes(b"fake-exe")
    (electron_dist / "resources" / "app.asar").write_bytes(b"fake-asar")

    # 伪造 PyInstaller 后端产物（onedir：exe + _internal/）
    backend_dist = tmp_path / "backend-dist"
    (backend_dist / "_internal").mkdir(parents=True)
    (backend_dist / "backend.exe").write_bytes(b"fake-backend")
    (backend_dist / "_internal" / "lib.dll").write_bytes(b"fake-dll")

    portable_root = str(tmp_path / "portable")
    build_mod.assemble(str(electron_dist), str(backend_dist), portable_root)

    # 壳产物平铺
    assert os.path.isfile(os.path.join(portable_root, "CX-A.exe"))
    assert os.path.isfile(os.path.join(portable_root, "resources", "app.asar"))
    # 后端落位 runtime/backend/
    assert os.path.isfile(os.path.join(portable_root, "runtime", "backend", "backend.exe"))
    assert os.path.isfile(os.path.join(portable_root, "runtime", "backend", "_internal", "lib.dll"))
    # bootstrap 初始化产物：数据目录 + 默认 config
    assert os.path.isdir(os.path.join(portable_root, "data", "lancedb"))
    assert os.path.isfile(os.path.join(portable_root, "config.json"))

    # zip：含 exe 与后端条目
    release_dir = str(tmp_path / "rel")
    zip_path = build_mod.zip_portable(portable_root, release_dir)
    assert os.path.isfile(zip_path)
    assert os.path.getsize(zip_path) > 0
    with zipfile.ZipFile(zip_path) as zf:
        names = [n.replace("\\", "/") for n in zf.namelist()]
    assert any(n.endswith("CX-A.exe") for n in names)
    assert any("runtime/backend/backend.exe" in n for n in names)