# -*- coding: utf-8 -*-
"""Task F2 自定义音色加载单元测试（VoiceManager）。

覆盖（全 tmp_path，无真实模型 / 无网络）：
- 音色目录不存在/为空 -> list_voices 空 + 不抛
- 构造 cx-open/ 与 custom_a/（G-4 口径认可的产物文件）-> 含 is_default 标记与排序
- G-4 口径收敛：仅 config.json / ckpt.txt / *.pth / *.ckpt 算可加载；
  任意杂文件（.pt / .bin）不再判定为音色包
- resolve_voice：cx-open 命中路径 / 自定义命中 / 不存在回 None
- set_default_voice 写 config 并在下一次 resolve 生效
- LiteTTS voice_resolver 注入冒烟：voice 参数被解析（不真实加载模型）
"""

import json
import os

from lite.audio import LiteTTS, MockTTSBackend, VoiceManager
from lite.config.config_manager import ConfigManager


def _make_config(tmp_path):
    """在临时目录构造 ConfigManager，避免污染 c:\\CX-A\\config.json。"""
    return ConfigManager(
        config_path=str(tmp_path / "config.json"),
        data_dir=str(tmp_path / "data"),
    )


def _make_voice(voices, name, *files):
    """在 voices 下构造一个音色包目录（含给定文件）。"""
    d = voices / name
    d.mkdir(parents=True, exist_ok=True)
    for f in files:
        (d / f).write_bytes(b"x")
    return d


# ------------------------------------------------------------------ #
# 1. 目录不存在 / 为空                                               #
# ------------------------------------------------------------------ #

def test_list_voices_missing_dir_returns_empty(tmp_path):
    """音色根目录不存在时应返回空列表且不抛异常。"""
    vm = VoiceManager(voices_dir=str(tmp_path / "voices"))
    assert vm.list_voices() == []


def test_list_voices_empty_dir_returns_empty(tmp_path):
    """音色根目录存在但为空时应返回空列表。"""
    voices = tmp_path / "voices"
    voices.mkdir()
    vm = VoiceManager(voices_dir=str(voices))
    assert vm.list_voices() == []


# ------------------------------------------------------------------ #
# 2. 音色识别                                                       #
# ------------------------------------------------------------------ #

def test_list_voices_marks_default_and_sorted(tmp_path):
    """应识别 cx-open 与自定义音色，cx-open 恒 is_default=True，且按 id 排序。"""
    voices = tmp_path / "voices"
    _make_voice(voices, "cx-open", "model.pth")
    _make_voice(voices, "custom_a", "config.json")
    vm = VoiceManager(voices_dir=str(voices))

    lst = vm.list_voices()
    ids = [v["id"] for v in lst]
    assert ids == ["custom_a", "cx-open"]  # 字典序排序

    by_id = {v["id"]: v for v in lst}
    assert by_id["cx-open"]["is_default"] is True
    assert by_id["cx-open"]["path"] == str(voices / "cx-open")
    assert by_id["custom_a"]["is_default"] is False
    assert by_id["custom_a"]["size"] > 0
    # 空目录子项（不含任何文件的目录）不应被识别为音色包
    _make_voice(voices, "empty_pack")
    assert [v["id"] for v in vm.list_voices()] == ["custom_a", "cx-open"]


def test_list_voices_multiformat_files(tmp_path):
    """G-4 口径收敛：*.pth / *.ckpt / config.json / ckpt.txt 算可加载；
    杂文件（.pt / .bin / 说明文本）不再判定为音色包。"""
    voices = tmp_path / "voices"
    _make_voice(voices, "pth_pack", "model.pth")
    _make_voice(voices, "ckpt_ext_pack", "weights.ckpt")
    _make_voice(voices, "json_pack", "config.json")
    _make_voice(voices, "txt_pack", "ckpt.txt")
    _make_voice(voices, "pt_pack", "model.pt")      # 旧口径认可，新口径拒绝
    _make_voice(voices, "bin_pack", "model.bin")    # 旧口径认可，新口径拒绝
    _make_voice(voices, "readme_pack", "说明.txt")   # 任意杂文件不判定
    vm = VoiceManager(voices_dir=str(voices))
    ids = {v["id"] for v in vm.list_voices()}
    assert ids == {"pth_pack", "ckpt_ext_pack", "json_pack", "txt_pack"}


# ------------------------------------------------------------------ #
# 3. resolve_voice                                                  #
# ------------------------------------------------------------------ #

def test_resolve_voice_paths(tmp_path):
    """cx-open / 自定义音色命中返回路径；不存在的 id 返回 None。"""
    voices = tmp_path / "voices"
    _make_voice(voices, "cx-open", "model.pth")
    _make_voice(voices, "custom_a", "ckpt.txt")
    vm = VoiceManager(voices_dir=str(voices))

    assert vm.resolve_voice("cx-open") == str(voices / "cx-open")
    assert vm.resolve_voice("custom_a") == str(voices / "custom_a")
    assert vm.resolve_voice("missing") is None
    # 默认 resolve（无显式 voice）回退 config 的 cx-open
    assert vm.resolve_voice() == str(voices / "cx-open")


def test_resolve_voice_default_dir_missing_returns_none(tmp_path):
    """cx-open 音色目录不存在但作为默认时，resolve 应返回 None（用内置默认）。"""
    vm = VoiceManager(voices_dir=str(tmp_path / "voices"))
    assert vm.resolve_voice("cx-open") is None
    assert vm.resolve_voice() is None


