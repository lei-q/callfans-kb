#!/usr/bin/env python3
"""
API 契约测试（离线）：kb/api_schema.py 的 OpenAPI spec ↔ kb/serve.py 实现。

不依赖 LLM / 网络 / 真机。跑法：python3 demo/run_api_contract_demo.py

**双后端**：内存 + SQLite 各跑完整一轮（SQLite 是桌面单机发布形态，
/stats 的 events 键等后端差异只有跨后端才能测出）。

防线（docs/05 §11 第 1 步）：
  1. spec 里每个操作都必须被实测调用（覆盖完整性：漏测 = 红）
  2. 响应按 spec 的 JSON Schema 严格校验——**closed schema** 的端点
     （/stats /health /account /decide /plan /report /accounts 等）上，
     实现新增/改名字段直接红；**开放 schema** 的端点（/rules /personas
     /topics /decisions——节点属性的自由扩展口）校验必填字段存在性与
     已声明字段的类型，不设防「新增可选字段」
  3. 错误路径按 spec 的 Error schema 校验（closed）
  4. approved/blocked 分支、幂等、非空记忆数组都是显式断言（不靠运气）

维护：改 serve.py 端点必须同步改 kb/api_schema.py；CI 另有导出 diff 检查。
"""
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from demo.seed import build_seed                                    # noqa: E402
from kb.api_schema import API_VERSION, openapi_spec                 # noqa: E402
from kb.executor import ExecutorAdapter                             # noqa: E402
from kb.serve import make_handler                                   # noqa: E402
from kb.tools import KBTools                                        # noqa: E402

LILY = "acc:xiaohongshu:lily_beauty"
MOMO = "acc:xiaohongshu:momo_beauty"     # 新号：触发规则闸门 → blocked 分支
TOPIC = "topic:beauty.skincare"
_HTTP_METHODS = {"get", "post", "put", "delete", "patch"}

SPEC = openapi_spec()
ERROR_SCHEMA = {"$ref": "#/components/schemas/Error"}
# spec 中每个操作都必须被实测（覆盖完整性）
OPS = {(m.upper(), p) for p, item in SPEC["paths"].items()
       for m in item if m in _HTTP_METHODS}

BASE = None
COVERED = set()
FAILURES = []


def h1(t):
    print("\n" + "=" * 62 + f"\n{t}\n" + "=" * 62)


# ---------------------------------------------------------------------------
# 迷你 JSON Schema 校验器（覆盖本 spec 用到的关键字子集，零依赖）
# ---------------------------------------------------------------------------
def _type_ok(value, t):
    if t == "null":
        return value is None
    if t == "string":
        return isinstance(value, str)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "object":
        return isinstance(value, dict)
    if t == "array":
        return isinstance(value, list)
    return False


def _resolve(schema):
    while "$ref" in schema:
        node = SPEC
        for seg in schema["$ref"].lstrip("#/").split("/"):
            node = node[seg]
        schema = node
    return schema


def _enum_ok(v, enum):
    """严格 enum 匹配：bool 与 int 不互认（避免 True == 1 漏报）。"""
    for x in enum:
        if type(v) is type(x) and v == x:
            return True
        if (isinstance(v, (int, float)) and isinstance(x, (int, float))
                and not isinstance(v, bool) and not isinstance(x, bool)
                and v == x):
            return True
    return False


