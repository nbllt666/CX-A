# -*- coding: utf-8 -*-
"""Task A1 配置系统单元测试。

覆盖：
- 首次启动自动生成含默认值的 config.json
- CXA_ 环境变量覆盖（CXA_CLOUD_API_KEY -> cloud.api_key）
- 缺失键 / 缺失段自动补全
- 热更新段判定（vector 需重启、cloud 不重启）
- Fernet 加密写读往返（config.json 落盘为密文，回读还原明文）
- 明文降级标记（cryptography 不可用时置 warning）
"""

import json
import os

import pytest

import lite.config.config_manager as cm
from lite.config.config_manager import (
    ConfigManager,
    DEFAULTS,
    HOT_RELOAD_SECTIONS,
    NEED_RESTART_SECTIONS,
)


def _make_manager(tmp_path, **kwargs):
    """在临时目录构造 ConfigManager，避免污染 c:\\CX-A\\config.json。"""
    kwargs.setdefault("config_path", str(tmp_path / "config.json"))
    kwargs.setdefault("data_dir", str(tmp_path / "data"))
    return ConfigManager(**kwargs)


# ------------------------------------------------------------------ #
# 1. 首次启动生成默认配置                                             #
# ------------------------------------------------------------------ #

def test_first_run_generates_default_config(tmp_path):
    """config.json 不存在时，实例化后应自动生成且内容等于 DEFAULTS。"""
    cfg = _make_manager(tmp_path)
    assert (tmp_path / "config.json").exists()
    raw = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert raw == DEFAULTS


def test_defaults_values():
    """DEFAULTS 与工程文档 §13.2 严格一致。"""
    assert DEFAULTS["cloud"] == {
        "provider": "deepseek",
        "api_key": "",
        "base_url": "",
        "temperature": 0.7,
        "model": "",
    }
    # device 为 GPU 开关键（cpu 默认 / gpu），打通 llama.cpp CPU/GPU 推理切换
    # backend 为 llama.cpp 后端构建键（20261002 批 A："" 默认 / cuda / vulkan，
    # 既有断言变更留痕：local_llm 段精确比较追加 backend 键）
    assert DEFAULTS["local_llm"] == {
        "enabled": False,
        "model_path": "",
        "source": "modelscope",
        "device": "cpu",
        "backend": "",
    }
    # embedding/vector（20260926_模块0_真实嵌入与向量持久化）：新增 model_path
    # 覆盖键（空串＝按约定目录解析）；向量后端默认由 lancedb 改为 sqlite（持久
    # 向量表落 memories.db，cosine 口径与 InMemory 一致；lancedb 为保留旧口径）
    # backend 为 llama.cpp 后端构建键（20261002 批 A 追加，既有断言变更留痕）
    assert DEFAULTS["embedding"] == {
        "model": "qwen3-embedding:0.6b",
        "runtime": "llama.cpp",
        "device": "cpu",
        "model_path": "",
        "backend": "",
    }
    assert DEFAULTS["vector"] == {"backend": "sqlite", "path": "data/lancedb"}
    # 全组件加速双模式 spec：tts 段新增 accel（默认 auto）与 accel_device（默认 ""）
    assert DEFAULTS["tts"] == {
        "engine": "melotts",
        "voice": "cx-open",
        "device": "cpu",
        "accel": "auto",
        "accel_device": "",
    }
    # 加速模式段（静态默认 performance；画像推导在安装/首启期完成）
    assert DEFAULTS["accel"] == {"mode": "performance"}
    assert DEFAULTS["asr"] == {"engine": "sensevoice", "device": "cpu"}
    assert DEFAULTS["vad"] == {"mode": "webrtc"}
    assert DEFAULTS["memory"] == {"max_memories": 30, "dedup": 0.85, "permanent_threshold": 0.95}
    assert DEFAULTS["computer_control"] == {"authorized": False, "confirm_dangerous": True}
    assert DEFAULTS["sync"] == {"enabled": False}
    assert DEFAULTS["remote"] == {"endpoint": "", "enabled": False}
    # 轻量版扩展段（批次E补齐：对照 config_manager.DEFAULTS acp/cxfc/tools）
    assert DEFAULTS["acp"] == {
        "enabled": False,
        "agent_id": "cxa-agent-001",
        "heartbeat_interval": 10,
        "lan_discovery": False,
        "group_enabled": False,
        "cloud_relay": True,
        "cloud_relay_endpoint": "",
    }
    # Task H1：cxfc 段新增 relay 窗口默认值（relay_timeout_s / replay_window_s，秒）
    assert DEFAULTS["cxfc"] == {
        "enabled": False,
        "embedded_only": True,
        "relay_timeout_s": 10.0,
        "replay_window_s": 60.0,
    }
    assert DEFAULTS["tools"] == {
        "computer_control": False,
        "memory_tools": True,
        "system_tools": True,
    }


