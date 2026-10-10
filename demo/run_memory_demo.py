#!/usr/bin/env python3
"""
长期记忆系统离线 Demo：层级 rollup → 记忆固化 → 承诺闭环 → 检索下钻
→ 公共记忆上卷灰度 → 风险分级预算 → 失真审计 → 幂等。

不需要 LLM / 真机：固化与摘要全部走确定性兜底（LLM 钩子接口见 kb/llm.py）。

运行：python3 demo/run_memory_demo.py
"""
import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from demo.seed import build_seed                                    # noqa: E402
from kb.consensus import (report_signal, rule_active_for,           # noqa: E402
                          _adoption_offset)
from kb.rollup import (daily_rollup, period_rollup,                 # noqa: E402
                       consolidate_memory, open_promises, fulfill_promise)
from kb.search import drill_down                                    # noqa: E402
from kb.tools import KBTools, RISK_BUDGETS                          # noqa: E402

LILY = "acc:xiaohongshu:lily_beauty"
KAI = "acc:xiaohongshu:kai_fit"
TOPIC = "topic:beauty.skincare"

# 锚定当前周（周一为一周第 1 天）：day_ts(1..7) → 本周一到周日。
# 不硬编码日历日期，保证 24h 频控窗 / consolidate 30 天窗 / Episode 周期
# 在任何日期运行都稳定（测试确定性；2026-10-10 硬编码版在 10-10 18:00 后永久红）。
_MONDAY = datetime.combine(
    datetime.now().date() - timedelta(days=datetime.now().weekday()),
    datetime.min.time())


def day_ts(d, hour=10):
    """d=1..7 → 本周一到周日 hour 点。"""
    return (_MONDAY + timedelta(days=d - 1, hours=hour)).timestamp()


def _week_key(monday):
    """ISO 周 key（YYYYWww），与 period_rollup 的周期 key 规范一致。"""
    y, w, _ = monday.isocalendar()
    return f"{y}W{w:02d}"


WEEK = _week_key(_MONDAY)
MONTH = _MONDAY.strftime("%Y%m")   # 月 key 按 ISO 周一归属月（与 _week_month 同规则）


def h1(title):
    print("\n" + "=" * 62)
    print(title)
    print("=" * 62)


