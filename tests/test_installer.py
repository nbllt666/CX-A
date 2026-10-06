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
from lite.config.download_sources import model_repo_for_channel


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
    """HP1：目录型组件须非空才算已安装——空占位目录不应误判为已装。

    20260926_模块0_真实嵌入与向量持久化：lancedb 组件已改 optional（生产后端改
    SQLite 持久向量库），本用例改用 melotts（builtin 目录型组件，落位
    data/voices/cx-open）验证同一口径。
    """
    root = str(tmp_path)
    bootstrap.ensure_dirs(root)

    # data/voices/cx-open 为空目录：MeloTTS 组件应报"待装态"
    problems = bootstrap.verify_components(root)
    assert any("data/voices/cx-open" in p and "待装态" in p for p in problems)

    # 放入一个文件后视为已安装，该组件不再报待装态
    voice_dir = os.path.join(root, "data", "voices", "cx-open")
    os.makedirs(voice_dir, exist_ok=True)
    with open(os.path.join(voice_dir, "config.json"), "w", encoding="utf-8") as fh:
        fh.write("{}")
    problems_after = bootstrap.verify_components(root)
    assert not any("data/voices/cx-open" in p for p in problems_after)


# ------------------------------------------------------------------ #
# HP1：install() 不得擦除既有运行数据                                  #
# ------------------------------------------------------------------ #

def test_install_preserves_existing_nonempty_voice_dir(tmp_path, monkeypatch, capsys):
    """预置非空 data/voices/cx-open 后重跑 install()：既有音色数据不被清空、内置源不覆盖进去。

    20260926_模块0_真实嵌入与向量持久化：lancedb 组件已改 optional（不再参与
    bundled 拷贝），本用例改用 melotts（builtin 目录型组件）验证"运行数据保护"口径。
    """
    root = str(tmp_path / "instroot")
    os.makedirs(root)
    bootstrap.ensure_dirs(root)

    voice_dir = os.path.join(root, "data", "voices", "cx-open")
    os.makedirs(voice_dir, exist_ok=True)
    user_ckpt = os.path.join(voice_dir, "ckpt.txt")
    with open(user_ckpt, "w", encoding="utf-8") as fh:
        fh.write("user-voice-data")

    # 构造假的内置组件源并替换 BUNDLED_DIR：验证"有源可拷"时同样跳过覆盖
    bundled_root = tmp_path / "bundled"
    fake_src = bundled_root / "melotts"
    fake_src.mkdir(parents=True)
    (fake_src / "melotts.bin").write_text("builtin-payload", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "BUNDLED_DIR", str(bundled_root))

    bootstrap.install(root)

    # 用户数据原样保留，内置载荷未被写入
    assert os.path.exists(user_ckpt)
    with open(user_ckpt, encoding="utf-8") as fh:
        assert fh.read() == "user-voice-data"
    assert not os.path.exists(os.path.join(voice_dir, "melotts.bin"))
    # 告警提示已输出
    assert "检测到已有运行数据" in capsys.readouterr().out


def test_install_fresh_empty_data_dir_still_receives_assets(tmp_path, monkeypatch):
    """普通全新落位行为不变：空 data/voices/cx-open 重跑 install() 后内置组件正常落位。

    20260926_模块0_真实嵌入与向量持久化：lancedb 组件已改 optional，本用例改用
    melotts（builtin 目录型组件）验证同一落位口径。
    """
    root = str(tmp_path / "freshroot")
    os.makedirs(root)
    bootstrap.ensure_dirs(root)  # 预建空的 data/voices（cx-open 由本用例预建为空占位）

    bundled_root = tmp_path / "bundled"
    fake_src = bundled_root / "melotts"
    fake_src.mkdir(parents=True)
    (fake_src / "melotts.bin").write_text("builtin-payload", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "BUNDLED_DIR", str(bundled_root))

    bootstrap.install(root)

    assert os.path.isfile(os.path.join(root, "data", "voices", "cx-open", "melotts.bin"))
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
    assert "Gemma 4 E2B" in joined
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
    """H-6：PUT /api/settings 支持 cloud.api_key——Fernet 加密落盘且 GET 视图仅脱敏回显。

    Task 5（向导选项入设置）契约升级：GET 视图由「不含 api_key 键」改为
    「脱敏回显 sk-****尾4位」；明文任何形式不出现在视图与配置文件中。
    """
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
        # Task 5 契约：GET 视图 api_key 脱敏回显（sk-test-abc123 → sk-****c123）
        with urllib.request.urlopen(f"{base}/api/settings", timeout=5) as resp:
            view = json.loads(resp.read().decode("utf-8"))
        assert view["cloud"]["api_key"] == "sk-****c123"
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
    # 20260926：本用例只校验便携根组装结果，安装器编译为副作用——若走真实 ISCC
    # 会对 installer/bundled 全量资产做分钟级 lzma 编译（本轮起还含 649MB 嵌入模型）。
    # 屏蔽编译器探测，让安装器步骤按"未检测到编译器"路径快速跳过（产物断言不受影响）。
    monkeypatch.setattr(build_mod, "find_iscc", lambda: None)
    build_mod.main([
        "--skip-frontend", "--skip-electron", "--skip-backend", "--skip-zip",
        "--output", alt_out,
    ])
    assert os.path.isfile(os.path.join(alt_out, "portable", build_mod.ELECTRON_SHELL_EXE))
    assert os.path.isfile(os.path.join(alt_out, "portable", "runtime", "backend", "backend.exe"))


# ------------------------------------------------------------------ #
# Task 9：安装器 CLI 向导与统一下载源（追加用例；既有断言一字不改）        #
# ------------------------------------------------------------------ #

