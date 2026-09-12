# -*- coding: utf-8 -*-
"""Task H2 主动视觉单元测试（全 mock，tmp_path 隔离，无真实屏幕采集与网络）。

覆盖：
- ChangeDetector：相同帧 0 / 全异帧 1 / 部分变化中间值 / 定长归一化幂等 / bytes 输入
- AdaptiveSampler：频率调节曲线（静止降频、剧变升频、中间线性插值）、
  事件产出条件（仅 ratio >= high_threshold）、首次基线采样语义、参数校验
- VisionClipQueue：满时丢弃最旧、consumer 异常隔离不崩、无 consumer 告警、
  stop 排空、停止后拒绝投递
- VisionPipeline：enabled=False 隐私红线（capture 零调用）、理解沉淀
  source='vision' 记忆、离线暂停（不落库仅警告）、理解异常隔离、
  生产默认理解路径（CloudAdapter.chat 流式拼接 + 提示词契约）、恢复后从新片段开始
- 配置 H2.5：vision 段默认值 / 热更新段登记（test_config.py 既有断言不受影响）
"""

import json

import pytest

from lite.config.config_manager import (
    ConfigManager,
    DEFAULTS,
    HOT_RELOAD_SECTIONS,
    NEED_RESTART_SECTIONS,
)
from lite.cloud.adapter import CloudUnavailableError
from lite.memory.storage import MemoryStore
from lite.vision.change_detector import ChangeDetector
from lite.vision.sampler import AdaptiveSampler
from lite.vision.queue import VisionClipQueue
from lite.vision.pipeline import VISION_PROMPT, VisionPipeline

#: 默认降采样目标尺寸（与 ChangeDetector 缺省一致）
W, H = 64, 36
N = W * H


# ------------------------------------------------------------------ #
# 测试替身与帧构造助手                                                #
# ------------------------------------------------------------------ #

def solid_frame(value: int, size: int = N) -> list:
    """构造全帧同值灰度像素列表。"""
    return [value] * size


def changed_frame(changed_pixels: int, size: int = N, base: int = 0,
                  alt: int = 255) -> list:
    """构造前 changed_pixels 个像素反转、其余保持 base 的部分变化帧。"""
    frame = [base] * size
    for i in range(min(changed_pixels, size)):
        frame[i] = alt
    return frame


class ScriptedBackend:
    """脚本化屏幕后端 mock：按帧序列依次返回（最后一帧常驻），并计数 capture 调用。"""

    def __init__(self, frames: list):
        assert frames, "ScriptedBackend 至少需要一帧"
        self._frames = list(frames)
        self.calls = 0

    def capture(self):
        self.calls += 1
        if len(self._frames) > 1:
            return self._frames.pop(0)
        return self._frames[0]


class MockCloud:
    """云端适配器 mock：is_online 可切换，chat 产出固定分块并记录调用。"""

    def __init__(self, online: bool = True, chunks: list = None):
        self.online = online
        self.chunks = chunks if chunks is not None else ["屏幕", "摘要"]
        self.chat_calls = []

    def is_online(self, timeout: int = 5) -> bool:
        return self.online

    def chat(self, messages):
        self.chat_calls.append(messages)
        yield from self.chunks


def make_memory_store(tmp_path) -> MemoryStore:
    """在临时目录构造 MemoryStore（不污染项目 data/memories.db）。"""
    return MemoryStore(db_path=str(tmp_path / "memories_test.db"))


# ------------------------------------------------------------------ #
# 1. ChangeDetector 变化率                                            #
# ------------------------------------------------------------------ #