# ------------------------------------------------------------------ #
# 2. CXA_ 环境变量覆盖                                               #
# ------------------------------------------------------------------ #

def test_env_override(monkeypatch, tmp_path):
    """CXA_CLOUD_API_KEY 应覆盖 cloud.api_key（字符串）。"""
    monkeypatch.setenv("CXA_CLOUD_API_KEY", "sk-test-env")
    cfg = _make_manager(tmp_path)
    assert cfg.get("cloud", "api_key") == "sk-test-env"


def test_env_override_bool_and_section_with_underscore(monkeypatch, tmp_path):
    """CXA_COMPUTER_CONTROL_AUTHORIZED 覆盖布尔值，且段名含下划线也能匹配。"""
    monkeypatch.setenv("CXA_COMPUTER_CONTROL_AUTHORIZED", "true")
    cfg = _make_manager(tmp_path)
    assert cfg.get("computer_control", "authorized") is True


def test_env_override_cleared(monkeypatch, tmp_path):
    """无环境变量时 api_key 回到默认空串。"""
    monkeypatch.delenv("CXA_CLOUD_API_KEY", raising=False)
    cfg = _make_manager(tmp_path)
    assert cfg.get("cloud", "api_key") == ""


# ------------------------------------------------------------------ #
# 3. 缺失字段自动补全                                                #
# ------------------------------------------------------------------ #

def test_missing_key_autofill(tmp_path):
    """已有 config.json 缺失个别键时，从默认值补齐该键。"""
    (tmp_path / "config.json").write_text(
        json.dumps({"cloud": {"provider": "tongyi"}}), encoding="utf-8"
    )
    cfg = _make_manager(tmp_path)
    # 已有值保留
    assert cfg.get("cloud", "provider") == "tongyi"
    # 缺失键补齐
    assert cfg.get("cloud", "api_key") == ""
    assert cfg.get("cloud", "base_url") == ""
    # L5 新增契约键同样自动补齐
    assert cfg.get("cloud", "temperature") == 0.7
    assert cfg.get("cloud", "model") == ""


def test_missing_section_autofill(tmp_path):
    """已有 config.json 缺失整段时，整段从默认值补齐。"""
    (tmp_path / "config.json").write_text(
        json.dumps({"cloud": {"provider": "deepseek"}}), encoding="utf-8"
    )
    cfg = _make_manager(tmp_path)
    # 缺失的 memory 整段补齐
    assert cfg.get("memory", "max_memories") == 30
    assert cfg.get("memory", "dedup") == 0.85
    assert cfg.get("memory", "permanent_threshold") == 0.95


# ------------------------------------------------------------------ #
# 4. 热更新段判定                                                    #
# ------------------------------------------------------------------ #

def test_reloadable_judgement(tmp_path):
    """reloadable 真实语义（批次E：直接调用被测方法 ConfigManager.reloadable）：
    cloud/tts 等热更新段 True，vector/embedding 需重启 False，未知段 None。"""
    cfg = _make_manager(tmp_path)
    # 热更新段抽样（含轻量版扩展段 acp/cxfc/tools）
    assert cfg.reloadable("cloud") is True
    assert cfg.reloadable("tts") is True
    assert cfg.reloadable("memory") is True
    assert cfg.reloadable("acp") is True
    assert cfg.reloadable("cxfc") is True
    assert cfg.reloadable("tools") is True
    # 需重启段
    assert cfg.reloadable("vector") is False
    assert cfg.reloadable("embedding") is False
    # 未知段
    assert cfg.reloadable("no_such_section") is None


