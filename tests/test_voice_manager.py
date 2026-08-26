# -*- coding: utf-8 -*-
"""Task F2 自定义音色加载单元测试（VoiceManager）。

覆盖（全 tmp_path，无真实模型 / 无网络）：
- 音色目录不存在/为空 -> list_voices 空 + 不抛
- 构造 cx-open/ 与 custom_a/（.pt 文件）-> 含 is_default 标记与排序
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
    _make_voice(voices, "cx-open", "p.pt")
    _make_voice(voices, "custom_a", "m.pt")
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
    """宽松探测：.pt / .bin / .pth 任意文件均视为可加载产物。"""
    voices = tmp_path / "voices"
    _make_voice(voices, "pth_pack", "model.pth")
    _make_voice(voices, "bin_pack", "model.bin")
    vm = VoiceManager(voices_dir=str(voices))
    ids = {v["id"] for v in vm.list_voices()}
    assert ids == {"pth_pack", "bin_pack"}


# ------------------------------------------------------------------ #
# 3. resolve_voice                                                  #
# ------------------------------------------------------------------ #

def test_resolve_voice_paths(tmp_path):
    """cx-open / 自定义音色命中返回路径；不存在的 id 返回 None。"""
    voices = tmp_path / "voices"
    _make_voice(voices, "cx-open", "a.pt")
    _make_voice(voices, "custom_a", "b.bin")
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


# ------------------------------------------------------------------ #
# 4. set_default_voice                                             #
# ------------------------------------------------------------------ #

def test_set_default_voice_writes_config_and_effective(tmp_path):
    """set_default_voice 应写 config tts.voice 并落盘，且下次 resolve 生效。"""
    voices = tmp_path / "voices"
    _make_voice(voices, "cx-open", "p.pt")
    _make_voice(voices, "custom_a", "m.pt")
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
    _make_voice(voices, "cx-open", "p.pt")
    _make_voice(voices, "custom_a", "m.pt")
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