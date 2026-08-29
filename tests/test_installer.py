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


def test_install_returns_post_install_verification(tmp_path, monkeypatch):
    """批次E：install() 返回的 problems 反映安装后状态——已落位组件不再列为待装。"""
    root = str(tmp_path / "postroot")
    os.makedirs(root)

    # 为全部 builtin 组件准备源，使安装后全部就位
    bundled_root = tmp_path / "bundled"
    manifest = bootstrap.load_manifest()
    for comp in manifest["components"]:
        if comp["status"] != "builtin":
            continue
        src = bundled_root / comp["key"]
        src.mkdir(parents=True, exist_ok=True)
        (src / "asset.bin").write_text("payload", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "BUNDLED_DIR", str(bundled_root))

    problems, builtin_warnings = bootstrap.install(root)

    # 源齐备：无缺失告警；安装后复查不应再有"待装态"报告
    assert builtin_warnings == []
    assert not any("待装态" in p for p in problems)


def test_install_preserves_existing_file_asset_under_data(tmp_path, monkeypatch, capsys):
    """批次E：文件型内置组件 dst 已存在且位于 data/ 前缀下时跳过覆盖并告警。"""
    root = str(tmp_path / "fileroot")
    os.makedirs(root)
    bootstrap.ensure_dirs(root)

    # 既有运行数据文件（未来文件型组件的潜在覆盖目标）
    target = os.path.join(root, "data", "user_asset.bin")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("user-data")

    # 构造文件型内置组件源 + 自定义 manifest（install_target 落在 data/ 下）
    bundled_root = tmp_path / "bundled"
    bundled_root.mkdir(parents=True)
    (bundled_root / "myfile.bin").write_text("builtin-payload", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "BUNDLED_DIR", str(bundled_root))
    custom_manifest = {
        "components": [
            {
                "name": "文件型组件",
                "key": "myfile.bin",
                "size_estimate": "约 1 KB",
                "status": "builtin",
                "install_target": os.path.join("data", "user_asset.bin"),
                "notes": "测试用文件型组件。",
            }
        ]
    }

    warnings = bootstrap.install_builtin_assets(root, manifest=custom_manifest)

    # 用户数据原样保留，内置载荷未覆盖
    with open(target, encoding="utf-8") as fh:
        assert fh.read() == "user-data"
    assert warnings == []
    assert "检测到已有运行数据" in capsys.readouterr().out


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
        # H-3（第三轮体检批次4）：--config 指向便携根顶层（与安装链统一真相源）
        "--config", os.path.join(str(tmp_path), "config.json"),
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
    # 端口预检注入为恒空闲：单测不依赖宿主机 8600 真实占用状态
    monkeypatch.setattr(backend_entry, "check_port_bindable", lambda host, port: True)

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


def test_backend_entry_port_occupied_exits_with_error(tmp_path, monkeypatch, capsys):
    """A-3：端口被占时 main() 应以退出码 1 终止，stderr 给出中文处置提示。"""
    from installer import backend_entry

    root = str(tmp_path / "portable")
    os.makedirs(os.path.join(root, "runtime", "backend"))
    fake_exe = os.path.join(root, "runtime", "backend", "backend.exe")
    with open(fake_exe, "wb") as fh:
        fh.write(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe), raising=False)
    # 预检注入为恒占用：不依赖真实端口状态
    monkeypatch.setattr(backend_entry, "check_port_bindable", lambda host, port: False)

    with pytest.raises(SystemExit) as exc_info:
        backend_entry.main()

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "8600" in err
    assert "已被占用" in err
    assert "backend.exe" in err


def test_check_port_bindable_probe_real_port():
    """A-3：check_port_bindable 真实探测——被监听端口返回 False，释放后返回 True。"""
    import socket as socket_mod

    from installer import backend_entry

    probe = socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))  # 由操作系统分配一个空闲端口
        probe.listen(1)
        occupied_port = probe.getsockname()[1]
        assert backend_entry.check_port_bindable("127.0.0.1", occupied_port) is False
    finally:
        probe.close()
    # 释放后同端口应可绑定
    assert backend_entry.check_port_bindable("127.0.0.1", occupied_port) is True


