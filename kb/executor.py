"""
执行层适配：连接知识库决策与云机脚本执行器。

适配的执行器形态（以 demo/xhs.py 为原型）：
- 独立 CLI 脚本，由调度平台以 argv 方式拉起（appium_url/device 等环境参数 + 业务参数）
- 标题/正文以 base64 传入（-text / -content）
- 自身不产出结构化结果，成败由进程退出码与平台日志判定

两个方向：
- plan()            decide() 输出 → ExecutionSpec（含 base64 标题/正文）
- build_command()   ExecutionSpec → xhs.py 完整命令行
- run()             通过可注入 runner 执行（默认 subprocess；测试用 FakeRunner）
- report()          执行结果 → result dict → tools.submit_result() 幂等回写

幂等设计：action_id 由 job_id 派生（act:xhs:<job_id>），调度平台重试同一
job 时事件 ID 相同，写队列层自动去重，不会重复计数。
"""
from __future__ import annotations

import base64
import subprocess
import time

from .schema import new_action_id

XHS_TITLE_LIMIT = 20          # 小红书标题字数上限
DEFAULT_SCRIPT = "demo/xhs.py"


def _id_slug(s):
    """job_id → 合法 ID 片段：仅保留 [\\w.-]，其余替换为 _（满足 schema ID 规范）。"""
    import re
    return re.sub(r"[^\w.\-]", "_", str(s))


def split_title_content(text, max_title=XHS_TITLE_LIMIT):
    """决策生成的整段内容 → (标题, 正文)。

    规则：首行为标题（超长截断到 20 字），其余为正文；
    单行内容则标题取前 20 字、正文保留全文（视频帖常见做法）。
    """
    text = (text or "").strip().replace("\\n", "\n")
    if not text:
        return "", ""
    lines = text.split("\n", 1)
    if len(lines) == 2 and lines[1].strip():
        return lines[0].strip()[:max_title], lines[1].strip()
    return text[:max_title], text


def _b64(text):
    return base64.b64encode((text or "").encode("utf-8")).decode("ascii")


def default_runner(argv, timeout=None):
    """默认 runner：真实子进程执行。云机任务耗时数分钟，timeout 必须放宽。"""
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


