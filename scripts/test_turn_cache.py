# -*- coding: utf-8 -*-
"""同请求连发：验证判定 prompt 的 cache-reuse 是否命中（第二次应显著快于第一次）。"""

import json
import time
import urllib.request

BASE = "http://127.0.0.1:8600"


def post(path, body, timeout=120):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=timeout)


for i in range(3):
    t0 = time.perf_counter()
    with post("/api/voice/turn_check", {"text": "今天我们"}) as r:
        data = json.loads(r.read())
    print(f"第{i + 1}次同请求: {data.get('decision')} {(time.perf_counter() - t0) * 1000:.0f}ms")

t0 = time.perf_counter()
with post("/api/chat/message", {"message": "你好呀"}) as r:
    json.loads(r.read())
print(f"主对话参照（更长生成）: {(time.perf_counter() - t0) * 1000:.0f}ms")
