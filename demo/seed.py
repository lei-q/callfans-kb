"""种子数据：小红书 + 6 账号 + 3 人设 + 话题树 + 4 规则。

全部经由事件写入（与生产同一条路径），保证 Schema 校验覆盖所有数据。
"""
from __future__ import annotations

import time

from kb.ingest import extract_events_from_result
from kb.schema import make_event
from kb.store import KnowledgeStore, WriteQueue

DAY = 86400.0
NOW = time.time()


def _seed_events():
    """节点先建、边后建（队列跨分区不保证顺序，同分区 FIFO）。"""
    ev = []

    def node(part, nid, ntype, props):
        ev.append(make_event(part, "upsert_node",
                             {"id": nid, "type": ntype, "props": props}))

    def edge(part, src, etype, dst, **props):
        ev.append(make_event(part, "upsert_edge",
                             {"src": src, "type": etype, "dst": dst,
                              "props": props}))

    # ---- 节点：人设 ----
    for pid, props in [
        ("per:beauty-lily", {"name": "小莉", "age_band": "22-28",
                             "occupation": "美妆博主", "location": "上海",
                             "mbti": "ENFP", "language_style": "幽默口语化",
                             "bio": "油皮护肤 + 平价彩妆，每周实测"}),
        ("per:fit-kai", {"name": "阿凯", "age_band": "28-35",
                         "occupation": "健身教练", "location": "北京",
                         "mbti": "ISTJ", "language_style": "严肃专业",
                         "bio": "徒手训练与体态纠正"}),
        ("per:tech-yang", {"name": "小杨", "age_band": "25-30",
                           "occupation": "程序员", "location": "深圳",
                           "mbti": "INTP", "language_style": "理性简洁",
                           "bio": "AI 工具控，效率流"}),
    ]:
        node("seed", pid, "Persona", props)

    # ---- 节点：话题树（层级编码在 ID 里） ----
    for tid, label in [
        ("topic:beauty", "美妆"), ("topic:beauty.skincare", "护肤"),
        ("topic:beauty.makeup", "彩妆"), ("topic:fitness", "健身"),
        ("topic:fitness.home_workout", "居家训练"), ("topic:tech", "科技"),
        ("topic:tech.ai", "人工智能"), ("topic:tech.ai.llm", "大模型"),
    ]:
        node("seed", tid, "Topic", {"label": label})

    # ---- 节点：规则（kind 对应 tools.RULE_CHECKERS 注册表） ----
    for rid, props in [
        ("rule:xhs-new-account",
         {"name": "新号限流期禁发帖", "kind": "min_account_age_days",
          "params": {"days": 7}, "actions": ["post"],
          "platform": "xiaohongshu", "hard": True,
          "text": "注册不满 7 天的新号不发帖，只浏览互动"}),
        ("rule:xhs-post-rate",
         {"name": "发帖频控", "kind": "rate_limit_per_day",
          "params": {"actions": ["post"], "max": 2},
          "platform": "xiaohongshu", "hard": True,
          "text": "每账号每日最多发帖 2 篇"}),
        ("rule:topic-overlap",
         {"name": "话题与人设匹配", "kind": "topic_overlap", "params": {},
          "actions": ["post", "comment"], "hard": True,
          "text": "发帖/评论的话题必须落在人设兴趣树内"}),
        ("rule:persona-cooldown",
         {"name": "风控冷却", "kind": "cooldown", "params": {},
          "hard": True, "text": "冷却期内不做任何动作"}),
    ]:
        node("seed", rid, "Rule", props)

    # ---- 节点：记忆主体（数字生命）----
    # 每个 Subject 持有记忆/人设/情景；Account 是它在某平台的操作句柄。
    # lily 跨双平台（小红书 + TikTok）共享同一主体 —— 跨平台不失忆的验收样本。
    for sid, name in [
        ("subj:lily_beauty", "小莉"),
        ("subj:kai_fit", "阿凯"),
        ("subj:yang_tech", "小杨"),
        ("subj:momo_beauty", "默默"),        # 与 lily 共用人设但独立数字生命
        ("subj:lin_fit", "小林"),
    ]:
        node("seed", sid, "Subject",
             {"kind": "persona", "name": name, "created_at": NOW - 90 * DAY})

    # ---- 节点：账号 ----
    for aid, subj, age_days in [
        ("acc:xiaohongshu:lily_beauty", "subj:lily_beauty", 90),
        ("acc:tiktok:lily_beauty", "subj:lily_beauty", 45),      # 同一数字生命，第二平台
        ("acc:xiaohongshu:kai_fit", "subj:kai_fit", 120),
        ("acc:xiaohongshu:yang_tech", "subj:yang_tech", 120),
        ("acc:xiaohongshu:momo_beauty", "subj:momo_beauty", 2),  # 新号：触发限流规则
        ("acc:xiaohongshu:lin_fit", "subj:lin_fit", 60),
        ("acc:xiaohongshu:skincare_lover", None, 200),           # 外部 KOL：无主体
    ]:
        props = {"platform": aid.split(":")[1], "status": "active",
                 "risk_level": "low", "created_at": NOW - age_days * DAY}
        if subj is None:
            props["external"] = True
        node(aid, aid, "Account", props)

    # ---- 边 ----
    for pid, topics in [
        ("per:beauty-lily", ["topic:beauty.skincare", "topic:beauty.makeup"]),
        ("per:fit-kai", ["topic:fitness", "topic:fitness.home_workout"]),
        ("per:tech-yang", ["topic:tech.ai", "topic:tech.ai.llm"]),
    ]:
        for t in topics:
            edge("seed", pid, "INTERESTED_IN", t)
    for sid, per in [
        ("subj:lily_beauty", "per:beauty-lily"),
        ("subj:kai_fit", "per:fit-kai"),
        ("subj:yang_tech", "per:tech-yang"),
        ("subj:momo_beauty", "per:beauty-lily"),   # 人设规格可复用，数字生命独立
        ("subj:lin_fit", "per:fit-kai"),
    ]:
        edge(sid, sid, "HAS_PERSONA", per)
    for aid, subj in [
        ("acc:xiaohongshu:lily_beauty", "subj:lily_beauty"),
        ("acc:tiktok:lily_beauty", "subj:lily_beauty"),
        ("acc:xiaohongshu:kai_fit", "subj:kai_fit"),
        ("acc:xiaohongshu:yang_tech", "subj:yang_tech"),
        ("acc:xiaohongshu:momo_beauty", "subj:momo_beauty"),
        ("acc:xiaohongshu:lin_fit", "subj:lin_fit"),
    ]:
        edge(subj, subj, "HAS_HANDLE", aid)
    return ev


