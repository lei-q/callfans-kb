"""
查询与决策工具：AI 执行任务时的统一访问层。

对应设计文档：docs/02-query-and-write-protocol-v1.md
- 读工具：Agent 按需调用，每次只拉决策相关的最小子图
- 阶段一：check_rules 纯代码规则闸门（毫秒级，不调 LLM）
- 阶段二：build_decision_context 组装预算化上下文 → LLM 决策
- 回写：submit_result 走摄取漏斗 → 分区队列 → 幂等写入
"""
from __future__ import annotations

import json
import time
import uuid

from .ingest import extract_events_from_result, platform_of
from .rollup import open_promises, record_promise, stub_promise_scanner
from .schema import make_event, topic_slug
from .search import InvertedIndex, drill_down

DAY = 86400.0

# 动作风险分级 → 上下文 token 预算（docs/03 第 4 节）
# 高风险动作允许更大预算/更细记忆，低风险动作用粗摘要
RISK_BUDGETS = {"post": 3000, "comment": 2000, "follow": 1000,
                "like": 800, "browse": 600}

# 上下文块优先级：预算不够时从列表尾部开始裁剪
BLOCK_PRIORITY = ["persona_card", "hard_rules", "account_state", "memory",
                  "topic_context", "recent_actions", "candidates"]


def _tokens(obj) -> int:
    """token 粗估：中文约 2 字符/token。"""
    return len(json.dumps(obj, ensure_ascii=False)) // 2


