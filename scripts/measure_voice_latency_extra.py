# -*- coding: utf-8 -*-
"""补充测量：长回复流式收益（流式主战场）+ 正常振幅句的 ASR 真实延迟。"""

import json
import time
import urllib.request

BASE = "http://127.0.0.1:8600"


def post_json(path, body, timeout=300):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=timeout)


def read_ndjson_lines(resp):
    for raw in resp:
        line = raw.decode("utf-8", errors="replace").strip()
        if line:
            yield json.loads(line)


print("=" * 64)
print("⑦ 长回复流式收益（开放问题，AI 回多句——流式主战场）")
print("=" * 64)
prompt = "给我讲讲你平时都喜欢做什么呀，多说一点"

t0 = time.perf_counter()
with post_json("/api/chat/message", {"message": prompt}) as r:
    old = json.loads(r.read())
old_total = time.perf_counter() - t0
print(f"  旧端点（攒完整）        {old_total * 1000:.0f}ms  共 {len(old['clean_text'])} 字")

t0 = time.perf_counter()
first_sentence = None
total = None
text = ""
with post_json("/api/chat/message_stream", {"message": prompt}) as r:
    for frame in read_ndjson_lines(r):
        now = time.perf_counter() - t0
        if frame.get("delta") is not None:
            text += frame["delta"]
            if first_sentence is None:
                for ch in text:
                    if ch in "。！？；…\n":  # 终端标点才算真句（逗号太碎，TTS 侧会合并）
                        first_sentence = now
                        break
        if frame.get("done"):
            total = now
print(f"  新端点 首句到达（speak） {first_sentence * 1000:.0f}ms")
print(f"  新端点 完整回复（done）  {total * 1000:.0f}ms  共 {len(text)} 字")
print(f"  ▶ 用户提前听到第一句    {(old_total - first_sentence) * 1000:.0f}ms")

print()
print("=" * 64)
print("⑧ ASR 真实延迟（正常振幅合成句，非塌陷句）")
print("=" * 64)
for text_in in ["你好呀，我在呢", "今天我们一起去公园散步好不好"]:
    t0 = time.perf_counter()
    with post_json("/api/voice/synthesize", {"text": text_in}) as r:
        audio = json.loads(r.read())["audio_base64"]
    synth_dt = time.perf_counter() - t0
    t0 = time.perf_counter()
    with post_json("/api/voice/transcribe", {"audio_base64": audio, "sample_rate": 16000}) as r:
        data = json.loads(r.read())
    asr_dt = time.perf_counter() - t0
    print(f"  「{text_in}」  合成 {synth_dt * 1000:.0f}ms → ASR {asr_dt * 1000:.0f}ms → 「{data.get('text', '')}」")
