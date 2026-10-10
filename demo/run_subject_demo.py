#!/usr/bin/env python3
"""
主体模型验收（docs/05 §11 第 2 步）：Subject = 记忆主体（数字生命），
Account = 平台操作句柄。三组验收：

1. 跨平台不失忆：同一数字生命（subj:lily_beauty）的小红书账号与 TikTok
   账号共享记忆/承诺/情景——从任一句柄看到的是同一个"人生"
2. 承诺按主体隔离（方案 B）：同一条事实文本被两个主体记住 → 共享同一
   个 MemoryNote 节点（指纹幂等），但 status 在各自的 HAS_MEMORY 边上——
   lily 兑现不影响 kai
3. 结构断言：MemoryNote 节点只存客观字段（fact/category）；主观状态
   （status/pinned/confidence/ts）只在边上；回写事件分区 = 记忆主体

跑法：python3 demo/run_subject_demo.py（无 LLM / 网络 / 真机）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from demo.seed import build_seed                                    # noqa: E402
from kb.rollup import (fulfill_promise, memory_edges, open_promises,  # noqa: E402
                       record_promise)
from kb.tools import KBTools                                        # noqa: E402

LILY_XHS = "acc:xiaohongshu:lily_beauty"
LILY_TT = "acc:tiktok:lily_beauty"        # 同一数字生命，第二平台
KAI = "acc:xiaohongshu:kai_fit"
SUBJ_LILY = "subj:lily_beauty"
SUBJ_KAI = "subj:kai_fit"
TOPIC = "topic:beauty.skincare"

checks = []


def ok(name, cond, detail=""):
    checks.append((name, bool(cond)))
    print(f"  {'✅' if cond else '❌'} {name}" + (f"（{detail}）" if detail else ""))


def h1(t):
    print("\n" + "=" * 62 + f"\n{t}\n" + "=" * 62)


def main():
    store, queue = build_seed()
    tools = KBTools(store, queue)

    h1("1. 跨平台不失忆：一个数字生命，两个平台句柄")
    subj_x = store.subject_of(LILY_XHS)
    subj_t = store.subject_of(LILY_TT)
    ok("两个平台账号归一到同一 Subject", subj_x == subj_t == SUBJ_LILY,
       f"{subj_x} == {subj_t}")
    ok("Subject 持有 2 个平台句柄", len(store.accounts_of(SUBJ_LILY)) == 2,
       f"{store.accounts_of(SUBJ_LILY)}")
    ok("人设在主体上（两平台共享同一设定）",
       store.persona_id_of(LILY_XHS) == store.persona_id_of(LILY_TT) ==
       "per:beauty-lily")

    # 从小红书句柄许下的承诺
    rec = record_promise(store, LILY_XHS, "下周实测早 C 晚 A 全流程", post_id=None)
    note_id = rec["note_id"]
    ok("承诺经小红书句柄写入", len(open_promises(store, LILY_XHS)) == 1)

    # TikTok 句柄能看到 → 不失忆
    tt_promises = open_promises(store, LILY_TT)
    ok("TikTok 句柄能看到同一承诺（记忆互通）",
       len(tt_promises) == 1 and tt_promises[0]["note_id"] == note_id)
    recall_tt = tools.recall_memory(LILY_TT, "实测")
    ok("TikTok 句柄能召回承诺（置顶）", len(recall_tt["open_promises"]) == 1)
    # 固化几条事实性语义记忆（stub 提取器，从种子帖子里沉淀），再验证召回
    from kb.rollup import consolidate_memory
    consolidate_memory(store, LILY_XHS, since_days=30)
    recall_tt = tools.recall_memory(LILY_TT, "实测")
    ok("TikTok 句柄能召回语义记忆（小红书经历沉淀）",
       len(recall_tt["memories"]) >= 1,
       f"首条: {(recall_tt['memories'][0]['fact'] if recall_tt['memories'] else '')[:24]}")

    # 行为也从两个平台汇入同一叙事
    tools.submit_result({"action_id": "act:subj-tt-1", "account_id": LILY_TT,
                         "action": "post", "topic_id": TOPIC,
                         "new_post_id": "post:tiktok:tt1", "digest": "TikTok 首帖"})
    from kb.rollup import daily_rollup, period_rollup
    daily_rollup(store, LILY_TT)
    period_rollup(store, LILY_TT, level="week")
    eps = [d for _, d, _ in store.out_edges(SUBJ_LILY, "HAS_EPISODE")]
    ok("情景记忆挂在主体上（Episode ID 用主体 slug）",
       len(eps) >= 1 and any(e.startswith("ep:lily_beauty.") for e in eps),
       f"{eps[:2]}")
    st = tools.get_account_state(LILY_TT)
    ok("日摘要是主体的（跨平台汇入同一天）", bool(st.get("last_summary")))

    h1("2. 承诺按主体隔离：共享节点、状态各持（方案 B）")
    kai_note = record_promise(store, KAI, "下周实测早 C 晚 A 全流程")["note_id"]
    ok("同文本 → 同一指纹节点（跨主体去重）", kai_note == note_id)
    holders = sorted(s for _, s, _ in store.in_edges(note_id, "HAS_MEMORY"))
    ok("该节点被两个主体共同持有", holders == [SUBJ_KAI, SUBJ_LILY], f"{holders}")
    ok("两个主体各自看到待兑现", len(open_promises(store, LILY_XHS)) == 1
       and len(open_promises(store, KAI)) == 1)

    fp = fulfill_promise(store, note_id, LILY_XHS, by_ref="post:tiktok:tt1")
    ok("lily 兑现成功", fp["changed"])
    ok("lily 侧承诺关闭", len(open_promises(store, LILY_XHS)) == 0)
    ok("kai 侧不受影响（A 兑现 ≠ B 兑现）", len(open_promises(store, KAI)) == 1)
    e_lily = store.get_edge(SUBJ_LILY, "HAS_MEMORY", note_id)
    e_kai = store.get_edge(SUBJ_KAI, "HAS_MEMORY", note_id)
    ok("边上状态分叉：lily=fulfilled kai=open",
       e_lily.get("status") == "fulfilled" and e_kai.get("status") == "open")

    h1("3. 结构与写入路径断言")
    n = store.node_props(note_id)
    ok("MemoryNote 节点只存客观字段（fact/category）",
       set(n.keys()) == {"fact", "category"}, f"{sorted(n.keys())}")
    ok("主观状态在 HAS_MEMORY 边上",
       {"status", "pinned", "confidence", "ts"} <= set(e_kai.keys()),
       f"{sorted(e_kai.keys())}")

    # 回写分区 = 记忆主体（同主体跨平台串行写）
    captured = []
    orig_submit = queue.submit

    def spy_submit(ev):
        captured.append(ev.partition)
        return orig_submit(ev)

    queue.submit = spy_submit
    tools.submit_result({"action_id": "act:subj-part-1", "account_id": LILY_TT,
                         "action": "like", "topic_id": TOPIC})
    ok("回写事件分区 = Subject（非账号）",
       captured and set(captured) == {SUBJ_LILY}, f"{set(captured)}")

    # 兑现帖走 /report 路径的自动承诺扫描也归属主体
    from kb.rollup import stub_promise_scanner
    tools.track_promises(LILY_TT, "姐妹们，下周分享实测数据！",
                         post_id="post:tiktok:tt1")
    ok("track_promises 经主体归属（TikTok 发帖埋的坑 xhs 可见）",
       any("实测" in p["fact"] for p in open_promises(store, LILY_XHS)))

    print("=" * 62)
    failed = [n for n, c in checks if not c]
    if failed:
        print(f"主体模型验收失败 {len(failed)} 项:")
        for n in failed:
            print(f"  ✗ {n}")
        sys.exit(1)
    print(f"主体模型验收通过：{len(checks)} 项全绿")
    print("Subject=数字生命（记忆/人设/情景），Account=平台句柄；")
    print("MemoryNote 节点客观、状态在边（跨主体共享 + 主观隔离）")


if __name__ == "__main__":
    main()
