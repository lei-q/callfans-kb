"""
维护任务：定时编排的统一入口（cron / 调度平台直接调用）。

设计原则（docs/04）：
- 全部幂等：到点就跑、跑挂了下次再跑、漏跑了补跑即可，无需调度语义
- 条件跳过：无数据的账号/周期自动 skip，不产生空摘要
- LLM 钩子全部可选：传入即用 LLM，不传走确定性兜底，永不因 LLM 故障中断

用法（cron 建议）：
  python3 -m kb.maintenance daily     # 每天 03:00-04:00，各实例错峰
  python3 -m kb.maintenance weekly    # 每周一 03:30
  python3 -m kb.maintenance monthly   # 每月 1 日 03:30
存储后端由 CALLFANS_STORE 决定（memory|neo4j）。
"""
from __future__ import annotations

from .rollup import (consolidate_memory, daily_rollup, expire_stale_notes,
                     period_rollup)


def matrix_subjects(store) -> list:
    """矩阵记忆主体 = 全部 Subject 节点（数字生命）。rollup/固化/淘汰按主体跑，
    同一主体的多平台账号自动汇入（rollup 内部归一）。"""
    return list(store.node_ids("Subject"))


def matrix_accounts(store) -> list:
    """矩阵账号 = 挂在某个记忆主体下的平台账号句柄（外部 KOL 不在其列）。
    决策流/账号墙等账号级视图用它。"""
    out = []
    for nid in store.node_ids("Account"):
        if any(True for _ in store.in_edges(nid, "HAS_HANDLE")):
            out.append(nid)
    return out


def daily_maintenance(tools, summarizer=None, day=None) -> dict:
    """每账号：未归档行为流水 → 日摘要（无未归档则跳过）。"""
    results = {}
    for subj in matrix_subjects(tools.store):
        try:
            r = daily_rollup(tools.store, subj, day=day, summarizer=summarizer)
            results[subj] = r
        except Exception as e:                      # 单账号失败不影响其他账号
            results[subj] = {"error": str(e)}
    return {"accounts": len(results),
            "rolled": sum(1 for r in results.values() if r.get("archived_actions")),
            "results": results}


def weekly_maintenance(tools, summarizer=None, extractor=None,
                       period=None, since_days=7, max_age_days=90) -> dict:
    """每账号：周卷积（Episode）+ 记忆固化（MemoryNote）+ 时效淘汰。建议周一跑。"""
    rollups, consos = {}, {}
    expired = 0
    for subj in matrix_subjects(tools.store):
        try:
            rollups[subj] = period_rollup(tools.store, subj, level="week",
                                          period=period, summarizer=summarizer)
        except Exception as e:
            rollups[subj] = {"error": str(e)}
        try:
            consos[subj] = consolidate_memory(tools.store, subj,
                                              extractor=extractor,
                                              since_days=since_days)
        except Exception as e:
            consos[subj] = {"error": str(e)}
        try:
            expired += expire_stale_notes(tools.store, subj,
                                          max_age_days=max_age_days)["expired"]
        except Exception:
            pass
    return {"accounts": len(rollups),
            "episodes": sum(1 for r in rollups.values() if r.get("episode_id")),
            "facts": sum(r.get("facts", 0) for r in consos.values()),
            "expired_notes": expired,
            "rollups": rollups, "consolidations": consos}


def monthly_maintenance(tools, summarizer=None, period=None) -> dict:
    """每账号：月卷积。建议每月 1 日跑。"""
    results = {}
    for subj in matrix_subjects(tools.store):
        try:
            results[subj] = period_rollup(tools.store, subj, level="month",
                                          period=period, summarizer=summarizer)
        except Exception as e:
            results[subj] = {"error": str(e)}
    return {"accounts": len(results),
            "episodes": sum(1 for r in results.values() if r.get("episode_id")),
            "results": results}


def yearly_maintenance(tools, summarizer=None, period=None) -> dict:
    """每账号：年卷积（年度叙事）。"""
    results = {}
    for subj in matrix_subjects(tools.store):
        try:
            results[subj] = period_rollup(tools.store, subj, level="year",
                                          period=period, summarizer=summarizer)
        except Exception as e:
            results[subj] = {"error": str(e)}
    return {"accounts": len(results),
            "episodes": sum(1 for r in results.values() if r.get("episode_id")),
            "results": results}


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------
def _main(argv=None):
    import argparse
    import json
    import sys
    import os

    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))

    from kb.factory import open_kb
    from kb.llm import (LLMClient, make_summarizer, make_period_summarizer,
                        make_memory_extractor)
    from kb.tools import KBTools

    parser = argparse.ArgumentParser(
        prog="kb.maintenance", description="知识库维护任务（cron 调用）")
    parser.add_argument("level", choices=["daily", "weekly", "monthly", "yearly"])
    parser.add_argument("--period", default=None, help="指定周期（如 2026W41）")
    parser.add_argument("--day", default=None, help="指定日期（如 20261009）")
    parser.add_argument("--llm", action="store_true",
                        help="启用 LLM 钩子（需 CALLFANS_LLM_* 环境变量）")
    args = parser.parse_args(argv)

    store, queue = open_kb()
    tools = KBTools(store, queue)

    summarizer = extractor = None
    if args.llm:
        client = LLMClient()
        summarizer = (make_summarizer(client) if args.level == "daily"
                      else make_period_summarizer(client))
        if args.level == "weekly":
            extractor = make_memory_extractor(client)

    if args.level == "daily":
        out = daily_maintenance(tools, summarizer=summarizer, day=args.day)
    elif args.level == "weekly":
        out = weekly_maintenance(tools, summarizer=summarizer,
                                 extractor=extractor, period=args.period)
    elif args.level == "monthly":
        out = monthly_maintenance(tools, summarizer=summarizer, period=args.period)
    else:
        out = yearly_maintenance(tools, summarizer=summarizer, period=args.period)

    summary = {k: v for k, v in out.items() if k not in ("results", "rollups",
                                                         "consolidations")}
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    _main()