#: 镜像通道下的清华 PyPI 索引（与 lite/config/download_sources.PIP_MIRROR_INDEX 同源）
_T9_MIRROR_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
#: 恒定 CPU 结论的检测报告（避免依赖宿主机真实硬件）
_T9_CPU_REPORT = {
    "gpu_vendor": "cpu",
    "cuda_version": None,
    "recommend": "cpu",
    "details": "测试替身：未检测到可用的独立 GPU",
}


def _t9_fake_detector(report):
    """构造 bootstrap.GpuDetector 替身：detect() 恒返回固定报告，不触碰真实硬件。"""
    return lambda runner=None: type(
        "_T9FakeDetector", (), {"detect": lambda self, runner=None: dict(report)}
    )()


def _t9_install_report(root):
    """读取 install() 落盘的 GPU 依赖报告 dict。"""
    path = os.path.join(root, "data", "install_report.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _t9_driver(root, responses, downloader=None):
    """构造注入固定输入序列的 FirstRunDriver，返回 ``(driver, 输出行列表)``。"""
    outputs = []
    seq = iter(list(responses))
    driver = FirstRunDriver(
        root,
        input_fn=lambda _p: next(seq),
        output_fn=outputs.append,
        downloader=downloader,
    )
    return driver, outputs


def _t9_stub_hardware(monkeypatch, use_local=True, tier="E2B-Q4", device="cpu"):
    """把 first_run 的硬件探测替换为确定性替身（不触碰真实硬件与外部命令）。"""
    from installer import first_run as first_run_mod

    profile = {
        "cpu_cores": 8,
        "ram_gb": 16.0,
        "gpu_vendor": "cpu",
        "vram_gb": None,
        "cuda_version": None,
        "disk_free_gb": 200.0,
        "probe_notes": [],
    }
    monkeypatch.setattr(first_run_mod, "detect_profile", lambda root=None, runner=None: dict(profile))
    monkeypatch.setattr(
        first_run_mod,
        "recommend_for",
        lambda profile, disk_free_gb=None: {
            "use_local": use_local,
            "device": device,
            "tier": tier,
            "config_patch": {"local_llm": {"enabled": use_local, "device": device}},
            "model": {
                "tier": tier,
                "repo": "Qwen/Qwen1.5-1.8B-Chat-GGUF",
                "filename": "qwen1_5-1_8b-chat-q4_k_m.gguf",
                "approximate_size_gb": 1.134,
            },
            "reasons": ["测试替身：内存满足本地推理阈值（≥ 8 GB）"],
            "probe_notes": [],
        },
    )


class _T9FakeDownloader:
    """替身下载器：仅记录调用与回调，绝不触网。"""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def download(self, repo, filename, source=None, progress_cb=None, verify_size_gb=None):
        """记录调用参数并按需回调进度；``fail=True`` 时抛出模拟异常。"""
        self.calls.append(
            {
                "repo": repo,
                "filename": filename,
                "source": source,
                "verify_size_gb": verify_size_gb,
            }
        )
        if progress_cb is not None:
            progress_cb(1024, 2048)
            progress_cb(2048, 2048)
        if self.fail:
            raise RuntimeError("模拟下载失败")
        return os.path.join("fake", filename)


# ---- build_gpu_dependency_commands：通道对命令清单的影响 ---- #

@pytest.mark.parametrize("recommend", ["cpu", "cuda", "rocm"])
def test_t9_build_commands_none_and_official_keep_legacy_output(recommend):
    """channel=None（未指定）与 channel="official" 输出与历史版本逐字相同。"""
    legacy = bootstrap.build_gpu_dependency_commands(recommend, "12.4")

    assert bootstrap.build_gpu_dependency_commands(recommend, "12.4") == legacy
    assert bootstrap.build_gpu_dependency_commands(recommend, "12.4", None) == legacy
    assert bootstrap.build_gpu_dependency_commands(recommend, "12.4", "official") == legacy
    assert not any(_T9_MIRROR_INDEX in cmd for cmd in legacy)


@pytest.mark.parametrize("recommend", ["cpu", "cuda", "rocm"])
def test_t9_build_commands_mirror_only_touches_plain_pip_packages(recommend):
    """mirror 通道：仅普通 pip 包追加镜像索引；torch / llama-cpp-python 逐字不变。

    Task 3：ORT 包已与 torch 链解耦，须显式传画像才会出现在清单中（普通 pip 包）。
    """
    profile = {"has_igpu": False, "recommend": recommend}
    legacy = bootstrap.build_gpu_dependency_commands(recommend, "12.4", profile=profile)
    mirrored = bootstrap.build_gpu_dependency_commands(recommend, "12.4", "mirror", profile=profile)

    assert len(mirrored) == len(legacy)
    plain = ("onnxruntime", "onnxruntime-gpu", "onnxruntime-directml")
    touched = 0
    for old, new in zip(legacy, mirrored):
        if old.startswith("#"):
            assert new == old  # 说明性注释不改写
        elif old.split()[2] in plain:
            assert new == f"{old} -i {_T9_MIRROR_INDEX}"
            touched += 1
        else:
            # torch（pytorch 官方轮子源）/ llama-cpp-python（abetlen 专用源）一字不改
            assert new == old
    assert touched >= 1
    assert any(cmd.startswith("pip install torch") for cmd in mirrored)


# ---- install()：安装期通道解析（显式 / config.json / 默认） ---- #

def test_t9_install_explicit_official_has_no_mirror_index(tmp_path, monkeypatch, capsys):
    """显式 official：生成的 pip 依赖命令不含国内镜像索引参数。"""
    root = str(tmp_path / "officialroot")
    os.makedirs(root)
    monkeypatch.setattr(bootstrap, "GpuDetector", _t9_fake_detector(_T9_CPU_REPORT))

    bootstrap.install(root, channel="official")

    report = _t9_install_report(root)
    assert report["pending_commands"] == [
        "pip install torch --index-url https://download.pytorch.org/whl/cpu",
        "pip install torchaudio --index-url https://download.pytorch.org/whl/cpu",
        "pip install onnxruntime",
    ]
    assert not any(_T9_MIRROR_INDEX in cmd for cmd in report["pending_commands"])
    assert "official" in capsys.readouterr().out


def test_t9_install_explicit_mirror_appends_index(tmp_path, monkeypatch):
    """显式 mirror：普通 pip 包追加国内镜像索引，torch 专用源命令保持不变。"""
    root = str(tmp_path / "mirrorroot")
    os.makedirs(root)
    monkeypatch.setattr(bootstrap, "GpuDetector", _t9_fake_detector(_T9_CPU_REPORT))

    bootstrap.install(root, channel="mirror")

    report = _t9_install_report(root)
    assert report["pending_commands"] == [
        "pip install torch --index-url https://download.pytorch.org/whl/cpu",
        "pip install torchaudio --index-url https://download.pytorch.org/whl/cpu",
        f"pip install onnxruntime -i {_T9_MIRROR_INDEX}",
    ]


def test_t9_install_reads_channel_from_root_config(tmp_path, monkeypatch):
    """未显式指定通道：读 root/config.json 的 download.channel（official → 不含镜像索引）。"""
    root = str(tmp_path / "cfgroot")
    os.makedirs(root)
    bootstrap.ensure_dirs(root)
    cm = ConfigManager(
        config_path=os.path.join(root, "config.json"),
        data_dir=os.path.join(root, "data"),
    )
    cm.set("download", "channel", "official")
    cm.save()
    monkeypatch.setattr(bootstrap, "GpuDetector", _t9_fake_detector(_T9_CPU_REPORT))

    bootstrap.install(root)

    report = _t9_install_report(root)
    assert not any(_T9_MIRROR_INDEX in cmd for cmd in report["pending_commands"])


def test_t9_install_default_channel_falls_back_to_mirror(tmp_path, monkeypatch):
    """既无显式通道也无配置取值：回落默认 mirror（普通 pip 包带镜像索引）。"""
    root = str(tmp_path / "defaultroot")
    os.makedirs(root)
    monkeypatch.setattr(bootstrap, "GpuDetector", _t9_fake_detector(_T9_CPU_REPORT))

    bootstrap.install(root)

    report = _t9_install_report(root)
    assert any(_T9_MIRROR_INDEX in cmd for cmd in report["pending_commands"])


def test_t9_resolve_channel_defaults_normalizes_and_survives_corrupt_config(tmp_path):
    """resolve_download_channel：显式归一 / 无配置默认镜像 / 坏配置降级不抛。"""
    root = str(tmp_path / "resolve")
    os.makedirs(root)
    assert bootstrap.resolve_download_channel(root) == "mirror"  # 无 config.json → 默认镜像
    assert bootstrap.resolve_download_channel(root, " OFFICIAL ") == "official"
    assert bootstrap.resolve_download_channel(root, "bogus") == "mirror"

    corrupt_root = str(tmp_path / "corrupt")
    os.makedirs(corrupt_root)
    with open(os.path.join(corrupt_root, "config.json"), "w", encoding="utf-8") as fh:
        fh.write("{不是合法 JSON")
    assert bootstrap.resolve_download_channel(corrupt_root) == "mirror"


def test_t9_bootstrap_cli_channel_flag(tmp_path, monkeypatch):
    """CLI 入口支持 --channel；非法取值由 argparse 拒绝。"""
    root = str(tmp_path / "cliroot")
    os.makedirs(root)
    monkeypatch.setattr(bootstrap, "PROJECT_ROOT", root)
    monkeypatch.setattr(bootstrap, "GpuDetector", _t9_fake_detector(_T9_CPU_REPORT))

    bootstrap.main(["--channel", "official"])

    report = _t9_install_report(root)
    assert not any(_T9_MIRROR_INDEX in cmd for cmd in report["pending_commands"])

    with pytest.raises(SystemExit):
        bootstrap.main(["--channel", "bogus"])


# ---- FirstRunDriver：新增步骤与同源配置键 ---- #

def test_t9_first_run_full_flow_writes_shared_config_keys(tmp_path, monkeypatch):
    """注入完整输入序列驱动全流程：新步骤写入与前端同源的配置键，来源由线路派生。"""
    root = str(tmp_path)
    bootstrap.ensure_dirs(root)
    _t9_stub_hardware(monkeypatch)
    # 输入：provider 留空→默认；api_key；线路选 mirror→派生 modelscope；本地小 LLM 不下载
    driver, outputs = _t9_driver(root, ["", "sk-t9-secret", "mirror", "n"])

    result = driver.run()

    assert result["download_channel"] == "mirror"
    assert result["model_repo"] == "modelscope"
    assert result["local_llm_source"] == "modelscope"
    assert result["setup_completed"] is True
    assert driver.cm.get("download", "channel") == "mirror"
    assert driver.cm.get("local_llm", "source") == "modelscope"
    assert driver.cm.get("setup", "completed") is True
    assert driver.cm.get("setup", "completed_at")

    joined = "\n".join(outputs)
    assert "硬件体检结果" in joined
    assert "推荐档位与体积" in joined
    assert "下载线路：国内（魔塔）" in joined
    # 线路镜像 → 模型来源 modelscope（映射关系）
    assert driver.cm.get("local_llm", "source") == model_repo_for_channel("mirror") == "modelscope"

    # 落盘：前端 /api/setup/status 读同一 config.json 的同一批键
    on_disk = json.loads(open(os.path.join(root, "config.json"), encoding="utf-8").read())
    assert on_disk["download"]["channel"] == "mirror"
    assert on_disk["local_llm"]["source"] == "modelscope"
    assert on_disk["setup"]["completed"] is True


def test_t9_first_run_official_derives_huggingface(tmp_path, monkeypatch):
    """选海外（official）→ local_llm.source 派生为 huggingface。"""
    root = str(tmp_path)
    bootstrap.ensure_dirs(root)
    _t9_stub_hardware(monkeypatch)
    # 输入序列：provider 留空；api_key 留空；运行偏好留空→默认；线路选 official
    driver, _outputs = _t9_driver(root, ["", "", "", "official"])

    result = driver.run()

    assert result["download_channel"] == "official"
    assert result["model_repo"] == "huggingface"
    assert driver.cm.get("local_llm", "source") == model_repo_for_channel("official") == "huggingface"
    assert driver.cm.get("setup", "completed") is True


def test_t9_first_run_domestic_default_derives_modelscope(tmp_path, monkeypatch):
    """国内（mirror）/ 留空 → local_llm.source 派生为 modelscope。"""
    root = str(tmp_path)
    bootstrap.ensure_dirs(root)
    _t9_stub_hardware(monkeypatch)
    # 线路留空 → 默认国内
    driver, _outputs = _t9_driver(root, ["", "", ""])

    result = driver.run()

    assert result["download_channel"] == "mirror"
    assert driver.cm.get("local_llm", "source") == model_repo_for_channel("mirror") == "modelscope"
    assert result["setup_completed"] is True


def test_t9_first_run_short_input_sequence_uses_defaults(tmp_path, monkeypatch):
    """输入序列很短（模拟既有测试）：新步骤自动取默认值且流程不抛。"""
    root = str(tmp_path)
    bootstrap.ensure_dirs(root)
    _t9_stub_hardware(monkeypatch)
    driver, outputs = _t9_driver(root, ["", "sk-t9-short"])

    result = driver.run()

    assert result["api_key"] == "sk-t9-short"
    assert result["download_channel"] == "mirror"
    assert result["model_repo"] == "modelscope"
    assert driver.cm.get("download", "channel") == "mirror"
    assert driver.cm.get("local_llm", "source") == "modelscope"
    assert driver.cm.get("setup", "completed") is True
    joined = "\n".join(outputs)
    assert "本地小 LLM" in joined and "Gemma 4 E2B" in joined and "data/local_llm/" in joined


def test_t9_first_run_hardware_probe_failure_degrades(tmp_path, monkeypatch):
    """硬件探测抛异常：降级为「跳过推荐」并继续完成向导。"""
    root = str(tmp_path)
    bootstrap.ensure_dirs(root)
    from installer import first_run as first_run_mod

    def _boom(root=None, runner=None):
        """模拟探测失败。"""
        raise RuntimeError("模拟探测失败")

    monkeypatch.setattr(first_run_mod, "detect_profile", _boom)
    driver, outputs = _t9_driver(root, ["", "", "official", "n"])

    result = driver.run()

    joined = "\n".join(outputs)
    assert "硬件体检不可用" in joined
    assert "已跳过推荐" in joined
    assert result["download_channel"] == "official"
    assert result["model_repo"] == "huggingface"
    assert result["setup_completed"] is True


def test_t9_first_run_downloader_injected_and_called(tmp_path, monkeypatch):
    """注入替身下载器并选择下载：download 被调用（repo / 文件名 / 体积来自档位表），不触网。"""
    root = str(tmp_path)
    bootstrap.ensure_dirs(root)
    _t9_stub_hardware(monkeypatch)
    fake = _T9FakeDownloader()
    # 输入序列：provider 留空；api_key 留空；运行偏好留空→默认；线路 mirror；下载 y
    driver, outputs = _t9_driver(root, ["", "", "", "mirror", "y"], downloader=fake)

    result = driver.run()

    assert len(fake.calls) == 2  # 多模态档位双文件：主模型 + mmproj 视觉组件
    assert fake.calls[0]["source"] == "modelscope"
    assert fake.calls[0]["repo"].count("/") == 1
    assert fake.calls[0]["filename"].endswith(".gguf")
    assert fake.calls[0]["verify_size_gb"] == 2.894  # 推荐档位 E2B-Q4 的实测体积（2026-10-04 联网核实）
    # 视觉组件与主模型同仓库同目录（llama-server --mmproj 挂载来源）
    assert fake.calls[1]["repo"] == fake.calls[0]["repo"]
    assert fake.calls[1]["filename"] == "mmproj-BF16.gguf"
    joined = "\n".join(outputs)
    assert "下载进度：50%" in joined
    assert "下载完成" in joined
    assert "视觉组件下载完成" in joined
    assert result["local_llm_source"] == "modelscope"


def test_t9_first_run_downloader_declined_not_called(tmp_path, monkeypatch):
    """注入下载器但选择不下载：download 不被调用，流程正常完成。"""
    root = str(tmp_path)
    bootstrap.ensure_dirs(root)
    _t9_stub_hardware(monkeypatch)
    fake = _T9FakeDownloader()
    driver, outputs = _t9_driver(root, ["", "", "mirror", "n"], downloader=fake)

    driver.run()

    assert fake.calls == []
    assert "已跳过下载" in "\n".join(outputs)


def test_t9_first_run_downloader_failure_does_not_break_flow(tmp_path, monkeypatch):
    """下载失败只告警不中断：向导仍完成并置 setup.completed=True。"""
    root = str(tmp_path)
    bootstrap.ensure_dirs(root)
    _t9_stub_hardware(monkeypatch)
    fake = _T9FakeDownloader(fail=True)
    # 输入序列：provider 留空；api_key 留空；运行偏好留空→默认；线路 mirror；下载 y
    driver, outputs = _t9_driver(root, ["", "", "", "mirror", "y"], downloader=fake)

    result = driver.run()

    assert len(fake.calls) == 1
    assert "下载失败" in "\n".join(outputs)
    assert result["setup_completed"] is True


def test_t9_cli_wizard_and_frontend_share_same_config_keys(tmp_path, monkeypatch):
    """CLI 向导选「国内线路」后，前端 GET /api/setup/status 读到同一配置键与派生来源。"""
    import threading
    import urllib.request
    from http.server import HTTPServer

    from lite.server.api_server import create_app

    root = str(tmp_path / "shared")
    os.makedirs(root)
    bootstrap.ensure_dirs(root)
    _t9_stub_hardware(monkeypatch)
    driver, _outputs = _t9_driver(root, ["", "", "mirror"])
    driver.run()

    _store, _pipeline, handler = create_app(
        data_dir=os.path.join(root, "data"),
        config_path=os.path.join(root, "config.json"),
    )
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/api/setup/status"
        with urllib.request.urlopen(url, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    assert payload["download"]["channel"] == "mirror"
    assert payload["local_llm_source"] == "modelscope"
    # 前端读到的来源与通道派生值一致（CLI 侧由同一映射函数派生）
    assert payload["local_llm_source"] == model_repo_for_channel(payload["download"]["channel"])
    assert payload["completed"] is True
    assert payload["wizard_required"] is False


def test_t9_model_repo_step_removed_and_no_legacy_endpoint_names():
    """步骤合并：``step_model_repo`` 已删除，且安装器源码不再残留旧端点字样。"""
    from installer import first_run as first_run_mod

    assert not hasattr(FirstRunDriver, "step_model_repo")
    assert not hasattr(first_run_mod, "_REPO_OPTIONS")

    source = open(first_run_mod.__file__, encoding="utf-8").read()
    assert "hf-mirror" not in source
    assert "HF_MIRROR" not in source
    assert "hf_endpoint" not in source
    assert "魔搭" not in source


# ------------------------------------------------------------------ #
# Task 5：模式询问（省电优先 / 性能优先）+ 口语化加速结论              #
# ------------------------------------------------------------------ #

#: 无核显 N 卡画像（性能优先 → tts.accel=cuda）。
_T5_NVIDIA_PROFILE = {
    "cpu_cores": 8,
    "ram_gb": 16.0,
    "gpu_vendor": "nvidia",
    "vram_gb": 8.0,
    "cuda_version": "12.4",
    "disk_free_gb": 200.0,
    "probe_notes": [],
    "gpus": [{"vendor": "nvidia", "name": "RTX 4060", "type": "dgpu", "vram_hint": ""}],
    "has_igpu": False,
    "dgpu_vendor": "nvidia",
}


def _t5_stub_hardware(monkeypatch, profile):
    """把 first_run 的探测 / 推荐替换为确定性替身（不触碰真实硬件，不改决策函数）。"""
    from installer import first_run as first_run_mod

    monkeypatch.setattr(
        first_run_mod, "detect_profile", lambda root=None, runner=None: dict(profile)
    )
    monkeypatch.setattr(
        first_run_mod,
        "recommend_for",
        lambda profile, disk_free_gb=None: {
            "use_local": True,
            "device": "cpu",
            "tier": "E2B-Q4",
            "config_patch": {"local_llm": {"enabled": True, "device": "cpu"}},
            "model": None,
            "reasons": ["测试替身：内存满足本地推理阈值"],
            "probe_notes": [],
        },
    )


def test_t5_first_run_default_mode_from_profile_and_zero_jargon(tmp_path, monkeypatch):
    """无核显 N 卡画像：默认性能优先 → accel.mode=performance、tts.accel=cuda；展示零术语。"""
    root = str(tmp_path)
    bootstrap.ensure_dirs(root)
    _t5_stub_hardware(monkeypatch, _T5_NVIDIA_PROFILE)
    # 输入：provider 留空；api_key 留空；运行偏好留空→按画像默认；线路留空；不下载
    outputs = []
    prompts = []
    seq = iter(["", "", "", "", "n"])
    driver = FirstRunDriver(
        root,
        input_fn=lambda p: (prompts.append(p), next(seq))[1],
        output_fn=outputs.append,
    )

    result = driver.run()

    assert result["accel_mode"] == "performance"
    assert driver.cm.get("accel", "mode") == "performance"
    assert driver.cm.get("tts", "accel") == "cuda"
    assert driver.cm.get("asr", "device") == "cpu"  # 20261006 裁决：ASR 恒 CPU
    assert driver.cm.get("local_llm", "device") == "gpu"
    assert driver.cm.get("embedding", "device") == "gpu"

    joined = "\n".join(outputs)
    assert "已选择：性能优先" in joined  # 选择结论
    assert "加速方案" in joined  # 口语化结论展示出现
    # 询问文案出现（提示经 input_fn 下发）
    assert any("省电优先 / 性能优先" in p for p in prompts), "运行偏好询问应出现"
    # 零术语：结论 / 询问文案不得出现技术术语
    for jargon in ("ORT", "DirectML", "CUDA", "ROCm", "DML", "onnxruntime", "ExecutionProvider"):
        assert jargon not in joined, f"向导文案不得出现术语 {jargon}"
        assert all(jargon not in p for p in prompts), f"询问文案不得出现术语 {jargon}"


def test_t5_first_run_explicit_eco_with_igpu_writes_plan(tmp_path, monkeypatch):
    """有核显画像 + 显式选省电优先 → accel.mode=eco、tts.accel=dml、accel_device=igpu。"""
    root = str(tmp_path)
    bootstrap.ensure_dirs(root)
    profile = dict(_T5_NVIDIA_PROFILE)
    profile.update(
        {
            "gpus": [
                {"vendor": "nvidia", "name": "RTX 4060", "type": "dgpu", "vram_hint": ""},
                {"vendor": "intel", "name": "Intel UHD Graphics", "type": "igpu", "vram_hint": ""},
            ],
            "has_igpu": True,
        }
    )
    _t5_stub_hardware(monkeypatch, profile)
    driver, outputs = _t9_driver(root, ["", "", "省电优先", "", "n"])

    result = driver.run()

    assert result["accel_mode"] == "eco"
    assert driver.cm.get("tts", "accel") == "dml"
    assert driver.cm.get("tts", "accel_device") == "igpu"
    assert driver.cm.get("asr", "device") == "cpu"
    assert "省电优先" in "\n".join(outputs)


# ------------------------------------------------------------------ #
# 悬浮桌宠模型：文件型内置组件随包分发（20260924_模块0_接入VRM悬浮桌宠）  #
# ------------------------------------------------------------------ #

def test_zip_portable_keeps_file_type_builtin_target(tmp_path):
    """文件型 install_target 保留进 zip：``data/pet/cx-open.vrm`` 命中白名单（精确匹配）。"""
    import zipfile

    from installer import build as build_mod

    root = tmp_path / "portable-file-target"
    root.mkdir(parents=True)
    (root / "CX-A.exe").write_bytes(b"fake-shell")
    (root / "data" / "pet").mkdir(parents=True)
    (root / "data" / "pet" / "cx-open.vrm").write_bytes(b"CXA-FAKE-VRM")
    # 对照：白名单外的运行期产物仍应被排除
    (root / "data" / "memories.db").write_bytes(b"sqlite-payload")

    # 白名单真相源为 manifest：pet_model 组件已登记该文件型 install_target
    rel_target = os.path.normpath(os.path.join("data", "pet", "cx-open.vrm"))
    whitelist = build_mod._bundled_data_whitelist()
    assert rel_target in whitelist
    assert build_mod._under_whitelist(rel_target, whitelist) is True

    zip_path = build_mod.zip_portable(str(root), str(tmp_path / "rel"))
    with zipfile.ZipFile(zip_path) as zf:
        names = [n.replace("\\", "/") for n in zf.namelist()]
    assert "CX-A-portable/data/pet/cx-open.vrm" in names
    assert "CX-A-portable/data/memories.db" not in names

# ------------------------------------------------------------------ #
# 安装器分发：语音桥落位 + Inno Setup 编译（步骤6）                     #
# ------------------------------------------------------------------ #

def test_assemble_places_voice_bridge_dispatch_script(tmp_path):
    """assemble 必须把 lite/audio/voice_bridge.py 复制到 runtime/voice_bridge/bridge.py。

    客户端（voice_bridge_client._resolve_script）**仅认该分发落点**——缺它则打包态
    语音全部降级为 Mock；单一真相源为源码文件，此处断言字节一致。
    """
    import zipfile

    from installer import build as build_mod

    electron_dist = tmp_path / "win-unpacked"
    (electron_dist / "resources").mkdir(parents=True)
    (electron_dist / "CX-A.exe").write_bytes(b"fake-exe")
    backend_dist = tmp_path / "backend-dist"
    backend_dist.mkdir(parents=True)
    (backend_dist / "backend.exe").write_bytes(b"fake-backend")

    portable_root = str(tmp_path / "portable")
    build_mod.assemble(str(electron_dist), str(backend_dist), portable_root)

    bridge_dst = os.path.join(portable_root, "runtime", "voice_bridge", "bridge.py")
    bridge_src = os.path.join(build_mod.PROJECT_ROOT, "lite", "audio", "voice_bridge.py")
    assert os.path.isfile(bridge_dst)
    with open(bridge_dst, "rb") as dst_fh, open(bridge_src, "rb") as src_fh:
        assert dst_fh.read() == src_fh.read()

    # 随包分发（zip 收录 runtime/ 树）
    zip_path = build_mod.zip_portable(portable_root, str(tmp_path / "rel"))
    with zipfile.ZipFile(zip_path) as zf:
        names = [n.replace("\\", "/") for n in zf.namelist()]
    assert "CX-A-portable/runtime/voice_bridge/bridge.py" in names


def test_find_iscc_returns_none_without_install(tmp_path, monkeypatch):
    """三处候选皆无 ISCC → None（build_installer 据此告警跳过）。"""
    from installer import build as build_mod

    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "pf86"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))

    assert build_mod.find_iscc() is None


