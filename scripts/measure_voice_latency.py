# -*- coding: utf-8 -*-
"""语音交互延迟实测（20261010_模块0_语音交互延迟优化）。

分段计时（API 直注，不依赖麦克风）：
  1. LLM 旧端点 /api/chat/message（攒完整）vs 新端点 /api/chat/message_stream（流式）：
     旧=总时长；新=TTFT（首个 delta 帧）+ done 总时长——首句提前量 = 旧总 - 新TTFT。
  2. TTS /api/voice/synthesize_stream：冷启动（首次，含 sidecar 进程 + MeloTTS 加载）
     vs 热态首块延迟。
  3. ASR /api/voice/transcribe：热态识别耗时（喂 TTS 合成的音频）。
  4. /api/voice/warmup：预热端点行为（引擎已热时秒回）。
"""

import base64
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8600"


def post_json(path, body, timeout=180):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=timeout)


def read_ndjson_lines(resp):
    """逐行读 NDJSON（每读到一行立即返回——TTFT 需要帧级时间戳）。"""
    for raw in resp:
        line = raw.decode("utf-8", errors="replace").strip()
        if line:
            yield json.loads(line)


def fmt(sec):
    return f"{sec * 1000:.0f}ms" if sec < 10 else f"{sec:.2f}s"


def measure_chat():
    print("=" * 64)
    print("① LLM 对话延迟（同 prompt：'在吗'）")
    print("=" * 64)

    # 旧端点：攒完整回复
    t0 = time.perf_counter()
    with post_json("/api/chat/message", {"message": "在吗"}) as r:
        data = json.loads(r.read())
    old_total = time.perf_counter() - t0
    old_text = data.get("clean_text", "")
    print(f"  旧端点 chat/message      总时长 {fmt(old_total)}  回复「{old_text[:24]}」")

    # 新端点：流式（TTFT = 首个 delta 帧到达）
    t0 = time.perf_counter()
    ttft = None
    first_sentence = None
    total = None
    text = ""
    with post_json("/api/chat/message_stream", {"message": "在吗"}) as r:
        for frame in read_ndjson_lines(r):
            now = time.perf_counter() - t0
            if frame.get("delta") is not None:
                text += frame["delta"]
                if ttft is None:
                    ttft = now
                if first_sentence is None:
                    # 首个句尾标点出现 ≈ 前端句级流水线的 speak 时机
                    for i, ch in enumerate(text):
                        if ch in "。！？；，…\n":
                            first_sentence = now
                            break
            if frame.get("done"):
                total = now
                if not ttft:
                    ttft = now
    print(f"  新端点 chat/message_stream")
    print(f"    首增量到达（TTFT）     {fmt(ttft)}")
    print(f"    首个完整句到达（speak）{fmt(first_sentence) if first_sentence else '（无标点，=TTFT 后）'}")
    print(f"    完整回复到达（done）   {fmt(total)}  回复「{text[:24]}」")
    if first_sentence:
        print(f"  ▶ 首句提前量（本轮优化收益）= 旧总时长 - 新首句 = {fmt(old_total - first_sentence)}")
    return old_total, ttft, first_sentence, total


def measure_tts_cold():
    print("=" * 64)
    print("② TTS 合成延迟（冷启动：sidecar 进程 + MeloTTS 引擎首次加载）")
    print("=" * 64)
    t0 = time.perf_counter()
    first_audio = None
    total = None
    with post_json("/api/voice/synthesize_stream", {"text": "你好呀，今天心情怎么样？"}) as r:
        for frame in read_ndjson_lines(r):
            now = time.perf_counter() - t0
            if frame.get("audio_base64") and first_audio is None:
                first_audio = now
                audio_b64 = frame["audio_base64"]
            if frame.get("done"):
                total = now
    print(f"  冷启动 首音频块（首响）  {fmt(first_audio)}")
    print(f"  冷启动 全部块完成        {fmt(total)}")
    return first_audio, total, audio_b64


def measure_warmup():
    print("=" * 64)
    print("③ 语音引擎预热端点（引擎已热 → 秒回 already/started）")
    print("=" * 64)
    t0 = time.perf_counter()
    with post_json("/api/voice/warmup", {}) as r:
        data = json.loads(r.read())
    dt = time.perf_counter() - t0
    print(f"  warmup 响应              {fmt(dt)}  → {data.get('warmup')}")


def measure_tts_hot():
    print("=" * 64)
    print("④ TTS 合成延迟（热态，预热后）")
    print("=" * 64)
    t0 = time.perf_counter()
    first_audio = None
    with post_json("/api/voice/synthesize_stream", {"text": "好呀，想吃什么呢？"}) as r:
        for frame in read_ndjson_lines(r):
            now = time.perf_counter() - t0
            if frame.get("audio_base64") and first_audio is None:
                first_audio = now
            if frame.get("done"):
                total = now
    print(f"  热态 首音频块（首响）    {fmt(first_audio)}")
    print(f"  热态 全部块完成          {fmt(total)}")
    return first_audio, total


def measure_asr(audio_b64):
    print("=" * 64)
    print("⑤ ASR 识别延迟（热态：喂 TTS 合成的同一句音频）")
    print("=" * 64)
    t0 = time.perf_counter()
    with post_json("/api/voice/transcribe", {"audio_base64": audio_b64, "sample_rate": 16000}) as r:
        data = json.loads(r.read())
    dt = time.perf_counter() - t0
    print(f"  ASR 识别耗时             {fmt(dt)}  识别「{data.get('text', '')[:24]}」")
    return dt


def main():
    old_total, ttft, first_sentence, stream_total = measure_chat()
    print()
    cold_first, _, audio_b64 = measure_tts_cold()
    print()
    measure_warmup()
    print()
    hot_first, _ = measure_tts_hot()
    print()
    asr_dt = measure_asr(audio_b64)
    print()
    print("=" * 64)
    print("⑥ 端到端首响估算（duplex：说完一句话 → AI 出声）")
    print("=" * 64)
    vad_tail = 0.7  # 断句尾静音（audioRecorder silenceTimeoutSec 默认）
    if first_sentence is not None and hot_first is not None:
        e2e_new = vad_tail + asr_dt + first_sentence + hot_first
        e2e_old = vad_tail + asr_dt + old_total + hot_first
        print(f"  优化前估算  尾静音 {fmt(vad_tail)} + ASR {fmt(asr_dt)} + LLM整段 {fmt(old_total)} + TTS首块 {fmt(hot_first)} = {fmt(e2e_old)}")
        print(f"  优化后估算  尾静音 {fmt(vad_tail)} + ASR {fmt(asr_dt)} + 首句TTFT {fmt(first_sentence)} + TTS首块 {fmt(hot_first)} = {fmt(e2e_new)}")
        print(f"  ▶ 首响节省  {fmt(e2e_old - e2e_new)}（{100 * (e2e_old - e2e_new) / e2e_old:.0f}%）")
    if ttft is not None and stream_total is not None:
        print(f"  （流式协议开销：TTFT {fmt(ttft)} / done 总时长 {fmt(stream_total)}，模型 0.5B 量级）")


if __name__ == "__main__":
    main()