def test_reloadable_sections_complete():
    """hot_reload 段与 need_restart 段覆盖全部配置段。"""
    all_sections = set(DEFAULTS.keys())
    assert set(HOT_RELOAD_SECTIONS) | set(NEED_RESTART_SECTIONS) == all_sections
    assert set(HOT_RELOAD_SECTIONS) & set(NEED_RESTART_SECTIONS) == set()


# ------------------------------------------------------------------ #
# 5. Fernet 加密写读往返                                             #
# ------------------------------------------------------------------ #

def test_fernet_roundtrip(tmp_path):
    """save 后 config.json 落盘为密文，重新加载后还原明文。"""
    cfg = _make_manager(tmp_path)
    cfg.set("cloud", "api_key", "super-secret")
    cfg.save()

    # 落盘为密文（带前缀），不含明文
    raw = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert raw["cloud"]["api_key"].startswith(cm._ENC_PREFIX)
    assert "super-secret" not in raw["cloud"]["api_key"]

    # 重新加载（新建实例）还原明文
    cfg2 = _make_manager(tmp_path)
    assert cfg2.get("cloud", "api_key") == "super-secret"


def test_key_file_created(tmp_path):
    """实例化并保存时应在 data 目录下生成密钥文件 data/.cxa_key。"""
    cfg = _make_manager(tmp_path)
    cfg.set("cloud", "api_key", "k")
    cfg.save()
    assert (tmp_path / "data" / ".cxa_key").exists()


# ------------------------------------------------------------------ #
# 6. 明文降级标记                                                    #
# ------------------------------------------------------------------ #

def test_plaintext_degredation_marker(monkeypatch, tmp_path):
    """cryptography 不可用时 API Key 降级明文，并置 warning 标记。"""
    monkeypatch.setattr(cm, "_HAS_FERNET", False)
    cfg = _make_manager(tmp_path)
    cfg.set("cloud", "api_key", "plaintext-key")
    cfg.save()

    # 生成配置落盘为明文
    raw = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert raw["cloud"]["api_key"] == "plaintext-key"
    # warning 标记存在
    assert any("明文" in w for w in cfg.warnings)

    # 重新加载仍为明文且保留 warning 提示（此时无已加密值可解密）
    cfg2 = _make_manager(tmp_path)
    assert cfg2.get("cloud", "api_key") == "plaintext-key"
    assert any("明文" in w for w in cfg2.warnings) or not cfg2.warnings


# ------------------------------------------------------------------ #
# 7. get / set / save 便捷接口                                       #
# ------------------------------------------------------------------ #

def test_get_set_save(tmp_path):
    """get / set / save 组合读写。"""
    cfg = _make_manager(tmp_path)
    cfg.set("tts", "voice", "cx-custom")
    assert cfg.get("tts", "voice") == "cx-custom"
    cfg.save()

    cfg2 = _make_manager(tmp_path)
    assert cfg2.get("tts", "voice") == "cx-custom"


def test_get_missing_returns_default(tmp_path):
    """get 不存在的键返回传入默认值。"""
    cfg = _make_manager(tmp_path)
    assert cfg.get("cloud", "no_such_key", "fb") == "fb"


# ------------------------------------------------------------------ #
# 8. N3：save 原子写 + 损坏 config.json 兜底                          #
# ------------------------------------------------------------------ #

def test_save_atomic_no_tmp_left(tmp_path):
    """N3：save 走 tmp + os.replace 原子写，成功后不留 .tmp 残留。"""
    cfg = _make_manager(tmp_path)
    cfg.set("tts", "voice", "atomic-check")
    cfg.save()

    assert (tmp_path / "config.json").exists()
    assert not (tmp_path / "config.json.tmp").exists()
    # 落盘内容可正常解析且含写入值
    raw = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert raw["tts"]["voice"] == "atomic-check"