def _seed_history(queue):
    """为 lily 写入少量历史行为 + 一篇已发布帖子，让查询有内容可查。"""
    results = [
        {"action_id": "act:seed-001", "account_id": "acc:xiaohongshu:lily_beauty",
         "action": "browse", "topic_id": "topic:beauty.skincare",
         "digest": "浏览护肤热帖 10 分钟", "ts": NOW - 3 * 3600,
         "knowledge_refs": ["per:beauty-lily"]},
        {"action_id": "act:seed-002", "account_id": "acc:xiaohongshu:lily_beauty",
         "action": "like", "topic_id": "topic:beauty.skincare",
         "target_account": "acc:xiaohongshu:skincare_lover",
         "digest": "点赞 KOL 干货帖", "ts": NOW - 2 * 3600,
         "knowledge_refs": ["per:beauty-lily"]},
        {"action_id": "act:seed-003", "account_id": "acc:xiaohongshu:lily_beauty",
         "action": "comment", "topic_id": "topic:beauty.skincare",
         "target_account": "acc:xiaohongshu:skincare_lover",
         "digest": "提问防晒用量", "ts": NOW - 1 * 3600,
         "knowledge_refs": ["per:beauty-lily", "topic:beauty.skincare"]},
        {"action_id": "act:seed-004", "account_id": "acc:xiaohongshu:lily_beauty",
         "action": "post", "topic_id": "topic:beauty.skincare",
         "new_post_id": "post:xiaohongshu:p001",
         "digest": "夏季油皮护肤清单", "content_type": "text_with_image",
         "stats": {"views": 1200, "likes": 89, "comments": 12},
         "ts": NOW - 5 * 3600,
         "knowledge_refs": ["per:beauty-lily", "topic:beauty.skincare"]},
    ]
    for r in results:
        for e in extract_events_from_result(r):
            queue.submit(e)
    queue.drain()


def build_seed(store=None, queue=None):
    """返回 (store, queue)，数据已全部入库。
    可注入 store/queue（如 Neo4j 后端）；缺省用进程内存实现。
    """
    store = store or KnowledgeStore()
    queue = queue or WriteQueue(store)
    for e in _seed_events():
        queue.submit(e)
    queue.drain()
    _seed_history(queue)
    return store, queue