def test_find_iscc_prefers_user_install_location(tmp_path, monkeypatch):
    """用户在 LOCALAPPDATA 下安装（winget 默认）也能被找到。"""
    from installer import build as build_mod

    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "pf86"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    lad = tmp_path / "lad"
    monkeypatch.setenv("LOCALAPPDATA", str(lad))
    iscc = lad / "Programs" / "Inno Setup 6" / "ISCC.exe"
    iscc.parent.mkdir(parents=True)
    iscc.write_bytes(b"fake-iscc")

    assert build_mod.find_iscc() == str(iscc)


def test_build_installer_skips_without_iscc(tmp_path, monkeypatch, capsys):
    """ISCC 缺失：告警跳过并返回 None，不阻断主链路。"""
    from installer import build as build_mod

    monkeypatch.setattr(build_mod, "find_iscc", lambda: None)

    assert build_mod.build_installer(str(tmp_path / "portable"), str(tmp_path / "rel")) is None
    assert "未检测到 Inno Setup 编译器" in capsys.readouterr().out


def test_build_installer_skips_when_runtime_sources_missing(tmp_path, monkeypatch, capsys):
    """随包运行时源缺失：明确告警跳过（不产生半成品安装器）。"""
    from installer import build as build_mod

    monkeypatch.setattr(build_mod, "find_iscc", lambda: str(tmp_path / "ISCC.exe"))
    monkeypatch.setattr(build_mod, "BUNDLED_DIR", str(tmp_path / "bundled-empty"))

    assert build_mod.build_installer(str(tmp_path / "portable"), str(tmp_path / "rel")) is None
    out = capsys.readouterr().out
    assert "随包运行时源缺失" in out
    assert "Miniconda 安装器" in out


