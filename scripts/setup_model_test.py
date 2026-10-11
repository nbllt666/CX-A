# -*- coding: utf-8 -*-
"""临时配置：挂 Gemma E4B-Q6（turn_check 实测用）。"""

import json

p = r"C:\CX-A\data\config.json"
c = json.load(open(p, encoding="utf-8"))
c.setdefault("local_llm", {})
c["local_llm"]["enabled"] = True
c["local_llm"]["model_path"] = (
    r"C:\CX-A\release\_install_voice_20260930\data\local_llm\gemma-4-E4B-it-Q6_K.gguf"
)
json.dump(c, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("config: local_llm.enabled=True(E4B-Q6)")