# ------------------------------------------------------------------ #
# 批次4（第三轮体检）：配置真相源统一 / frozen-aware 路径               #
# ------------------------------------------------------------------ #


def test_api_server_config_path_unifies_install_and_runtime(tmp_path):
    """H-3：config_path 指向便携根顶层 config.json 时，服务读写同一真相源。

    模拟安装链先写根 config.json（bootstrap.init_workplace 同口径），再以
    create_app(data_dir, config_path) 起服——运行链 PUT settings 落盘到根
    config.json 而非 data/config.json。
    """
    from lite.server.api_server import create_app

    root = tmp_path / "portable"
    root.mkdir()
    root_config = root / "config.json"
    # 安装链：bootstrap.init_workplace 写根 config.json
    install_cm = ConfigManager(config_path=str(root_config))
    install_cm.set("cloud", "provider", "moonshot")
    install_cm.save()

    data_dir = root / "data"
    _store, _pipeline, handler = create_app(
        data_dir=str(data_dir), config_path=str(root_config)
    )
    # 运行链读到安装链写入的值（修复前运行链读 data/config.json 恒为默认 deepseek）
    assert handler._config.get("cloud", "provider", "deepseek") == "moonshot"
    # 运行链写 settings 落盘到根 config.json
    handler._config.set("tts", "voice", "my-voice")
    handler._config.save()
    on_disk = json.loads(root_config.read_text(encoding="utf-8"))
    assert on_disk["tts"]["voice"] == "my-voice"
    assert not (data_dir / "config.json").exists()


