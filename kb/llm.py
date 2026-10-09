"""
LLM 接入层：三个钩子（决策 / 抽取 / 摘要）的真实实现。

传输协议：OpenAI Chat Completions 兼容（DeepSeek / Qwen / GLM / OpenAI / vLLM 等均支持）。
配置优先级：构造参数 > 环境变量
  CALLFANS_LLM_BASE_URL   例如 https://api.proma.cool 或 https://api.deepseek.com
  CALLFANS_LLM_API_KEY
  CALLFANS_LLM_MODEL

设计原则：
- 失败降级不阻塞业务：LLM 异常时 KBTools.decide / submit_result / rollup
  自动回退确定性逻辑（降级原因写入返回值）
- Schema 即过滤器：LLM 输出只作为"候选事实"，入库前仍过 kb.schema 校验
- 关键字段（action / topic）以调用方上下文为准，不信任 LLM 复述
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from .schema import EDGE_TYPES, EVENT_KINDS, NODE_TYPES


class LLMError(RuntimeError):
    pass


class LLMClient:
    """极简 OpenAI Chat Completions 客户端（零第三方依赖，urllib 实现）。

    - 429/5xx 指数退避重试，其余 4xx 立即失败
    - 自动累计 usage，供成本核算
    - 推理模型注意：max_tokens 不要给太小（思考链占用输出预算）
    """

    RETRYABLE = (429, 500, 502, 503, 504)

    def __init__(self, base_url=None, api_key=None, model=None, timeout=90):
        self.base_url = (base_url or os.environ.get("CALLFANS_LLM_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("CALLFANS_LLM_API_KEY", "")
        self.model = model or os.environ.get("CALLFANS_LLM_MODEL", "")
        self.timeout = timeout
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
        if not (self.base_url and self.api_key and self.model):
            raise LLMError(
                "LLM 未配置：需要 base_url / api_key / model（构造参数或环境变量 "
                "CALLFANS_LLM_BASE_URL / CALLFANS_LLM_API_KEY / CALLFANS_LLM_MODEL）")

    def chat(self, system: str, user: str, max_tokens=1024,
             temperature=0.7, retries=2) -> str:
        base = self.base_url if self.base_url.endswith("/v1") else self.base_url + "/v1"
        url = base + "/chat/completions"
        body = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }).encode("utf-8")

        last_err = None
        for attempt in range(retries + 1):
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {self.api_key}"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                u = data.get("usage") or {}
                self.usage["calls"] += 1
                self.usage["prompt_tokens"] += u.get("prompt_tokens", 0) or 0
                self.usage["completion_tokens"] += u.get("completion_tokens", 0) or 0
                try:
                    content = data["choices"][0]["message"]["content"]
                except (KeyError, IndexError):
                    raise LLMError(f"响应结构异常: {json.dumps(data, ensure_ascii=False)[:300]}")
                if not content:
                    # 推理模型思考链可能吃掉输出预算：加倍重试一次
                    if attempt < retries and max_tokens < 8192:
                        max_tokens *= 2
                        body = json.dumps({
                            "model": self.model,
                            "messages": [
                                {"role": "system", "content": system},
                                {"role": "user", "content": user},
                            ],
                            "max_tokens": max_tokens,
                            "temperature": temperature,
                        }).encode("utf-8")
                        continue
                    raise LLMError("模型返回空 content（推理模型 max_tokens 不足）")
                return content
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "ignore")[:300]
                last_err = LLMError(f"HTTP {e.code}: {detail}")
                if e.code in self.RETRYABLE and attempt < retries:
                    time.sleep(3 ** attempt)   # 1s, 3s
                    continue
                raise last_err
            except urllib.error.URLError as e:
                last_err = LLMError(f"网络错误: {e}")
                if attempt < retries:
                    time.sleep(3 ** attempt)
                    continue
                raise last_err
        raise last_err or LLMError("LLM 调用失败")


def parse_json_block(text: str):
    """从 LLM 输出中提取 JSON（容忍 ```json 围栏、前后废话、尾随文字）。"""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        if t[:4].lower() == "json":
            t = t[4:]
        t = t.strip()
    best = None
    for opener in ("{", "["):
        i = t.find(opener)
        if i < 0:
            continue
        try:
            obj, _ = json.JSONDecoder().raw_decode(t[i:])  # 只取第一个完整 JSON 值
            if best is None or i < best[0]:
                best = (i, obj)
        except json.JSONDecodeError:
            continue
    if best is not None:
        return best[1]
    raise LLMError(f"输出中未找到 JSON: {text[:200]}")


# ---------------------------------------------------------------------------
# 钩子 1：决策（传给 KBTools.decide(llm=...)）
# ---------------------------------------------------------------------------
DECISION_SYSTEM = (
    "你是社交媒体账号矩阵的运营决策引擎。你只依据给定的人设与上下文生成下一步动作的内容，"
    "绝不虚构上下文中不存在的信息。只输出一个 JSON 对象，不要输出任何其他文字。"
)


def make_decision_llm(client: LLMClient = None, max_tokens=2048, temperature=0.8):
    """返回 (context) -> decision_dict 回调。action/topic 以上下文为准。"""
    client = client or LLMClient()

    def llm(ctx):
        user = (
            f"账号下一个动作：{ctx['action_type']}\n"
            f"目标话题：{ctx['topic_id']}\n\n"
            f"决策上下文（JSON）：\n"
            f"{json.dumps(ctx['blocks'], ensure_ascii=False, indent=1)}\n\n"
            "严格按以下格式输出 JSON（不要任何其他文字）：\n"
            "{\n"
            f'  "action": "{ctx["action_type"]}",\n'
            '  "content_type": "text_with_image 或 text",\n'
            f'  "topic": "{ctx["topic_id"]}",\n'
            '  "generated_content": "符合人设 name/occupation/language_style 的中文内容，'
            '80~200 字；若上下文含互动候选(candidates)，可针对其内容设计评论；'
            '若上下文含未兑现承诺(memory.open_promises)，优先围绕它创作（兑现连续性）",\n'
            '  "persona_fidelity_score": 0到1的小数（自评与人设的契合度）,\n'
            '  "rationale": "一句话理由"\n'
            "}\n"
        )
        raw = client.chat(DECISION_SYSTEM, user,
                          max_tokens=max_tokens, temperature=temperature)
        d = parse_json_block(raw)
        # 关键字段以上下文为准，不信任 LLM 复述
        d["action"] = ctx["action_type"]
        if ctx["topic_id"]:
            d["topic"] = ctx["topic_id"]
        score = d.get("persona_fidelity_score")
        if isinstance(score, (int, float)):
            d["persona_fidelity_score"] = max(0.0, min(1.0, float(score)))
        else:
            d["persona_fidelity_score"] = 0.5
        content = d.get("generated_content")
        if not isinstance(content, str) or not content.strip():
            raise LLMError("generated_content 为空")
        return d

    return llm


# ---------------------------------------------------------------------------
# 钩子 2：抽取（传给 KBTools.submit_result(llm=...)）
# ---------------------------------------------------------------------------
EXTRACT_SYSTEM = (
    "你是知识抽取器。从执行器反馈的自由文本中抽取值得长期记住的事实，"
    "只输出 JSON 数组，不要任何其他文字。宁缺毋滥：没有值得抽取的就输出 []。"
    "你抽取的只是候选事实，系统会再做 Schema 校验。"
)


def make_extract_llm(client: LLMClient = None, max_tokens=2048):
    """返回 (result) -> [{"kind", "payload"}] 回调。仅在 result 带 raw_text 时调用。"""
    client = client or LLMClient()
    schema_doc = (f"允许的节点类型: {sorted(NODE_TYPES)}\n"
                  f"允许的边类型: {sorted(EDGE_TYPES)}\n"
                  f"允许的事件类型: {sorted(EVENT_KINDS)}")

    def llm(result):
        raw_text = (result.get("raw_text") or "").strip()
        if not raw_text:
            return []          # 纯结构化结果：规则映射已覆盖，无需 LLM
        known = json.dumps({k: v for k, v in result.items() if k != "raw_text"},
                           ensure_ascii=False)
        user = (
            f"结构化结果（已由规则入库，不要重复抽取）：\n{known}\n\n"
            f"自由文本反馈：\n{raw_text}\n\n"
            f"{schema_doc}\n\n"
            "输出 JSON 数组，元素形如 {\"kind\": ..., \"payload\": {...}}：\n"
            '- 新事实节点: {"kind": "upsert_node", "payload": {"id": "节点ID", "type": "节点类型", "props": {...}}}\n'
            '- 已有节点补属性: {"kind": "set_prop", "payload": {"id": "已有节点ID", "prop": "属性名", "value": "值"}}\n'
            '- 新关系: {"kind": "upsert_edge", "payload": {"src": "...", "type": "边类型", "dst": "..."}}\n'
            "ID 规范：Account=acc:平台:handle，Topic=topic:a.b.c，Post=post:平台:帖子ID。\n"
            "特别地，若反馈中出现对粉丝的承诺（如“下期讲刷酸”“下周实测”），"
            "抽取为承诺节点：type=MemoryNote，id=note:promise-<短横线slug>，"
            "props 包含 fact=承诺原文、category=promise、confidence、status=open；"
            "并可用边 COMMITTED_IN 指向做出承诺的帖子（type 为边类型，src=承诺节点 id）。"
            "只使用上下文中出现过或符合规范的 ID，不要发明新格式。"
        )
        raw = client.chat(EXTRACT_SYSTEM, user,
                          max_tokens=max_tokens, temperature=0.2)
        cands = parse_json_block(raw)
        if isinstance(cands, dict):
            cands = [cands]
        return cands

    return llm


# ---------------------------------------------------------------------------
# 钩子 3：摘要（传给 daily_rollup(summarizer=...)）
# ---------------------------------------------------------------------------
SUMMARY_SYSTEM = "你是账号运营数据分析师。输出一段精炼的中文小结，不要列表符号，不要客套。"


def make_summarizer(client: LLMClient = None, max_tokens=2048):
    """返回 (acts) -> summary_str 回调。acts 为 [(action_id, props)]。"""
    client = client or LLMClient()

    def summarize(acts):
        lines = [
            f"- {p['action']} @ {(p.get('topic_id') or '-').replace('topic:', '')}"
            f"：{(p.get('digest') or '')[:30]}"
            for _, p in acts[:30]
        ]
        user = ("把以下账号当日行为流水压缩成 80 字以内的中文小结，"
                "突出行为结构与值得跟进的信号（如某话题互动密集）：\n" + "\n".join(lines))
        return client.chat(SUMMARY_SYSTEM, user, max_tokens=max_tokens)

    return summarize


# ---------------------------------------------------------------------------
# 钩子 4：周期叙事摘要（传给 period_rollup(summarizer=...)）
# ---------------------------------------------------------------------------
PERIOD_SUMMARY_SYSTEM = (
    "你是账号运营回忆录撰写者。把子级摘要卷积成一段周期叙事，"
    "只输出叙事正文，不要列表符号、不要客套。"
)


def make_period_summarizer(client: LLMClient = None, max_tokens=2048):
    """返回 (ctx) -> narrative_str 回调。ctx 为 period_rollup 组装的周期上下文。"""
    client = client or LLMClient()

    def summarize(ctx):
        items = "\n".join(f"- {p}: {s[:60]}" for p, s in ctx.get("items", [])[:14])
        drift = ctx.get("drift", 0)
        drift_note = (f"\n注意：期内有 {drift:.0%} 的动作发生在人设兴趣之外，"
                      "叙事中请点评是否人设漂移。" if drift > 0.2 else "")
        user = (f"账号 {ctx['level']} 期（{ctx['period']}）的子级摘要：\n{items}\n"
                f"压缩成 80 字以内的周期叙事（保留关键转折与信号）：{drift_note}")
        return client.chat(PERIOD_SUMMARY_SYSTEM, user, max_tokens=max_tokens)

    return summarize


# ---------------------------------------------------------------------------
# 钩子 5：记忆固化抽取（传给 consolidate_memory(extractor=...)）
# ---------------------------------------------------------------------------
MEMORY_EXTRACT_SYSTEM = (
    "你是账号记忆整理师。从近期情景叙事、帖子表现与失败记录中沉淀"
    "值得长期记住的稳定事实。只输出 JSON 数组，不要任何其他文字。"
    "宁缺毋滥：没有稳定结论就输出 []。"
    "元素格式：{\"fact\": \"结论（主谓完整的一句话）\", "
    "\"category\": \"fact|lesson|milestone|promise\", "
    "\"confidence\": 0到1小数, \"pinned\": true|false, "
    "\"post_id\": \"相关帖子ID或省略\"}。"
    "承诺(promise) = 账号对粉丝做出但尚未兑现的约定（如\"下期讲刷酸\"）。"
    "里程碑(milestone) 用 pinned=true。"
)


def make_memory_extractor(client: LLMClient = None, max_tokens=2048):
    """返回 (ctx) -> [{"fact","category","confidence","pinned","post_id"?}] 回调。"""
    client = client or LLMClient()
    valid = {"fact", "lesson", "milestone", "promise"}

    def extract(ctx):
        episodes = "\n".join(f"- {e.get('narrative', '')[:60]}"
                             for e in ctx.get("episodes", [])[:4])
        posts = "\n".join(f"- {p.get('digest', '')[:30]}"
                          f"（likes={p.get('likes', 0)}）"
                          for p in ctx.get("posts", [])[:8])
        failures = "\n".join(f"- {f[:50]}" for f in ctx.get("failures", [])[:4])
        user = (f"近期情景叙事：\n{episodes or '（无）'}\n\n"
                f"近期帖子：\n{posts or '（无）'}\n\n"
                f"失败记录：\n{failures or '（无）'}\n\n"
                "沉淀 2~4 条稳定事实（事实/教训/里程碑/承诺）：")
        raw = client.chat(MEMORY_EXTRACT_SYSTEM, user,
                          max_tokens=max_tokens, temperature=0.2)
        cands = parse_json_block(raw)
        if isinstance(cands, dict):
            cands = [cands]
        out = []
        for c in cands or []:
            if not isinstance(c, dict):
                continue
            fact = c.get("fact")
            if not isinstance(fact, str) or not fact.strip():
                continue
            cat = c.get("category", "fact")
            item = {"fact": fact.strip()[:120],
                    "category": cat if cat in valid else "fact",
                    "confidence": max(0.0, min(1.0, float(c.get("confidence", 0.7)))),
                    "pinned": bool(c.get("pinned"))}
            if isinstance(c.get("post_id"), str) and c["post_id"]:
                item["post_id"] = c["post_id"]
            out.append(item)
        return out

    return extract


# ---------------------------------------------------------------------------
# 钩子 6：承诺扫描（传给 KBTools.track_promises(scanner=...)）
# ---------------------------------------------------------------------------
PROMISE_SCAN_SYSTEM = (
    "你是承诺识别器。判断文本中账号对粉丝做出了哪些尚未兑现的承诺"
    "（如\"下期讲刷酸\"\"下周实测\"\"扣1人多就安排\"）。"
    "只输出 JSON 数组，不要任何其他文字；没有承诺输出 []。"
    "元素格式：{\"fact\": \"承诺内容（一句话）\", \"confidence\": 0到1小数}。"
    "注意区分：已经兑现的回顾不是承诺；对事实的陈述不是承诺。"
)


def make_promise_scanner(client: LLMClient = None, max_tokens=2048):
    """返回 (content) -> [{"fact", "confidence"}] 回调。"""
    client = client or LLMClient()

    def scan(content):
        if not content or not content.strip():
            return []
        raw = client.chat(PROMISE_SCAN_SYSTEM, content.strip()[:2000],
                          max_tokens=max_tokens, temperature=0.1)
        cands = parse_json_block(raw)
        if isinstance(cands, dict):
            cands = [cands]
        out = []
        for c in cands or []:
            if isinstance(c, dict) and isinstance(c.get("fact"), str) \
                    and c["fact"].strip():
                out.append({"fact": c["fact"].strip()[:120],
                            "confidence": max(0.0, min(1.0,
                                           float(c.get("confidence", 0.8))))})
        return out

    return scan