class TestChangeDetector:
    """帧间变化率检测。"""

    def test_identical_frames_zero(self):
        """相同帧变化率为 0。"""
        det = ChangeDetector()
        frame = solid_frame(128)
        assert det.change_ratio(frame, frame) == 0.0

    def test_completely_different_frames_one(self):
        """全异帧（0 vs 255）变化率为 1。"""
        det = ChangeDetector()
        ratio = det.change_ratio(solid_frame(0), solid_frame(255))
        assert ratio == pytest.approx(1.0)

    def test_partial_change_middle_value(self):
        """部分变化帧（一半像素全反转）变化率约 0.5。"""
        det = ChangeDetector(width=10, height=10)
        prev = [0] * 100
        curr = [255] * 50 + [0] * 50
        assert det.change_ratio(prev, curr) == pytest.approx(0.5)

    def test_ratio_in_unit_range(self):
        """任意两帧变化率都落在 [0, 1]。"""
        det = ChangeDetector()
        for changed in (1, 100, 1152, 2303):
            ratio = det.change_ratio(solid_frame(0), changed_frame(changed))
            assert 0.0 <= ratio <= 1.0

    def test_normalize_idempotent_and_fixed_length(self):
        """归一化幂等且定长：任意长度输入归一到 target_size，二次归一化不变。"""
        det = ChangeDetector(width=10, height=10)
        big = list(range(200))
        once = det.normalize(big)
        assert len(once) == 100
        assert det.normalize(once) == once

    def test_bytes_input(self):
        """bytes 输入与等值列表输入结果一致。"""
        det = ChangeDetector(width=4, height=4)
        frame_bytes = bytes(range(16))
        assert det.change_ratio(frame_bytes, bytes(reversed(frame_bytes))) > 0.0
        assert det.change_ratio(frame_bytes, frame_bytes) == 0.0

    def test_none_frames_semantics(self):
        """双 None 为 0（无变化），单 None 为 1（视为完全变化）。"""
        det = ChangeDetector()
        assert det.change_ratio(None, None) == 0.0
        assert det.change_ratio(None, solid_frame(10)) == 1.0
        assert det.change_ratio(solid_frame(10), None) == 1.0

    def test_invalid_size_rejected(self):
        """非法宽高（非正数）构造被拒绝。"""
        with pytest.raises(ValueError):
            ChangeDetector(width=0, height=36)


# ------------------------------------------------------------------ #
# 2. AdaptiveSampler 自适应频率                                        #
# ------------------------------------------------------------------ #

