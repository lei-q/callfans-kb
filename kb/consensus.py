"""
公共记忆上卷：个体信号 → 多账号确证 → 全局规则（灰度生效）。

设计（docs/03 第 2 节）：
- 私有记忆严格分区（账号只写自己的分区）；公共区只由确证机制写入
- 单账号遭遇的异常（限流迹象、规则变化）只是候选信号；N 个独立账号
  遭遇才确证为全局规则——避免把巧合固化成知识
- 生效灰度：confirmed 后设 effective_at，且每个账号按确定性哈希错峰
  采纳（全员同日改变行为模式本身就是机器特征）
"""
from __future__ import annotations

import hashlib
import re
import time

from .schema import make_event

DEFAULT_THRESHOLD = 2          # 独立账号确证数阈值
DEFAULT_BASE_DELAY = 0.0       # confirmed → effective_at 的基础延迟（秒）
DEFAULT_JITTER_WINDOW = 86400  # 各账号错峰采纳的抖动窗口（秒）


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9\-]+", "-", str(s).lower()).strip("-")[:40] or "sig"


def rule_active_for(rule_props: dict, account_id: str, now=None,
                    rule_id: str = "") -> bool:
    """规则对该账号当前是否生效（灰度判定）。

    - 无 status 字段的规则（种子/手工规则）视为始终生效（向后兼容）
    - candidate：未确证，不生效
    - confirmed：now >= effective_at + 账号抖动偏移 才对该账号生效
    """
    now = now or time.time()
    status = rule_props.get("status")
    if not status:
        return True
    if status != "confirmed":
        return False
    effective = rule_props.get("effective_at", 0)
    if now < effective:
        return False
    offset = _adoption_offset(rule_id or rule_props.get("_rule_id", ""), account_id,
                              rule_props.get("jitter_window", DEFAULT_JITTER_WINDOW))
    return now >= effective + offset


def _adoption_offset(rule_id: str, account_id: str, window: int) -> float:
    """确定性抖动：同 (rule, account) 恒定，不同账号错峰。离线可测。"""
    h = int(hashlib.md5(f"{rule_id}|{account_id}".encode()).hexdigest()[:8], 16)
    return (h % 1000) / 1000.0 * window


def report_signal(store, queue, account_id, signal_id, name, message,
                  params=None, kind="advisory", threshold=DEFAULT_THRESHOLD,
                  base_delay=DEFAULT_BASE_DELAY,
                  jitter_window=DEFAULT_JITTER_WINDOW) -> dict:
    """账号上报异常信号 → 候选全局规则；多账号独立确证后固化并灰度生效。

    幂等：同一账号重复上报同一信号不重复计数（confirmations 去重）。
    """
    rid = f"rule:sig-{_slug(signal_id)}"
    now = time.time()
    node = store.get_node(rid)
    confirmations = list((node or {}).get("props", {}).get("confirmations", []))
    newly = account_id not in confirmations
    if newly:
        confirmations.append(account_id)
    confirmed = len(confirmations) >= threshold
    status = "confirmed" if confirmed else "candidate"

    rule_props = {
        "name": name, "text": message, "kind": kind,
        "params": params or {}, "platform": None,       # 全平台信号
        "status": status, "confirmations": confirmations,
        "jitter_window": jitter_window, "_rule_id": rid,
        "first_seen": (node or {}).get("props", {}).get("first_seen", now),
    }
    if confirmed and not (node and node["props"].get("effective_at")):
        rule_props["effective_at"] = now + base_delay

    events = [make_event(account_id, "upsert_node",
                         {"id": rid, "type": "Rule", "props": rule_props})]
    for ev in events:
        queue.submit(ev)
    queue.drain()
    return {"rule_id": rid, "status": status,
            "confirmations": len(confirmations), "newly_confirmed_by": newly,
            "confirmed": confirmed}