def test_build_installer_compiles_with_defines(tmp_path, monkeypatch):
    """源齐备：以 /D 传参调用 ISCC，产物存在则返回其路径。"""
    from installer import build as build_mod

    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "miniconda_installer.exe").write_bytes(b"MZ")
    (bundled / "melotts_src").mkdir()
    (bundled / "nltk_data").mkdir()
    (bundled / "sensevoice").mkdir()
    # 20260926：随包运行时源清单新增"嵌入模型"与"llama.cpp 运行时"（缺失即跳过编译）
    (bundled / "embedding_model").mkdir()
    (bundled / "llama_cpp").mkdir()
    # 20261002 批 A：补检表新增 llama.cpp Vulkan 运行时（缺失即跳过编译）
    (bundled / "llama_cpp_vulkan").mkdir()
    iscc = tmp_path / "ISCC.exe"
    iscc.write_bytes(b"fake")
    monkeypatch.setattr(build_mod, "find_iscc", lambda: str(iscc))
    monkeypatch.setattr(build_mod, "BUNDLED_DIR", str(bundled))

    captured = {}

    class _Proc:
        returncode = 0
        stdout = "compiled"
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        # 编译产物由 ISCC 产生：此处伪造为脚本路径推导出的目标名
        rel = tmp_path / "rel"
        rel.mkdir(exist_ok=True)
        (rel / "CX-A-Setup-9.9.9.exe").write_bytes(b"MZ-installer")
        return _Proc()

    monkeypatch.setattr(build_mod.subprocess, "run", fake_run)

    result = build_mod.build_installer(
        str(tmp_path / "portable"), str(tmp_path / "rel"), version="9.9.9"
    )

    assert result == str(tmp_path / "rel" / "CX-A-Setup-9.9.9.exe")
    cmd = captured["cmd"]
    assert cmd[0] == str(iscc)
    assert f"/DPayloadDir={tmp_path / 'portable'}" in cmd
    assert f"/DBundledDir={bundled}" in cmd
    assert f"/DOutputDir={tmp_path / 'rel'}" in cmd
    assert "/DAppVersion=9.9.9" in cmd
    assert cmd[-1].endswith("installer.iss")


