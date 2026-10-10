"""
本地 HTTP API（kb serve）：桌面 GUI / 远程客户端的统一入口。

零第三方依赖（http.server 实现）。定位是本机/内网 API，不是公网服务——
没有鉴权与限流，公网暴露请加反向代理。

**契约**：本文件只列端点速览；权威定义在 kb/api_schema.py（OpenAPI 3.1，
导出 docs/api/openapi.json，TS 类型由它生成）。改端点必须同步改 spec，
demo/run_api_contract_demo.py 与 CI 漂移检查负责打红。

端点速览：
  GET  /            /index.html        Web 控制台（静态单页）
  GET  /health                          存活检查 + 后端
  GET  /stats                           图谱统计
  GET  /personas                        人设及关联账号
  GET  /topics                          话题
  GET  /rules                           规则（含确证/灰度字段）
  GET  /decisions?account=&limit=       决策流（DECIDED_VIA 可解释性视图）
  GET  /accounts                        矩阵账号及状态
  GET  /account?account=&q=             单账号全景（persona/state/promises/…）
  GET  /memory?account=                 人可读记忆摘要（Markdown）
  GET  /search?q=&k=                    知识检索
  POST /accounts                        创建账号（含人设）
  POST /accounts/import                 批量导入
  POST /decide                          两段式决策
  POST /plan                            决策→执行规格（+argv）
  POST /report                          执行结果回写（幂等 + 承诺扫描）
  POST /maintenance/{level}             维护任务（body 可选、当前被忽略）
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
                if u.path == "/personas":
                    with lock:
                        out = []
                        for pid in tools.store.node_ids("Persona"):
                            n = tools.store.get_node(pid)
                            accounts = [s for _, s, _
                                        in tools.store.in_edges(pid, "HAS_PERSONA")]
                            out.append({"persona_id": pid,
                                        "props": n["props"] if n else {},
                                        "accounts": accounts})
                    return self._send(200, {"personas": out})
                if u.path == "/topics":
                    with lock:
                        out = [{"topic_id": t,
                                "label": tools.store.get_node(t)["props"].get("label")}
                               for t in tools.store.node_ids("Topic")
                               if tools.store.get_node(t)]
                    return self._send(200, {"topics": out})
                if u.path == "/rules":
                    with lock:
                        out = []
                        for rid in tools.store.node_ids("Rule"):
                            n = tools.store.get_node(rid)
                            if n:
                                out.append({"rule_id": rid, **n["props"]})
                    return self._send(200, {"rules": out})
                if u.path == "/decisions":
                    with lock:
                        acc = q.get("account")
                        limit = int(q.get("limit", 20))
                        if acc:
                            items = tools.recent_decisions(acc, limit)
                        else:              # 全账号合并决策流
                            from kb.maintenance import matrix_accounts
                            items = []
                            for a in matrix_accounts(tools.store):
                                items += tools.recent_decisions(a, limit)
                            items.sort(key=lambda x: x.get("ts", 0), reverse=True)
                            items = items[:limit]
                    return self._send(200, {"decisions": items})
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
                        subj = tools.store.subject_of(acc)
                        episodes = []
                        for _, dst, _ in tools.store.out_edges(subj, "HAS_EPISODE"):
                            n = tools.store.get_node(dst)
                            if n:
                                episodes.append({"id": dst, **n["props"]})
                        episodes.sort(key=lambda x: x.get("ts", 0), reverse=True)
                        return self._send(200, {
                            "subject_id": subj,
                            "persona": tools.get_persona(acc),
                            "state": tools.get_account_state(acc),
                            "promises": open_promises(tools.store, subj),
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
                if u.path == "/accounts":
                    with lock:
                        r = tools.create_account(
                            platform=body["platform"], handle=body["handle"],
                            name=body.get("name"),
                            persona_id=body.get("persona_id"),
                            persona_props=body.get("persona"),
                            interests=body.get("interests"),
                            device=body.get("device"),
                            subject_id=body.get("subject_id"))
                    return self._send(200, r)
                if u.path == "/accounts/import":
                    with lock:
                        results = []
                        for a in body.get("accounts", []):
                            try:
                                results.append(tools.create_account(
                                    platform=a["platform"], handle=a["handle"],
                                    name=a.get("name"),
                                    persona_id=a.get("persona_id"),
                                    persona_props=a.get("persona"),
                                    interests=a.get("interests"),
                                    device=a.get("device"),
                                    subject_id=a.get("subject_id")))
                            except Exception as e:
                                # 非法项（含非 dict）归入 error 项，不中断批量导入
                                results.append({"error": str(e),
                                                "handle": a.get("handle")
                                                if isinstance(a, dict) else None})
                    ok = sum(1 for r in results if r.get("created"))
                    return self._send(200, {"imported": ok,
                                            "total": len(results),
                                            "results": results})
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


def _port_busy_action(host, port, open_browser) -> int:
    """端口被占时的处理：是已有 callfans 实例则复用（开控制台后退出），
    是其他程序则给出可行动的报错。返回进程退出码（不抛异常、不打 traceback）。"""
    import urllib.request
    url = f"http://{host}:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            is_callfans = r.status == 200
    except Exception:
        is_callfans = False
    if is_callfans:
        print(f"端口 {port} 上已有 callfans 实例在运行：http://{host}:{port}/")
        if open_browser:
            import webbrowser
            webbrowser.open(f"http://{host}:{port}/")
        print("复用现有实例，不重复启动。指定其他端口：serve --port 8766")
        return 0
    print(f"错误：端口 {port} 已被其他程序占用（且不是 callfans）。\n"
          f"  查看占用者：lsof -nP -iTCP:{port} -sTCP:LISTEN\n"
          f"  换端口启动：callfans serve --port 8766")
    return 1


def run(host="127.0.0.1", port=8765, open_browser=None):
    import errno
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    from kb.executor import ExecutorAdapter
    from kb.factory import open_kb, open_queue
    from kb.tools import KBTools

    if open_browser is None:
        open_browser = (os.environ.get("CALLFANS_NO_BROWSER", "") == ""
                        and sys.stdout.isatty())   # 无头/管道环境不自动开浏览器
    store, queue = open_kb()
    tools = KBTools(store, queue)
    adapter = ExecutorAdapter(tools)
    try:
        httpd = ThreadingHTTPServer((host, port),
                                    make_handler(tools, queue, adapter))
    except OSError as e:
        if getattr(e, "errno", None) == errno.EADDRINUSE:
            return _port_busy_action(host, port, open_browser)
        raise
    print(f"kb serve  http://{host}:{port}  "
          f"backend={type(store).__name__}  （Ctrl+C 停止）")
    if open_browser:
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(
            f"http://{host}:{port}/")).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