class KBTools:
    def __init__(self, store, queue=None):
        self.store = store
        self.queue = queue
        self._index = None
        self._index_sig = None

    # ===============================================================
    # 读工具（Agent 调用面，详见 docs/02）
    # ===============================================================
    def get_persona(self, account_id) -> dict:
        """人设卡：约 100-300 token，决策上下文的第一优先块。"""
        per_id = self.store.persona_id_of(account_id)
        props = self.store.node_props(per_id)
        interests = [d for _, d, _ in self.store.out_edges(per_id, "INTERESTED_IN")]
        return {"account_id": account_id, "persona_id": per_id,
                "interests": interests, **props}

    def get_account_state(self, account_id) -> dict:
        props = self.store.node_props(account_id)
        now = time.time()
        actions = self._recent_actions(account_id)
        today = [a for a in actions if now - a["props"].get("ts", 0) < DAY]
        counts = {}
        for a in today:
            counts[a["props"]["action"]] = counts.get(a["props"]["action"], 0) + 1
        summaries = {k: v for k, v in props.items() if k.startswith("summary_day:")}
        return {
            "account_id": account_id,
            "status": props.get("status"),
            "risk_level": props.get("risk_level"),
            "age_days": round((now - props.get("created_at", now)) / DAY, 1),
            "cooldown_remaining_h": max(
                0, round((props.get("cooldown_until", 0) - now) / 3600, 1)),
            "today_action_counts": counts,
            "last_summary": sorted(summaries.items())[-1][1] if summaries else None,
            "recent_actions": [
                {"action": a["props"]["action"],
                 "topic_id": a["props"].get("topic_id"),
                 "digest": (a["props"].get("digest") or "")[:40]}
                for a in actions[:5]],
        }

    def get_topic_context(self, topic_id) -> dict:
        props = self.store.node_props(topic_id)
        slug = topic_slug(topic_id)
        parent = None
        if "." in slug:
            pid = f"topic:{slug.rsplit('.', 1)[0]}"
            if self.store.has_node(pid):
                parent = {"id": pid, "label": self.store.node_props(pid).get("label")}
        rules = [s for _, s, _ in self.store.in_edges(topic_id, "APPLIES_TO")]
        posts = []
        for _, s, _ in self.store.in_edges(topic_id, "ABOUT"):
            n = self.store.get_node(s)
            if n and n["type"] == "Post":
                posts.append({**n["props"], "id": s})
        posts.sort(key=lambda x: x.get("published_at", 0), reverse=True)
        return {"topic_id": topic_id, "label": props.get("label"), "parent": parent,
                "rules": rules,
                "recent_posts": [{"id": p["id"], "digest": p.get("digest", ""),
                                  "likes": p.get("likes")} for p in posts[:3]]}

    def find_interaction_targets(self, account_id, limit=5) -> list:
        """互动候选：强联结（历史互动边）+ 弱联结（人设话题重合，仅做召回）。

        注意：生产环境需在此过滤"同矩阵账号互互动"，本 MVP 不过滤。
        """
        out = {}
        # 1) 强联结：行为聚合边
        for _, dst, props in self.store.out_edges(account_id, "INTERACTS_WITH"):
            c = props.get("count", 0)
            out[dst] = {"account_id": dst, "score": min(1.0, c / 10),
                        "reasons": [f"历史互动 {c} 次"]}
        # 2) 弱联结：人设兴趣树重合（语义相似边的 v1 替代）
        my = self._interest_slugs(account_id)
        for other in self.store.node_ids("Account"):
            if other == account_id or other in out:
                continue
            if not any(True for _ in self.store.out_edges(other, "HAS_PERSONA")):
                continue  # 外部账号无人设，跳过
            overlap = self._slug_overlap(my, self._interest_slugs(other))
            if overlap:
                out[other] = {"account_id": other,
                              "score": min(0.5, 0.2 * len(overlap)),
                              "reasons": [f"共同话题: {', '.join(sorted(overlap)[:3])}"]}
        ranked = sorted(out.values(), key=lambda x: -x["score"])
        return ranked[:limit]

    def search_knowledge(self, query, k=5) -> list:
        """bigram 倒排检索（中文召回）。接口形状与向量检索一致，接 embedding
        时替换实现。索引按 store 版本懒重建。"""
        sig = self.store.stats()
        if self._index is None or self._index_sig != sig:
            self._index = InvertedIndex.build(self.store)
            self._index_sig = sig
        return self._index.search(query, k=k)

    def recall_memory(self, account_id, query, limit=5) -> dict:
        """记忆召回（决策上下文 memory 块）：
        relevance × recency × importance 三因子排序，未兑现承诺优先置顶。
        """
        now = time.time()
        qg = set(query) if query else set()
        scored = []
        for _, dst, _ in self.store.out_edges(account_id, "HAS_MEMORY"):
            n = self.store.get_node(dst)
            if not n:
                continue
            p = n["props"]
            if p.get("category") == "promise":
                continue                    # 承诺单独置顶，不参与排序
            if p.get("status") == "stale":
                continue                    # 时效淘汰：不再进入召回（docs/03 §1.4）
            grams = {p["fact"][i:i + 2] for i in range(len(p["fact"]) - 1)} \
                if p.get("fact") else set()
            relevance = (len(grams & qg) / len(qg)) if qg and grams else 0.0
            recency = 2.718 ** (-(now - p.get("ts", 0)) / (30 * DAY))
            importance = p.get("confidence", 0.5) * (1.5 if p.get("pinned") else 1.0)
            scored.append({"note_id": dst, "fact": p.get("fact", ""),
                           "category": p.get("category"), "pinned": p.get("pinned", False),
                           "score": round(0.5 * relevance + 0.2 * recency
                                           + 0.3 * importance / 1.5, 3)})
        scored.sort(key=lambda x: -x["score"])
        episode = None
        eps = [(d, self.store.get_node(d)) for _, d, _
               in self.store.out_edges(account_id, "HAS_EPISODE")]
        eps = [(d, n) for d, n in eps if n]
        if eps:
            d, n = max(eps, key=lambda x: x[1]["props"].get("ts", 0))
            episode = {"id": d, "level": n["props"].get("level"),
                       "narrative": n["props"].get("narrative", "")[:120]}
        return {"open_promises": open_promises(self.store, account_id),
                "memories": scored[:limit], "latest_episode": episode}

    # ===============================================================
    # 阶段一：规则闸门（纯代码，毫秒级，不调 LLM）
    # ===============================================================
    def check_rules(self, account_id, action_type, topic_id=None) -> dict:
        from .consensus import rule_active_for
        violations, checked = [], []
        for rid in self.store.node_ids("Rule"):
            props = self.store.node_props(rid)
            if not rule_active_for(props, account_id, rule_id=rid):
                continue                        # 灰度未到该账号 / 候选信号未确证
            if props.get("platform") and props["platform"] != platform_of(account_id):
                continue
            actions = props.get("actions")
            if actions and action_type not in actions:
                continue
            checker = RULE_CHECKERS.get(props.get("kind"))
            if not checker:
                continue
            checked.append(rid)
            msg = checker(self, account_id, action_type, topic_id,
                          props.get("params", {}))
            if msg:
                violations.append({"rule": rid, "name": props.get("name", ""),
                                   "message": msg})
        return {"pass": not violations, "violations": violations, "checked": checked}

    # ===============================================================
    # 阶段二：上下文组装 + LLM 决策
    # ===============================================================
    def build_decision_context(self, account_id, action_type, topic_id=None,
                               budget_tokens=2000) -> dict:
        blocks = {}
        blocks["persona_card"] = self.get_persona(account_id)
        blocks["hard_rules"] = self._applicable_rule_texts(account_id, action_type)
        blocks["account_state"] = self.get_account_state(account_id)
        # 记忆块：语义记忆 + 未兑现承诺（连续性）+ 最新情景叙事
        query = action_type
        if topic_id and self.store.has_node(topic_id):
            query += " " + (self.store.node_props(topic_id).get("label") or "")
        blocks["memory"] = self.recall_memory(account_id, query)
        if topic_id and self.store.has_node(topic_id):
            blocks["topic_context"] = self.get_topic_context(topic_id)
        if action_type in ("comment", "like", "follow"):
            blocks["candidates"] = self.find_interaction_targets(account_id, limit=3)
        blocks["recent_actions"] = blocks["account_state"]["recent_actions"]

        # 预算化：从低优先级块开始裁剪
        usage = {k: _tokens(v) for k, v in blocks.items()}
        total = sum(usage.values())
        for name in reversed(BLOCK_PRIORITY):
            if total <= budget_tokens:
                break
            if name in blocks:
                total -= usage[name]
                blocks.pop(name)
        return {"account_id": account_id, "action_type": action_type,
                "topic_id": topic_id, "blocks": blocks, "token_estimate": total}

    def decide(self, account_id, action_type, topic_id=None, llm=None,
               budget_tokens=None) -> dict:
        """两段式决策。llm=None 时走确定性桩，先离线验证闭环再接真模型。
        budget_tokens=None 时按动作风险分级取预算（RISK_BUDGETS）。"""
        if budget_tokens is None:
            budget_tokens = RISK_BUDGETS.get(action_type, 2000)
        gate = self.check_rules(account_id, action_type, topic_id)
        if not gate["pass"]:
            return {"status": "blocked", "stage": "rules", **gate}

        ctx = self.build_decision_context(account_id, action_type, topic_id,
                                          budget_tokens)
        if llm is not None:
            try:
                decision = dict(llm(ctx))
                _validate_decision(decision)
                decision["llm"] = "ok"
            except Exception as e:     # LLM 失败 → 降级桩决策，不阻塞业务
                decision = self._stub_decision(ctx)
                decision["llm"] = f"fallback: {e}"
        else:
            decision = self._stub_decision(ctx)

        refs = [ctx["blocks"]["persona_card"]["persona_id"], topic_id]
        refs += [r.split("]")[0][1:] for r in ctx["blocks"].get("hard_rules", [])]
        decision.update({
            "status": "approved", "stage": "llm",
            "decision_id": f"dec:{uuid.uuid4().hex[:12]}",
            "knowledge_refs": [r for r in refs if r],
            "context_tokens": ctx["token_estimate"],
        })
        return decision

    # ===============================================================
    # 回写
    # ===============================================================
    def submit_result(self, result: dict, llm=None) -> dict:
        """执行结果回写：抽取 → 入队（幂等）→ 消费 → 返回统计。"""
        events = extract_events_from_result(result, llm=llm)
        accepted = sum(1 for e in events if self.queue.submit(e))
        self.queue.drain()
        return {"events": len(events), "accepted": accepted,
                "duplicates": len(events) - accepted}

    # ------------------------------ 内部 ------------------------------
    def _recent_actions(self, account_id, limit=None):
        acts = []
        for _, dst, _ in self.store.out_edges(account_id, "PERFORMED"):
            n = self.store.get_node(dst)
            if n and n["type"] == "ActionRecord" and not n["props"].get("archived"):
                acts.append({"id": dst, "props": n["props"]})
        acts.sort(key=lambda a: a["props"].get("ts", 0), reverse=True)
        return acts if limit is None else acts[:limit]

    def _interest_slugs(self, account_id):
        per_id = self.store.persona_id_of(account_id)
        return {topic_slug(d) for _, d, _ in self.store.out_edges(per_id, "INTERESTED_IN")}

    @staticmethod
    def _slug_overlap(a, b):
        out = set()
        for x in a:
            for y in b:
                if x == y or x.startswith(y + ".") or y.startswith(x + "."):
                    out.add(x if len(x) <= len(y) else y)
        return out

    def _applicable_rule_texts(self, account_id, action_type):
        from .consensus import rule_active_for
        texts = []
        for rid in self.store.node_ids("Rule"):
            props = self.store.node_props(rid)
            if not rule_active_for(props, account_id, rule_id=rid):
                continue                    # 候选信号不进决策上下文
            if props.get("platform") and props["platform"] != platform_of(account_id):
                continue
            actions = props.get("actions")
            if actions and action_type not in actions:
                continue
            texts.append(f"[{rid}] {props.get('name', '')}: {props.get('text', '')}")
        return texts

    # ===============================================================
    # 承诺追踪：生成内容中"埋的坑"
    # ===============================================================
    def track_promises(self, account_id, content, post_id=None, scanner=None) -> dict:
        """扫描（已发布的）生成内容，把对粉丝的新承诺写入记忆。

        scanner(content) -> [{"fact", "confidence"}] 可接 LLM
        （make_promise_scanner）；未传时用确定性关键词兜底。
        指纹 ID 保证同一承诺重复扫描不膨胀。
        """
        try:
            found = (scanner or stub_promise_scanner)(content) or []
        except Exception:
            found = []          # 扫描失败不阻塞发布后回写
        recorded = []
        for f in found:
            if not isinstance(f, dict) or not f.get("fact"):
                continue
            r = record_promise(self.store, account_id, f["fact"],
                               f.get("confidence", 0.7),
                               f.get("post_id") or post_id)
            recorded.append(r["note_id"])
        return {"promises_found": len(found), "notes": recorded}

    # ===============================================================
    # 人可读记忆摘要（借鉴 Proma：分层路由 + 索引只做路由）
    # ===============================================================
    def memory_digest(self, account_id, recent_limit=5) -> str:
        """账号记忆的 Markdown 摘要：CLI `kb memory` / GUI 检查器 / 人工导出。

        分层路由原则：情景记忆只列周期与一行摘要（路由索引，需全文时
        按 Episode ID 下钻）；语义记忆全文（量小且是决策依据）；
        承诺置顶（连续性优先）。
        """
        from .rollup import open_promises
        persona = self.get_persona(account_id)
        state = self.get_account_state(account_id)
        lines = [f"# {persona.get('name', account_id)}（{account_id}）",
                 ""]
        # —— 状态层 ——
        lines += ["## 当前状态",
                  f"- 风险等级: {state.get('risk_level') or '正常'}"
                  f"  账号年龄: {state.get('age_days')} 天",
                  f"- 今日行为: {state.get('today_action_counts') or '无'}"
                  f"  冷却剩余: {state.get('cooldown_remaining_h')}h", ""]
        # —— 语义记忆：承诺置顶 ——
        promises = open_promises(self.store, account_id)
        lines.append("## 语义记忆")
        if promises:
            lines.append("### 未兑现承诺（决策优先兑现）")
            for pr in promises:
                lines.append(f"- [ ] {pr['fact']}")
        notes = []
        for _, dst, _ in self.store.out_edges(account_id, "HAS_MEMORY"):
            n = self.store.get_node(dst)
            if not n or n["props"].get("category") == "promise":
                continue
            notes.append((dst, n["props"]))
        notes.sort(key=lambda x: (x[1].get("status") == "stale",
                                  -x[1].get("ts", 0)))
        for nid, p in notes:
            flag = "📌 " if p.get("pinned") else ""
            stale = "（已过期）" if p.get("status") == "stale" else ""
            lines.append(f"- {flag}[{p.get('category')}] "
                         f"{p.get('fact', '')}{stale}")
        if not promises and not notes:
            lines.append("-（无）")
        lines.append("")
        # —— 情景记忆：索引只做路由 ——
        lines.append("## 情景记忆（索引：按周期下钻全文）")
        eps = []
        for _, dst, _ in self.store.out_edges(account_id, "HAS_EPISODE"):
            n = self.store.get_node(dst)
            if n:
                eps.append((dst, n["props"]))
        eps.sort(key=lambda x: x[1].get("ts", 0), reverse=True)
        for eid, p in eps[:8]:
            drift = f" 漂移{p.get('drift', 0):.0%}" if p.get("drift") else ""
            lines.append(f"- {p.get('level')}/{p.get('period')}{drift}: "
                         f"{(p.get('narrative') or '')[:60]}…（{eid}）")
        if not eps:
            lines.append("-（无）")
        lines.append("")
        # —— 近期行为 ——
        lines.append("## 近期行为")
        for a in state.get("recent_actions", [])[:recent_limit]:
            topic = (a.get("topic_id") or "-").replace("topic:", "")
            lines.append(f"- {a['action']}@{topic}: {a.get('digest', '')}")
        if not state.get("recent_actions"):
            lines.append("-（无）")
        return "\n".join(lines)

    # ===============================================================
    # 账号管理（手动添加 / 导入：把现有平台账号与设备纳入知识库）
    # ===============================================================
    def create_account(self, platform, handle, name=None, persona_id=None,
                       persona_props=None, interests=None, device=None,
                       status="warming", risk_level="low") -> dict:
        """手动添加矩阵账号：Account + 人设（复用或新建）+ 兴趣树 + 设备属性。

        - platform/handle → 账号 ID（acc:平台:handle，Schema 强校验）
        - persona_id 已存在则复用（多人设共享）；否则按 persona_props 新建
        - interests：话题 slug 列表（如 beauty.skincare），话题不存在则建
        - device：云机设备参数（deviceName/adbPort/systemPort/appiumUrl…），
          存为账号属性，执行层 build_command 可直接取用
        """
        import re
        import time as _t
        account_id = f"acc:{platform}:{handle}"
        if self.store.has_node(account_id):
            raise ValueError(f"账号已存在: {account_id}")
        if persona_id and not self.store.has_node(persona_id):
            raise ValueError(f"人设不存在: {persona_id}")
        if persona_id is None:
            pp = persona_props or {}
            slug = re.sub(r"[^\w\-]+", "-", pp.get("name", handle)).strip("-").lower()
            persona_id = f"per:{slug or handle}"

        def ev(i, kind, payload):
            return make_event(account_id, kind, payload,
                              event_id=f"evt:acc:{account_id}:{i:02d}",
                              ts=_t.time())

        events = [ev(0, "upsert_node", {
            "id": account_id, "type": "Account",
            "props": {"name": name or handle, "status": status,
                      "risk_level": risk_level, "created_at": _t.time(),
                      **(device or {})}})]
        if not self.store.has_node(persona_id):
            events.append(ev(1, "upsert_node", {
                "id": persona_id, "type": "Persona", "props": persona_props or {}}))
        events.append(ev(2, "upsert_edge", {"src": account_id,
                                             "type": "HAS_PERSONA", "dst": persona_id}))
        i = 3
        for slug in (interests or []):
            topic_id = slug if slug.startswith("topic:") else f"topic:{slug}"
            if not self.store.has_node(topic_id):
                label = slug.rsplit(".", 1)[-1]
                events.append(ev(i, "upsert_node", {
                    "id": topic_id, "type": "Topic", "props": {"label": label}}))
                i += 1
            events.append(ev(i, "upsert_edge", {
                "src": persona_id, "type": "INTERESTED_IN", "dst": topic_id}))
            i += 1
        for e in events:
            self.queue.submit(e)
        self.queue.drain()
        return {"account_id": account_id, "persona_id": persona_id,
                "created": True, "events": len(events)}

    def recent_decisions(self, account_id, limit=20) -> list:
        """决策流：近期行为 + DECIDED_VIA 引用的知识节点（可解释性视图）。"""
        acts = []
        for _, dst, _ in self.store.out_edges(account_id, "PERFORMED"):
            n = self.store.get_node(dst)
            if n and n["type"] == "ActionRecord":
                acts.append({"id": dst, **n["props"]})
        acts.sort(key=lambda a: a.get("ts", 0), reverse=True)
        out = []
        for a in acts[:limit]:
            refs = []
            for _, dst2, _ in self.store.out_edges(a["id"], "DECIDED_VIA"):
                node = self.store.get_node(dst2)
                if not node:
                    continue
                props = node["props"]
                refs.append({"id": dst2, "type": node["type"],
                             "label": props.get("label") or props.get("name")
                                      or props.get("fact") or props.get("text")
                                      or props.get("digest") or dst2})
            out.append({"action_id": a["id"], "action": a.get("action"),
                        "outcome": a.get("outcome"), "digest": a.get("digest"),
                        "decision_id": a.get("decision_id"),
                        "topic_id": a.get("topic_id"), "ts": a.get("ts"),
                        "refs": refs})
        return out

    # ===============================================================
    # 失真审计：决策可追溯性的事后检查
    # ===============================================================
    def audit_decision(self, decision) -> dict:
        """审计一条决策：引用的知识节点是否存在、人设保真度是否达标。

        配合 DECIDED_VIA 回写：抽检"决策引用的记忆是否真实支撑了结论"，
        不支撑 → 反馈回 rollup/固化提示词，形成摘要质量闭环（docs/03 第 4 节）。
        """
        refs = decision.get("knowledge_refs", [])
        missing = [r for r in refs if not self.store.has_node(r)]
        findings = []
        if missing:
            findings.append(f"引用了不存在的知识节点: {missing}")
        fid = decision.get("persona_fidelity_score")
        if decision.get("status") == "approved" and fid is not None and fid < 0.7:
            findings.append(f"人设保真度偏低: {fid}")
        content = decision.get("generated_content", "")
        if decision.get("status") == "approved" and len(content) < 10:
            findings.append("生成内容过短，疑似摘要/上下文失真")
        return {"decision_id": decision.get("decision_id"),
                "ok": not findings, "refs_checked": len(refs),
                "missing_refs": missing, "findings": findings}

    def _stub_decision(self, ctx):
        """确定性桩决策：不依赖 LLM，用于离线验证整条闭环。"""
        card = ctx["blocks"].get("persona_card", {})
        topic = ctx["blocks"].get("topic_context", {})
        interests = {topic_slug(i) for i in card.get("interests", [])}
        t = topic_slug(ctx["topic_id"]) if ctx["topic_id"] else None
        fidelity = 1.0 if (t and any(s == t or s.startswith(t + ".") or t.startswith(s + ".")
                                     for s in interests)) else 0.6
        label = topic.get("label") or t or "日常内容"
        content = (f"【{card.get('name', '')}｜{card.get('occupation', '')}】"
                   f"用{card.get('language_style', '自然')}的语气聊聊{label}："
                   "分享真实体验、给出可操作建议，并以一个开放式问题引导评论。")
        return {"action": ctx["action_type"],
                "content_type": "text_with_image" if ctx["action_type"] == "post" else "text",
                "topic": ctx["topic_id"],
                "persona_fidelity_score": fidelity,
                "generated_content": content,
                "rationale": f"话题与 {card.get('persona_id')} 兴趣匹配，规则闸门已通过"}


