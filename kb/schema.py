"""
v1 最小知识 Schema：节点、边、事件与校验。

对应设计文档：docs/01-knowledge-schema-v1.md
核心原则：Schema 即过滤器 —— 任何不符合本定义的数据在入库前被直接拒绝。
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 节点类型
# ---------------------------------------------------------------------------
NODE_TYPES = {"Subject", "Account", "Persona", "Topic", "Post", "Rule",
              "ActionRecord", "Episode", "MemoryNote"}

# 节点类型说明（记忆层）：
#   Subject    记忆主体（数字生命/人/agent/项目）：记忆与人设的归属者，
#              Account 是它在某平台上的操作句柄（1 Subject : N Account）
#   Episode    情景记忆：周/月/年卷积叙事（level=week|month|year，含人设漂移检查）
#   MemoryNote 语义记忆：从经历中沉淀的稳定事实/经验教训/里程碑/对粉丝的承诺
#              节点只存客观部分（fact/category，全局内容指纹去重）；
#              主观状态（status/pinned/confidence/ts）在 HAS_MEMORY 边上，按主体私有

# ID 规范（入库前强校验）
ID_PATTERNS = {
    "Subject": re.compile(r"^subj:[\w\-]+$"),             # subj:slug（跨平台主体）
    "Account": re.compile(r"^acc:[a-z]+:[\w.\-]+$"),      # acc:平台:handle
    "Persona": re.compile(r"^per:[\w\-]+$"),              # per:slug
    "Topic": re.compile(r"^topic:[a-z0-9_.\-]+$"),        # topic:a.b.c（层级用 . 分隔）
    "Post": re.compile(r"^post:[a-z]+:[\w.\-]+$"),        # post:平台:原生帖子ID
    "Rule": re.compile(r"^rule:[\w\-]+$"),                # rule:slug
    "ActionRecord": re.compile(r"^act:[\w\-]+$"),         # act:uuid 或 act:seed-001
    "Episode": re.compile(r"^ep:[\w.\-]+$"),              # ep:主体slug.周期
    "MemoryNote": re.compile(r"^note:[\w\-]+$"),          # note:md5前10（事实指纹，幂等）
}

# ---------------------------------------------------------------------------
# 边类型：(起点类型, 终点类型)，"*" 表示任意已存在节点
# ---------------------------------------------------------------------------
EDGE_TYPES = {
    "HAS_HANDLE":     ("Subject", "Account"),       # 主体 → 平台账号句柄（1:N）
    "HAS_PERSONA":    ("Subject", "Persona"),       # 主体 → 人设（本体定义边）
    "INTERESTED_IN":  ("Persona", "Topic"),         # 人设兴趣（本体定义边）
    "FOLLOWS":        ("Account", "Topic"),         # 行为聚合的话题关注（带计数）
    "PUBLISHED":      ("Account", "Post"),
    "ABOUT":          ("Post", "Topic"),
    "INTERACTS_WITH": ("Account", "Account"),       # 行为聚合的互动（带计数/权重）
    "APPLIES_TO":     ("Rule", "Topic"),
    "PERFORMED":      ("Account", "ActionRecord"),  # 行为流水（热区数据，账号级）
    "DECIDED_VIA":    ("ActionRecord", "*"),        # 决策依据（provenance，可追溯）
    "HAS_EPISODE":    ("Subject", "Episode"),       # 主体 → 情景记忆（周/月/年叙事）
    "HAS_MEMORY":     ("Subject", "MemoryNote"),    # 主体 → 语义记忆；主观状态在本边上
    "COMMITTED_IN":   ("MemoryNote", "Post"),       # 承诺在哪条内容中做出
    "FULFILLED_BY":   ("MemoryNote", "*"),          # 承诺由哪次行为兑现（闭环追踪）
}

# ---------------------------------------------------------------------------
# 事件（写入的唯一合法形态）
# ---------------------------------------------------------------------------
EVENT_KINDS = {"upsert_node", "set_prop", "upsert_edge", "incr_edge"}


@dataclass
class Event:
    event_id: str                 # 全局唯一：幂等去重的依据
    partition: str                # 分区键（通常为 account_id）：同分区串行写
    kind: str
    payload: dict
    ts: float = field(default_factory=time.time)


def make_event(partition: str, kind: str, payload: dict,
               event_id: str = None, ts: float = None) -> Event:
    """构造并校验事件。校验失败直接抛 ValueError —— 即"Schema 即过滤器"。"""
    ev = Event(
        event_id=event_id or f"evt:{uuid.uuid4().hex[:12]}",
        partition=partition, kind=kind, payload=payload,
        ts=ts if ts is not None else time.time(),
    )
    validate_event(ev)
    return ev


def new_action_id() -> str:
    return f"act:{uuid.uuid4().hex[:16]}"


def topic_slug(topic_id: str) -> str:
    """topic:beauty.skincare → beauty.skincare"""
    return topic_id.split(":", 1)[1] if topic_id.startswith("topic:") else topic_id


def _require(payload: dict, keys: tuple):
    for k in keys:
        if k not in payload:
            raise ValueError(f"payload 缺少字段 {k}: {payload}")


def validate_event(ev: Event) -> None:
    """不认识的 kind / 边类型 / 节点类型 / ID 一律拒绝。"""
    if ev.kind not in EVENT_KINDS:
        raise ValueError(f"未知事件类型: {ev.kind}")
    p = ev.payload
    if ev.kind == "upsert_node":
        _require(p, ("id", "type"))
        if p["type"] not in NODE_TYPES:
            raise ValueError(f"未知节点类型: {p['type']}")
        if not ID_PATTERNS[p["type"]].match(p["id"]):
            raise ValueError(f"ID 不符合规范: {p['id']} ({p['type']})")
    elif ev.kind == "set_prop":
        _require(p, ("id", "prop", "value"))
    elif ev.kind in ("upsert_edge", "incr_edge"):
        _require(p, ("src", "type", "dst"))
        if p["type"] not in EDGE_TYPES:
            raise ValueError(f"未知边类型: {p['type']}")