# ------------------------------------------------------------------ #
# Task 3：ORT 包分叉（四类机器）/ 方案落盘（保守读改）/ 预热固定 CPU     #
# ------------------------------------------------------------------ #

#: (标签, 画像, 期望 ORT 变体)——四类机器分叉断言表。
_ORT_FORK_CASES = [
    ("有核显", {"has_igpu": True, "recommend": "cuda", "dgpu_vendor": "nvidia"},
     "onnxruntime-directml"),
    ("无核显 N 卡", {"has_igpu": False, "recommend": "cuda", "gpu_vendor": "nvidia"},
     "onnxruntime-gpu"),
    ("无核显 AMD", {"has_igpu": False, "recommend": "rocm", "gpu_vendor": "amd"},
     "onnxruntime-directml"),
    ("无 GPU", {"has_igpu": False, "recommend": "cpu", "gpu_vendor": "cpu"},
     "onnxruntime"),
]


@pytest.mark.parametrize("label,profile,expected", _ORT_FORK_CASES)
def test_task3_ort_fork_four_classes_mutually_exclusive(label, profile, expected):
    """四类机器 ORT 分叉正确，且命令清单互斥（只含一个 onnxruntime* 变体）。"""
    assert bootstrap.resolve_ort_package(profile) == expected
    assert bootstrap.build_ort_package_command(profile) == f"pip install {expected}"

    commands = bootstrap.build_gpu_dependency_commands(
        profile["recommend"], "12.4", profile=profile
    )
    variants = [c for c in commands if c.startswith("pip install onnxruntime")]
    assert variants == [f"pip install {expected}"]

    # cuda 分支不捆绑 ORT：有核显的 cuda 机器得到 DirectML，而非 onnxruntime-gpu
    if profile["recommend"] == "cuda" and profile["has_igpu"]:
        assert not any("onnxruntime-gpu" in c for c in commands)