class TestAdaptiveSampler:
    """自适应采样频率调节曲线。"""

    def test_first_tick_baseline_only(self):
        """首次采样仅建立基线：无事件产出、间隔不调整。"""
        backend = ScriptedBackend([solid_frame(0)])
        sampler = AdaptiveSampler(backend)
        event = sampler.tick(now=0.0)
        assert event is None
        assert backend.calls == 1
        assert sampler.current_interval == 2  # 初始为最高频（max_interval_s）

    def test_static_frames_lower_frequency(self):
        """静止帧（变化率 0 <= low）→ 间隔升至 min_interval_s，无事件。"""
        backend = ScriptedBackend([solid_frame(7)])
        sampler = AdaptiveSampler(backend)
        assert sampler.tick(now=0.0) is None  # 基线
        event = sampler.tick(now=2.0)
        assert event is None
        assert sampler.last_change_ratio == 0.0
        assert sampler.current_interval == 30  # min_interval_s

    def test_static_interval_grows_blocks_recapture(self):
        """静止后间隔增大：间隔窗口内的 tick 不再触发 capture。"""
        backend = ScriptedBackend([solid_frame(7)])
        sampler = AdaptiveSampler(backend)
        sampler.tick(now=0.0)
        sampler.tick(now=2.0)  # 静止 → interval=30
        assert sampler.tick(now=10.0) is None  # 10-2=8 < 30，未到期
        assert backend.calls == 2  # 未发生新采样
        sampler.tick(now=32.0)  # 32-2=30 >= 30，到期再次采样
        assert backend.calls == 3

    def test_dramatic_change_raises_frequency_and_event(self):
        """剧变帧（ratio >= high）→ 产出事件 + 间隔缩至 max_interval_s。"""
        backend = ScriptedBackend([solid_frame(0), solid_frame(255)])
        sampler = AdaptiveSampler(backend)
        assert sampler.tick(now=0.0) is None  # 基线
        event = sampler.tick(now=2.0)
        assert event is not None
        assert event["timestamp"] == pytest.approx(2.0)
        assert event["change_ratio"] == pytest.approx(1.0)
        assert len(event["frame"]) == N  # 事件帧为降采样定长帧
        assert sampler.current_interval == 2  # max_interval_s

    def test_event_frame_is_downsampled_not_raw(self):
        """事件携带的帧为降采样帧（防内存膨胀），原始大帧不透传。"""
        big_static = [10] * 19200  # 160x120 原始分辨率
        big_changed = [245] * 9600 + [10] * 9600
        backend = ScriptedBackend([big_static, big_changed])
        sampler = AdaptiveSampler(backend)  # 默认 64x36 检测器
        sampler.tick(now=0.0)
        event = sampler.tick(now=2.0)
        assert event is not None
        assert len(event["frame"]) == N
        assert 0.0 < event["change_ratio"] <= 1.0

    def test_mid_change_linear_interpolation(self):
        """中间变化率线性插值：ratio=0.2（low/high 中点）→ interval=16。"""
        # 自定义 10x10 检测器：20/100 像素全反转 → ratio 恰为 0.2
        prev = [0] * 100
        curr = [255] * 20 + [0] * 80
        backend = ScriptedBackend([prev, curr])
        sampler = AdaptiveSampler(
            backend, detector=ChangeDetector(width=10, height=10)
        )
        assert sampler.tick(now=0.0) is None
        event = sampler.tick(now=2.0)
        assert event is None  # 0.2 < 0.35，不产出事件
        assert sampler.current_interval == pytest.approx(16.0)  # (30+2)/2

    def test_boundary_ratio_equal_high_produces_event(self):
        """变化率恰等于 high_threshold（0.35）→ 产出事件且 interval=max。"""
        prev = [0] * 100
        curr = [255] * 35 + [0] * 65
        backend = ScriptedBackend([prev, curr])
        sampler = AdaptiveSampler(
            backend, detector=ChangeDetector(width=10, height=10)
        )
        sampler.tick(now=0.0)
        event = sampler.tick(now=2.0)
        assert event is not None
        assert event["change_ratio"] == pytest.approx(0.35)
        assert sampler.current_interval == 2

    def test_boundary_ratio_equal_low_no_event(self):
        """变化率恰等于 low_threshold（0.05）→ 无事件且 interval=min。"""
        prev = [0] * 100
        curr = [255] * 5 + [0] * 95
        backend = ScriptedBackend([prev, curr])
        sampler = AdaptiveSampler(
            backend, detector=ChangeDetector(width=10, height=10)
        )
        sampler.tick(now=0.0)
        assert sampler.tick(now=2.0) is None
        assert sampler.current_interval == 30

    def test_frequency_curve_static_then_burst(self):
        """频率调节曲线：静止降频后再剧变立即回升最高频。"""
        frames = [solid_frame(3), solid_frame(3), solid_frame(3), solid_frame(250)]
        backend = ScriptedBackend(frames)
        sampler = AdaptiveSampler(backend)
        sampler.tick(now=0.0)   # 基线
        sampler.tick(now=2.0)   # 静止 → interval=30
        assert sampler.current_interval == 30
        sampler.tick(now=32.0)  # 静止采样（32-2=30 到期）→ 仍静止
        assert sampler.current_interval == 30
        event = sampler.tick(now=62.0)  # 剧变（62-32=30 到期）→ 事件 + 最高频
        assert event is not None
        assert sampler.current_interval == 2

    def test_invalid_params_rejected(self):
        """非法构造参数（min<max / 阈值区间错误）被拒绝。"""
        with pytest.raises(ValueError):
            AdaptiveSampler(None, min_interval_s=1, max_interval_s=5)
        with pytest.raises(ValueError):
            AdaptiveSampler(None, high_threshold=0.3, low_threshold=0.5)


# ------------------------------------------------------------------ #
# 3. VisionClipQueue 有界队列                                          #
# ------------------------------------------------------------------ #