def test_settings_put_api_key_encrypted_and_hidden(tmp_path):
    """H-6：PUT /api/settings 支持 cloud.api_key——Fernet 加密落盘且 GET 视图不含。"""
    import urllib.error
    import urllib.request
    import threading
    from http.server import HTTPServer

    from lite.server.api_server import create_app

    root = tmp_path / "portable"
    root.mkdir()
    root_config = root / "config.json"
    data_dir = root / "data"
    _store, _pipeline, handler = create_app(
        data_dir=str(data_dir), config_path=str(root_config)
    )
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(
            f"{base}/api/settings",
            data=json.dumps({"cloud": {"api_key": "sk-test-abc123"}}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload["ok"] is True
        assert "cloud.api_key" in payload["applied"]
        # GET 视图不含 api_key（脱敏不变）
        with urllib.request.urlopen(f"{base}/api/settings", timeout=5) as resp:
            view = json.loads(resp.read().decode("utf-8"))
        assert "api_key" not in view["cloud"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
    # 落盘为 Fernet 加密形态（cxa_enc: 前缀），明文不进配置文件
    on_disk = json.loads(root_config.read_text(encoding="utf-8"))
    assert on_disk["cloud"]["api_key"].startswith("cxa_enc:")
    assert "sk-test-abc123" not in root_config.read_text(encoding="utf-8")


def test_app_root_frozen_resolves_portable_root(tmp_path, monkeypatch):
    """M-14：frozen-aware app_root——冻结态从 sys.executable 上溯到便携根。"""
    from lite.config import paths as paths_mod

    fake_exe = tmp_path / "runtime" / "backend" / "backend.exe"
    fake_exe.parent.mkdir(parents=True)
    fake_exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe), raising=False)

    assert paths_mod.app_root() == str(tmp_path)
    assert paths_mod.data_root() == os.path.join(str(tmp_path), "data")
    monkeypatch.delattr(sys, "frozen", raising=False)


def test_first_run_providers_derived_from_adapter():
    """L-8：first_run.DEFAULT_PROVIDERS 与 adapter.PROVIDER_BASE_URLS 同源。"""
    from lite.cloud.adapter import PROVIDER_BASE_URLS

    from installer import first_run as first_run_mod

    assert first_run_mod.DEFAULT_PROVIDERS == tuple(PROVIDER_BASE_URLS.keys())


def test_bootstrap_ensure_dirs_covers_required(tmp_path):
    """M-15：ensure_dirs 从 REQUIRED_DATA_DIRS 派生——两清单天然一致。"""
    bootstrap.ensure_dirs(str(tmp_path))
    for rel in bootstrap.REQUIRED_DATA_DIRS:
        assert (tmp_path / rel).is_dir(), rel
    assert (tmp_path / "logs").is_dir()
    assert (tmp_path / "data" / "memories.db").exists()


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
    # 批次E：模拟 bundled 组件资产落入 manifest install_target（data/local_llm/...），
    # 验证白名单内的内置组件目录应随包分发
    marker = os.path.join(
        portable_root, "data", "local_llm", "qwen3-embedding-0.6b", "model.gguf"
    )
    os.makedirs(os.path.dirname(marker), exist_ok=True)
    with open(marker, "wb") as fh:
        fh.write(b"fake-gguf")

    # zip：固定顶层前缀 + 关键条目 + 运行期产物排除（A-1/A-4，批次E修订）
    release_dir = str(tmp_path / "rel")
    zip_path = build_mod.zip_portable(portable_root, release_dir)
    assert os.path.isfile(zip_path)
    assert os.path.getsize(zip_path) > 0
    with zipfile.ZipFile(zip_path) as zf:
        names = [n.replace("\\", "/") for n in zf.namelist()]
    # A-4：所有条目恒以 CX-A-portable/ 顶层前缀开头（与实际目录名 portable 解耦）
    assert names, "zip 不应为空"
    assert all(n.startswith("CX-A-portable/") for n in names)
    assert "CX-A-portable/CX-A.exe" in names
    assert "CX-A-portable/runtime/backend/backend.exe" in names
    # A-1：顶层 config.json 与运行期产物（memories.db / logs）不入包
    assert "CX-A-portable/config.json" not in names
    assert "CX-A-portable/data/memories.db" not in names
    assert not any(n.startswith("CX-A-portable/logs/") for n in names)
    # 批次E：manifest 白名单内的内置组件目录随包分发（不再 data/ 整棵缺席）
    assert "CX-A-portable/data/local_llm/qwen3-embedding-0.6b/model.gguf" in names


def test_zip_portable_excludes_user_data_and_fixed_prefix(tmp_path):
    """A-1/A-4 专测（批次E修订）：运行期产物不入包、内置组件目录保留、顶层前缀固定。"""
    import zipfile

    from installer import build as build_mod

    # 故意使用非 CX-A-portable 的目录名：验证前缀与实际目录名解耦
    root = tmp_path / "portable-with-timestamp"
    (root / "resources").mkdir(parents=True)
    (root / "CX-A.exe").write_bytes(b"fake-shell")
    (root / "resources" / "app.asar").write_bytes(b"fake-asar")
    (root / "runtime" / "backend" / "_internal").mkdir(parents=True)
    (root / "runtime" / "backend" / "backend.exe").write_bytes(b"fake-backend")
    (root / "runtime" / "backend" / "_internal" / "lib.dll").write_bytes(b"fake-dll")
    # 运行期产物：顶层 config.json + memories.db + 白名单外 data 子目录 + logs/
    (root / "config.json").write_text('{"cloud": {}}', encoding="utf-8")
    (root / "data").mkdir(parents=True)
    (root / "data" / "memories.db").write_bytes(b"sqlite-payload")
    (root / "data" / "runtime_tables").mkdir(parents=True)
    (root / "data" / "runtime_tables" / "user.tbl").write_bytes(b"user-runtime")
    (root / "logs").mkdir(parents=True)
    (root / "logs" / "app.log").write_text("log-line", encoding="utf-8")
    # 内置组件目录（模拟 bundled 资产落入 manifest install_target 白名单）
    (root / "data" / "lancedb").mkdir(parents=True)
    (root / "data" / "lancedb" / "vectors-0001.lance").write_bytes(b"builtin-vectors")
    (root / "data" / "local_llm" / "qwen3-embedding-0.6b").mkdir(parents=True)
    (root / "data" / "local_llm" / "qwen3-embedding-0.6b" / "model.gguf").write_bytes(
        b"fake-gguf"
    )

    zip_path = build_mod.zip_portable(str(root), str(tmp_path / "rel"))

    with zipfile.ZipFile(zip_path) as zf:
        names = [n.replace("\\", "/") for n in zf.namelist()]

    # A-4：顶层目录恒为 CX-A-portable/，与实际目录名 portable-with-timestamp 无关
    assert names, "zip 不应为空"
    assert all(n.startswith("CX-A-portable/") for n in names)
    assert "CX-A-portable/CX-A.exe" in names
    assert "CX-A-portable/resources/app.asar" in names
    assert "CX-A-portable/runtime/backend/backend.exe" in names
    assert "CX-A-portable/runtime/backend/_internal/lib.dll" in names
    # A-1：运行期产物缺席（顶层 config.json / memories.db / 白名单外 data 子目录 / logs）
    assert "CX-A-portable/config.json" not in names
    assert "CX-A-portable/data/memories.db" not in names
    assert not any(n.startswith("CX-A-portable/data/runtime_tables/") for n in names)
    assert not any(n.startswith("CX-A-portable/logs/") for n in names)
    # 批次E：manifest 白名单内的内置组件目录随包分发
    assert "CX-A-portable/data/lancedb/vectors-0001.lance" in names
    assert "CX-A-portable/data/local_llm/qwen3-embedding-0.6b/model.gguf" in names


def test_build_skip_electron_rejects_incomplete_artifacts(tmp_path, monkeypatch):
    """--skip-electron 时：目录缺失或缺 CX-A.exe 均应报错退出，不静默组装坏包。"""
    from installer import build as build_mod

    # 壳产物目录指到临时区，避免触碰真实 frontend/release
    fake_unpacked = str(tmp_path / "win-unpacked")
    monkeypatch.setattr(build_mod, "ELECTRON_UNPACKED", fake_unpacked)

    # 场景1：目录不存在 → 报错
    alt_out = str(tmp_path / "alt-out")
    with pytest.raises(SystemExit):
        build_mod.main([
            "--skip-frontend", "--skip-electron", "--skip-backend", "--skip-zip",
            "--output", alt_out,
        ])

    # 场景2：目录存在但缺 CX-A.exe（残缺产物）→ 同样报错
    os.makedirs(fake_unpacked, exist_ok=True)
    with open(os.path.join(fake_unpacked, "stale.tmp"), "w", encoding="utf-8") as fh:
        fh.write("leftover")
    with pytest.raises(SystemExit):
        build_mod.main([
            "--skip-frontend", "--skip-electron", "--skip-backend", "--skip-zip",
            "--output", alt_out,
        ])

    # 场景3（对照）：补上 CX-A.exe 与后端产物后应通过校验继续组装
    with open(os.path.join(fake_unpacked, build_mod.ELECTRON_SHELL_EXE), "wb") as fh:
        fh.write(b"fake-exe")
    work_dist = os.path.join(alt_out, "_work", "dist", "backend")
    os.makedirs(work_dist, exist_ok=True)
    with open(os.path.join(work_dist, "backend.exe"), "wb") as fh:
        fh.write(b"fake-backend")
    build_mod.main([
        "--skip-frontend", "--skip-electron", "--skip-backend", "--skip-zip",
        "--output", alt_out,
    ])
    assert os.path.isfile(os.path.join(alt_out, "portable", build_mod.ELECTRON_SHELL_EXE))
    assert os.path.isfile(os.path.join(alt_out, "portable", "runtime", "backend", "backend.exe"))