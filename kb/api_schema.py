"""
HTTP API 契约（OpenAPI 3.1）：kb/serve.py 全部端点的唯一权威描述。

定案背景见 docs/05 §7.1 / §11 第 1 步：Python 内核 + TS 前端，HTTP API 是
跨语言 ABI——spec 是事实源，TS 类型由它生成，不手写两遍。

用法：
  python3 -m kb.api_schema > docs/api/openapi.json   # 导出 spec（入库）

防漂移（双向）：
  - demo/run_api_contract_demo.py：起真实 serve，逐端点按 spec 校验
    响应（路由存在 / 状态码 / 必填字段 / 类型），spec 里的每个操作都必须被
    实测覆盖
  - CI：重新导出 openapi.json 与 TS 类型后 git diff --exit-code

维护纪律：改 serve.py 的任何端点（新增/删除/改字段）必须同步改本文件，
契约测试与 CI 漂移检查会把两边不一致打红。
"""
from __future__ import annotations

import json

# API 版本（语义化）：破坏性变更（删字段/改语义/删端点）bump 主版本；
# 新增可选字段/新增端点 bump 次版本；纯修正 bump 补丁版本。
# 注意：这是「API 契约版本」，独立于应用发布 tag（v*）演进，两者无对应关系。
API_VERSION = "0.2.0"

_ERROR = {"type": "object", "required": ["error"],
          "properties": {"error": {"type": "string"}},
          "additionalProperties": False}

# ---------------------------------------------------------------------------
# spec 构造小工具（定义在常量区之前，供模块级 schema 常量使用）
# ---------------------------------------------------------------------------
def _obj(required, props, additional=False, description=None):
    s = {"type": "object", "properties": props}
    if required:
        s["required"] = list(required)
    if not additional:
        s["additionalProperties"] = additional
    if description:
        s["description"] = description
    return s


def _resp(schema_ref, description="OK"):
    return {"description": description, "content": {
        "application/json": {"schema": {"$ref": f"#/components/schemas/{schema_ref}"}}}}


def _q_account():
    return {"name": "account", "in": "query", "required": True,
            "schema": {"type": "string"},
            "example": "acc:xiaohongshu:lily_beauty"}


# 执行结果 schema：/report 的包裹形态（body.result）与平铺形态（body 即 result）共用
_RESULT_SCHEMA = _obj(["action_id", "account_id", "action"], {
    "action_id": {"type": "string"}, "account_id": {"type": "string"},
    "action": {"type": "string"}, "topic_id": {"type": "string"},
    "new_post_id": {"type": "string"}, "digest": {"type": "string"},
    "outcome": {"type": "string"}, "ts": {"type": "number"},
    "stats": {"type": "object", "additionalProperties": True},
    "raw_text": {"type": "string"},
    "decision_id": {"type": "string"},
    "knowledge_refs": {"type": "array", "items": {"type": "string"}},
}, additional=True)

# AccountState 与 /accounts 列表项共用的字段集（列表项 = 状态 + name）
_STATE_PROPS = {
    "account_id": {"type": "string"},
    "status": {"type": ["string", "null"]},
    "risk_level": {"type": ["string", "null"]},
    "age_days": {"type": "number"},
    "cooldown_remaining_h": {"type": "number"},
    "today_action_counts": {"type": "object",
                            "additionalProperties": {"type": "integer"}},
    "last_summary": {"type": ["string", "null"]},
    "recent_actions": {"type": "array", "items": {
        "type": "object", "required": ["action"],
        "properties": {"action": {"type": "string"},
                       "topic_id": {"type": ["string", "null"]},
                       "digest": {"type": "string"}}}},
}


