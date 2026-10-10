#!/usr/bin/env python3
"""
端到端 Demo：验证知识库六问闭环
  摄取 → 联结 → 查询/决策 → 执行回写 → rollup 压缩 → 并发幂等

运行：python3 demo/run_demo.py   （Python 3.9+，零第三方依赖）
"""
import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from demo.seed import build_seed            # noqa: E402
from kb.ingest import extract_events_from_result  # noqa: E402
from kb.rollup import daily_rollup          # noqa: E402
from kb.tools import KBTools                # noqa: E402


def h1(title):
    print("\n" + "=" * 62)
    print(title)
    print("=" * 62)


def main():
    store, queue = build_seed()
    tools = KBTools(store, queue)
    lily = "acc:xiaohongshu:lily_beauty"
    momo = "acc:xiaohongshu:momo_beauty"

    # -------------------------------------------------------------
    h1("0. 种子知识库（全部经事件写入，Schema 校验覆盖）")
    print(f"节点 {store.stats()['nodes']} / 边 {store.stats()['edges']}"
          f"  | 账号 {len(store.node_ids('Account'))} / 人设 {len(store.node_ids('Persona'))}"
          f" / 话题 {len(store.node_ids('Topic'))} / 规则 {len(store.node_ids('Rule'))}")

    # -------------------------------------------------------------
    h1("1. 阶段一：规则闸门（纯代码，毫秒级，不调 LLM）")
    gate = tools.check_rules(momo, "post", "topic:beauty.skincare")
    print(f"新号 momo_beauty(2天) 发帖 → {'通过' if gate['pass'] else '拦截'}")
    for v in gate["violations"]:
        print(f"  ✗ [{v['rule']}] {v['name']}: {v['message']}")

    gate = tools.check_rules(lily, "post", "topic:beauty.skincare")
    print(f"老号 lily_beauty(90天) 发护肤帖 → {'通过' if gate['pass'] else '拦截'}"
          f"（检查了 {len(gate['checked'])} 条规则）")

    gate = tools.check_rules(lily, "post", "topic:fitness.home_workout")
    print(f"lily_beauty 发健身帖 → {'通过' if gate['pass'] else '拦截'}")
    for v in gate["violations"]:
        print(f"  ✗ [{v['rule']}] {v['name']}: {v['message']}")

    # -------------------------------------------------------------
    h1("2. 阶段二：决策（llm=None 走确定性桩，接真模型时传 llm=回调）")
    decision = tools.decide(lily, "post", "topic:beauty.skincare")
    for k in ("status", "action", "topic", "persona_fidelity_score",
              "context_tokens"):
        print(f"  {k}: {decision.get(k)}")
    print(f"  generated_content: {decision.get('generated_content', '')[:56]}…")
    print(f"  knowledge_refs: {decision['knowledge_refs']}")

    # -------------------------------------------------------------
    h1("3. 查询工具（Agent 视角的只读视图）")
    print("get_persona:",
          json.dumps(tools.get_persona(lily), ensure_ascii=False)[:160], "…")
    print("find_interaction_targets:",
          json.dumps(tools.find_interaction_targets(lily), ensure_ascii=False))
    print("search_knowledge('护肤'):",
          json.dumps(tools.search_knowledge("护肤"), ensure_ascii=False))

    # -------------------------------------------------------------
    h1("4. 执行结果回写（摄取漏斗 → 幂等队列 → 图谱）")
    result = {
        "action_id": "act:demo-101",
        "account_id": lily,
        "action": "post",
        "topic_id": "topic:beauty.skincare",
        "new_post_id": "post:xiaohongshu:p002",
        "digest": "秋冬换季护肤避坑指南",
        "content_type": "text_with_image",
        "stats": {"views": 350, "likes": 21, "comments": 3},
        "decision_id": decision["decision_id"],
        "knowledge_refs": decision["knowledge_refs"],
    }
    print("首次回写:  ", tools.submit_result(result))
    print("重试回写:  ", tools.submit_result(result), "← 同一结果，应全部去重")
    post = store.node_props("post:xiaohongshu:p002")
    print(f"帖子已入库: {post['digest']}  views={post.get('views')} likes={post.get('likes')}")
    print("决策可追溯 act:demo-101 --DECIDED_VIA-->",
          [d for _, d, _ in store.out_edges("act:demo-101", "DECIDED_VIA")])

    # -------------------------------------------------------------
    h1("5. 并发写入（3 账号并行提交 + 重复事件去重）")
    topics = {"lily_beauty": "topic:beauty.skincare",
              "kai_fit": "topic:fitness.home_workout",
              "yang_tech": "topic:tech.ai.llm"}

    def worker(handle, topic):
        for i in range(5):
            r = {"action_id": f"act:conc-{handle}-{i}",
                 "account_id": f"acc:xiaohongshu:{handle}",
                 "action": "like", "topic_id": topic,
                 "target_account": "acc:xiaohongshu:skincare_lover",
                 "digest": f"并发点赞 {i}"}
            events = extract_events_from_result(r)
            for e in events:
                queue.submit(e)
            if i == 0:  # 模拟网络重试：同一结果重复提交
                for e in events:
                    queue.submit(e)

    threads = [threading.Thread(target=worker, args=(h, t))
               for h, t in topics.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"队列统计: {queue.stats}  pending={queue.pending()}")
    queue.drain()
    edge = store.get_edge(lily, "INTERACTS_WITH", "acc:xiaohongshu:skincare_lover")
    print(f"lily→skincare_lover 互动计数 = {edge['count']}（种子 2 + 并发 5，重试被去重）")

    # -------------------------------------------------------------
    h1("6. Rollup 压缩（日流水 → 日摘要 → 归档）")
    ru = daily_rollup(store, lily)
    print(f"归档动作数: {ru['archived_actions']}  计数: {ru['counts']}")
    print(f"摘要: {ru['summary']}")
    print(f"压缩效果: {ru['compression']['raw_chars']} 字符"
          f" → {ru['compression']['summary_chars']} 字符")
    state = tools.get_account_state(lily)
    print("归档后热区 recent_actions:", state["recent_actions"], "（只剩未归档）")
    print("摘要可被检索:", tools.search_knowledge("动作")[:1])

    # -------------------------------------------------------------
    h1("完成")
    print(f"最终: 节点 {store.stats()['nodes']} / 边 {store.stats()['edges']}"
          f" / 队列 {queue.stats}")


if __name__ == "__main__":
    main()
