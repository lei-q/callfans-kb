"""
知识库存储（MVP：进程内实现，接口按可换 Neo4j 设计）。

设计要点：
- 读路径无锁（MVP 单进程；生产环境换 Neo4j 只读副本）
- 写路径只能经 WriteQueue → apply()：同分区串行、event_id 幂等
- 属性更新保留最近 HISTORY_KEEP 条历史（支持回溯与纠错）
- 边冲突仲裁：ts 大者胜；ts 相同时比 confidence
"""
from __future__ import annotations

import threading
from collections import defaultdict, deque

from .schema import EDGE_TYPES, Event, validate_event

HISTORY_KEEP = 10  # 每个属性保留的历史版本数


class KnowledgeStore:
    def __init__(self):
        self._nodes = {}                    # id -> {"type", "props", "version", "history"}
        self._edges = {}                    # (src, type, dst) -> props（含 ts/confidence/count 等）
        self._adj_out = defaultdict(list)   # src -> [(etype, dst)]
        self._adj_in = defaultdict(list)    # dst -> [(etype, src)]

    # ------------------------------ 读 ------------------------------
    def has_node(self, node_id: str) -> bool:
        return node_id in self._nodes

    def get_node(self, node_id):
        return self._nodes.get(node_id)

    def node(self, node_id) -> dict:
        n = self._nodes.get(node_id)
        if n is None:
            raise KeyError(f"节点不存在: {node_id}")
        return n

    def node_props(self, node_id) -> dict:
        return self.node(node_id)["props"]

    def node_type(self, node_id) -> str:
        return self.node(node_id)["type"]

    def node_ids(self, node_type: str = None):
        if node_type is None:
            return list(self._nodes)
        return [i for i, n in self._nodes.items() if n["type"] == node_type]

    def iter_nodes(self):
        return self._nodes.items()

    def get_edge(self, src, etype, dst):
        return self._edges.get((src, etype, dst))

    def out_edges(self, src, etype=None):
        """返回 [(etype, dst, props), ...]"""
        return [(t, d, self._edges[(src, t, d)])
                for (t, d) in self._adj_out.get(src, [])
                if etype is None or t == etype]

    def in_edges(self, dst, etype=None):
        """返回 [(etype, src, props), ...]"""
        return [(t, s, self._edges[(s, t, dst)])
                for (t, s) in self._adj_in.get(dst, [])
                if etype is None or t == etype]

    def subject_of(self, node_id) -> str:
        """记忆主体归一：Account → 所属 Subject（HAS_HANDLE 的起点）；
        Subject → 自身；无主体的账号（如外部 KOL）→ 自身（记忆操作不会发生
        在这类账号上，返回自身仅保证 partition 语义不崩）。
        所有记忆层操作（HAS_EPISODE/HAS_MEMORY/摘要/固化）经此归一到主体。"""
        n = self.get_node(node_id)
        if n is None:
            raise KeyError(f"节点不存在: {node_id}")
        if n["type"] == "Subject":
            return node_id
        if n["type"] == "Account":
            for _, src, _ in self.in_edges(node_id, "HAS_HANDLE"):
                return src
        return node_id

    def persona_id_of(self, node_id) -> str:
        """人设归属：主体（或账号 → 先归一主体）→ HAS_PERSONA。"""
        subj = self.subject_of(node_id)
        for _, dst, _ in self.out_edges(subj, "HAS_PERSONA"):
            return dst
        raise KeyError(f"主体未绑定人设: {node_id} → {subj}")

    def accounts_of(self, subject_id) -> list:
        """主体的全部平台账号句柄（按 HAS_HANDLE）。"""
        return [d for _, d, _ in self.out_edges(subject_id, "HAS_HANDLE")]

    def stats(self) -> dict:
        return {"nodes": len(self._nodes), "edges": len(self._edges)}

    # ---------------- 写（仅由 WriteQueue 消费者调用） ----------------
    def apply(self, ev: Event) -> str:
        """应用一个事件，返回 created / updated / conflict_ignored。"""
        validate_event(ev)
        p, k = ev.payload, ev.kind

        if k == "upsert_node":
            nid = p["id"]
            existed = nid in self._nodes
            if not existed:
                self._nodes[nid] = {"type": p["type"], "props": {},
                                    "version": 0, "history": {}}
            n = self._nodes[nid]
            for key, val in p.get("props", {}).items():
                if val is not None:          # None 表示"不覆盖"
                    self._set_prop(nid, key, val, ev.ts)
            n["version"] += 1
            return "created" if not existed else "updated"

        if k == "set_prop":
            self.node(p["id"])               # 必须已存在
            self._set_prop(p["id"], p["prop"], p["value"], ev.ts)
            self._nodes[p["id"]]["version"] += 1
            return "updated"

        if k == "upsert_edge":
            self._check_endpoints(p)
            key = (p["src"], p["type"], p["dst"])
            cur = self._edges.get(key)
            meta = {"ts": ev.ts, "confidence": p.get("confidence", 1.0),
                    "source": p.get("source", ""), "event_id": ev.event_id}
            if cur is None:
                props = dict(p.get("props", {}))
                props.update(meta)
                self._edges[key] = props
                self._index(key)
                return "created"
            # 冲突仲裁：ts 大者胜；同 ts 比 confidence
            if (ev.ts, meta["confidence"]) >= (cur.get("ts", 0), cur.get("confidence", 0)):
                for key2, val in p.get("props", {}).items():
                    if val is not None:
                        cur[key2] = val
                cur.update(meta)
                return "updated"
            return "conflict_ignored"

        if k == "incr_edge":
            self._check_endpoints(p)
            key = (p["src"], p["type"], p["dst"])
            cur = self._edges.get(key)
            if cur is None:
                cur = {"count": 0, "weight": 0.0, "confidence": 1.0}
                self._edges[key] = cur
                self._index(key)
            # 有界压缩：同一条关系只累加 count，不产生新记录
            cur["count"] = cur.get("count", 0) + p.get("n", 1)
            cur["last_active"] = ev.ts
            cur["ts"] = ev.ts
            cur["source"] = p.get("source", cur.get("source", ""))
            return "updated"

        raise ValueError(f"未知事件类型: {k}")

    # ------------------------------ 内部 ------------------------------
    def _check_endpoints(self, p):
        src_t, dst_t = EDGE_TYPES[p["type"]]
        for nid, expect in ((p["src"], src_t), (p["dst"], dst_t)):
            n = self.get_node(nid)
            if expect == "*":
                if n is None:
                    raise KeyError(f"边端点不存在: {nid}")
            elif n is None or n["type"] != expect:
                raise ValueError(f"边端点类型不匹配: {nid} 期望 {expect} 实际 {n and n['type']}")

    def _index(self, key):
        src, etype, dst = key
        self._adj_out[src].append((etype, dst))
        self._adj_in[dst].append((etype, src))

    def _set_prop(self, nid, prop, value, ts):
        n = self.node(nid)
        old = n["props"].get(prop)
        if prop in n["props"] and old != value:
            hist = n["history"].setdefault(prop, [])
            hist.append({"ts": ts, "value": old})
            del hist[:-HISTORY_KEEP]         # 只保留最近 N 条历史
        n["props"][prop] = value


