"""
统一 CLI：python3 -m kb <command>

命令与 GUI 动作一一对应（Cmd+K 面板复用同一套词汇）：
  status                      图谱/后端/账号概览
  query <文本>                知识检索（bigram 倒排）
  memory <account_id>         人可读记忆摘要（分层路由索引）
  plan <account> <action> [topic] [--job-id]   决策 → 执行命令（dry run）
  maintenance <daily|weekly|monthly|yearly> [--llm]   维护任务（cron 同款）
  serve [--host --port]       本地 HTTP API（GUI / 远程客户端）

存储后端由 CALLFANS_STORE 决定（memory|sqlite|neo4j）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _kb():
    """惰性初始化（保持 --help 零开销）。"""
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    from kb.factory import open_kb
    from kb.tools import KBTools
    store, queue = open_kb()
    return store, queue, KBTools(store, queue)


def _backend_name(store) -> str:
    return type(store).__name__.replace("KnowledgeStore", "").lower() or "memory"


# ---------------------------------------------------------------------------
def cmd_status(args):
    store, queue, tools = _kb()
    from kb.maintenance import matrix_accounts
    stats = store.stats()
    accounts = matrix_accounts(store)
    print(f"后端: {_backend_name(store)}")
    print(f"图谱: {stats}")
    print(f"矩阵账号: {len(accounts)}")
    for acc in accounts[:10]:
        try:
            s = tools.get_account_state(acc)
            print(f"  {acc}  风险={s.get('risk_level')}  "
                  f"今日={s.get('today_action_counts') or '无'}")
        except Exception as e:
            print(f"  {acc}  (状态读取失败: {e})")


def cmd_query(args):
    store, queue, tools = _kb()
    hits = tools.search_knowledge(args.text, k=args.k)
    if not hits:
        print("（无命中）")
        return
    for h in hits:
        print(f"[{h['type']}] {h['node_id']}  score={h['score']}")
        print(f"  {h['snippet'][:76]}")


def cmd_memory(args):
    store, queue, tools = _kb()
    print(tools.memory_digest(args.account))


def cmd_plan(args):
    store, queue, tools = _kb()
    from kb.executor import ExecutorAdapter
    adapter = ExecutorAdapter(tools)
    plan = adapter.plan(args.account, args.action, args.topic,
                        job_id=args.job_id, task_params={})
    if plan["status"] != "approved":
        print(f"status: blocked（规则闸门）")
        for v in plan["decision"].get("violations", []):
            print(f"  [{v['rule']}] {v['message']}")
        sys.exit(1)
    spec = plan["spec"]
    d = plan["decision"]
    print(f"decision: {d['decision_id']}  fidelity={d.get('persona_fidelity_score')}")
    print(f"标题: {spec['title']}")
    print(f"正文: {spec['content']}")
    print(f"action_id: {spec['action_id']}  （job_id 派生，回写幂等）")
    print(f"知识依据: {spec['knowledge_refs']}")
    print("\n执行命令（补设备参数后即可拉起）：")
    print("  " + " ".join(adapter.build_command(spec, {
        "appium_url": "<appium_url>", "deviceName": "<device>",
        "systemPort": "<port>", "adbPort": "<adb>", "platformVersion": "<ver>"})))


def cmd_account(args):
    store, queue, tools = _kb()
    if args.op == "list":
        from kb.maintenance import matrix_accounts
        for acc in matrix_accounts(store):
            try:
                s = tools.get_account_state(acc)
                p = tools.get_persona(acc)
                print(f"{acc}  {p.get('name', '')}  风险={s.get('risk_level')}  "
                      f"设备={store.node_props(acc).get('deviceName', '未绑定')}")
            except Exception as e:
                print(f"{acc}  ({e})")
        return
    if args.op == "add":
        device = dict(kv.split("=", 1) for kv in (args.device or []).split(",") if "=" in kv) \
            if isinstance(args.device, str) else dict(args.device or [])
        r = tools.create_account(
            platform=args.platform, handle=args.handle, name=args.name,
            persona_props={"name": args.persona_name or args.handle,
                           "occupation": args.occupation or "",
                           "language_style": args.style or "自然"},
            interests=(args.interests.split(",") if args.interests else []),
            device=device)
        print(f"已创建: {r['account_id']}（人设 {r['persona_id']}）")
        return
    if args.op == "import":
        import json
        accounts = json.load(open(args.file))
        if isinstance(accounts, dict):
            accounts = accounts.get("accounts", [])
        ok = 0
        for a in accounts:
            try:
                tools.create_account(**a)
                ok += 1
            except Exception as e:
                print(f"  跳过 {a.get('handle', '?')}: {e}")
        print(f"导入完成: {ok}/{len(accounts)}")


def cmd_serve(args):
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    from kb.serve import run
    code = run(host=args.host, port=args.port, open_browser=not args.no_browser)
    if code:                       # 端口冲突等友好退出（非 0），代替 traceback
        sys.exit(code)


# ---------------------------------------------------------------------------
def main(argv=None):
    # Windows 默认控制台编码（cp1252/gbk）无法打印中文：强制 UTF-8 + 替换不可编码字符
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:            # Python < 3.7 无 reconfigure
            pass
    parser = argparse.ArgumentParser(
        prog="kb", description="callfans 知识库统一 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="图谱/后端/账号概览").set_defaults(func=cmd_status)

    p = sub.add_parser("query", help="知识检索")
    p.add_argument("text", help="检索文本")
    p.add_argument("--k", type=int, default=5)
    p.set_defaults(func=cmd_query)

    p = sub.add_parser("memory", help="人可读记忆摘要")
    p.add_argument("account", help="账号 ID，如 acc:xiaohongshu:lily_beauty")
    p.set_defaults(func=cmd_memory)

    p = sub.add_parser("plan", help="决策 → 执行命令（dry run）")
    p.add_argument("account")
    p.add_argument("action", choices=["post", "comment", "like", "follow",
                                      "browse"])
    p.add_argument("topic", nargs="?", default=None)
    p.add_argument("--job-id", default=None, help="调度平台任务 ID（幂等锚点）")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("account", help="账号管理（手动添加/导入现有平台账号）")
    p.add_argument("op", choices=["add", "list", "import"])
    p.add_argument("--platform", help="平台（如 xiaohongshu）")
    p.add_argument("--handle", help="账号 handle（如 lily_beauty）")
    p.add_argument("--name", help="显示名")
    p.add_argument("--persona-name", help="人设名")
    p.add_argument("--occupation", help="人设职业（如 美妆博主）")
    p.add_argument("--style", help="语言风格（如 幽默口语化）")
    p.add_argument("--interests", help="兴趣话题 slug，逗号分隔（如 beauty.skincare,beauty.makeup）")
    p.add_argument("--device", help="设备参数 k=v,k=v（deviceName=cp01,adbPort=5555）")
    p.add_argument("--file", help="import：JSON 文件路径")
    p.set_defaults(func=cmd_account)

    p = sub.add_parser("maintenance", help="维护任务（与 cron 同款）")
    p.add_argument("level", choices=["daily", "weekly", "monthly", "yearly"])
    p.add_argument("--llm", action="store_true")
    p.set_defaults(func=_maintenance_main)

    p = sub.add_parser("serve", help="本地 HTTP API + Web 控制台")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true",
                   help="不自动打开浏览器（无头环境用）")
    p.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    args.func(args)


def _maintenance_main(args):
    """委托 kb.maintenance 的 CLI（保持单一实现）。"""
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    from kb.maintenance import _main
    _main([args.level] + (["--llm"] if args.llm else []))


if __name__ == "__main__":
    main()
