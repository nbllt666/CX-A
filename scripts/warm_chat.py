# -*- coding: utf-8 -*-
"""warm chat：等待本地大模型加载完成（不计入延迟数据）。"""

import json
import time
import urllib.request

req = urllib.request.Request(
    "http://127.0.0.1:8600/api/chat/message",
    data=json.dumps({"message": "你好"}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
t0 = time.perf_counter()
with urllib.request.urlopen(req, timeout=600) as r:
    data = json.loads(r.read())
print(
    f"warm chat {time.perf_counter() - t0:.1f}s  offline={data.get('offline')}  "
    f"text={data.get('clean_text', '')[:40]!r}"
)
