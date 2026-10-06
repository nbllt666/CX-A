# -*- coding: utf-8 -*-
"""管理面令牌链路真实冒烟（20261004_模块0_管理面CX-A管理CX-O）。

链路：真 CX-A 后端（8600，令牌模式落盘 api_token.json）
  → 假 CX-O（18601，Bearer 校验）注册上门
  → 管理 Agent 读 logs/api_token.json 拿 CX-A 令牌
  → 带令牌调 /api/fleet/instances/{id}/manifest（CX-A 透传到假 CX-O）。
"""
import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer


class FakeCXO(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        auth = self.headers.get("Authorization") or ""
        if auth != "Bearer fake-cxo-token":
            self.send_response(401)
            self.end_headers()
            return
        body = json.dumps({"instance_id": "smoke-node", "capabilities": {"autonomy": True}}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    srv = HTTPServer(("127.0.0.1", 18601), FakeCXO)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # 0) 管理 Agent 先读令牌文件（CX-A 令牌，后续所有 /api/fleet/* 调用都要带）
    with open(r"C:\CX-A\logs\api_token.json", encoding="utf-8") as f:
        token = json.load(f)["token"]
    cx_a_headers = {"Content-Type": "application/json", "X-Client-Token": token}

    # 1) CX-O 注册上门（无鉴权头，同机回环）
    req = urllib.request.Request(
        "http://127.0.0.1:8600/api/admin/register",
        data=json.dumps({
            "instance_id": "smoke-node",
            "endpoint": "http://127.0.0.1:18601",
            "role": "active",
            "timestamp": "2026-10-04T06:00:00+00:00",
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    print("register:", urllib.request.urlopen(req, timeout=5).read().decode("utf-8"))

    # 1.5) 补录令牌（注册来源无 token → 手动登记同地址即补录）
    req = urllib.request.Request(
        "http://127.0.0.1:8600/api/fleet/instances",
        data=json.dumps({"name": "smoke", "base_url": "http://127.0.0.1:18601",
                         "token": "fake-cxo-token"}).encode("utf-8"),
        headers=cx_a_headers,
        method="POST",
    )
    print("complete:", urllib.request.urlopen(req, timeout=5).read().decode("utf-8"))

    # 2) 带 CX-A 令牌透传读 manifest
    req = urllib.request.Request(
        "http://127.0.0.1:8600/api/fleet/instances/smoke-node/manifest",
        headers={"X-Client-Token": token},
    )
    print("manifest:", urllib.request.urlopen(req, timeout=5).read().decode("utf-8"))
    srv.shutdown()
    print("SMOKE OK")


if __name__ == "__main__":
    main()
