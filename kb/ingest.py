"""
摄取漏斗：原始执行结果 → Schema 约束的事件列表。

- 规则映射优先，LLM 抽取兜底；所有输出必须通过 schema 校验
- 事件 ID 由 action_id 派生 → 同一结果重试提交天然幂等
"""
from __future__ import annotations

import time

from .schema import make_event, new_action_id
from .rollup import note_id_of


def platform_of(account_id: str) -> str:
    """acc:xiaohongshu:lily → xiaohongshu"""
    return account_id.split(":")[1]


def extract_events_from_result(result: dict, llm=None) -> list:
    """执行结果 → 事件列表。

    result 字段：
      account_id*  action*  (post_id=新帖ID  target_account  topic_id
       digest  outcome  stats  decision_id  knowledge_refs  ts  source  action_id)
    llm(result) -> [{"kind": ..., "payload": ...}] 可选钩子，候选事实必须过 schema。
    """
    aid = result["account_id"]
    action = result["action"]
    ts = float(result.get("ts", time.time()))
    src = result.get("source", "executor")
    base = result.get("action_id") or new_action_id()
    events = []

    def ev(i, kind, payload):
        # 事件 ID 由 action_id 派生 → 重试提交时 ID 相同 → 队列层幂等去重
        return make_event(aid, kind, payload, event_id=f"{base}:{i:02d}", ts=ts)

    # 1) 行为流水（热区数据）
    events.append(ev(0, "upsert_node", {
        "id": base, "type": "ActionRecord",
        "props": {
            "action": action,
            "outcome": result.get("outcome", "success"),
            "topic_id": result.get("topic_id"),
            "post_id": result.get("new_post_id"),
            "target_account": result.get("target_account"),
            "digest": result.get("digest", ""),
            "decision_id": result.get("decision_id"),
            "archived": False,
            "ts": ts,
        }}))
    events.append(ev(1, "upsert_edge", {"src": aid, "type": "PERFORMED", "dst": base}))

    # 2) 决策依据（provenance：决策可追溯到知识节点）
    i = 2
    for ref in result.get("knowledge_refs", []):
        events.append(ev(i, "upsert_edge", {
            "src": base, "type": "DECIDED_VIA", "dst": ref, "source": src}))
        i += 1

    # 3) 互动计数（有界压缩：count 合并而非逐条流水）
    if action in ("like", "comment", "follow") and result.get("target_account"):
        events.append(ev(i, "incr_edge", {
            "src": aid, "type": "INTERACTS_WITH", "dst": result["target_account"],
            "n": 1, "source": src}))
        i += 1

    # 4) 话题关注计数
    if result.get("topic_id"):
        events.append(ev(i, "incr_edge", {
            "src": aid, "type": "FOLLOWS", "dst": result["topic_id"],
            "n": 1, "source": src}))
        i += 1

    # 5) 发帖 → Post 节点（冷知识沉淀，只存 digest 不存全文）
    if action == "post" and result.get("new_post_id"):
        pid = result["new_post_id"]
        props = {
            "platform": platform_of(aid),
            "published_at": ts,
            "content_type": result.get("content_type", "text"),
            "digest": result.get("digest", ""),
        }
        props.update(result.get("stats", {}))
        events.append(ev(i, "upsert_node", {"id": pid, "type": "Post", "props": props}))
        i += 1
        events.append(ev(i, "upsert_edge", {"src": aid, "type": "PUBLISHED", "dst": pid}))
        i += 1
        if result.get("topic_id"):
            events.append(ev(i, "upsert_edge", {"src": pid, "type": "ABOUT",
                                                "dst": result["topic_id"]}))
            i += 1

    # 6) LLM 兜底抽取（可选钩子）：候选事实必须通过 schema 校验，否则丢弃
    if llm is not None:
        try:
            candidates = llm(result) or []
        except Exception:
            candidates = []   # LLM 抽取失败不阻塞回写，结构化部分照常入库
        # MemoryNote 候选归一化：ID 换成事实指纹（note_id_of），
        # LLM 发明的 ID 只是临时引用；引用它的边同步重映射。
        # 效果：同一承诺重复抽取 → 同一节点；措辞不合规的 ID 也能入库。
        remap = {}
        for cand in candidates:
            if not isinstance(cand, dict):
                continue
            p = cand.get("payload", {})
            if cand.get("kind") == "upsert_node" \
                    and p.get("type") == "MemoryNote":
                fact = (p.get("props") or {}).get("fact")
                if isinstance(fact, str) and fact.strip():
                    canonical = note_id_of(fact)
                    remap[p.get("id")] = canonical
                    p["id"] = canonical
                    cat = (p.get("props") or {}).get("category")
                    if cat not in ("fact", "lesson", "milestone", "promise"):
                        p["props"]["category"] = "fact"
                    p["props"].setdefault(
                        "status", "open" if cat == "promise" else "stable")
                    # 结构性字段由系统保证，不依赖 LLM 自觉
                    p["props"].setdefault("ts", ts)
                    p["props"].setdefault("pinned", False)
                    p["props"].setdefault("confidence", 0.7)
        norm = []
        auto_link = []          # 自动补的 HAS_MEMORY 边（确定性，不依赖 LLM）
        for cand in candidates:
            if not isinstance(cand, dict):
                continue
            p = cand.get("payload", {})
            if cand.get("kind") in ("upsert_edge", "incr_edge"):
                if p.get("src") in remap:
                    p["src"] = remap[p["src"]]
                if p.get("dst") in remap:
                    # 语义约束：COMMITTED_IN/FULFILLED_BY 的 dst 不能指向
                    # 另一条 MemoryNote（LLM 偶发错指），直接丢弃该候选边
                    if p.get("type") in ("COMMITTED_IN", "FULFILLED_BY"):
                        continue
                    p["dst"] = remap[p["dst"]]
            norm.append(cand)
        candidates = norm
        # 从本账号结果中抽取的记忆自动归属本账号（HAS_MEMORY），
 # 事件 ID 确定性派生 → 同一结果重提不重复
        for canonical in sorted(set(remap.values())):
            auto_link.append({"kind": "upsert_edge",
                              "payload": {"src": aid, "type": "HAS_MEMORY",
                                          "dst": canonical},
                              "event_id": f"{base}:mm:{canonical}"})
        for cand in candidates:
            try:
                events.append(make_event(aid, cand["kind"], cand["payload"], ts=ts))
            except (ValueError, KeyError):
                continue  # Schema 即过滤器：不符合本体的候选事实直接丢弃
        for link in auto_link:
            try:
                events.append(make_event(aid, link["kind"], link["payload"],
                                         event_id=link["event_id"], ts=ts))
            except (ValueError, KeyError):
                continue
    return events