def validate(value, schema, path="$"):
    """返回错误列表（空 = 通过）。"""
    schema = _resolve(schema)
    errs = []
    if "oneOf" in schema:
        # 严格 oneOf：恰好匹配一个分支（同时匹配两个 = 歧义，报错）
        matches = sum(1 for sub in schema["oneOf"]
                      if not validate(value, sub, path))
        if matches != 1:
            errs.append(f"{path}: oneOf 匹配 {matches} 个分支（应为恰好 1）")
        return errs
    if "allOf" in schema:
        # 合并语义（与 openapi-typescript 一致）：object 成员的 properties/
        # required 取并集；任一成员 closed 则整体按并集闭合。
        props, req, closed = {}, [], False
        for sub in schema["allOf"]:
            sub = _resolve(sub)
            if "properties" in sub or sub.get("type") == "object":
                props.update(sub.get("properties", {}))
                req += sub.get("required", [])
                if sub.get("additionalProperties") is False:
                    closed = True
            else:
                errs += validate(value, sub, path)
        merged = {"type": "object", "properties": props, "required": req}
        if closed:
            merged["additionalProperties"] = False
        return errs + validate(value, merged, path)
    if "enum" in schema and not _enum_ok(value, schema["enum"]):
        errs.append(f"{path}: {value!r} 不在 enum {schema['enum']}")
    t = schema.get("type")
    if t:
        types = t if isinstance(t, list) else [t]
        if not any(_type_ok(value, x) for x in types):
            return errs + [f"{path}: 期望类型 {t}，实际 {type(value).__name__}"]
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in value:
                errs.append(f"{path}: 缺必填字段 {k}")
        ap = schema.get("additionalProperties", True)
        for k, v in value.items():
            if k in props:
                errs += validate(v, props[k], f"{path}.{k}")
            elif ap is False:
                errs.append(f"{path}.{k}: 实现返回了 spec 未声明的字段（漂移）")
            elif isinstance(ap, dict):
                errs += validate(v, ap, f"{path}.{k}")
    elif isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errs += validate(item, schema["items"], f"{path}[{i}]")
    return errs


# ---------------------------------------------------------------------------
# HTTP 小客户端
# ---------------------------------------------------------------------------
def call(method, path, body=None):
    from urllib.parse import quote
    url = quote(BASE + path, safe=":/?&=%")   # 中文查询词百分号编码
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    try:
        return status, json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return status, {"_raw": raw.decode("utf-8", "replace")}


def _op_schema(path, method, status):
    try:
        op = SPEC["paths"][path][method.lower()]
        return (op["responses"][str(status)]["content"]
                .get("application/json", {}).get("schema"))
    except KeyError:
        return None


def check(method, path, query="", body=None, expect=200, validate_body=True,
          actual=None):
    """按 spec 校验一个操作：状态码 + 响应体 schema + 覆盖登记。

    path 用 spec 模板形式（如 /maintenance/{level}），actual 传实际请求路径。
    """
    COVERED.add((method, path))
    url = (actual or path) + (f"?{query}" if query else "")
    status, payload = call(method, url, body)
    label = f"{method} {path}"
    if status != expect:
        FAILURES.append(f"{label}: 期望 {expect} 实际 {status}: {payload}")
        return payload
    if not validate_body:
        return payload
    schema = _op_schema(path, method, expect)
    if schema is None:
        return payload
    errs = validate(payload, schema)
    if errs:
        FAILURES.append(f"{label}: 响应不符合契约:\n    " + "\n    ".join(errs))
    return payload


def negative(method, path, body=None, expect=404, note="", validate_error=True):
    """负例：状态码 + Error schema 校验（spec 声明了该码才校验 schema）。"""
    status, payload = call(method, path, body)
    label = f"[负例] {method} {path.split('?')[0]} {note}"
    if status != expect:
        FAILURES.append(f"{label}: 期望 {expect} 实际 {status}: {payload}")
        return
    if validate_error:
        errs = validate(payload, ERROR_SCHEMA)
        if errs:
            FAILURES.append(f"{label}: 错误体不符合 Error schema:\n    "
                            + "\n    ".join(errs))