def test_save_cleans_tmp_on_failure(tmp_path, monkeypatch):
    """N3：写入失败（os.replace 抛错）时清理 tmp 并上抛原异常。"""
    cfg = _make_manager(tmp_path)
    cfg.set("tts", "voice", "will-fail")

    import lite.config.config_manager as cm_mod

    real_replace = os.replace

    def _boom_replace(src, dst):
        if str(dst).endswith(".tmp"):
            return real_replace(src, dst)
        raise OSError("replace failed")

    monkeypatch.setattr(cm_mod.os, "replace", _boom_replace)
    with pytest.raises(OSError):
        cfg.save()

    # tmp 已清理，原 config.json 未被破坏
    assert not (tmp_path / "config.json.tmp").exists()
    raw = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert "tts" in raw


def test_corrupt_config_renamed_and_defaults_applied(tmp_path):
    """N3：坏 config.json 改名为 .corrupt-<时间戳> 留证，按 DEFAULTS 起步可正常使用。"""
    config_path = tmp_path / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text('{"cloud": {"provider": "deep', encoding="utf-8")  # 截断 JSON

    cfg = _make_manager(tmp_path)

    # 坏文件已改名留证（含 corrupt- 前缀），不再占用 config.json
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith("config.json.corrupt-")]
    assert len(leftovers) == 1
    assert not config_path.exists() or config_path.read_text(encoding="utf-8") != '{"cloud": {"provider": "deep'

    # 按 DEFAULTS 起步：缺省段键已补齐，读写与 save 可正常进行
    assert cfg.get("cloud", "provider") == DEFAULTS["cloud"]["provider"]
    assert cfg.get("memory", "dedup") == DEFAULTS["memory"]["dedup"]
    cfg.set("tts", "voice", "after-corrupt")
    cfg.save()
    cfg2 = _make_manager(tmp_path)
    assert cfg2.get("tts", "voice") == "after-corrupt"


# ------------------------------------------------------------------ #
# 9. 首启向导：download / setup 段与升级兼容                          #
# ------------------------------------------------------------------ #

def test_download_and_setup_defaults(tmp_path):
    """全新生成 config.json：download.channel 默认 mirror，setup.completed 为 False。"""
    cfg = _make_manager(tmp_path)
    assert cfg.get("download", "channel") == "mirror"
    assert cfg.get("setup", "completed") is False
    assert cfg.get("setup", "completed_at") == ""
    # 段内容与 DEFAULTS 一致（配置契约承载于 DEFAULTS）
    assert DEFAULTS["download"] == {"channel": "mirror"}
    assert DEFAULTS["setup"] == {"completed": False, "completed_at": ""}


def test_setup_upgrade_compat_existing_config_without_setup(tmp_path):
    """升级兼容：既有 config.json 无 setup 段 -> 视为老用户已完成初始化（True）。"""
    (tmp_path / "config.json").write_text(
        json.dumps({"cloud": {"provider": "deepseek"}}), encoding="utf-8"
    )
    cfg = _make_manager(tmp_path)
    assert cfg.get("setup", "completed") is True
    # 既有键保留，download 段仍按默认补齐
    assert cfg.get("cloud", "provider") == "deepseek"
    assert cfg.get("download", "channel") == "mirror"


def test_setup_explicit_false_not_treated_as_legacy(tmp_path):
    """既有 config.json 显式含 setup.completed=false -> 保持 False（不误判为老用户）。"""
    (tmp_path / "config.json").write_text(
        json.dumps({"cloud": {"provider": "deepseek"}, "setup": {"completed": False}}),
        encoding="utf-8",
    )
    cfg = _make_manager(tmp_path)
    assert cfg.get("setup", "completed") is False
    # 缺失键（completed_at）自动补齐
    assert cfg.get("setup", "completed_at") == ""


def test_env_override_download_channel(monkeypatch, tmp_path):
    """CXA_DOWNLOAD_CHANNEL 覆盖 download.channel（单词段名自动可用）。"""
    monkeypatch.setenv("CXA_DOWNLOAD_CHANNEL", "official")
    cfg = _make_manager(tmp_path)
    assert cfg.get("download", "channel") == "official"


def test_env_override_setup_completed(monkeypatch, tmp_path):
    """CXA_SETUP_COMPLETED 覆盖 setup.completed（布尔转换生效）。"""
    monkeypatch.setenv("CXA_SETUP_COMPLETED", "true")
    (tmp_path / "config.json").write_text(
        json.dumps({"setup": {"completed": False}}), encoding="utf-8"
    )
    cfg = _make_manager(tmp_path)
    assert cfg.get("setup", "completed") is True


