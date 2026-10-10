#!/usr/bin/env python3
"""
LLM 接入层实机 Demo：真实调用 LLM 完成三个钩子 + 故障降级验证。

运行前配置（任一 OpenAI Chat Completions 兼容服务）：
  export CALLFANS_LLM_BASE_URL=https://api.proma.cool   # 或自有网关
  export CALLFANS_LLM_API_KEY=sk-...
  export CALLFANS_LLM_MODEL=glm-5.3-flash

运行：python3 demo/run_llm_demo.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from demo.seed import build_seed                      # noqa: E402
from kb.llm import (LLMClient, make_decision_llm,     # noqa: E402
                    make_extract_llm, make_summarizer)
from kb.rollup import daily_rollup                    # noqa: E402
from kb.tools import KBTools                          # noqa: E402


def h1(title):
    print("\n" + "=" * 62)
    print(title)
    print("=" * 62)


def main():
    client = LLMClient()
    print(f"LLM 网关: {client.base_url}  模型: {client.model}")

    store, queue = build_seed()
    tools = KBTools(store, queue)
    lily = "acc:xiaohongshu:lily_beauty"
    decide_llm = make_decision_llm(client)

    # -------------------------------------------------------------
    h1("1. LLM 决策：发帖（阶段一闸门仍由代码完成）")
    d = tools.decide(lily, "post", "topic:beauty.skincare", llm=decide_llm)
    print(f"status: {d['status']}  llm: {d.get('llm')}")
    print(f"fidelity: {d['persona_fidelity_score']}  context_tokens: {d['context_tokens']}")
    print(f"content: {d['generated_content']}")
    print(f"rationale: {d.get('rationale')}")

    # -------------------------------------------------------------
    h1("2. LLM 决策：评论（上下文带互动候选）")
    d2 = tools.decide(lily, "comment", "topic:beauty.skincare", llm=decide_llm)
    print(f"status: {d2['status']}  llm: {d2.get('llm')}")
    print(f"content: {d2['generated_content']}")

    # -------------------------------------------------------------
    h1("3. 回写 + LLM 自由文本抽取（候选事实过 Schema 校验）")
    before = set(store.node_ids())
    res = tools.submit_result({
        "action_id": "act:llm-demo-1",
        "account_id": lily,
        "action": "post",
        "topic_id": "topic:beauty.skincare",
        "new_post_id": "post:xiaohongshu:p101",
        "digest": "秋冬换季护肤避坑指南",
        "content_type": "text_with_image",
        "stats": {"views": 480, "likes": 45, "comments": 6},
        "decision_id": d["decision_id"],
        "knowledge_refs": d["knowledge_refs"],
        "raw_text": ("发布成功。评论区高赞反馈：干皮姐妹求面霜推荐；"
                     "另有用户抱怨换季爆痘，下一期可以讲刷酸。"),
    }, llm=make_extract_llm(client))
    print(f"回写统计: {res}  （accepted = 规则映射 + 通过校验的 LLM 候选）")
    new_nodes = [i for i in store.node_ids() if i not in before]
    print(f"LLM 抽取新增节点: {new_nodes}")
    post = store.node_props("post:xiaohongshu:p101")
    extra = {k: v for k, v in post.items()
             if k not in ("platform", "published_at", "content_type", "digest",
                          "views", "likes", "comments")}
    print(f"LLM 为新帖补充的属性: {extra or '（无）'}")

    # -------------------------------------------------------------
    h1("4. LLM 摘要 rollup（叙事化日小结）")
    ru = daily_rollup(store, lily, summarizer=make_summarizer(client))
    print(f"归档动作数: {ru['archived_actions']}  计数: {ru['counts']}")
    print(f"LLM 摘要: {ru['summary']}")

    # -------------------------------------------------------------
    h1("5. LLM 用量（成本核算）")
    print(client.usage)

    # -------------------------------------------------------------
    h1("6. 故障降级验证（错误模型名 → 自动回退桩决策，业务不中断）")
    bad = LLMClient(model="nonexistent-model-xyz")
    d3 = tools.decide(lily, "post", "topic:beauty.skincare",
                      llm=make_decision_llm(bad))
    print(f"status: {d3['status']}  llm: {str(d3.get('llm'))[:80]}…")
    print(f"降级内容（确定性桩）: {d3['generated_content'][:50]}…")

    # -------------------------------------------------------------
    h1("完成")
    print(f"最终图谱: {store.stats()}  |  用量: {client.usage}")


if __name__ == "__main__":
    main()
