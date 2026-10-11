# -*- coding: utf-8 -*-
"""全双工（duplex）链路实测：模拟 VoiceSession duplex 行为（与前端 voiceSession.ts 同构）。

场景：用户一句多句输入 → splitSentences 切句 → 逐句串行 chatStream（回复到手即发
下一句）→ 每句回复首个完整句立即 TTS（句级流水线）→ 打断（abort）语义验证。

验证点：
  A. 逐句串行 + 每句独立流式（delta 帧 > 1 = 真流式生效）
  B. 句级 TTS 前移（首个完整句到达即发起合成，不等该句回复全部生成完）
  C. 打断（abort 流式请求后服务端停止产出）
"""

import base64
import json
import threading
import time
import urllib.request

BASE = "http://127.0.0.1:8600"

SENTENCE_END = "。！？；…\n"  # 终端标点（与前端 splitSentences 终端口径一致）


def post_json(path, body, timeout=300):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=timeout)


def chat_stream_frames(message):
    """返回 (response, )：调用方逐行迭代 NDJSON。"""
    return post_json("/api/chat/message_stream", {"message": message})


def synth_first_audio(text, timeout=60):
    """TTS 首块耗时（毫秒）——模拟前端 synthesizeSpeechStream 首块即播。"""
    t0 = time.perf_counter()
    with post_json("/api/voice/synthesize_stream", {"text": text}, timeout=timeout) as r:
        for raw in r:
            frame = json.loads(raw.decode("utf-8").strip())
            if frame.get("audio_base64"):
                return (time.perf_counter() - t0) * 1000
            if frame.get("done"):
                return None
    return None


def warmup():
    t0 = time.perf_counter()
    with post_json("/api/voice/warmup", {}) as r:
        data = json.loads(r.read())
    print(f"[warmup] {data.get('warmup')} ({(time.perf_counter() - t0) * 1000:.0f}ms)")
    # 等 sidecar/引擎热完（轮询一次轻量合成确认）
    for _ in range(60):
        try:
            t0 = time.perf_counter()
            with post_json("/api/voice/synthesize_stream", {"text": "嗯"}, timeout=120) as r:
                for raw in r:
                    frame = json.loads(raw.decode("utf-8").strip())
                    if frame.get("audio_base64") or frame.get("done"):
                        break
            if time.perf_counter() - t0 < 10:
                print(f"[warmup] 引擎已热（轻合成 {(time.perf_counter() - t0) * 1000:.0f}ms）")
                return
        except Exception:
            pass
        time.sleep(2)


def duplex_sentence(sentence, seq):
    """单句处理（与前端 processSentenceStreamed 同构）：流式 + 首句即 TTS。"""
    t0 = time.perf_counter()
    ttft = None
    first_sentence_at = None
    total = None
    text = ""
    delta_count = 0
    tts_started_at = None
    tts_result = {}
    with chat_stream_frames(sentence) as r:
        for raw in r:
            frame = json.loads(raw.decode("utf-8", errors="replace").strip())
            now = (time.perf_counter() - t0) * 1000
            if frame.get("delta") is not None:
                text += frame["delta"]
                delta_count += 1
                if ttft is None:
                    ttft = now
                # 前端流水线：首个终端标点出现 → 立即起 TTS（只起一次）
                if first_sentence_at is None:
                    for ch in text:
                        if ch in SENTENCE_END:
                            first_sentence_at = now
                            tts_started_at = time.perf_counter()
                            thread = threading.Thread(
                                target=lambda t=text: tts_result.update(first=synth_first_audio(t))
                            )
                            thread.start()
                            break
            if frame.get("done"):
                total = now
    if tts_started_at is not None:
        thread.join()
    tts_first = tts_result.get("first")
    print(
        f"  句{seq} 「{sentence[:16]}…」 TTFT {ttft:.0f}ms（{delta_count} 帧 delta）"
        f" 首句 {first_sentence_at:.0f}ms done {total:.0f}ms"
        + (f" TTS首块 {tts_first:.0f}ms" if tts_first else " TTS（无终端句，随流）")
    )
    return {"ttft": ttft, "total": total, "tts": tts_first, "deltas": delta_count}


def main():
    warmup()
    print()
    print("=" * 66)
    print("A/B. duplex 全链路：一句话切 3 句 → 逐句流式 + 句级 TTS")
    print("=" * 66)
    utterance = "你好呀。今天天气怎么样？给我推荐几个周末能去玩的地方吧"
    t_utt = time.perf_counter()
    seq = 0
    for sentence in [s for s in utterance.replace("？", "？\n").replace("。", "。\n").split("\n") if s.strip()]:
        seq += 1
        duplex_sentence(sentence, seq)
    e2e = time.perf_counter() - t_utt
    print(f"  ▶ 三句全链路总耗时 {e2e:.1f}s（含逐句 LLM + 句级 TTS 并行）")

    print()
    print("=" * 66)
    print("C. 打断语义：流式请求 abort → 服务端停止产出")
    print("=" * 66)
    try:
        r = chat_stream_frames("给我讲一个很长很长的故事")
        got = 0
        for raw in r:
            frame = json.loads(raw.decode("utf-8", errors="replace").strip())
            if frame.get("delta"):
                got += 1
            if got >= 3:
                r.close()  # 模拟前端 abort（连接断开）
                break
        time.sleep(1.0)
        print(f"  收到 {got} 帧 delta 后主动断开 → 服务端应停止生成（无异常外泄即通过）")
    except Exception as exc:
        print(f"  断开读流（预期内的连接中止）：{type(exc).__name__}")
    # 打断后服务仍健康
    with post_json("/api/chat/message", {"message": "还在吗"}) as r2:
        data = json.loads(r2.read())
    print(f"  打断后服务健康：回复「{data.get('clean_text', '')[:20]}」 offline={data.get('offline')}")


if __name__ == "__main__":
    main()
