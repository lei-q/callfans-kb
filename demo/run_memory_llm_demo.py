#!/usr/bin/env python3
"""
长期记忆 LLM 实机 Demo：周期叙事摘要 / 记忆固化抽取 / 承诺识别与兑现。

运行前配置（任一 OpenAI Chat Completions 兼容服务）：
  export CALLFANS_LLM_BASE_URL=https://api.proma.cool
  export CALLFANS_LLM_API_KEY=sk-...
  export CALLFANS_LLM_MODEL=glm-5.3-flash

运行：python3 demo/run_memory_llm_demo.py
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from demo.seed import build_seed                                    # noqa: E402
from kb.llm import (LLMClient, make_decision_llm, make_extract_llm, # noqa: E402
                    make_summarizer, make_period_summarizer,
                    make_memory_extractor, make_promise_scanner)
from kb.rollup import (daily_rollup, period_rollup,                 # noqa: E402
                       consolidate_memory, open_promises, fulfill_promise)
from kb.tools import KBTools                                        # noqa: E402

LILY = "acc:xiaohongshu:lily_beauty"
TOPIC = "topic:beauty.skincare"


def day_ts(d, hour=10):
    return datetime(2026, 10, d, hour).timestamp()


def h1(title):
    print("\n" + "=" * 62)
    print(title)
    print("=" * 62)


def main():
    client = LLMClient()
    print(f"LLM 网关: {client.base_url}  模型: {client.model}")

    store, queue = build_seed()
    tools = KBTools(store, queue)

    # -------------------------------------------------------------
    h1("1. 模拟三天运营（发帖带 raw_text，评论区出现承诺性追问）")
    total_write = {"events": 0, "accepted": 0, "duplicates": 0}
    for d, actions in [(5, [("browse", None), ("like", None)]),
                       (6, [("post", "防晒实测帖")]),
                       (7, [("comment", None)])]:
        for i, (action, note) in enumerate(actions):
            res = tools.submit_result({
                "action_id": f"act:llmmem-{d}-{i}",
                "account_id": LILY, "action": action, "topic_id": TOPIC,
                "digest": note or f"{action}@skincare",
                "new_post_id": "post:xiaohongshu:lp1" if action == "post" else None,
                "stats": {"views": 4800, "likes": 96, "comments": 15}
                          if action == "post" else {},
                "ts": day_ts(d, 10 + i),
                "raw_text": ("发布成功。评论区高赞反馈：干皮姐妹求面霜推荐；"
                             "多人追问下一期能不能讲刷酸——先答应粉丝下周安排。"
                             ) if action == "post" else "",
            }, llm=make_extract_llm(client))
            for k in total_write:
                total_write[k] += res.get(k, 0)
        ru = daily_rollup(store, LILY, day=f"202610{d:02d}",
                          summarizer=make_summarizer(client))
        print(f"  10-{d:02d}: {ru['summary'][:50]}…")
    print(f"  三天回写累计: {total_write}（accepted 含 LLM 候选）")

    # -------------------------------------------------------------
    h1("2. LLM 周叙事（period_rollup + make_period_summarizer）")
    wr = period_rollup(store, LILY, level="week", period="2026W41",
                       summarizer=make_period_summarizer(client))
    print(f"  漂移: {wr['drift']}  Episode: {wr['episode_id']}")
    print(f"  叙事: {wr['narrative']}")

    # -------------------------------------------------------------
    h1("3. LLM 记忆固化（consolidate_memory + make_memory_extractor）")
    cm = consolidate_memory(store, LILY, extractor=make_memory_extractor(client),
                            since_days=30)
    print(f"  固化事实 {cm['facts']} 条（含承诺 {cm['promises']} 条）")
    for nid in cm["notes"]:
        p = store.node_props(nid)
        print(f"  {nid} [{p['category']}/{p['status']}{'/pinned' if p['pinned'] else ''}] {p['fact']}")

    # 抽取钩子（第 1 步）也可能识别到承诺：合并看未兑现清单
    promises = open_promises(store, LILY)
    print(f"  当前未兑现承诺（含抽取钩子识别）: {len(promises)} 条")
    for pr in promises:
        print(f"    - {pr['fact']}")

    # -------------------------------------------------------------
    h1("4. 承诺进入决策：LLM 围绕未兑现承诺创作")
    d = tools.decide(LILY, "post", TOPIC, llm=make_decision_llm(client))
    print(f"status: {d['status']}  llm: {d.get('llm')}")
    print(f"content: {d['generated_content']}")
    audit = tools.audit_decision(d)
    print(f"审计: ok={audit['ok']}  findings={audit['findings'] or '无'}")

    # 生成内容埋坑：LLM 在创作中新埋的承诺也要追踪（v2 缺口补齐）
    tp = tools.track_promises(LILY, d["generated_content"],
                              post_id="post:xiaohongshu:lp1",
                              scanner=make_promise_scanner(client))
    print(f"内容埋坑: {tp}")
    for pr in open_promises(store, LILY):
        print(f"  未兑现: {pr['fact']}")

    # -------------------------------------------------------------
    h1("5. 兑现闭环 + 幂等")
    if promises:
        # 真实流程：执行层发布兑现帖 → 回写 → fulfill_promise 指向该帖
        tools.submit_result({
            "action_id": "act:llmmem-fulfill", "account_id": LILY,
            "action": "post", "topic_id": TOPIC,
            "new_post_id": "post:xiaohongshu:lp2",
            "digest": "刷酸实测：答应粉丝的专题来了", "ts": day_ts(9, 18)})
        fp = fulfill_promise(store, promises[0]["note_id"],
                             by_ref="post:xiaohongshu:lp2")
        print(f"  兑现: {fp}")
        print(f"  剩余未兑现: {len(open_promises(store, LILY))} 条")
    n_before = len(store.node_ids())
    consolidate_memory(store, LILY, extractor=make_memory_extractor(client),
                       since_days=30)
    print(f"  重复固化: 节点 {n_before} → {len(store.node_ids())}（幂等）")

    # -------------------------------------------------------------
    h1("6. LLM 用量（成本核算）")
    print(client.usage)
    print(f"最终图谱: {store.stats()}")


if __name__ == "__main__":
    main()
