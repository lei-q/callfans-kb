#!/usr/bin/env python3
"""
Neo4j 持久化后端实机 Demo：全链路 + 双进程并发去重 + 状态恢复。

前置：
  docker compose up -d（或已有 Neo4j 实例）
  pip install neo4j
  export CALLFANS_NEO4J_URI=bolt://localhost:7687
  export CALLFANS_NEO4J_USER=neo4j
  export CALLFANS_NEO4J_PASSWORD=callfans-dev

运行：python3 demo/run_neo4j_demo.py [--keep]（默认结束清库，--keep 保留）
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from demo.seed import build_seed                                  # noqa: E402
from kb.factory import open_store, open_queue                     # noqa: E402
from kb.rollup import open_promises, fulfill_promise              # noqa: E402
from kb.tools import KBTools                                      # noqa: E402

LILY = "acc:xiaohongshu:lily_beauty"
TOPIC = "topic:beauty.skincare"

# 双进程并发测试用（子进程执行同一份逻辑）
_DUAL_PROC_SCRIPT = r"""
import sys, os
sys.path.insert(0, os.getcwd())
from kb.factory import open_store, open_queue
from kb.tools import KBTools
store = open_store()
queue = open_queue(store)
tools = KBTools(store, queue)
res = tools.submit_result({
    "action_id": "act:neo4j-dual",
    "account_id": "acc:xiaohongshu:lily_beauty",
    "action": "like", "topic_id": "topic:beauty.skincare",
    "digest": "双进程并发测试", "target_account": "acc:xiaohongshu:skincare_lover",
})
print("SUBMIT_STATS", res, "QUEUE", dict(queue.stats))
"""


def h1(title):
    print("\n" + "=" * 62)
    print(title)
    print("=" * 62)


def main():
    keep = "--keep" in sys.argv
    # 显式切换 Neo4j 后端（子进程继承，避免工厂默认回落内存模式）
    os.environ["CALLFANS_STORE"] = "neo4j"
    store = open_store()
    if hasattr(store, "wipe") and not keep:
        store.wipe()
        print("已清空 Neo4j 数据（--keep 可保留上一轮数据）")

    queue = open_queue(store)
    tools = KBTools(store, queue)

    # -------------------------------------------------------------
    h1("1. 种子数据经事件路径写入 Neo4j")
    store, queue = build_seed(store=store, queue=queue)
    tools = KBTools(store, queue)
    print(f"图谱: {store.stats()}")
    p = tools.get_persona(LILY)
    print(f"人设卡: {p['name']}（{p['persona_id']}）兴趣 {len(p['interests'])} 项")
    assert p["name"] == "小莉"

    # -------------------------------------------------------------
    h1("2. 决策 → 回写 → rollup → 承诺闭环（与内存版同一条路径）")
    d = tools.decide(LILY, "post", TOPIC)
    assert d["status"] == "approved"
    print(f"决策: {d['decision_id']}  fidelity={d['persona_fidelity_score']}")
    res = tools.submit_result({
        "action_id": "act:neo4j-1", "account_id": LILY, "action": "post",
        "topic_id": TOPIC, "new_post_id": "post:xiaohongshu:n1",
        "digest": "Neo4j 持久化验证帖", "stats": {"likes": 42},
        "decision_id": d["decision_id"], "knowledge_refs": d["knowledge_refs"]})
    print(f"回写: {res}")
    tp = tools.track_promises(LILY, "下周实测持妆粉底，人多我立马安排！",
                              post_id="post:xiaohongshu:n1")
    print(f"承诺扫描: {tp}")
    from kb.rollup import daily_rollup, period_rollup, consolidate_memory
    ru = daily_rollup(store, LILY, day="20261010")
    wr = period_rollup(store, LILY, level="week", period="2026W41")
    cm = consolidate_memory(store, LILY, since_days=30)
    print(f"日 rollup 归档 {ru['archived_actions']} 动作；周 Episode {wr['episode_id']}；固化 {cm['facts']} 条")
    promises = open_promises(store, LILY)
    assert promises, "承诺应已入库"
    fp = fulfill_promise(store, promises[0]["note_id"], LILY, by_ref="post:xiaohongshu:n1")
    print(f"兑现: {fp['status']}  剩余未兑现 {len(open_promises(store, LILY))}")

    # -------------------------------------------------------------
    h1("3. 幂等：同结果重提 → 全部事件 duplicate")
    before = store.stats()
    res2 = tools.submit_result({
        "action_id": "act:neo4j-1", "account_id": LILY, "action": "post",
        "topic_id": TOPIC, "new_post_id": "post:xiaohongshu:n1",
        "digest": "Neo4j 持久化验证帖", "stats": {"likes": 42}})
    after = store.stats()
    print(f"重提: accepted={res2['accepted']} duplicates={res2['duplicates']}")
    print(f"事件数: {before['events']} → {after['events']}（不再增长）")
    assert res2["accepted"] == 0 and res2["duplicates"] == res2["events"]
    assert after["events"] == before["events"]

    # -------------------------------------------------------------
    h1("4. 双进程并发：两进程同时提交同一事件 → 唯一约束去重")
    edge_key = (LILY, "INTERACTS_WITH", "acc:xiaohongshu:skincare_lover")
    base_count = (store.get_edge(*edge_key) or {}).get("count", 0)
    procs = [subprocess.Popen([sys.executable, "-c", _DUAL_PROC_SCRIPT],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True)
             for _ in range(2)]
    outs = [p.communicate()[0] for p in procs]
    codes = [p.returncode for p in procs]
    for o, c in zip(outs, codes):
        line = [l for l in o.splitlines() if l.startswith("SUBMIT_STATS")]
        print(f"  子进程(exit={c}): {line[0] if line else o[-200:]}")
    import re as _re
    def _stat(key, outs):
        total = 0
        for o in outs:
            for l in o.splitlines():
                if l.startswith("SUBMIT_STATS"):
                    m = _re.search(rf"'{key}': (\d+)", l)
                    if m:
                        total += int(m.group(1))
        return total
    total_applied = _stat("applied", outs)
    total_dup = _stat("duplicated", outs)
    print(f"  合计: applied={total_applied} duplicated={total_dup}")
    # 双进程同一批事件：胜者应用全部，败者全部 duplicate（无重复应用）
    assert total_applied == 4 and total_dup == 4, "应一胜一败，无重复应用"
    edge = store.get_edge(*edge_key)
    print(f"  互动边 count={edge['count']}（基线 {base_count} + 1，无重复计数）")
    assert edge["count"] == base_count + 1

    # -------------------------------------------------------------
    h1("5. 状态恢复：新进程（新连接）读回全部数据")
    store2 = open_store()
    tools2 = KBTools(store2, open_queue(store2))
    p2 = tools2.get_persona(LILY)
    state2 = tools2.get_account_state(LILY)
    mem = tools2.recall_memory(LILY, "post 护肤")
    print(f"新进程读回: 人设={p2['name']}  状态摘要={bool(state2['last_summary'])}"
          f"  记忆召回={len(mem['memories'])} 条")
    assert p2["name"] == "小莉" and state2["last_summary"]
    assert store2.stats()["nodes"] == store.stats()["nodes"]
    print(f"图谱一致: {store2.stats()}")

    # -------------------------------------------------------------
    h1("6. maintenance（cron 入口）在 Neo4j 上运行")
    from kb.maintenance import daily_maintenance, weekly_maintenance
    dm = daily_maintenance(tools)
    wm = weekly_maintenance(tools)
    print(f"daily: {dm['accounts']} 账号, rolled={dm['rolled']}")
    print(f"weekly: episodes={wm['episodes']}, facts={wm['facts']}")

    # -------------------------------------------------------------
    if not keep:
        store.wipe()
        print("\n已清库。完整流程验证通过。")
    else:
        print(f"\n数据保留: {store.stats()}")


if __name__ == "__main__":
    main()
