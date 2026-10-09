"""
本地 HTTP API（kb serve）：桌面 GUI / 远程客户端的统一入口。

零第三方依赖（http.server 实现）。定位是本机/内网 API，不是公网服务——
没有鉴权与限流，公网暴露请加反向代理。

端点：
  GET  /health                          存活检查 + 后端
  GET  /stats                           图谱统计
  GET  /accounts                        矩阵账号及状态
  GET  /account?account=...             单账号状态（persona/state/recall）
  GET  /memory?account=...              人可读记忆摘要（Markdown）
  GET  /search?q=...&k=5                知识检索
  POST /decide    {account_id, action_type, topic_id?, budget_tokens?}
  POST /plan      {account_id, action_type, topic_id?, job_id?, task_params?}
  POST /report    {result: {...}}       执行结果回写（含承诺扫描）
  POST /maintenance/{level}             维护任务（body 可选 {"llm": true}）
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def make_handler(tools, queue, adapter):
    lock = threading.RLock()        # 内存后端线程安全兜底；sqlite/neo4j 自带锁

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):          # 静默访问日志
            pass

        # ---------------- 基础 ----------------
        def _send(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path in ("/", "/index.html"):
                return self._static()
            try:
                if u.path == "/health":
                    return self._send(200, {"ok": True,
                                            "backend": type(tools.store).__name__})
                if u.path == "/stats":
                    with lock:
                        return self._send(200, tools.store.stats())
                if u.path == "/accounts":
                    from kb.maintenance import matrix_accounts
                    with lock:
                        out = []
                        for a in matrix_accounts(tools.store):
                            try:
                                name = tools.get_persona(a).get("name", a)
                            except Exception:
                                name = a
                            out.append({"account_id": a, "name": name,
                                        **tools.get_account_state(a)})
                    return self._send(200, {"accounts": out})
                if u.path == "/account":
                    with lock:
                        acc = q["account"]
                        from kb.rollup import open_promises
                        episodes = []
                        for _, dst, _ in tools.store.out_edges(acc, "HAS_EPISODE"):
                            n = tools.store.get_node(dst)
                            if n:
                                episodes.append({"id": dst, **n["props"]})
                        episodes.sort(key=lambda x: x.get("ts", 0), reverse=True)
                        return self._send(200, {
                            "persona": tools.get_persona(acc),
                            "state": tools.get_account_state(acc),
                            "promises": open_promises(tools.store, acc),
                            "memories": tools.recall_memory(
                                acc, q.get("q", ""))["memories"],
                            "episodes": episodes,
                            "digest": tools.memory_digest(acc)})
                if u.path == "/memory":
                    with lock:
                        return self._send(200, {"account": q["account"],
                                                "digest": tools.memory_digest(q["account"])})
                if u.path == "/search":
                    with lock:
                        return self._send(200, {"hits": tools.search_knowledge(
                            q.get("q", ""), k=int(q.get("k", 5)))})
                return self._send(404, {"error": f"unknown path {u.path}"})
            except KeyError as e:
                return self._send(404, {"error": str(e)})
            except Exception as e:
                return self._send(500, {"error": str(e)})

        def _static(self):
            import os
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "static", "index.html")
            try:
                body = open(path, "rb").read()
            except OSError:
                return self._send(404, {"error": "static/index.html 不存在"})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            u = urlparse(self.path)
            try:
                body = self._body()
                if u.path == "/decide":
                    with lock:
                        d = tools.decide(body["account_id"],
                                         body["action_type"],
                                         body.get("topic_id"),
                                         budget_tokens=body.get("budget_tokens"))
                    return self._send(200, d)
                if u.path == "/plan":
                    with lock:
                        plan = adapter.plan(body["account_id"],
                                            body["action_type"],
                                            body.get("topic_id"),
                                            job_id=body.get("job_id"),
                                            task_params=body.get("task_params"))
                    if plan["status"] != "approved":
                        return self._send(200, plan)
                    device_env = body.get("device_env") or {
                        "appium_url": "<appium_url>", "deviceName": "<device>",
                        "systemPort": "<port>", "adbPort": "<adb>",
                        "platformVersion": "<ver>"}
                    cmd = adapter.build_command(plan["spec"], device_env)
                    return self._send(200, {**plan, "command": cmd})
                if u.path == "/report":
                    result = body.get("result") or body
                    with lock:
                        write = tools.submit_result(result)
                        tracked = {"promises_found": 0}
                        if result.get("action") == "post" and result.get("new_post_id"):
                            tracked = tools.track_promises(
                                result["account_id"],
                                result.get("digest", ""),
                                post_id=result["new_post_id"])
                    return self._send(200, {"write": write, "promises": tracked})
                if u.path.startswith("/maintenance/"):
                    from kb.maintenance import (daily_maintenance,
                                                monthly_maintenance,
                                                weekly_maintenance,
                                                yearly_maintenance)
                    level = u.path.rsplit("/", 1)[1]
                    fn = {"daily": daily_maintenance, "weekly": weekly_maintenance,
                          "monthly": monthly_maintenance,
                          "yearly": yearly_maintenance}.get(level)
                    if fn is None:
                        return self._send(404, {"error": f"unknown level {level}"})
                    with lock:
                        out = fn(tools)
                    return self._send(200, {k: v for k, v in out.items()
                                            if k not in ("results", "rollups",
                                                         "consolidations")})
                return self._send(404, {"error": f"unknown path {u.path}"})
            except KeyError as e:
                return self._send(400, {"error": f"missing field: {e}"})
            except Exception as e:
                return self._send(500, {"error": str(e)})

    return Handler


def run(host="127.0.0.1", port=8765):
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    from kb.executor import ExecutorAdapter
    from kb.factory import open_kb, open_queue
    from kb.tools import KBTools

    store, queue = open_kb()
    tools = KBTools(store, queue)
    adapter = ExecutorAdapter(tools)
    httpd = ThreadingHTTPServer((host, port), make_handler(tools, queue, adapter))
    print(f"kb serve  http://{host}:{port}  "
          f"backend={type(store).__name__}  （Ctrl+C 停止）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
