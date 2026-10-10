#!/usr/bin/env python3
"""
SQLite 嵌入式后端实机 Demo：全链路 + 双进程并发去重 + 新进程状态恢复。

前置：无（sqlite3 是 Python stdlib）。默认库文件 build/callfans-demo.db。

运行：python3 demo/run_sqlite_demo.py [--keep]
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "build", "callfans-demo.db")

_DUAL_PROC_SCRIPT = r"""
import sys, os
sys.path.insert(0, os.getcwd())
from kb.factory import open_store, open_queue
from kb.tools import KBTools
store = open_store()
queue = open_queue(store)
tools = KBTools(store, queue)
res = tools.submit_result({
    "action_id": "act:sqlite-dual",
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
    from demo.seed import build_seed
    from kb.factory import open_store, open_queue
    from kb.rollup import (daily_rollup, fulfill_promise, open_promises,
                           period_rollup)
    from kb.tools import KBTools

    keep = "--keep" in sys.argv
    env = {"CALLFANS_STORE": "sqlite", "CALLFANS_SQLITE_PATH": DB}
    os.environ.update(env)          # 子进程继承
    store = open_store(env)
    if not keep:
        store.wipe()
        print(f"已清空 {DB}（--keep 可保留上一轮数据）")

    # -------------------------------------------------------------
    h1("1. 种子数据经事件路径写入 SQLite")
    store, queue = build_seed(store=store, queue=open_queue(store))
    tools = KBTools(store, queue)
    print(f"图谱: {store.stats()}")
    assert tools.get_persona("acc:xiaohongshu:lily_beauty")["name"] == "小莉"

    # -------------------------------------------------------------
    h1("2. 决策 → 回写 → rollup → 承诺闭环")
    LILY, TOPIC = "acc:xiaohongshu:lily_beauty", "topic:beauty.skincare"
    d = tools.decide(LILY, "post", TOPIC)
    assert d["status"] == "approved"
    res = tools.submit_result({
        "action_id": "act:sqlite-1", "account_id": LILY, "action": "post",
        "topic_id": TOPIC, "new_post_id": "post:xiaohongshu:s1",
        "digest": "SQLite 持久化验证帖", "stats": {"likes": 42},
        "decision_id": d["decision_id"], "knowledge_refs": d["knowledge_refs"]})
    print(f"回写: {res}")
    tools.track_promises(LILY, "下周实测持妆粉底，人多我立马安排！",
                         post_id="post:xiaohongshu:s1")
    daily_rollup(store, LILY, day="20261010")
    wr = period_rollup(store, LILY, level="week", period="2026W41")
    promises = open_promises(store, LILY)
    fp = fulfill_promise(store, promises[0]["note_id"], LILY,
                         by_ref="post:xiaohongshu:s1")
    print(f"周 Episode: {wr['episode_id']}  兑现: {fp['status']}")
    assert fp["status"] == "fulfilled"

    # -------------------------------------------------------------
    h1("3. 幂等：同结果重提 → 全部 duplicate，事件数不涨")
    before = store.stats()["events"]
    res2 = tools.submit_result({
        "action_id": "act:sqlite-1", "account_id": LILY, "action": "post",
        "topic_id": TOPIC, "new_post_id": "post:xiaohongshu:s1",
        "digest": "SQLite 持久化验证帖", "stats": {"likes": 42}})
    after = store.stats()["events"]
    print(f"重提: accepted={res2['accepted']} duplicates={res2['duplicates']}"
          f"  事件数 {before} → {after}")
    assert res2["accepted"] == 0 and before == after

    # -------------------------------------------------------------
    h1("4. 双进程并发：两进程同时提交同一事件 → INSERT OR IGNORE 去重")
    edge_key = (LILY, "INTERACTS_WITH", "acc:xiaohongshu:skincare_lover")
    base = (store.get_edge(*edge_key) or {}).get("count", 0)
    procs = [subprocess.Popen([sys.executable, "-c", _DUAL_PROC_SCRIPT],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, cwd=ROOT)
             for _ in range(2)]
    outs = [p.communicate()[0] for p in procs]
    import re
    applied = dup = 0
    for o in outs:
        for l in o.splitlines():
            if l.startswith("SUBMIT_STATS"):
                print(f"  {l[:90]}")
                m = re.search(r"'applied': (\d+)", l)
                m2 = re.search(r"'duplicated': (\d+)", l)
                applied += int(m.group(1)) if m else 0
                dup += int(m2.group(1)) if m2 else 0
    edge = store.get_edge(*edge_key)
    print(f"  合计 applied={applied} duplicated={dup}  "
          f"互动边 count={edge['count']}（基线 {base} + 1）")
    assert applied == 4 and dup == 4 and edge["count"] == base + 1

    # -------------------------------------------------------------
    h1("5. 状态恢复：新进程（新连接）读回全部数据")
    store2 = open_store(env)
    tools2 = KBTools(store2, open_queue(store2))
    p2 = tools2.get_persona(LILY)
    assert p2["name"] == "小莉" and tools2.get_account_state(LILY)["last_summary"]
    assert store2.stats()["nodes"] == store.stats()["nodes"]
    print(f"新进程读回一致: {store2.stats()}")

    # -------------------------------------------------------------
    h1("6. maintenance 在 SQLite 上运行（幂等）")
    from kb.maintenance import daily_maintenance, weekly_maintenance
    dm = daily_maintenance(tools)
    dm2 = daily_maintenance(tools)
    print(f"daily: {dm['accounts']} 账号 rolled={dm['rolled']}；"
          f"重复执行 rolled={dm2['rolled']}（幂等）")
    assert dm2["rolled"] == 0

    if not keep:
        store.wipe()
        print("\n已清库。完整流程验证通过。")
    else:
        print(f"\n数据保留: {store.stats()}")


if __name__ == "__main__":
    main()
