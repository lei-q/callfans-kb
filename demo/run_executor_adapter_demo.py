#!/usr/bin/env python3
"""
执行层适配离线 Demo：知识库决策 → xhs.py 命令 → 模拟执行 → 结果回写。

不需要 sma_autoui、真机或 LLM：runner 用 FakeRunner 替代真实子进程，
决策走确定性桩（如需 LLM 实机验证见 run_llm_demo.py）。

运行：python3 demo/run_executor_adapter_demo.py
"""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from demo.seed import build_seed                      # noqa: E402
from kb.executor import ExecutorAdapter, split_title_content  # noqa: E402
from kb.tools import KBTools                          # noqa: E402

LILY = "acc:xiaohongshu:lily_beauty"
MOMO = "acc:xiaohongshu:momo_beauty"      # 新号 2 天：应被规则闸门拦截

DEVICE_ENV = {
    "appium_url": "http://127.0.0.1:4723",
    "deviceName": "cloudphone-042",
    "systemPort": "8200",
    "adbPort": "5555",
    "platformVersion": "13",
}


class FakeRunner:
    """记录 argv 并模拟执行成功（测试替身，替代 sma_autoui 真实执行）。"""

    def __init__(self, exit_code=0):
        self.exit_code = exit_code
        self.calls = []

    def __call__(self, argv, timeout=None):
        self.calls.append(argv)
        return self.exit_code, "[FakeRunner] 任务模拟执行完成"


def h1(title):
    print("\n" + "=" * 62)
    print(title)
    print("=" * 62)


def main():
    store, queue = build_seed()
    tools = KBTools(store, queue)
    runner = FakeRunner()
    adapter = ExecutorAdapter(tools, runner=runner)

    # -------------------------------------------------------------
    h1("1. plan：决策 → ExecutionSpec（标题/正文拆分 + base64）")
    plan = adapter.plan(LILY, "post", "topic:beauty.skincare",
                        job_id="job-20261009-001",
                        task_params={"folder_name": "Movies", "images_nums": 1})
    assert plan["status"] == "approved", plan
    spec = plan["spec"]
    print(f"action_id: {spec['action_id']}   （job_id 派生，重试幂等）")
    print(f"decision_id: {spec['decision_id']}")
    print(f"知识依据: {spec['knowledge_refs']}")
    print(f"标题: {spec['title']}")
    print(f"正文: {spec['content']}")

    # 校验 base64 往返与 xhs.py 的解码逻辑一致
    rt = base64.b64decode(spec["content_b64"]).decode("utf-8")
    assert rt == spec["content"], "base64 往返不一致"
    print(f"base64 往返校验: OK（与 xhs.py _decode_from_base64 兼容）")

    # -------------------------------------------------------------
    h1("2. build_command：ExecutionSpec → xhs.py 完整命令行")
    argv = adapter.build_command(spec, DEVICE_ENV)
    print(" ".join(argv))
    # 关键契约断言：必需参数、base64 编码、accountId 传递
    assert "-text" in argv and "-content" in argv and "-accountId" in argv
    assert argv.index("-accountId") + 1 < len(argv)

    # -------------------------------------------------------------
    h1("3. run：FakeRunner 模拟执行（真实环境为 subprocess 拉起云机任务）")
    run = adapter.run(spec, DEVICE_ENV, timeout=600)
    print(f"ok: {run['ok']}  exit_code: {run['exit_code']}  调用次数: {len(runner.calls)}")

    # -------------------------------------------------------------
    h1("4. report：执行结果 → 摄取漏斗 → 幂等回写")
    rep = adapter.report(spec, outcome={
        "ok": True,
        "stats": {"views": 320, "likes": 28, "comments": 3},
        "raw_text": "发布成功。评论区反馈：求防晒用量和肤质说明。",
    })
    print(f"回写统计: {rep['write']}")
    print(f"内容埋坑追踪: {rep['promises']}（stub 决策内容无承诺关键词 → 不误报）")
    assert rep["promises"]["promises_found"] == 0
    state = tools.get_account_state(LILY)
    print(f"当日行为计数: {state['today_action_counts']}")

    # -------------------------------------------------------------
    h1("5. 幂等验证：调度平台重试同一 job → 事件 ID 相同 → 自动去重")
    rep2 = adapter.report(spec, outcome={"ok": True})
    print(f"重试回写统计: {rep2['write']}   （duplicates = 全部事件）")
    state2 = tools.get_account_state(LILY)
    assert state2["today_action_counts"] == state["today_action_counts"], "重试导致重复计数！"
    print(f"当日行为计数不变: {state2['today_action_counts']}  ✓")
    post = store.node_props(f"post:xiaohongshu:job-20261009-001")
    print(f"Post 节点: post:xiaohongshu:job-20261009-001  likes={post['likes']}  "
          f"digest={post['digest'][:20]}")

    # -------------------------------------------------------------
    h1("6. 规则闸门：新号发帖在 plan 阶段即被拦截，不生成执行命令")
    plan2 = adapter.plan(MOMO, "post", "topic:beauty.makeup", job_id="job-x")
    print(f"status: {plan2['status']}")
    for v in plan2["decision"]["violations"]:
        print(f"  拦截: [{v['rule']}] {v['message']}")
    assert plan2["spec"] is None and len(runner.calls) == 1

    # -------------------------------------------------------------
    h1("7. 失败回写：执行失败也记录 ActionRecord（outcome=failed）")
    # 注：lily 今日发帖已达频控上限，plan 会被闸门拦截（间 #6 同机制）；
    # 换一个账号 kai_fit 演示失败回写
    plan_lily = adapter.plan(LILY, "post", "topic:beauty.skincare",
                             job_id="job-20261009-002")
    assert plan_lily["status"] == "blocked", "频控应拦截 lily 再次发帖"
    print(f"lily 第二次发帖被频控拦截: {plan_lily['decision']['violations'][0]['message']}")
    plan3 = adapter.plan("acc:xiaohongshu:kai_fit", "post", "topic:fitness",
                         job_id="job-20261009-002")
    assert plan3["status"] == "approved"
    rep3 = adapter.report(plan3["spec"], outcome={"ok": False})
    print(f"回写统计: {rep3['write']}")
    print(f"digest: {rep3['result']['digest']}")

    # -------------------------------------------------------------
    h1("8. 标题/正文拆分规则（split_title_content）")
    cases = [
        "秋冬换季护肤避坑指南\n干皮姐妹注意：\n1. 别叠加酸类\n2. 修复屏障优先",
        "单行长内容标题超过二十个字符的情况正文与标题相同",
    ]
    for c in cases:
        t, b = split_title_content(c)
        print(f"  标题({len(t)}): {t}")
        print(f"  正文: {b[:30]}{'…' if len(b) > 30 else ''}\n")

    print("=" * 62)
    print(f"完成  最终图谱: {store.stats()}")


if __name__ == "__main__":
    main()
