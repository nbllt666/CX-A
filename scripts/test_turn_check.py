# -*- coding: utf-8 -*-
"""turn_check 主模型话轮判定实测：判定延迟（含 cache 命中前后）与三态准确性。"""

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


# 等服务就绪 + 预热一轮对话（让主对话前缀进 cache）
for _ in range(30):
    try:
        with urllib.request.urlopen(f"{BASE}/api/health", timeout=2) as r:
            if r.status == 200:
                break
    except Exception:
        time.sleep(2)

with post("/api/chat/message", {"message": "你好"}) as r:
    json.loads(r.read())
print("[warm] 主对话前缀已进 cache\n")

cases = [
    ("今天我们", "CONTINUE"),  # 半截话
    ("然后我还想说的就是那个", "CONTINUE"),  # 悬停词收尾
    ("今天天气怎么样？", "SEND"),  # 完整问句
    ("陪我说说话吧", "SEND"),  # 完整请求
    ("嗯嗯啊哦", "IGNORE"),  # 无意义音节
]
for text, expect in cases:
    t0 = time.perf_counter()
    with post("/api/voice/turn_check", {"text": text}) as r:
        data = json.loads(r.read())
    dt = (time.perf_counter() - t0) * 1000
    got = data.get("decision")
    mark = "PASS" if got == expect else f"DIFF(expect {expect})"
    print(f"  {mark:22s} 「{text}」 → {got}（{dt:.0f}ms）{data.get('reason', '')[:30]}")