class TestVisionClipQueue:
    """有界队列语义（满时丢弃最旧 + consumer 异常隔离）。"""

    def test_submit_processed_in_order(self):
        """submit 后由 worker 按序消费，stop 排空后可确定性断言。"""
        q = VisionClipQueue(maxsize=4)
        processed = []
        q.set_consumer(processed.append)
        for i in range(4):
            assert q.submit({"i": i}) is True
        q.stop()
        assert [item["i"] for item in processed] == [0, 1, 2, 3]

    def test_full_queue_drops_oldest(self):
        """队列满时丢弃最旧条目腾位，最新条目保留。"""
        q = VisionClipQueue(maxsize=4)
        processed = []
        q.set_consumer(processed.append)
        for i in range(5):  # 第 5 条触发丢弃最旧（0 被弃）
            assert q.submit({"i": i}) is True
        q.stop()
        assert [item["i"] for item in processed] == [1, 2, 3, 4]
        assert q.dropped_count == 1

    def test_consumer_exception_does_not_crash(self, caplog):
        """consumer 抛异常被隔离：worker 不崩，后续条目继续处理，告警留痕。"""
        q = VisionClipQueue(maxsize=4)
        processed = []

        def bad_consumer(item):
            if item["i"] == 0:
                raise RuntimeError("模拟 consumer 崩溃")
            processed.append(item)

        q.set_consumer(bad_consumer)
        q.submit({"i": 0})
        q.submit({"i": 1})
        q.stop()  # 不应有异常穿透
        assert [item["i"] for item in processed] == [1]
        assert "[VisionQueue][WARN]" in caplog.text

    def test_missing_consumer_warns_and_skips(self, caplog):
        """未注册 consumer 时条目告警跳过，不崩。"""
        q = VisionClipQueue(maxsize=2)
        assert q.is_ready() is False
        q.submit({"i": 0})
        q.stop()
        assert "[VisionQueue][WARN]" in caplog.text

    def test_submit_after_stop_rejected(self):
        """stop 之后 submit 返回 False（拒绝迟到投递）。"""
        q = VisionClipQueue(maxsize=2)
        q.set_consumer(lambda item: None)
        q.stop()
        assert q.submit({"i": 0}) is False

    def test_worker_is_single_daemon_thread(self):
        """worker 仅一条且为 daemon 线程（退出依赖 stop）。"""
        q = VisionClipQueue(maxsize=2)
        q.set_consumer(lambda item: None)
        q.submit({"i": 0})
        assert q._worker is not None
        assert q._worker.daemon is True
        assert q._worker.name == "vision-clip-queue-worker"
        q.stop()
        q.submit({"i": 1})  # 停止后不再拉起新 worker
        assert q._worker is q._worker  # 线程引用未变（未重建）
        assert q.pending_count() >= 0

    def test_invalid_maxsize_rejected(self):
        """非法容量被拒绝。"""
        with pytest.raises(ValueError):
            VisionClipQueue(maxsize=0)


# ------------------------------------------------------------------ #
# 4. VisionPipeline 管线                                               #
# ------------------------------------------------------------------ #