def _accel_profile_fixture():
    """构造一份「有核显 + N 卡」画像（性能模式 → tts.accel=dml/dgpu）。"""
    return {
        "recommend": "cuda",
        "gpu_vendor": "nvidia",
        "dgpu_vendor": "nvidia",
        "has_igpu": True,
        "vram_gb": 8.0,
    }


def test_apply_accel_plan_writes_missing_keys(tmp_path):
    """方案落盘场景1（缺键写入）：缺键写入、既有键保留、报告如实。"""
    from installer import conda_runtime

    root = str(tmp_path / "portable")
    os.makedirs(root)
    config_path = os.path.join(root, "config.json")
    with open(config_path, "w", encoding="utf-8") as fh:
        json.dump({"cloud": {"provider": "deepseek"}}, fh)

    result = conda_runtime.apply_accel_plan(root, profile=_accel_profile_fixture())

    assert result["applied_ok"] is True
    # 20261002 批 A：追加 local_llm.backend / embedding.backend 两键（既有断言变更留痕）
    assert set(result["applied"]) == {
        "accel.mode", "tts.accel", "tts.accel_device",
        "asr.device", "local_llm.device", "local_llm.backend",
        "embedding.device", "embedding.backend",
    }
    assert result["mode"] == "performance"
    on_disk = json.loads(open(config_path, encoding="utf-8").read())
    assert on_disk["accel"]["mode"] == "performance"
    assert on_disk["tts"]["accel"] == "dml"
    # 20261006 裁决：TTS 有核显跨模式恒核显（原 dgpu → igpu）
    assert on_disk["tts"]["accel_device"] == "igpu"
    assert on_disk["asr"]["device"] == "cpu"  # 20261006 裁决：ASR 恒 CPU
    # N 卡显存满足阈值 → backend=cuda（批 A 新键落盘值）
    assert on_disk["local_llm"]["device"] == "gpu"
    assert on_disk["local_llm"]["backend"] == "cuda"
    assert on_disk["embedding"]["device"] == "gpu"
    assert on_disk["embedding"]["backend"] == "cuda"
    assert on_disk["cloud"]["provider"] == "deepseek"  # 既有键不被清除