def test_reloadable_download(tmp_path):
    """download 段纳入热更新；setup 段亦被 reload 列表覆盖（全段覆盖不变量）。"""
    cfg = _make_manager(tmp_path)
    assert cfg.reloadable("download") is True
    assert "download" in HOT_RELOAD_SECTIONS


# ------------------------------------------------------------------ #
# 10. 全组件加速双模式：accel.mode / tts.accel / tts.accel_device     #
# ------------------------------------------------------------------ #

def test_accel_defaults_values(tmp_path):
    """全新配置：accel.mode=performance、tts.accel=auto、tts.accel_device=""，且 accel 可热更新。"""
    cfg = _make_manager(tmp_path)
    assert cfg.get("accel", "mode") == "performance"
    assert cfg.get("tts", "accel") == "auto"
    assert cfg.get("tts", "accel_device") == ""
    assert cfg.reloadable("accel") is True
    assert "accel" in HOT_RELOAD_SECTIONS
    assert "accel" not in NEED_RESTART_SECTIONS


def test_accel_missing_key_and_section_autofill(tmp_path):
    """缺省补全：tts 段缺 accel/accel_device 逐键补齐；缺整段 accel 整段补齐。"""
    (tmp_path / "config.json").write_text(
        json.dumps({"tts": {"voice": "cx-custom"}}), encoding="utf-8"
    )
    cfg = _make_manager(tmp_path)
    # 已有值保留
    assert cfg.get("tts", "voice") == "cx-custom"
    # tts 段缺失键补齐
    assert cfg.get("tts", "accel") == "auto"
    assert cfg.get("tts", "accel_device") == ""
    # 缺失整段 accel 从默认补齐
    assert cfg.get("accel", "mode") == "performance"


def test_env_override_accel_mode(monkeypatch, tmp_path):
    """CXA_ACCEL_MODE 覆盖 accel.mode（新增段名可被环境变量机制解析）。"""
    monkeypatch.setenv("CXA_ACCEL_MODE", "eco")
    cfg = _make_manager(tmp_path)
    assert cfg.get("accel", "mode") == "eco"


def test_env_override_tts_accel_device(monkeypatch, tmp_path):
    """CXA_TTS_ACCEL / CXA_TTS_ACCEL_DEVICE 覆盖 tts 段新增键。"""
    monkeypatch.setenv("CXA_TTS_ACCEL", "dml")
    monkeypatch.setenv("CXA_TTS_ACCEL_DEVICE", "igpu")
    cfg = _make_manager(tmp_path)
    assert cfg.get("tts", "accel") == "dml"
    assert cfg.get("tts", "accel_device") == "igpu"


# ------------------------------------------------------------------ #
# 11. llama.cpp 后端构建键（20261002 批 A）：local_llm/embedding.backend #
# ------------------------------------------------------------------ #

def test_backend_defaults_values(tmp_path):
    """backend 两键默认 ""（老配置零修改行为不变），归一非法值靠运行时层。"""
    cfg = _make_manager(tmp_path)
    assert cfg.get("local_llm", "backend") == ""
    assert cfg.get("embedding", "backend") == ""
    assert DEFAULTS["local_llm"]["backend"] == ""
    assert DEFAULTS["embedding"]["backend"] == ""


def test_backend_missing_key_and_section_autofill(tmp_path):
    """缺省补全：local_llm / embedding 段缺 backend 逐键补齐为 ""；缺整段整段补齐。"""
    (tmp_path / "config.json").write_text(
        json.dumps({"local_llm": {"device": "gpu"}, "embedding": {"device": "gpu"}}),
        encoding="utf-8",
    )
    cfg = _make_manager(tmp_path)
    # 既有值保留
    assert cfg.get("local_llm", "device") == "gpu"
    assert cfg.get("embedding", "device") == "gpu"
    # 缺失 backend 键从默认补齐
    assert cfg.get("local_llm", "backend") == ""
    assert cfg.get("embedding", "backend") == ""