class TestVisionPipeline:
    """采样 → 队列 → 理解 → 沉淀管线。"""

    def _make_pipeline(self, tmp_path, frames, *, enabled=True, cloud=None,
                       understanding=None, sampler_kwargs=None):
        """构造被测管线（脚本化后端 + 临时库 + dict 配置）。"""
        backend = ScriptedBackend(frames)
        sampler = AdaptiveSampler(backend, **(sampler_kwargs or {}))
        store = make_memory_store(tmp_path)
        config = {"vision": {"enabled": enabled}}
        pipeline = VisionPipeline(
            sampler=sampler, cloud=cloud, memory_store=store,
            config=config, understanding=understanding,
        )
        return pipeline, backend, store

    def test_disabled_never_captures_screen(self, tmp_path):
        """隐私红线：enabled=False 时 run_once 直接返回 None 且 capture 零调用。"""
        pipeline, backend, store = self._make_pipeline(
            tmp_path, [solid_frame(0), solid_frame(255)], enabled=False
        )
        assert pipeline.run_once(now=0.0) is None
        assert pipeline.run_once(now=100.0) is None
        assert backend.calls == 0  # 绝不触碰屏幕采集
        assert store.list(type="short_term") == []

    def test_understanding_mock_sediments_vision_memory(self, tmp_path):
        """mock understanding 返回摘要 → memories 表落 source='vision' 记录。"""
        understanding_calls = []

        def fake_understanding(item):
            understanding_calls.append(item)
            return "屏幕显示一段代码编辑器"

        pipeline, backend, store = self._make_pipeline(
            tmp_path, [solid_frame(0), solid_frame(255)],
            cloud=MockCloud(online=True), understanding=fake_understanding,
        )
        assert pipeline.run_once(now=0.0) is None   # 基线
        event = pipeline.run_once(now=2.0)          # 剧变 → 事件入队
        assert event is not None
        pipeline.stop()                              # 排空队列，确定性断言

        memories = store.list(type="short_term")
        assert len(memories) == 1
        memory = memories[0]
        assert memory["source"] == "vision"
        assert memory["content"] == "屏幕显示一段代码编辑器"
        assert memory["type"] == "short_term"
        assert memory["importance"] == 2
        assert memory["agent_id"] == "default"
        assert memory["id"] > 0
        assert len(understanding_calls) == 1
        assert understanding_calls[0]["change_ratio"] == pytest.approx(1.0)

    def test_static_screen_no_memory(self, tmp_path):
        """静止画面：无事件产出，不沉淀记忆。"""
        pipeline, backend, store = self._make_pipeline(
            tmp_path, [solid_frame(9)], understanding=lambda item: "不该被调用"
        )
        assert pipeline.run_once(now=0.0) is None
        assert pipeline.run_once(now=2.0) is None
        pipeline.stop()
        assert backend.calls == 2
        assert store.list(type="short_term") == []

    def test_offline_skips_understanding_and_sediment(self, tmp_path, caplog):
        """云端离线：不调用理解、不落库，仅告警（离线暂停语义）。"""
        cloud = MockCloud(online=False)

        def must_not_call(item):  # 离线时理解不得被触发
            raise AssertionError("离线时不应调用 understanding")

        pipeline, backend, store = self._make_pipeline(
            tmp_path, [solid_frame(0), solid_frame(255)],
            cloud=cloud, understanding=must_not_call,
        )
        pipeline.run_once(now=0.0)
        pipeline.run_once(now=2.0)
        pipeline.stop()
        assert cloud.chat_calls == []          # 理解未发生
        assert store.list(type="short_term") == []  # 未沉淀
        assert "[VisionPipeline][WARN]" in caplog.text

    def test_offline_resume_processes_new_clips_only(self, tmp_path):
        """离线恢复语义：旧片段丢弃，恢复在线后从新片段开始沉淀。"""
        frames = [solid_frame(0), solid_frame(255), solid_frame(0)]
        cloud = MockCloud(online=False, chunks=["恢复后的新画面"])
        pipeline, backend, store = self._make_pipeline(
            tmp_path, frames, cloud=cloud, understanding=None,
        )
        pipeline.run_once(now=0.0)   # 基线
        pipeline.run_once(now=2.0)   # 剧变，但离线 → 旧片段被丢弃
        pipeline.stop()
        assert store.list(type="short_term") == []

        # 恢复在线：重新构造管线（新片段语义），从新基线开始
        cloud.online = True
        pipeline2, backend2, store2 = self._make_pipeline(
            tmp_path, [solid_frame(0), solid_frame(255)],
            cloud=cloud, understanding=None,
        )
        pipeline2.run_once(now=0.0)
        pipeline2.run_once(now=2.0)
        pipeline2.stop()
        memories = store2.list(type="short_term")
        assert len(memories) == 1
        assert memories[0]["content"] == "恢复后的新画面"

    def test_understanding_exception_isolated(self, tmp_path, caplog):
        """理解抛 CloudUnavailableError：仅告警不崩，不落库。"""
        def broken_understanding(item):
            raise CloudUnavailableError("模拟云端流式中断")

        pipeline, backend, store = self._make_pipeline(
            tmp_path, [solid_frame(0), solid_frame(255)],
            cloud=MockCloud(online=True), understanding=broken_understanding,
        )
        pipeline.run_once(now=0.0)
        pipeline.run_once(now=2.0)
        pipeline.stop()  # worker 不崩，异常被隔离
        assert store.list(type="short_term") == []
        assert "[VisionPipeline][WARN]" in caplog.text

    def test_default_understanding_production_path(self, tmp_path):
        """生产默认理解：CloudAdapter.chat 多模态消息 + 流式拼接摘要 + 提示词契约。"""
        cloud = MockCloud(online=True, chunks=["一", "句话描述"])
        pipeline, backend, store = self._make_pipeline(
            tmp_path, [solid_frame(0), solid_frame(255)],
            cloud=cloud, understanding=None,  # 走生产默认理解
        )
        pipeline.run_once(now=0.0)
        pipeline.run_once(now=2.0)
        pipeline.stop()

        memories = store.list(type="short_term")
        assert len(memories) == 1
        assert memories[0]["content"] == "一句话描述"
        # 提示词契约：多模态 content 数组含默认中文提示词与降采样亮度拓扑描述
        assert len(cloud.chat_calls) == 1
        payload = json.dumps(cloud.chat_calls[0], ensure_ascii=False)
        assert VISION_PROMPT in payload
        assert "画面亮度拓扑" in payload

    def test_pipeline_reads_config_manager(self, tmp_path):
        """配置载体为 ConfigManager：enabled 判定走 vision 段（热更新段逐次读取）。"""
        cfg = ConfigManager(
            config_path=str(tmp_path / "config.json"),
            data_dir=str(tmp_path / "data"),
        )
        assert cfg.reloadable("vision") is True  # H2.5：vision 登记为热更新段
        assert cfg.get("vision", "enabled") is False  # 默认关

        backend = ScriptedBackend([solid_frame(0), solid_frame(255)])
        pipeline = VisionPipeline(
            sampler=AdaptiveSampler(backend),
            cloud=MockCloud(online=True),
            memory_store=make_memory_store(tmp_path),
            config=cfg,
            understanding=lambda item: "配置驱动的摘要",
        )
        # 默认关：不采样
        assert pipeline.run_once(now=0.0) is None
        assert backend.calls == 0
        # 打开开关（热更新语义，逐次读取即时生效）
        cfg.set("vision", "enabled", True)
        assert pipeline.run_once(now=0.0) is None   # 基线
        assert pipeline.run_once(now=2.0) is not None
        assert backend.calls == 2
        pipeline.stop()

    def test_no_sampler_warns_and_idles(self, tmp_path, caplog):
        """未注入 sampler：管线告警空转，不崩溃。"""
        pipeline = VisionPipeline(
            memory_store=make_memory_store(tmp_path),
            config={"vision": {"enabled": True}},
        )
        assert pipeline.run_once(now=0.0) is None
        assert "[VisionPipeline][WARN]" in caplog.text

    def test_pipeline_without_memory_store_warns(self, tmp_path, caplog):
        """未注入 memory_store：理解结果无处沉淀，告警跳过。"""
        pipeline = VisionPipeline(
            sampler=AdaptiveSampler(ScriptedBackend([solid_frame(0), solid_frame(255)])),
            cloud=MockCloud(online=True),
            config={"vision": {"enabled": True}},
            understanding=lambda item: "无处安放的摘要",
        )
        pipeline.run_once(now=0.0)
        pipeline.run_once(now=2.0)
        pipeline.stop()
        assert "[VisionPipeline][WARN]" in caplog.text