def test_apply_accel_plan_respects_existing_keys(tmp_path):
    """方案落盘场景2（键存在不覆盖，含显式值）：全部保留，不改动文件。"""
    from installer import conda_runtime

    root = str(tmp_path / "portable")
    os.makedirs(root)
    config_path = os.path.join(root, "config.json")
    explicit = {
        "accel": {"mode": "eco"},
        "tts": {"accel": "off", "accel_device": "igpu"},
        "asr": {"device": "cpu"},
        "local_llm": {"device": "cpu"},
        "embedding": {"device": "cpu"},
    }
    with open(config_path, "w", encoding="utf-8") as fh:
        json.dump(explicit, fh)

    result = conda_runtime.apply_accel_plan(root, profile=_accel_profile_fixture())

    # 20261002 批 A：explicit 未含 backend 键 → 两键新写入（既有断言变更留痕：
    # 原「applied == [] 全保留」收窄为「既有六键保留 + backend 两键补写」）
    assert set(result["applied"]) == {"local_llm.backend", "embedding.backend"}
    assert set(result["existing"]) == {
        "accel.mode", "tts.accel", "tts.accel_device",
        "asr.device", "local_llm.device", "embedding.device",
    }
    on_disk = json.loads(open(config_path, encoding="utf-8").read())
    # 显式值一律保留（不被画像决策覆盖）
    assert on_disk["accel"]["mode"] == "eco"
    assert on_disk["tts"]["accel"] == "off"
    assert on_disk["tts"]["accel_device"] == "igpu"
    assert on_disk["asr"]["device"] == "cpu"
    # 显式 device=cpu 保留；backend 缺键由方案补写（N 卡 → cuda）
    assert on_disk["local_llm"]["device"] == "cpu"
    assert on_disk["local_llm"]["backend"] == "cuda"
    assert on_disk["embedding"]["device"] == "cpu"
    assert on_disk["embedding"]["backend"] == "cuda"