def _validate_decision(d):
    if not isinstance(d, dict) or "action" not in d:
        raise ValueError(f"LLM 决策输出缺少 action 字段: {d!r}")


# ---------------------------------------------------------------------------
# 规则 checker 注册表：新规则 = 一条 Rule 数据 + 一个 checker 函数
# ---------------------------------------------------------------------------
def _check_min_age(tools, account_id, action_type, topic_id, params):
    created = tools.store.node_props(account_id).get("created_at", 0)
    age = (time.time() - created) / DAY
    if age < params.get("days", 7):
        return f"账号注册 {age:.1f} 天，不满 {params.get('days', 7)} 天，处于限流期"
    return None


def _check_rate_limit(tools, account_id, action_type, topic_id, params):
    actions = params.get("actions", [action_type])
    counts = tools.get_account_state(account_id)["today_action_counts"]
    total = sum(counts.get(a, 0) for a in actions)
    if total >= params.get("max", 1):
        return f"今日 {actions} 已执行 {total} 次，达到上限 {params.get('max', 1)}"
    return None


def _check_topic_overlap(tools, account_id, action_type, topic_id, params):
    if not topic_id:
        return None
    interests = tools._interest_slugs(account_id)
    t = topic_slug(topic_id)
    hit = any(s == t or s.startswith(t + ".") or t.startswith(s + ".") for s in interests)
    if not hit:
        return f"话题 {t} 与人设兴趣（{', '.join(sorted(interests))}）不匹配"
    return None


def _check_cooldown(tools, account_id, action_type, topic_id, params):
    until = tools.store.node_props(account_id).get("cooldown_until", 0)
    if until > time.time():
        return f"账号处于风控冷却期，剩余 {(until - time.time()) / 3600:.1f} 小时"
    return None


RULE_CHECKERS = {
    "min_account_age_days": _check_min_age,
    "rate_limit_per_day": _check_rate_limit,
    "topic_overlap": _check_topic_overlap,
    "cooldown": _check_cooldown,
}