def openapi_spec() -> dict:
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "callfans KB HTTP API",
            "version": API_VERSION,
            "description": (
                "本地/内网 API（`python3 -m kb serve`）。零鉴权与限流，"
                "公网暴露需加反向代理。Web 控制台（kb/static/index.html）"
                "与桌面壳都是本 API 的消费者。"
            ),
        },
        "servers": [{"url": "http://127.0.0.1:8765"}],
        "paths": {
            "/": {
                "get": {
                    "summary": "Web 控制台（静态单页）",
                    "responses": {
                        "200": {"description": "text/html",
                                "content": {"text/html": {"schema": {"type": "string"}}}},
                        "404": {"description": "静态文件缺失",
                                "content": {"application/json": {"schema": {
                                    "$ref": "#/components/schemas/Error"}}}},
                    },
                },
            },
            "/index.html": {
                "get": {
                    "summary": "Web 控制台（“/”的别名，静态单页）",
                    "responses": {"200": {"description": "text/html",
                                          "content": {"text/html": {"schema": {"type": "string"}}}}},
                },
            },
            "/health": {
                "get": {
                    "summary": "存活检查 + 存储后端",
                    "responses": {"200": _resp("Health")},
                },
            },
            "/stats": {
                "get": {
                    "summary": "图谱统计",
                    "responses": {"200": _resp("GraphStats"), "500": _resp("Error", "服务端异常")},
                },
            },
            "/personas": {
                "get": {
                    "summary": "全部人设及关联账号",
                    "responses": {"200": _resp("PersonaList"), "500": _resp("Error", "服务端异常")},
                },
            },
            "/topics": {
                "get": {
                    "summary": "全部话题",
                    "responses": {"200": _resp("TopicList"), "500": _resp("Error", "服务端异常")},
                },
            },
            "/rules": {
                "get": {
                    "summary": "全部规则（含确证状态/灰度字段）",
                    "responses": {"200": _resp("RuleList"), "500": _resp("Error", "服务端异常")},
                },
            },
            "/decisions": {
                "get": {
                    "summary": "决策流（近期行为 + DECIDED_VIA 引用，可解释性视图）",
                    "parameters": [
                        {"name": "account", "in": "query",
                         "schema": {"type": "string"},
                         "description": "缺省 = 全账号合并，按 ts 倒序"},
                        {"name": "limit", "in": "query",
                         "schema": {"type": "integer", "default": 20}},
                    ],
                    "responses": {"200": _resp("DecisionList"), "500": _resp("Error", "服务端异常")},
                },
            },
            "/accounts": {
                "get": {
                    "summary": "矩阵账号及状态（账号墙）",
                    "responses": {"200": _resp("AccountList"), "500": _resp("Error", "服务端异常")},
                },
                "post": {
                    "summary": "创建账号（含人设）",
                    "requestBody": {"required": True, "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/CreateAccountInput"}}}},
                    "responses": {
                        "200": _resp("CreateAccountResult"),
                        "400": _resp("Error", "缺必填字段"),
                        "500": _resp("Error"),
                    },
                },
            },
            "/accounts/import": {
                "post": {
                    "summary": "批量导入账号（单项失败不中断）",
                    "requestBody": {"required": True, "content": {
                        "application/json": {"schema": {"type": "object",
                                                        # accounts 缺省 = 空导入（200, 0/0）
                                                        "properties": {"accounts": {"type": "array", "items": {"$ref": "#/components/schemas/CreateAccountInput"}}}}}}},
                    "responses": {"200": _resp("ImportResult"),
                                      "500": _resp("Error", "列表项结构异常（当前实现）")},
                },
            },
            "/account": {
                "get": {
                    "summary": "单账号全景（人设/状态/承诺/召回/情景/摘要）",
                    "parameters": [
                        {"name": "account", "in": "query", "required": True,
                         "schema": {"type": "string"}, "example": "acc:xiaohongshu:lily_beauty"},
                        {"name": "q", "in": "query",
                         "schema": {"type": "string"}, "description": "召回查询词（可选）"},
                    ],
                    "responses": {
                        "200": _resp("AccountOverview"),
                        "500": _resp("Error", "服务端异常"),
                        "404": _resp("Error", "缺 account 参数或账号不存在"),
                    },
                },
            },
            "/memory": {
                "get": {
                    "summary": "人可读记忆摘要（Markdown）",
                    "parameters": [_q_account()],
                    "responses": {
                        "200": _resp("MemoryDigest"),
                        "500": _resp("Error", "服务端异常"),
                        "404": _resp("Error", "缺 account 参数或账号不存在"),
                    },
                },
            },
            "/search": {
                "get": {
                    "summary": "知识检索（bigram 倒排，中文召回）",
                    "parameters": [
                        {"name": "q", "in": "query", "schema": {"type": "string"},
                         "description": "查询词（空 = 空结果）"},
                        {"name": "k", "in": "query",
                         "schema": {"type": "integer", "default": 5}},
                    ],
                    "responses": {"200": _resp("SearchResult"),
                                  "500": _resp("Error", "畸形查询参数（如 k 非整数）当前返 500")},
                },
            },
            "/decide": {
                "post": {
                    "summary": "两段式决策（阶段一规则闸门纯代码；阶段二 stub/LLM）",
                    "requestBody": {"required": True, "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/DecideInput"}}}},
                    "responses": {
                        "200": _resp("Decision"),
                        "400": _resp("Error", "缺必填字段；未知账号当前实现同样返 400（见 error 文本）"),
                        "500": _resp("Error"),
                    },
                },
            },
            "/plan": {
                "post": {
                    "summary": "决策 → 执行规格（+执行器 argv）；闸门拦截时 spec=null",
                    "requestBody": {"required": True, "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/PlanInput"}}}},
                    "responses": {
                        "200": _resp("PlanResult"),
                        "400": _resp("Error", "缺必填字段"),
                        "500": _resp("Error"),
                    },
                },
            },
            "/report": {
                "post": {
                    "summary": "执行结果回写（幂等；发帖自动承诺扫描）",
                    "description": "body 为 {\"result\": {...}}，或直接平铺 result 字段。",
                    "requestBody": {"required": True, "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/ReportInput"}}}},
                    "responses": {
                        "200": _resp("ReportResult"),
                        "400": _resp("Error", "缺必填字段"),
                        "500": _resp("Error"),
                    },
                },
            },
            "/maintenance/{level}": {
                "post": {
                    "summary": "维护任务（幂等：漏跑补跑即可）",
                    "description": "body 可选、当前被忽略（LLM 钩子走 CLI --llm）。",
                    "parameters": [{
                        "name": "level", "in": "path", "required": True,
                        "schema": {"type": "string", "enum": ["daily", "weekly",
                                                              "monthly", "yearly"]}}],
                    "responses": {
                        "200": _resp("MaintenanceSummary"),
                        "404": _resp("Error", "未知 level"),
                    },
                },
            },
        },
        "components": {"schemas": {
            "Error": _ERROR,
            "Health": _obj(["ok", "backend"], {
                "ok": {"type": "boolean"},
                "backend": {"type": "string",
                            "description": "存储后端类名（KnowledgeStore / SQLiteKnowledgeStore / ...）"}}),
            "GraphStats": _obj(["nodes", "edges"], {
                "nodes": {"type": "integer"}, "edges": {"type": "integer"},
                "events": {"type": "integer",
                           "description": "仅 sqlite/neo4j 后端返回（事件日志数）；内存后端无此键"}}),
            "PersonaCard": _obj(["account_id", "persona_id", "interests"], {
                "account_id": {"type": "string"},
                "persona_id": {"type": "string"},
                "interests": {"type": "array", "items": {"type": "string"}},
                "name": {"type": "string"},
                "age_band": {"type": "string"}, "occupation": {"type": "string"},
                "location": {"type": "string"}, "mbti": {"type": "string"},
                "language_style": {"type": "string"}, "bio": {"type": "string"}},
                additional=True,
                description="人设卡 + 人设属性（additionalProperties 承载领域扩展）"),
            "AccountState": _obj(
                ["account_id", "status", "risk_level", "age_days",
                 "cooldown_remaining_h", "today_action_counts",
                 "last_summary", "recent_actions"], _STATE_PROPS),
            "AccountListItem": _obj(
                ["account_id", "name", "status", "risk_level", "age_days",
                 "cooldown_remaining_h", "today_action_counts",
                 "last_summary", "recent_actions"],
                {**_STATE_PROPS, "name": {"type": "string"}}),
            "OpenPromise": _obj(["note_id", "fact"], {
                "note_id": {"type": "string"}, "fact": {"type": "string"},
                "ts": {"type": "number"}}),
            "RecalledMemory": _obj(["note_id", "fact", "score"], {
                "note_id": {"type": "string"}, "fact": {"type": "string"},
                "category": {"type": ["string", "null"]},
                "pinned": {"type": "boolean"},
                "score": {"type": "number"}}),
            "Episode": _obj(["id", "level", "period"], {
                "id": {"type": "string"}, "level": {"type": "string"},
                "period": {"type": "string"},
                "narrative": {"type": "string"}, "coverage": {"type": "number"},
                "drift": {"type": "number"}, "confidence": {"type": "number"},
                "ts": {"type": "number"}}, additional=True),
            "SearchHit": _obj(["node_id", "type", "score", "snippet"], {
                "node_id": {"type": "string"}, "type": {"type": "string"},
                "score": {"type": "number"}, "snippet": {"type": "string"}}),
            "Violation": _obj(["rule", "name", "message"], {
                "rule": {"type": "string"}, "name": {"type": "string"},
                "message": {"type": "string"}}),
            "Decision": _obj(["status"], {
                "status": {"type": "string", "enum": ["approved", "blocked"]},
                "stage": {"type": "string", "enum": ["rules", "llm"]},
                "decision_id": {"type": "string"},
                "knowledge_refs": {"type": "array", "items": {"type": "string"}},
                "context_tokens": {"type": "integer"},
                "action": {"type": "string"}, "content_type": {"type": "string"},
                "topic": {"type": ["string", "null"]},
                "persona_fidelity_score": {"type": "number"},
                "generated_content": {"type": "string"},
                "rationale": {"type": "string"},
                "pass": {"type": "boolean"},
                "violations": {"type": "array", "items": {"$ref": "#/components/schemas/Violation"}},
                "checked": {"type": "array", "items": {"type": "string"}},
                "llm": {"type": "string",
                        "description": "llm=ok 或 fallback: <原因>；HTTP 端点当前不传 llm，此键为将来接入预留"}},
                description="approved 或 blocked 的超集；blocked 走规则闸门字段（pass/violations/checked）"),
            "ExecutionSpec": _obj(["action_id", "job_id", "account_id", "action",
                                   "title", "content", "text_b64", "content_b64",
                                   "content_type", "task_params"], {
                "action_id": {"type": "string"}, "job_id": {"type": "string"},
                "account_id": {"type": "string"}, "action": {"type": "string"},
                "topic_id": {"type": ["string", "null"]},
                "decision_id": {"type": ["string", "null"]},
                "knowledge_refs": {"type": "array", "items": {"type": "string"}},
                "title": {"type": "string"}, "content": {"type": "string"},
                "text_b64": {"type": "string"}, "content_b64": {"type": "string"},
                "content_type": {"type": "string"},
                "task_params": {"type": "object", "additionalProperties": True}}),
            "PlanResult": _obj(["status", "decision"], {
                "status": {"type": "string", "enum": ["approved", "blocked"]},
                "decision": {"$ref": "#/components/schemas/Decision"},
                "spec": {"oneOf": [{"$ref": "#/components/schemas/ExecutionSpec"},
                                   {"type": "null"}]},
                "command": {"type": "array", "items": {"type": "string"},
                            "description": "approved 时必有（body 缺省 device_env 时服务端填占位值，即 dry-run 形态）；blocked 时无"}}),
            "WriteStats": _obj(["events", "accepted", "duplicates"], {
                "events": {"type": "integer"}, "accepted": {"type": "integer"},
                "duplicates": {"type": "integer"}}),
            "PromiseScan": _obj(["promises_found"], {
                "promises_found": {"type": "integer"},
                "notes": {"type": "array", "items": {"type": "string"}}}),
            "CreateAccountInput": _obj(["platform", "handle"], {
                "platform": {"type": "string"}, "handle": {"type": "string"},
                "subject_id": {"type": "string",
                               "description": "归属的既有记忆主体（跨平台同数字生命）；缺省新建"},
                "name": {"type": "string"}, "persona_id": {"type": "string"},
                "persona": {"type": "object", "additionalProperties": True,
                            "description": "人设属性（persona_props）"},
                "interests": {"type": "array", "items": {"type": "string"}},
                "device": {"type": "string"}}),
            "CreateAccountResult": _obj(["account_id", "subject_id",
                                         "persona_id", "created", "events"], {
                "account_id": {"type": "string"},
                "subject_id": {"type": "string",
                               "description": "记忆主体（缺省新建 subj:{handle}；传 subject_id 可挂到既有数字生命）"},
                "persona_id": {"type": "string"},
                "created": {"type": "boolean"}, "events": {"type": "integer"}}),
            "ImportResultItem": {
                "oneOf": [{"$ref": "#/components/schemas/CreateAccountResult"},
                          _obj(["error", "handle"], {
                              "error": {"type": "string"},
                              "handle": {"type": ["string", "null"]}})]},
            "ImportResult": _obj(["imported", "total", "results"], {
                "imported": {"type": "integer"}, "total": {"type": "integer"},
                "results": {"type": "array",
                            "items": {"$ref": "#/components/schemas/ImportResultItem"}}}),
            "DecisionItem": _obj(["action_id", "action"], {
                "action_id": {"type": "string"}, "action": {"type": "string"},
                "ts": {"type": "number"}, "digest": {"type": "string"},
                "outcome": {"type": ["string", "null"]},
                "topic_id": {"type": ["string", "null"]},
                "decision_id": {"type": ["string", "null"],
                                "description": "种子/旧数据可能无决策 ID"},
                "refs": {"type": "array", "items": _obj(["id", "type", "label"], {
                    "id": {"type": "string"}, "type": {"type": "string"},
                    "label": {"type": "string"}})}}, additional=True),
            "AccountOverview": _obj(["subject_id", "persona", "state", "promises",
                                     "memories", "episodes", "digest"], {
                "subject_id": {"type": "string",
                               "description": "记忆主体（数字生命）；同主体的跨平台账号共享全部记忆"},
                "persona": {"$ref": "#/components/schemas/PersonaCard"},
                "state": {"$ref": "#/components/schemas/AccountState"},
                "promises": {"type": "array",
                             "items": {"$ref": "#/components/schemas/OpenPromise"}},
                "memories": {"type": "array",
                             "items": {"$ref": "#/components/schemas/RecalledMemory"}},
                "episodes": {"type": "array",
                             "items": {"$ref": "#/components/schemas/Episode"}},
                "digest": {"type": "string"}}),
            "MemoryDigest": _obj(["account", "digest"], {
                "account": {"type": "string"}, "digest": {"type": "string"}}),
            "SearchResult": _obj(["hits"], {
                "hits": {"type": "array",
                         "items": {"$ref": "#/components/schemas/SearchHit"}}}),
            "PersonaItem": _obj(["persona_id", "props", "accounts"], {
                "persona_id": {"type": "string"},
                "props": {"type": "object", "additionalProperties": True},
                "accounts": {"type": "array", "items": {"type": "string"}}}),
            "PersonaList": _obj(["personas"], {
                "personas": {"type": "array",
                             "items": {"$ref": "#/components/schemas/PersonaItem"}}}),
            "TopicItem": _obj(["topic_id"], {
                "topic_id": {"type": "string"},
                "label": {"type": ["string", "null"]}}),
            "TopicList": _obj(["topics"], {
                "topics": {"type": "array",
                           "items": {"$ref": "#/components/schemas/TopicItem"}}}),
            "RuleItem": _obj(["rule_id"], {
                "rule_id": {"type": "string"}, "name": {"type": "string"},
                "kind": {"type": "string"}, "status": {"type": "string"},
                "text": {"type": "string"}, "hard": {"type": "boolean"},
                "platform": {"type": ["string", "null"]},
                "confirmations": {"type": "array", "items": {"type": "string"}},
                "effective_at": {"type": "number"},
                "jitter_window": {"type": "integer"}},
                additional=True, description="rule_id + Rule 节点全部属性"),
            "RuleList": _obj(["rules"], {
                "rules": {"type": "array",
                          "items": {"$ref": "#/components/schemas/RuleItem"}}}),
            "DecisionList": _obj(["decisions"], {
                "decisions": {"type": "array",
                              "items": {"$ref": "#/components/schemas/DecisionItem"}}}),
            "AccountList": _obj(["accounts"], {
                "accounts": {"type": "array", "items": {
                    "$ref": "#/components/schemas/AccountListItem"}}}),
            "DecideInput": _obj(["account_id", "action_type"], {
                "account_id": {"type": "string"},
                "action_type": {"type": "string",
                                "enum": ["post", "comment", "follow", "like", "browse"]},
                "topic_id": {"type": "string"},
                "budget_tokens": {"type": "integer"}}),
            "PlanInput": _obj(["account_id", "action_type"], {
                "account_id": {"type": "string"},
                "action_type": {"type": "string",
                                "enum": ["post", "comment", "follow", "like", "browse"]},
                "topic_id": {"type": "string"}, "job_id": {"type": "string"},
                "task_params": {"type": "object", "additionalProperties": True},
                "device_env": {"type": "object", "additionalProperties": True,
                               "description": "缺省时填占位值（dry-run 形态）"}}),
            "ReportInput": {
                "description": "包裹形态 {\"result\": {...}}（推荐）或平铺形态（result 字段直接作 body，serve 兼容）",
                "oneOf": [
                    _obj(["result"], {"result": _RESULT_SCHEMA}),
                    _RESULT_SCHEMA,
                ],
            },
            "ReportResult": _obj(["write", "promises"], {
                "write": {"$ref": "#/components/schemas/WriteStats"},
                "promises": {"$ref": "#/components/schemas/PromiseScan"}}),
            "MaintenanceSummary": {
                "type": "object", "required": ["accounts"],
                "properties": {
                    "accounts": {"type": "integer",
                                 "description": "处理的账号数"},
                    "rolled": {"type": "integer", "description": "daily：有归档动作的账号数"},
                    "expired": {"type": "integer", "description": "weekly：时效淘汰的记忆条数"},
                },
                "additionalProperties": True,
                "description": "按 level 不同键集不同（daily={accounts,rolled}；"
                               "weekly/monthly/yearly 含 expired 等）。"
                               "results/rollups/consolidations 明细被剥离。"},
        }},
    }

def _main():
    print(json.dumps(openapi_spec(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()