def main():
    store, queue = build_seed()
    tools = KBTools(store, queue)

    # -------------------------------------------------------------
    h1("1. 模拟一周运营：每天提交行为 + 周中一篇高赞帖 + 一次失败")
    week_plan = [
        (1, [("browse", TOPIC), ("like", TOPIC)]),
        (2, [("post", TOPIC)]),                       # 高赞防晒帖
        (3, [("comment", TOPIC), ("browse", "topic:tech.ai")]),  # 期外话题 → 漂移
        (4, [("like", TOPIC)]),
        (5, [("post", TOPIC)]),                       # 失败帖
    ]
    for d, actions in week_plan:
        for i, (action, topic) in enumerate(actions):
            res = tools.submit_result({
                "action_id": f"act:mem-w{d}-{i}",
                "account_id": LILY, "action": action, "topic_id": topic,
                "digest": "防晒实测：通勤 8 小时不补涂" if action == "post"
                          else f"{action}@{topic}",
                "new_post_id": f"post:xiaohongshu:mw{d}" if action == "post" else None,
                "stats": {"views": 5200, "likes": 89, "comments": 12}
                          if (action == "post" and d == 2) else {},
                "outcome": "failed" if (action == "post" and d == 5) else "success",
                "ts": day_ts(d, 10 + i),
            })
        day_key = (_MONDAY + timedelta(days=d - 1)).strftime("%Y%m%d")
        ru = daily_rollup(store, LILY, day=day_key)
        print(f"  {day_key}: 归档 {ru['archived_actions']} 个动作 "
              f"（{ru['compression']['raw_chars']}→{ru['compression']['summary_chars']} 字符）")
    assert not tools.get_account_state(LILY)["recent_actions"], "热区应已清空"

    # -------------------------------------------------------------
    h1("2. 周卷积（period_rollup）：日摘要 → Episode 情景记忆 + 人设漂移检查")
    wr = period_rollup(store, LILY, level="week", period=WEEK)
    print(f"  子周期数: {wr['children']}  漂移分: {wr['drift']}")
    print(f"  Episode: {wr['episode_id']}")
    print(f"  叙事: {wr['narrative'][:80]}…")
    assert 0 < wr["drift"] < 0.5, "期内有 1 个期外话题动作，漂移应为小正值"

    h1("3. 月卷积：周摘要 → 月叙事（层级下钻的地基）")
    mr = period_rollup(store, LILY, level="month", period=MONTH)
    print(f"  子周期数: {mr['children']}  Episode: {mr['episode_id']}")

    # -------------------------------------------------------------
    h1("4. 记忆固化（consolidate_memory）：情景 → 语义记忆 MemoryNote")
    # 自定义 extractor 模拟 LLM 钩子：稳定事实 + 对粉丝的承诺（连续性来源）
    def demo_extractor(ctx):
        out = [{"fact": "防晒实测类内容互动最高（89 赞），可发展为系列选题",
                "category": "fact", "confidence": 0.85, "pinned": True},
               {"fact": "答应粉丝下期讲刷酸（评论区高赞追问）",
                "category": "promise", "confidence": 0.9,
                "post_id": "post:xiaohongshu:mw2"}]
        return out

    cm = consolidate_memory(store, LILY, extractor=demo_extractor, since_days=30)
    print(f"  固化事实: {cm['facts']} 条（含承诺 {cm['promises']} 条）")
    for nid in cm["notes"]:
        p = store.node_props(nid)
        print(f"  {nid} [{p['category']}/{p['status']}] {p['fact'][:36]}")

    # -------------------------------------------------------------
    h1("5. 承诺闭环：决策上下文置顶未兑现承诺 → 发兑现帖 → FULFILLED_BY")
    ctx = tools.build_decision_context(LILY, "post", TOPIC)
    mem = ctx["blocks"]["memory"]
    assert mem["open_promises"], "决策上下文应包含未兑现承诺"
    print(f"  决策上下文未兑现承诺: {mem['open_promises'][0]['fact']}")
    print(f"  召回语义记忆 {len(mem['memories'])} 条，最新情景: "
          f"{(mem['latest_episode'] or {}).get('narrative', '')[:40]}…")

    note_id = mem["open_promises"][0]["note_id"]
    tools.submit_result({
        "action_id": "act:mem-fulfill", "account_id": LILY, "action": "post",
        "topic_id": TOPIC, "new_post_id": "post:xiaohongshu:mw5b",
        # ts 用真实 now（不锚定周内某天）：第 8 步的 24h 频控窗依赖此帖在窗内，
        # 硬编码日历日期会在该日期 +24h 后让断言永久失败（2026-10-10 18:00 实测）
        "digest": "刷酸实测来了：答应你们的那期", "ts": time.time()})
    fp = fulfill_promise(store, note_id, by_ref="post:xiaohongshu:mw5b")
    print(f"  兑现结果: {fp}")
    assert fp["changed"] and not open_promises(store, LILY)
    assert store.get_edge(note_id, "FULFILLED_BY", "post:xiaohongshu:mw5b")

    # -------------------------------------------------------------
    h1("5b. 生成内容埋坑追踪：正文里新埋的承诺自动入库（v2 缺口补齐）")
    content = ("姐妹们闭口终于瘪下去了！评论区蹲一波你们的刷酸翻车实录，"
               "下周实测平价妆前乳，人多我立马安排！")
    tp = tools.track_promises(LILY, content, post_id="post:xiaohongshu:mw5b")
    print(f"  扫描结果: {tp}")
    assert tp["promises_found"] == 1, "应识别出 1 条新承诺"
    pl = open_promises(store, LILY)
    assert len(pl) == 1 and "妆前乳" in pl[0]["fact"]
    print(f"  未兑现承诺: {pl[0]['fact']}")
    # 重复扫描不膨胀（指纹 ID）
    tools.track_promises(LILY, content, post_id="post:xiaohongshu:mw5b")
    assert len(open_promises(store, LILY)) == 1
    print("  重复扫描: 未兑现承诺仍 1 条（指纹 ID 幂等）✓")

    # -------------------------------------------------------------
    h1("6. 检索：倒排索引召回 + 分层下钻（年/月定位 → 日明细）")
    hits = tools.search_knowledge("防晒", k=3)
    for h in hits:
        print(f"  [{h['type']}] {h['node_id']}  score={h['score']}\n    {h['snippet'][:60]}")
    assert any("防晒" in h["snippet"] for h in hits), "应召回防晒相关记忆"
    dd = drill_down(store, LILY, "month", MONTH)
    print(f"  下钻 {MONTH} → {len(dd['children'])} 个子级摘要（周/日）")

    # -------------------------------------------------------------
    h1("7. 公共记忆上卷：单账号信号=候选 → 双账号确证 → 灰度生效")
    s1 = report_signal(store, queue, LILY, "xhs-limit-hint", "限流迹象",
                       "多位用户反馈笔记曝光骤降，疑似新限流规则",
                       params={"max": 1, "actions": ["post"]},
                       kind="rate_limit_per_day", jitter_window=0)
    print(f"  lily 上报: {s1['status']}（确证 {s1['confirmations']}）")
    assert s1["status"] == "candidate"
    texts_before = tools._applicable_rule_texts(LILY, "post")
    assert not any("限流迹象" in t for t in texts_before), "候选信号不应进决策上下文"

    s2 = report_signal(store, queue, KAI, "xhs-limit-hint", "限流迹象",
                       "多位用户反馈笔记曝光骤降，疑似新限流规则",
                       params={"max": 1, "actions": ["post"]},
                       kind="rate_limit_per_day", jitter_window=0)
    print(f"  kai 独立上报: {s2['status']}（确证 {s2['confirmations']}）→ 固化为全局规则")
    texts_after = tools._applicable_rule_texts(LILY, "post")
    assert any("限流迹象" in t for t in texts_after), "确证后应进入决策上下文"

    # 灰度抖动：不同账号错峰采纳（确定性哈希，离线可测）
    s3 = report_signal(store, queue, LILY, "xhs-algo-change", "算法调整传闻",
                       "信息流排序疑似变化，观察即可", kind="advisory",
                       jitter_window=3600)
    report_signal(store, queue, KAI, "xhs-algo-change", "算法调整传闻",
                  "信息流排序疑似变化，观察即可", kind="advisory",
                  jitter_window=3600)   # 第二账号确证 → 生效时间与抖动窗口就绪
    rid = s3["rule_id"]
    off_l = _adoption_offset(rid, LILY, 3600)
    off_k = _adoption_offset(rid, KAI, 3600)
    import time as _t
    now = _t.time()
    act_l = rule_active_for(store.node_props(rid), LILY, now=now, rule_id=rid)
    act_k = rule_active_for(store.node_props(rid), KAI, now=now, rule_id=rid)
    print(f"  抖动偏移: lily={off_l:.0f}s kai={off_k:.0f}s → 当前生效 lily={act_l} kai={act_k}")
    assert act_l == (now >= store.node_props(rid)["effective_at"] + off_l)
    assert act_k == (now >= store.node_props(rid)["effective_at"] + off_k)

    # -------------------------------------------------------------
    h1("8. 风险分级预算 + 跨层联动：公共规则拦截 / post 3000 / like 600")
    # 跨层联动：第 7 步确证的公共限流规则（jitter_window=0，立即生效）拦截 lily
    d_lily = tools.decide(LILY, "post", TOPIC, budget_tokens=None)
    print(f"  lily 发帖: {d_lily['status']} ← 被确证的公共规则拦截"
          f"（{d_lily.get('violations', [{}])[0].get('message', '')}）")
    assert d_lily["status"] == "blocked"

    LIN = "acc:xiaohongshu:lin_fit"
    FT = "topic:fitness.home_workout"
    d_post = tools.decide(LIN, "post", FT, budget_tokens=None)
    d_like = tools.decide(LIN, "like", FT, budget_tokens=None)
    print(f"  lin 发帖决策: context_tokens={d_post['context_tokens']} ≤ {RISK_BUDGETS['post']}")
    print(f"  lin 点赞决策: context_tokens={d_like['context_tokens']} ≤ {RISK_BUDGETS['like']}")
    assert d_post["context_tokens"] <= RISK_BUDGETS["post"]
    assert d_like["context_tokens"] <= RISK_BUDGETS["like"]
    tiny = tools.build_decision_context(LIN, "like", FT, budget_tokens=300)
    print(f"  300 token 硬预算下保留块: {sorted(tiny['blocks'])}")

    # -------------------------------------------------------------
    h1("9. 失真审计（audit_decision）：引用存在性 + 保真度检查")
    audit = tools.audit_decision(d_post)
    print(f"  ok: {audit['ok']}  检查引用: {audit['refs_checked']}  发现: {audit['findings'] or '无'}")
    assert audit["ok"]
    fake = dict(d_post, knowledge_refs=d_post["knowledge_refs"] + ["per:not-exist"])
    assert not tools.audit_decision(fake)["ok"], "缺失引用应被审计发现"

    # -------------------------------------------------------------
    h1("10. 幂等：重复固化 → 事实指纹 ID 相同 → 零新增节点")
    n_before = len(store.node_ids())
    consolidate_memory(store, LILY, extractor=demo_extractor, since_days=30)
    n_after = len(store.node_ids())
    print(f"  节点数: {n_before} → {n_after}（重复固化不膨胀）")
    assert n_before == n_after

    print("=" * 62)
    print(f"完成  最终图谱: {store.stats()}")
    print("记忆分层: 工作记忆(热区) / 情景(Episode) / 语义(MemoryNote) / 公共(确证规则)")


if __name__ == "__main__":
    main()