def test_apply_accel_plan_skips_when_config_missing(tmp_path, capsys):
    """方案落盘场景3（文件缺失跳过并告警）：不新建文件、不抛错。"""
    from installer import conda_runtime

    root = str(tmp_path / "portable")
    os.makedirs(root)

    result = conda_runtime.apply_accel_plan(root, profile=_accel_profile_fixture())

    assert result["applied_ok"] is False
    assert result["reason"] == "config-missing"
    assert "config.json 不存在" in capsys.readouterr().out
    assert not os.path.exists(os.path.join(root, "config.json"))


def test_apply_accel_plan_corrupt_config_only_warns(tmp_path, capsys):
    """方案落盘场景4（异常仅告警不阻断）：坏 JSON 只告警，不抛错。"""
    from installer import conda_runtime

    root = str(tmp_path / "portable")
    os.makedirs(root)
    config_path = os.path.join(root, "config.json")
    with open(config_path, "w", encoding="utf-8") as fh:
        fh.write("{不是合法 JSON")

    result = conda_runtime.apply_accel_plan(root, profile=_accel_profile_fixture())

    assert result["applied_ok"] is False
    assert result["reason"] == "config-unreadable"
    assert "读取失败" in capsys.readouterr().out
    # 坏文件未被改写（保守：不覆盖用户内容）
    with open(config_path, encoding="utf-8") as fh:
        assert fh.read() == "{不是合法 JSON"


def test_apply_accel_plan_without_profile_skips(tmp_path, capsys):
    """方案落盘：无画像时跳过（不猜测），只告警不抛错。"""
    from installer import conda_runtime

    root = str(tmp_path / "portable")
    os.makedirs(root)

    result = conda_runtime.apply_accel_plan(root, profile=None)

    assert result["applied_ok"] is False
    assert result["reason"] == "profile-missing"
    assert "缺少硬件画像" in capsys.readouterr().out


def test_provision_runtime_warms_tts_on_cpu(tmp_path, monkeypatch):
    """Task 3：安装期 TTS 预热设备固定 CPU（隔离 melo torch-GPU 风险路径）。

    即便检测结论为 cuda，预热也必须以 CPU 执行。
    """
    from installer import conda_runtime

    root = str(tmp_path / "portable")
    captured = {}

    monkeypatch.setattr(conda_runtime, "_detect_gpu", lambda runner=None: ("cuda", "13.4", "nvidia"))
    monkeypatch.setattr(conda_runtime, "_detect_gpu_inventory", lambda runner=None: (False, "nvidia"))
    monkeypatch.setattr(conda_runtime, "install_conda", lambda r, runner=None: {"installed": True})
    monkeypatch.setattr(conda_runtime, "create_voice_env", lambda r, runner=None: {"created": True})
    monkeypatch.setattr(
        conda_runtime, "install_voice_dependencies",
        lambda r, recommend, cuda, channel=None, runner=None: {"installed": True, "errors": []},
    )
    monkeypatch.setattr(conda_runtime, "provision_nltk_data", lambda r: {"provisioned": True})
    monkeypatch.setattr(
        conda_runtime, "apply_accel_plan",
        lambda r, profile=None: {"mode": "performance", "plan": {"accel.mode": "performance"}},
    )
    monkeypatch.setattr(
        conda_runtime, "warmup_voice_models",
        lambda r, device, runner=None: captured.update(device=device) or {"warmed": True},
    )
    monkeypatch.setattr(bootstrap, "resolve_download_channel", lambda root=None: "mirror")

    conda_runtime.provision_runtime(root)

    assert captured["device"] == "cpu"
    # 报告含画像与决策（spec：报告 SHALL 含画像与决策）
    report = json.loads(
        open(os.path.join(root, "data", "install_report.json"), encoding="utf-8").read()
    )
    assert report["accel_mode"] == "performance"
    assert report["accel_plan"] == {"accel.mode": "performance"}