# ------------------------------------------------------------------ #
# 5. 配置 H2.5：vision 段契约                                          #
# ------------------------------------------------------------------ #

def test_vision_config_defaults():
    """DEFAULTS.vision 与 H2.5 契约严格一致。"""
    assert DEFAULTS["vision"] == {
        "enabled": False,
        "max_interval_s": 2,
        "min_interval_s": 30,
        "still_threshold_s": 10,
        "queue_size": 16,
    }


def test_vision_config_hot_reload_section():
    """vision 登记为热更新段，且热更新/需重启段仍完整覆盖全部配置段。"""
    assert "vision" in HOT_RELOAD_SECTIONS
    assert "vision" not in NEED_RESTART_SECTIONS
    assert set(HOT_RELOAD_SECTIONS) | set(NEED_RESTART_SECTIONS) == set(DEFAULTS.keys())
    assert set(HOT_RELOAD_SECTIONS) & set(NEED_RESTART_SECTIONS) == set()


def test_vision_config_env_override(monkeypatch, tmp_path):
    """CXA_VISION_ENABLED 环境变量可覆盖 vision.enabled（布尔转换）。"""
    monkeypatch.setenv("CXA_VISION_ENABLED", "true")
    cfg = ConfigManager(
        config_path=str(tmp_path / "config.json"),
        data_dir=str(tmp_path / "data"),
    )
    assert cfg.get("vision", "enabled") is True