def test_resolve_voice_rejects_traversal_ids(tmp_path):
    """L13：含 / \\ .. 或盘符的音色 id 一律拒绝返回 None（路径穿越防护）。"""
    voices = tmp_path / "voices"
    # 即使对应目录真实存在（含可加载文件），穿越 id 也必须被拒绝
    _make_voice(voices, "cx-open", "model.pth")
    evil_target = tmp_path / "evil" / "secret.pt"
    evil_target.parent.mkdir(parents=True, exist_ok=True)
    evil_target.write_bytes(b"x")

    vm = VoiceManager(voices_dir=str(voices))
    malicious_ids = [
        "../evil",
        "..\\evil",
        "sub/dir",
        "sub\\dir",
        "..",
        "C:windows-system32",
        "a/../..",
    ]
    for vid in malicious_ids:
        assert vm.resolve_voice(vid) is None, f"非法 id 未被拒绝：{vid!r}"

    # 合法 id 不受影响
    assert vm.resolve_voice("cx-open") == str(voices / "cx-open")


# ------------------------------------------------------------------ #
# 4. set_default_voice                                             #
# ------------------------------------------------------------------ #

def test_set_default_voice_writes_config_and_effective(tmp_path):
    """set_default_voice 应写 config tts.voice 并落盘，且下次 resolve 生效。"""
    voices = tmp_path / "voices"
    _make_voice(voices, "cx-open", "model.pth")
    _make_voice(voices, "custom_a", "config.json")
    cfg = _make_config(tmp_path)
    vm = VoiceManager(voices_dir=str(voices), config=cfg)

    assert vm.resolve_voice() == str(voices / "cx-open")

    vm.set_default_voice("custom_a")
    # 内存配置与落盘均更新
    assert cfg.get("tts", "voice") == "custom_a"
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))[
        "tts"
    ]["voice"] == "custom_a"
    # 下一次 resolve（无显式 voice）路由到新默认音色
    assert vm.resolve_voice() == str(voices / "custom_a")


# ------------------------------------------------------------------ #
# 5. LiteTTS 集成冒烟                                               #
# ------------------------------------------------------------------ #

def test_litets_voice_resolver_injected(tmp_path):
    """注入 voice_resolver 后，synthesize 的 voice 参数应被解析为路径。"""
    voices = tmp_path / "voices"
    _make_voice(voices, "cx-open", "model.pth")
    _make_voice(voices, "custom_a", "config.json")
    vm = VoiceManager(voices_dir=str(voices))

    calls = []

    class RecordingBackend(MockTTSBackend):
        def synthesize(self, text, voice="cx-open"):
            calls.append(voice)
            return b"wav"

    tts = LiteTTS(
        backend=RecordingBackend(),
        default_voice="cx-open",
        voice_resolver=vm.resolve_voice,
    )
    # 自定义音色：voice 经 resolver 解析为绝对路径后传给 backend
    tts.synthesize("你好", voice="custom_a")
    assert calls == [str(voices / "custom_a")]
    # 不存在的音色：resolver 返回 None，backends 收到 None
    tts.synthesize("你好", voice="missing")
    assert calls[-1] is None


def test_litets_no_resolver_passthrough(tmp_path):
    """未注入 resolver（默认 None）时 voice 应原样传递，保持既有行为。"""
    calls = []

    class RecordingBackend(MockTTSBackend):
        def synthesize(self, text, voice="cx-open"):
            calls.append(voice)
            return b"wav"

    tts = LiteTTS(backend=RecordingBackend(), default_voice="cx-open")
    tts.synthesize("hi")
    assert calls == ["cx-open"]


# ------------------------------------------------------------------ #
# 6. 包导出                                                         #
# ------------------------------------------------------------------ #

def test_voice_manager_exported():
    """VoiceManager 应通过 lite.audio 导出。"""
    import lite.audio as audio

    assert hasattr(audio, "VoiceManager")
    assert audio.VoiceManager is VoiceManager


# ------------------------------------------------------------------ #
# 7. 第四轮体检批次C：任意位置冒号（NTFS ADS）拦截                      #
# ------------------------------------------------------------------ #

def test_is_unsafe_voice_id_rejects_ads_colon():
    """第四轮体检批次C：任意位置的冒号（含 NTFS ADS 形态 a:b）一律判非法。"""
    from lite.audio.voice_manager import is_unsafe_voice_id

    assert is_unsafe_voice_id("a:b") is True
    assert is_unsafe_voice_id("ab:cd") is True
    assert is_unsafe_voice_id(":lead") is True
    assert is_unsafe_voice_id("trail:") is True
    # 正常音色 id 不受影响
    assert is_unsafe_voice_id("cx-open") is False
    assert is_unsafe_voice_id("custom_a") is False


def test_resolve_voice_rejects_midfix_colon_ads(tmp_path):
    """中缀冒号 id（NTFS ADS 形态 a:b）经 resolve_voice 同样拒绝返回 None。"""
    voices = tmp_path / "voices"
    _make_voice(voices, "cx-open", "model.pth")
    vm = VoiceManager(voices_dir=str(voices))
    assert vm.resolve_voice("a:b") is None
    assert vm.resolve_voice("cx-open") == str(voices / "cx-open")