# ---------------------------------------------------------------------------
def run_suite(tag, store, queue, tools):
    """在一个后端上跑完整契约套件。"""
    global BASE, COVERED
    COVERED = set()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                make_handler(tools, queue,
                                             ExecutorAdapter(tools)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    BASE = f"http://127.0.0.1:{httpd.server_address[1]}"
    print(f"  测试服务: {BASE}（{tag} 后端）")
    try:
        h1(f"[{tag}] 1. 只读端点")
        check("GET", "/", validate_body=False)
        check("GET", "/index.html", validate_body=False)
        check("GET", "/health")
        check("GET", "/stats")
        check("GET", "/personas")
        check("GET", "/topics")
        check("GET", "/rules")
        check("GET", "/decisions", query=f"account={LILY}")
        check("GET", "/decisions")                        # 全账号合并
        check("GET", "/accounts")
        ov = check("GET", "/account", query=f"account={LILY}")
        check("GET", "/memory", query=f"account={LILY}")
        hits = check("GET", "/search", query="q=防晒&k=3")
        print(f"  search 命中 {len(hits.get('hits', []))} 条；"
              f"此刻 memories={len(ov.get('memories', []))}（记忆尚空，2.5 步复检）")

        h1(f"[{tag}] 2. 写端点（分支显式断言）")
        r = check("POST", "/accounts", body={
            "platform": "xiaohongshu", "handle": "contract_tester",
            "persona": {"name": "契约测试员", "language_style": "自然"}})
        print(f"  创建账号: {r.get('account_id')} events={r.get('events')}")
        ir = check("POST", "/accounts/import", body={"accounts": [
            {"platform": "xiaohongshu", "handle": "import_ok"},
            {"platform": "xiaohongshu"},                  # 缺 handle → error 项
            "notadict"]})                                 # 非 dict → error 项不中断
        print(f"  导入: {ir.get('imported')}/{ir.get('total')}（含非法项不 500）")
        d1 = check("POST", "/decide", body={
            "account_id": LILY, "action_type": "post", "topic_id": TOPIC})
        d2 = check("POST", "/decide", body={
            "account_id": MOMO, "action_type": "post", "topic_id": TOPIC})
        if d1.get("status") != "approved":
            FAILURES.append(f"分支断言: lily 决策应 approved，实际 {d1.get('status')}")
        if d2.get("status") != "blocked" or not d2.get("violations"):
            FAILURES.append(f"分支断言: momo 决策应 blocked+violations，实际 {d2.get('status')}")
        print(f"  决策: lily={d1.get('status')} momo={d2.get('status')}"
              f"（blocked 分支: {len(d2.get('violations', []))} 违规）")
        p = check("POST", "/plan", body={
            "account_id": LILY, "action_type": "post", "topic_id": TOPIC,
            "job_id": "contract-job-1", "task_params": {"images_nums": 1}})
        if p.get("status") != "approved" or not p.get("command"):
            FAILURES.append(f"分支断言: plan 应 approved+command，实际 {p.get('status')}")
        print(f"  计划: {p['status']} argv={len(p.get('command') or [])} 参数")
        # digest 带承诺话术：让 PromiseScan.notes 非空（字段级覆盖）
        w = check("POST", "/report", body={"result": {
            "action_id": "act:contract-job-1", "account_id": LILY,
            "action": "post", "topic_id": TOPIC,
            "new_post_id": "post:xiaohongshu:contract-1",
            "digest": "契约测试帖，下次分享实测全流程",
            "decision_id": p["decision"]["decision_id"]}})
        if w["promises"]["promises_found"] < 1 or not w["promises"].get("notes"):
            FAILURES.append(f"分支断言: report 承诺扫描应为 1，实际 {w['promises']}")
        print(f"  回写: {w['write']} 承诺扫描: {w['promises']['promises_found']} 条")
        m = check("POST", "/maintenance/{level}", actual="/maintenance/daily",
                  body={})
        if "accounts" not in m or "rolled" not in m:
            FAILURES.append(f"分支断言: daily 维护应含 accounts/rolled，实际 {m}")
        print(f"  维护: {m}")

        h1(f"[{tag}] 2.5 记忆非空复检（RecalledMemory/Episode/OpenPromise 字段级覆盖）")
        check("POST", "/maintenance/{level}", actual="/maintenance/weekly",
              body={})                    # 周卷积 → Episode + 固化 → MemoryNote
        ov2 = check("GET", "/account", query=f"account={LILY}&q=实测")
        for key in ("memories", "episodes"):
            n = len(ov2.get(key, []))
            if n == 0:
                FAILURES.append(f"覆盖断言: /account.{key} 应非空（{key} 的 item schema 从未被数据校验）")
        n_p = len(ov2.get("promises", []))
        if n_p == 0:
            FAILURES.append("覆盖断言: /account.promises 应非空（OpenPromise 从未被数据校验）")
        print(f"  复检: memories={len(ov2.get('memories', []))} "
              f"episodes={len(ov2.get('episodes', []))} promises={n_p}")

        h1(f"[{tag}] 3. 幂等与错误路径（负例，Error schema 校验）")
        w2 = call("POST", "/report", body={"result": {
            "action_id": "act:contract-job-1", "account_id": LILY,
            "action": "post", "topic_id": TOPIC,
            "new_post_id": "post:xiaohongshu:contract-1", "digest": "契约测试帖"}})
        if not (w2[1].get("write", {}).get("duplicates", 0) > 0):
            FAILURES.append(f"幂等断言: 同 action_id 重报应全 duplicate，实际 {w2[1].get('write')}")
        print(f"  重报去重: {w2[1]['write']}")
        negative("GET", "/account?account=acc:xiaohongshu:nope", expect=404, note="账号不存在")
        negative("GET", "/memory?account=acc:xiaohongshu:nope", expect=404, note="账号不存在")
        negative("GET", "/nope", expect=404, note="未知路径")
        negative("POST", "/nope", body={}, expect=404, note="未知路径")
        negative("POST", "/decide", body={"action_type": "post"}, expect=400, note="缺 account_id")
        negative("POST", "/plan", body={"account_id": LILY}, expect=400, note="缺 action_type")
        negative("POST", "/maintenance/nope", body={}, expect=404, note="未知维护级别")
        # spec 已声明的 500：畸形数值参数（当前实现语义，见 spec 描述）
        negative("GET", "/search?q=x&k=abc", expect=500, note="k 非整数")
        negative("GET", "/decisions?limit=abc", expect=500, note="limit 非整数")
    finally:
        httpd.shutdown()

    h1(f"[{tag}] 4. 覆盖完整性")
    missed = OPS - COVERED
    print(f"  已实测 {len(COVERED & OPS)}/{len(OPS)} 个操作")
    if missed:
        FAILURES.append(f"spec 中有操作未被实测: {sorted(missed)}")
    extra = COVERED - OPS
    if extra:
        FAILURES.append(f"实测了 spec 未声明的操作: {sorted(extra)}")


def main():
    h1(f"0. 契约元数据（API_VERSION={API_VERSION}，独立于应用发布 tag）")
    assert SPEC["openapi"] == "3.1.0" and SPEC["info"]["version"] == API_VERSION
    print(f"  spec 操作数: {len(OPS)}  端点数: {len(SPEC['paths'])}")

    import tempfile
    from kb.factory import open_queue

    # 后端 1：内存（CI/测试形态）
    store, queue = build_seed()
    run_suite("memory", store, queue, KBTools(store, queue))

    # 后端 2：SQLite（桌面单机发布形态；/stats.events 等后端差异在此覆盖）
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        from kb.store_sqlite import SQLiteKnowledgeStore
        sq = SQLiteKnowledgeStore(path=tmp.name)
        sq_queue = open_queue(sq)
        build_seed(store=sq, queue=sq_queue)
        run_suite("sqlite", sq, sq_queue, KBTools(sq, sq_queue))
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(tmp.name + suffix)
            except OSError:
                pass

    print("=" * 62)
    if FAILURES:
        print(f"契约测试失败 {len(FAILURES)} 处:")
        for f in FAILURES:
            print(f"  ✗ {f}")
        sys.exit(1)
    print(f"契约测试通过: 双后端 × {len(OPS)} 操作全部实测且响应符合 spec")
    print("防漂移: closed schema 端点对新增/改名/删除字段直接红；开放 schema 端点")
    print("（rules/personas/topics/decisions——节点属性扩展口）校验必填与已声明字段；")
    print("CI 另有 openapi.json 重导出 + TS 类型重生成双 diff 检查")


if __name__ == "__main__":
    main()