class WriteQueue:
    """按 partition（账号）分区的写入队列。

    - submit()：线程安全、幂等（重复 event_id 直接丢弃）
    - drain()：顺序消费。MVP 为单消费者；生产环境改为"每分区独立 worker"，
      即可实现"同账号串行、跨账号并发"。
    """

    def __init__(self, store: KnowledgeStore):
        self.store = store
        self._queues = defaultdict(deque)
        self._seen = set()
        self._lock = threading.Lock()
        self.stats = {"submitted": 0, "duplicated": 0, "applied": 0}

    def submit(self, ev: Event) -> bool:
        with self._lock:
            if ev.event_id in self._seen:
                self.stats["duplicated"] += 1
                return False
            self._seen.add(ev.event_id)
            self._queues[ev.partition].append(ev)
            self.stats["submitted"] += 1
            return True

    def pending(self) -> int:
        with self._lock:
            return sum(len(q) for q in self._queues.values())

    def drain(self, max_events: int = None) -> int:
        n = 0
        while max_events is None or n < max_events:
            ev = None
            with self._lock:
                for q in self._queues.values():
                    if q:
                        ev = q.popleft()
                        break
            if ev is None:
                break
            try:
                result = self.store.apply(ev)
                if result == "duplicate":
                    # 跨进程重复（Neo4j 后端：另一进程已应用同一事件）
                    self.stats["duplicated"] += 1
                else:
                    self.stats["applied"] += 1
            except (ValueError, KeyError):
                # Schema 即过滤器的第二层：端点类型/存在性不合法的候选事件
                # （典型：LLM 抽取的边）只计数不中断，业务回写继续
                self.stats["rejected"] = self.stats.get("rejected", 0) + 1
            n += 1
        return n