class ExecutorAdapter:
    """知识库 ⇄ 云机执行器的双向适配。

    runner 注入点用于测试：FakeRunner 记录 argv 并返回假结果，
    不需要 sma_autoui / 真机即可验证全链路。
    """

    def __init__(self, tools, script=DEFAULT_SCRIPT, python="python3",
                 runner=None, action_prefix="act:xhs"):
        self.tools = tools
        self.script = script
        self.python = python
        self.runner = runner or default_runner
        self.action_prefix = action_prefix

    # ===============================================================
    # 方向一：决策 → 执行命令
    # ===============================================================
    def plan(self, account_id, action_type, topic_id=None, job_id=None,
             task_params=None, llm=None, budget_tokens=2000) -> dict:
        """两段式决策 → ExecutionSpec。规则闸门拦截时返回 blocked，不生成命令。"""
        decision = self.tools.decide(account_id, action_type, topic_id,
                                     llm=llm, budget_tokens=budget_tokens)
        if decision["status"] != "approved":
            return {"status": "blocked", "decision": decision, "spec": None}

        params = dict(task_params or {})
        # job_id 派生 action_id：同一 job 重试 → 同一 action_id → 回写幂等
        job_slug = _id_slug(job_id) if job_id else new_action_id().split(":", 1)[1]
        action_id = f"{self.action_prefix}-{job_slug}"
        title, body = split_title_content(decision.get("generated_content", ""))
        spec = {
            "action_id": action_id,
            "job_id": job_id or action_id,
            "account_id": account_id,
            "action": action_type,
            "topic_id": decision.get("topic") or topic_id,
            "decision_id": decision.get("decision_id"),
            "knowledge_refs": decision.get("knowledge_refs", []),
            "title": title,
            "content": body,
            "text_b64": _b64(title),
            "content_b64": _b64(body),
            "content_type": decision.get("content_type", "text"),
            # 执行器参数（folder_name/images_nums/draft 等），平台侧配置
            "task_params": params,
        }
        return {"status": "approved", "decision": decision, "spec": spec}

    def build_command(self, spec, device_env) -> list:
        """ExecutionSpec + 设备环境 → 完整 argv（demo/xhs.py 的调用契约）。"""
        p = spec["task_params"]
        argv = [
            self.python, self.script,
            "-j", str(spec["job_id"]),
            "-u", device_env["appium_url"],
            "-d", device_env["deviceName"],
            "-s", str(device_env.get("systemPort", "")),
            "-a", str(device_env.get("adbPort", "")),
            "-p", "Android",
            "-ver", str(device_env.get("platformVersion", "")),
            "-accountId", spec["account_id"],
            "-folder_name", str(p.get("folder_name", "Movies")),
            "-images_nums", str(p.get("images_nums", 1)),
            "-text", spec["text_b64"],
            "-content", spec["content_b64"],
            "-enable_bgm", "1" if p.get("enable_bgm") else "0",
            "-original", "1" if p.get("original") else "0",
            "-draft", "1" if p.get("draft") else "0",
        ]
        return argv

    def run(self, spec, device_env, timeout=None) -> dict:
        """拉起执行脚本。返回 {ok, exit_code, output}，不在此处回写。"""
        argv = self.build_command(spec, device_env)
        code, output = self.runner(argv, timeout=timeout)
        return {"ok": code == 0, "exit_code": code, "output": output,
                "argv": argv}

    # ===============================================================
    # 方向二：执行结果 → 知识库回写
    # ===============================================================
    def report(self, spec, outcome=None, llm=None, promise_scanner=None) -> dict:
        """执行结果回写知识库（走摄取漏斗，Schema 约束 + 幂等）。

        发布成功后自动扫描生成内容（标题+正文）中的新承诺并入库
        （v2 缺口补齐：LLM 埋的坑也要追踪）。promise_scanner 可接
        make_promise_scanner，缺省用确定性关键词兑底。

        outcome 字段（全部可选）：
          ok            执行成败（默认 True）
          new_post_id   发布成功的帖子 ID（缺省时按 job 派生稳定 ID）
          post_url      分享链接（存入 digest 附注，便于人工核对）
          stats         平台数据 {views/likes/comments...}
          raw_text      执行日志/评论原文等自由文本 → LLM 抽取候选事实
          digest        覆盖默认摘要
        """
        outcome = dict(outcome or {})
        ok = outcome.get("ok", True)
        action = spec["action"]
        digest = outcome.get("digest") or spec["title"] or spec["action_id"]

        new_post_id = outcome.get("new_post_id")
        if not new_post_id and ok and action == "post" \
                and not spec["task_params"].get("draft"):
            # 稳定派生：同 job 重试/补报数据时指向同一 Post 节点
            new_post_id = (f"post:{spec['account_id'].split(':')[1]}:"
                           f"{_id_slug(spec['job_id'])}")

        result = {
            "action_id": spec["action_id"],
            "account_id": spec["account_id"],
            "action": action,
            "topic_id": spec["topic_id"],
            "new_post_id": new_post_id,
            "digest": digest if ok else f"[失败] {digest}",
            "outcome": "success" if ok else "failed",
            "content_type": spec.get("content_type", "text"),
            "stats": outcome.get("stats", {}),
            "decision_id": spec["decision_id"],
            "knowledge_refs": spec["knowledge_refs"],
            "ts": time.time(),
            "source": "executor:xhs",
            "raw_text": outcome.get("raw_text", ""),
        }
        if outcome.get("post_url"):
            result["raw_text"] = (result["raw_text"] + "\n"
                                  + f"分享链接: {outcome['post_url']}").strip()
        write_stats = self.tools.submit_result(result, llm=llm)
        tracked = {"promises_found": 0, "notes": []}
        if ok and action == "post" and not spec["task_params"].get("draft"):
            content = f"{spec.get('title', '')}\n{spec.get('content', '')}"
            try:
                tracked = self.tools.track_promises(
                    spec["account_id"], content,
                    post_id=new_post_id, scanner=promise_scanner)
            except Exception:
                tracked = {"promises_found": 0, "notes": [],
                           "error": "scanner_failed"}
        return {"result": result, "write": write_stats, "promises": tracked}
