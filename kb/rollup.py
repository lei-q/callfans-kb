"""
滚动压缩（rollup）：日级行为流水 → 日摘要 → 层级卷积 → 记忆固化。

对应六问中的"结果压缩更新"与长期记忆设计（docs/03）：
- 热区只保留未归档明细（约当天），摘要写入账号属性 summary_day:YYYYMMDD
- 层级卷积：日 → 周 → 月 → 年（summary_week: / summary_month: / summary_year:）
  同时写入 Episode 节点（情景记忆，带人设漂移检查）
- 记忆固化：consolidate_memory 从经历中沉淀稳定事实/经验/里程碑/承诺
  → MemoryNote 节点（语义记忆，事实指纹 ID 保证幂等）
- summarizer / extractor 钩子可接 LLM，失败自动回退确定性逻辑
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime

from .schema import make_event, topic_slug


def daily_rollup(store, account_id, day=None, summarizer=None) -> dict:
    """把该账号当前未归档的行为流水压缩成日摘要。建议每天跑一次。"""
    day = day or time.strftime("%Y%m%d")

    acts = []
    for _, dst, _ in store.out_edges(account_id, "PERFORMED"):
        n = store.get_node(dst)
        if n and n["type"] == "ActionRecord" and not n["props"].get("archived"):
            acts.append((dst, n["props"]))

    counts = {}
    for _, p in acts:
        counts[p["action"]] = counts.get(p["action"], 0) + 1

    summary = None
    if summarizer is not None:
        try:
            summary = summarizer(acts)   # 钩子：接 LLM 生成叙事摘要
        except Exception:
            summary = None               # LLM 失败 → 回退模板摘要，不阻塞 rollup
    if not summary:
        detail = "；".join(
            f"{p['action']}@{(p.get('topic_id') or '-').replace('topic:', '')}"
            for _, p in acts[:10])
        summary = f"{day} 共执行 {len(acts)} 个动作：{detail}"

    store.apply(make_event(account_id, "set_prop",
                           {"id": account_id, "prop": f"summary_day:{day}",
                            "value": summary}))
    store.apply(make_event(account_id, "set_prop",
                           {"id": account_id, "prop": f"summary_counts:{day}",
                            "value": counts}))
    for act_id, _ in acts:
        store.apply(make_event(account_id, "set_prop",
                               {"id": act_id, "prop": "archived", "value": True}))

    raw_chars = sum(len(str(p)) for _, p in acts)
    return {"day": day, "archived_actions": len(acts), "counts": counts,
            "summary": summary,
            "compression": {"raw_chars": raw_chars, "summary_chars": len(summary)}}

# ===========================================================================
# 层级卷积：日 → 周 → 月 → 年（情景记忆）
# ===========================================================================
LEVEL_CHILD = {"week": "summary_day", "month": "summary_week", "year": "summary_month"}


def _period_of(day_key: str, level: str) -> str:
    """'20261009' → week '2026W41' / month '202610' / year '2026'。"""
    d = datetime.strptime(day_key, "%Y%m%d")
    if level == "week":
        y, w, _ = d.isocalendar()
        return f"{y}W{w:02d}"
    if level == "month":
        return d.strftime("%Y%m")
    return d.strftime("%Y")


def _account_slug(account_id: str) -> str:
    """acc:xiaohongshu:lily_beauty → xiaohongshu.lily_beauty（Episode ID 片段）。"""
    parts = account_id.split(":")
    return ".".join(parts[1:]) if len(parts) > 1 else parts[0]


def _period_actions(store, account_id, day_keys):
    """取属于这些天的 ActionRecord（含已归档），用于话题分布与漂移检查。"""
    lo = min(day_keys)
    hi = max(day_keys)
    lo_ts = datetime.strptime(lo, "%Y%m%d").timestamp()
    hi_ts = datetime.strptime(hi, "%Y%m%d").timestamp() + 86400
    out = []
    for _, dst, _ in store.out_edges(account_id, "PERFORMED"):
        n = store.get_node(dst)
        if n and n["type"] == "ActionRecord" and lo_ts <= n["props"].get("ts", 0) < hi_ts:
            out.append(n["props"])
    return out


def _drift_check(store, account_id, act_props):
    """人设漂移检查：期内动作话题分布 vs 人设兴趣重合度。返回 (漂移分, 明细)。"""
    per_id = store.persona_id_of(account_id)
    interests = {topic_slug(d) for _, d, _ in store.out_edges(per_id, "INTERESTED_IN")}
    topics = [p.get("topic_id") for p in act_props if p.get("topic_id")]
    if not topics or not interests:
        return 0.0, {"topic_actions": len(topics), "in_persona": 0}
    hit = sum(1 for t in topics if topic_slug(t) in interests)
    ratio = hit / len(topics)
    dist = {}
    for t in topics:
        s = topic_slug(t)
        dist[s] = dist.get(s, 0) + 1
    return round(1 - ratio, 3), {"topic_actions": len(topics), "in_persona": hit,
                                 "topic_dist": dist}


def _week_month(week_key: str) -> str:
    """'2026W41' → '202610'（ISO 周一所在月份；跨月周归属周一）。"""
    y, w = week_key.split("W")
    return datetime.fromisocalendar(int(y), int(w), 1).strftime("%Y%m")


def period_rollup(store, account_id, level="week", period=None, summarizer=None) -> dict:
    """把该账号某周期的子级摘要卷积成周期叙事（情景记忆）。

    level=week 聚合 summary_day:*；month 聚合 summary_week:*；year 聚合
    summary_month:*。产出：账号属性 summary_<level>:<period> + Episode 节点
    （含漂移检查，人设漂移会在叙事中标记）。建议每周/每月定时跑。
    """
    if level not in LEVEL_CHILD:
        raise ValueError(f"未知层级: {level}（可选 week/month/year）")
    props = store.node_props(account_id)
    day_keys = sorted(k.split(":", 1)[1] for k in props
                      if k.startswith("summary_day:"))

    if period is None:
        if not day_keys:
            return {"level": level, "skipped": "no_child_summaries"}
        last = day_keys[-1]
        period = _period_of(last, level) if level == "week" else \
            (last[:6] if level == "month" else last[:4])

    if level == "week":
        child_keys = [k for k in props if k.startswith("summary_day:")
                      and _period_of(k.split(":", 1)[1], "week") == period]
        days = [k.split(":", 1)[1] for k in child_keys]
    elif level == "month":
        child_keys = [k for k in props if k.startswith("summary_week:")
                      and _week_month(k.split(":", 1)[1]) == period]
        days = [d for d in day_keys if d.startswith(period)]
    else:
        child_keys = [k for k in props if k.startswith("summary_month:")
                      and k.split(":", 1)[1].startswith(period)]
        days = [d for d in day_keys if d.startswith(period)]

    if not child_keys:
        return {"level": level, "period": period, "skipped": "no_child_summaries"}

    items = [(k.split(":", 1)[1], props[k]) for k in sorted(child_keys)]
    act_props = _period_actions(store, account_id, days) if days else []
    drift, drift_detail = _drift_check(store, account_id, act_props)

    narrative = None
    if summarizer is not None:
        try:
            narrative = summarizer({"level": level, "period": period,
                                    "items": items, "drift": drift,
                                    "drift_detail": drift_detail})
        except Exception:
            narrative = None                 # LLM 失败 → 回退模板叙事
    if not narrative:
        drift_note = f"；人设漂移 {drift:.0%}（期外话题占比）" if drift > 0.2 else ""
        narrative = (f"{period}（{len(items)} 个子周期）："
                     + "；".join(f"{p}: {s[:40]}" for p, s in items[:6])
                     + drift_note)

    store.apply(make_event(account_id, "set_prop",
                           {"id": account_id, "prop": f"summary_{level}:{period}",
                            "value": narrative}))
    ep_id = f"ep:{_account_slug(account_id)}.{period}"
    store.apply(make_event(account_id, "upsert_node", {
        "id": ep_id, "type": "Episode",
        "props": {"level": level, "period": period, "narrative": narrative,
                  "coverage": len(items), "drift": drift,
                  "drift_detail": drift_detail,
                  "confidence": 1.0 - drift * 0.5, "ts": time.time()}}))
    store.apply(make_event(account_id, "upsert_edge",
                           {"src": account_id, "type": "HAS_EPISODE", "dst": ep_id}))

    raw_chars = sum(len(s) for _, s in items)
    return {"level": level, "period": period, "children": len(items),
            "narrative": narrative, "drift": drift, "episode_id": ep_id,
            "compression": {"raw_chars": raw_chars, "narrative_chars": len(narrative)}}


# ===========================================================================
# 记忆固化：情景记忆 → 语义记忆（MemoryNote）
# ===========================================================================
def note_id_of(fact: str) -> str:
    """事实指纹 ID：同一事实重复抽取 → 同一节点 → 天然幂等。"""
    return "note:" + hashlib.md5(fact.encode("utf-8")).hexdigest()[:10]


def _stub_extractor(ctx):
    """确定性固化（无 LLM 时的兜底）：从期数据提取稳定事实。"""
    facts = []
    posts = ctx.get("posts", [])
    if posts:
        best = max(posts, key=lambda p: p.get("likes", 0))
        if best.get("likes", 0) > 0:
            facts.append({"fact": f"高表现内容：「{best.get('digest', '')[:30]}」"
                                  f"获 {best['likes']} 赞，模式可复用",
                          "category": "milestone", "confidence": 0.8, "pinned": True})
    dist = ctx.get("drift_detail", {}).get("topic_dist") or {}
    if dist:
        top = max(dist.items(), key=lambda x: x[1])
        facts.append({"fact": f"内容重心在 {top[0]}（期内 {top[1]} 次相关动作）",
                      "category": "fact", "confidence": 0.7, "pinned": False})
    for f in ctx.get("failures", [])[:2]:
        facts.append({"fact": f"执行失败教训：{f[:50]}",
                      "category": "lesson", "confidence": 0.6, "pinned": False})
    return facts


def consolidate_memory(store, account_id, extractor=None, since_days=7) -> dict:
    """记忆固化 job：读近期情景记忆/帖子/失败记录 → 沉淀语义记忆。

    extractor(ctx) -> [{"fact", "category", "confidence", "pinned",
                        "topic_id"?, "post_id"?}] 可接 LLM；
    失败或未传时回退 _stub_extractor。建议每周跑一次。
    category: fact（稳定事实）/ lesson（经验教训）/ milestone（里程碑，pin）/
              promise（对粉丝的承诺，进入兑现追踪）
    """
    now = time.time()
    props = store.node_props(account_id)
    episodes = []
    for _, dst, _ in store.out_edges(account_id, "HAS_EPISODE"):
        n = store.get_node(dst)
        if n and now - n["props"].get("ts", 0) < since_days * 86400:
            episodes.append({"id": dst, **n["props"]})

    posts = []
    for _, dst, _ in store.out_edges(account_id, "PUBLISHED"):
        n = store.get_node(dst)
        if n and now - n["props"].get("published_at", 0) < since_days * 86400:
            posts.append(n["props"])

    act_all = []
    for _, dst, _ in store.out_edges(account_id, "PERFORMED"):
        n = store.get_node(dst)
        if n and now - n["props"].get("ts", 0) < since_days * 86400:
            act_all.append(n["props"])
    failures = [f"{p['action']}:{p.get('digest', '')}" for p in act_all
                if p.get("outcome") == "failed"]

    drift_detail = {}
    for ep in episodes:
        if ep.get("drift_detail"):
            drift_detail = ep["drift_detail"]
            break
    ctx = {"account_id": account_id, "episodes": episodes, "posts": posts,
           "failures": failures, "drift_detail": drift_detail}

    try:
        facts = (extractor or _stub_extractor)(ctx) or []
    except Exception:
        facts = _stub_extractor(ctx)

    written = []
    for f in facts:
        nid = note_id_of(f["fact"])
        store.apply(make_event(account_id, "upsert_node", {
            "id": nid, "type": "MemoryNote",
            "props": {"fact": f["fact"], "category": f.get("category", "fact"),
                      "confidence": f.get("confidence", 0.7),
                      "pinned": bool(f.get("pinned")),
                      "status": "open" if f.get("category") == "promise" else "stable",
                      "ts": now}}))
        store.apply(make_event(account_id, "upsert_edge",
                               {"src": account_id, "type": "HAS_MEMORY", "dst": nid}))
        if f.get("category") == "promise" and f.get("post_id"):
            store.apply(make_event(account_id, "upsert_edge",
                                   {"src": nid, "type": "COMMITTED_IN",
                                    "dst": f["post_id"]}))
        written.append(nid)
    return {"facts": len(facts), "notes": written,
            "promises": sum(1 for f in facts if f.get("category") == "promise")}


def open_promises(store, account_id) -> list:
    """未兑现的承诺（决策上下文优先块：连续性的来源）。"""
    out = []
    for _, dst, _ in store.out_edges(account_id, "HAS_MEMORY"):
        n = store.get_node(dst)
        if n and n["props"].get("category") == "promise" \
                and n["props"].get("status") == "open":
            out.append({"note_id": dst, "fact": n["props"]["fact"],
                        "ts": n["props"].get("ts")})
    out.sort(key=lambda x: x["ts"])
    return out


def fulfill_promise(store, note_id, by_ref) -> dict:
    """兑现承诺：状态 closed + FULFILLED_BY 指向兑现它的行为/帖子。"""
    n = store.get_node(note_id)
    if n is None or n["type"] != "MemoryNote":
        raise KeyError(f"承诺节点不存在: {note_id}")
    if n["props"].get("status") != "open":
        return {"note_id": note_id, "status": n["props"]["status"], "changed": False}
    account_id = next(s for s, _, _ in store.in_edges(note_id, "HAS_MEMORY"))
    store.apply(make_event(account_id, "set_prop",
                           {"id": note_id, "prop": "status", "value": "fulfilled"}))
    store.apply(make_event(account_id, "upsert_edge",
                           {"src": note_id, "type": "FULFILLED_BY", "dst": by_ref}))
    return {"note_id": note_id, "status": "fulfilled", "changed": True, "by": by_ref}


# ===========================================================================
# 承诺记录：生成内容中"埋的坑"也要追踪（v2 缺口补齐）
# ===========================================================================
def record_promise(store, account_id, fact, confidence=0.7, post_id=None) -> dict:
    """把一条对粉丝的承诺写入记忆（指纹 ID 幂等，重复记录不膨胀）。"""
    nid = note_id_of(fact)
    store.apply(make_event(account_id, "upsert_node", {
        "id": nid, "type": "MemoryNote",
        "props": {"fact": fact, "category": "promise",
                  "confidence": confidence, "pinned": False,
                  "status": "open", "ts": time.time()}}))
    store.apply(make_event(account_id, "upsert_edge",
                           {"src": account_id, "type": "HAS_MEMORY", "dst": nid}))
    if post_id:
        store.apply(make_event(account_id, "upsert_edge",
                               {"src": nid, "type": "COMMITTED_IN", "dst": post_id}))
    return {"note_id": nid, "fact": fact}


# 确定性兜底扫描：句内同时出现"未来时点标记 + 承诺动词"判为承诺。
# 保守取向：宁可漏判（LLM 钩子补召回），不可误报（凭空造承诺）。
_FUTURE_MARKERS = ("下一期", "下期", "下次", "下周", "明天", "后天", "稍后",
                   "回头", "月底", "周内", "之后")
_COMMIT_VERBS = ("讲", "测", "评测", "实测", "更", "更新", "分享", "安排",
                 "盘点", "发布", "肝")


def stub_promise_scanner(content):
    """(content) -> [{"fact", "confidence"}]。LLM 钩子的离线兜底。"""
    import re
    if not content:
        return []
    out = []
    for s in re.split(r"[。！？!?\n～~]+", content):
        s = s.strip()
        if len(s) < 6:
            continue
        if any(m in s for m in _FUTURE_MARKERS) \
                and any(v in s for v in _COMMIT_VERBS):
            out.append({"fact": f"对粉丝的承诺：{s[:60]}",
                        "confidence": 0.7})
    return out


# ===========================================================================
# 语义记忆时效淘汰（借鉴 Proma 的时效标注纪律）
# ===========================================================================
def expire_stale_notes(store, account_id, max_age_days=90, now=None) -> dict:
    """把过期的语义记忆标记为 stale（保留可追溯，但不再进入召回）。

    淘汰条件：category ∈ {fact, lesson} 且 status=stable 且未 pin 且
    超过 max_age_days 未更新。承诺(promise)有自己的生命周期，不参与。
    pinned 里程碑永不淘汰。
    """
    now = now or time.time()
    expired = []
    for _, dst, _ in store.out_edges(account_id, "HAS_MEMORY"):
        n = store.get_node(dst)
        if not n:
            continue
        p = n["props"]
        if (p.get("category") in ("fact", "lesson")
                and p.get("status") == "stable"
                and not p.get("pinned")
                and now - p.get("ts", 0) > max_age_days * 86400):
            store.apply(make_event(account_id, "set_prop",
                                   {"id": dst, "prop": "status",
                                    "value": "stale"}))
            expired.append(dst)
    return {"expired": len(expired), "notes": expired}
