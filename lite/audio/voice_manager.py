# -*- coding: utf-8 -*-
"""VoiceManager：自定义音色加载管理器（Task F2）。

对齐工程文档 §12：
- 默认内置 CX-OPEN 官方音色（``cx-open``）开箱即用；
- 自定义音色由 CX-O 音频工作站训练后下载至 ``data/voices/<id>/``，
  每个子目录即一个音色包，由 MeloTTS 加载。

职责：
- ``list_voices``：扫描 ``data/voices/`` 子目录，识别音色包并返回元信息。
- ``resolve_voice``：把音色 id 解析为 MeloTTS 可加载的绝对路径；
  返回 ``None`` 表示回退后端内置默认音色。
- ``set_default_voice``：将某音色写入 config 的 ``tts.voice`` 并落盘，
  刷新 resolve 的默认音色。
"""

import os
import warnings

from lite.config.config_manager import ConfigManager

#: 默认音色标识（内置 CX-OPEN 官方音色）
DEFAULT_VOICE_ID = "cx-open"


def default_voices_dir():
    """推导默认音色根目录：``<root>/data/voices``。

    本文件位于 ``<root>/lite/audio/voice_manager.py``，逐级向上取两次
    dirname 即得 ``<root>``。路径推导一律基于
    ``os.path.dirname(os.path.abspath(__file__))``，禁止相对路径。
    """
    _audio_dir = os.path.dirname(os.path.abspath(__file__))  # <root>/lite/audio
    _lite_dir = os.path.dirname(_audio_dir)                  # <root>/lite
    _root = os.path.dirname(_lite_dir)                       # <root>
    return os.path.join(_root, "data", "voices")


class VoiceManager:
    """自定义音色加载管理器。

    :param voices_dir: 音色根目录；缺省回退 ``<root>/data/voices``
    :param config: 配置（ConfigManager 或裸 dict）；用于读写默认音色
    """

    def __init__(self, voices_dir=None, config=None):
        """初始化音色管理器。

        :param voices_dir: 音色根目录绝对路径；留空回退 ``data/voices``
        :param config: ConfigManager 或裸 dict；留空则不读写配置
        """
        self.voices_dir = voices_dir or default_voices_dir()
        self.config = config
        #: set_default_voice 后的显式默认音色缓存（优先于 config 读取）
        self._default_voice = None

    # ------------------------------------------------------------------ #
    # 内部：音色探测 / 磁盘辅助                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _is_loadable_voice(dirpath):
        """宽松探测某目录是否可作为 MeloTTS 音色包。

        只要目录内含有任意文件（``.pt`` / ``.bin`` / ``.pth`` 或其他）
        即判定为可加载产物存在。
        """
        if not os.path.isdir(dirpath):
            return False
        for _root, _dirs, files in os.walk(dirpath):
            if files:
                return True
        return False

    @staticmethod
    def _dir_size(dirpath):
        """递归统计目录总字节数（单个文件跳过读取失败）。"""
        total = 0
        for _root, _dirs, files in os.walk(dirpath):
            for name in files:
                path = os.path.join(_root, name)
                try:
                    total += os.path.getsize(path)
                except OSError:
                    continue
        return total

    # ------------------------------------------------------------------ #
    # 内部：配置默认音色读取                                              #
    # ------------------------------------------------------------------ #

    def _config_default_voice(self):
        """从 config 读取默认音色 id；无配置或缺失回退 ``cx-open``。"""
        if self.config is None:
            return DEFAULT_VOICE_ID
        if isinstance(self.config, dict):
            sec = self.config.get("tts", {})
            if not isinstance(sec, dict):
                return DEFAULT_VOICE_ID
            return sec.get("voice", DEFAULT_VOICE_ID)
        return self.config.get("tts", "voice", DEFAULT_VOICE_ID)

    def _effective_default(self):
        """解析当前默认音色 id：显式缓存优先，否则读 config。"""
        if getattr(self, "_default_voice", None):
            return self._default_voice
        return self._config_default_voice()

    # ------------------------------------------------------------------ #
    # 公开接口                                                            #
    # ------------------------------------------------------------------ #

    def list_voices(self):
        """扫描音色根目录，返回当前可用音色包列表。

        每个子目录=一个音色包；内含任意文件即判定存在（宽松探测）。
        返回 ``[{id, path, is_default, size}]``：``cx-open`` 恒标记
        ``is_default=True``。音色根目录不存在/为空时返回空列表。
        """
        if not os.path.isdir(self.voices_dir):
            warnings.warn(
                f"音色目录不存在：{self.voices_dir}，返回空列表",
                UserWarning,
            )
            return []
        voices = []
        for name in sorted(os.listdir(self.voices_dir)):
            subdir = os.path.join(self.voices_dir, name)
            if not self._is_loadable_voice(subdir):
                continue
            voices.append({
                "id": name,
                "path": subdir,
                "is_default": (name == DEFAULT_VOICE_ID),
                "size": self._dir_size(subdir),
            })
        return voices

    def resolve_voice(self, voice=None):
        """把音色 id 解析为 MeloTTS 可加载的绝对路径。

        - ``voice="cx-open"`` -> ``data/voices/cx-open``（存在则返回；
          否则返回 ``None``，回退后端内置默认音色）；
        - ``voice`` 为自定义 id -> ``data/voices/{id}``（存在则返回；
          不存在返回 ``None``）；
        - ``voice=None`` -> 解析当前默认音色（config ``tts.voice``）。

        :param voice: 音色 id；留空使用默认音色
        :return: 音色包绝对路径；不存在返回 ``None``（用内置默认音色）
        """
        target = voice or self._effective_default() or DEFAULT_VOICE_ID
        if not isinstance(target, str) or not target.strip():
            target = DEFAULT_VOICE_ID
        path = os.path.join(self.voices_dir, target.strip())
        if self._is_loadable_voice(path):
            return path
        return None

    def set_default_voice(self, voice_id):
        """把 ``voice_id`` 设为默认音色并写入 config（``tts.voice``）。

        - ConfigManager：``set`` 内存生效 + ``save`` 落盘；
        - 裸 dict：就地更新 ``tts.voice``；
        - 无配置：仅刷新内存缓存，下次 ``resolve_voice(None)`` 生效。
        """
        self._default_voice = voice_id
        if isinstance(self.config, ConfigManager):
            self.config.set("tts", "voice", voice_id, reloadable=True)
            self.config.save()
        elif isinstance(self.config, dict):
            sec = self.config.setdefault("tts", {})
            if not isinstance(sec, dict):
                sec = {}
                self.config["tts"] = sec
            sec["voice"] = voice_id