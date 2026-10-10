"""
SQLite 嵌入式后端：桌面单机形态（与 memory / Neo4j 同接口）。

定位（docs/04）：
- 桌面应用打包的默认后端：单文件、零服务、随 App 分发
- events 表是唯一真相（INSERT OR IGNORE = 跨进程幂等去重）；
  nodes/edges 物化表加速读路径（无需重放）
- WAL 模式支持"多进程读 + 单写者"（与 cron 维护进程并存）

与内存版语义对齐：apply 返回 created/updated/conflict_ignored/duplicate；
属性历史（10 条）、边仲裁（ts 大者胜、同 ts 比 confidence）一致。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading

from .schema import EDGE_TYPES, Event, validate_event
from .store import HISTORY_KEEP

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  event_id      TEXT PRIMARY KEY,
  partition_key TEXT NOT NULL,
  kind          TEXT NOT NULL,
  payload       TEXT NOT NULL,
  ts            REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS nodes (
  id      TEXT PRIMARY KEY,
  type    TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 0,
  history TEXT NOT NULL DEFAULT '{}',
  props   TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS edges (
  src   TEXT NOT NULL,
  type  TEXT NOT NULL,
  dst   TEXT NOT NULL,
  props TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (src, type, dst)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges (src, type);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges (dst, type);
"""


class SQLiteKnowledgeStore:
    """接口与 kb.store.KnowledgeStore 一致。"""

    def __init__(self, path=None):
        path = path or os.environ.get(
            "CALLFANS_SQLITE_PATH",
            os.path.join(os.path.expanduser("~"), ".callfans", "callfans.db"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, timeout=30,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    def wipe(self):
        """清空全部数据（Demo/测试用）。"""
        with self._lock:
            with self._conn:
                self._conn.execute("DELETE FROM events")
                self._conn.execute("DELETE FROM nodes")
                self._conn.execute("DELETE FROM edges")

    # ------------------------------ 读 ------------------------------
    def has_node(self, node_id: str) -> bool:
        with self._lock:
            return self._conn.execute(
                "SELECT 1 FROM nodes WHERE id=?", (node_id,)).fetchone() \
                is not None

    def get_node(self, node_id):
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        return self._decode(r) if r else None

    def node(self, node_id) -> dict:
        n = self.get_node(node_id)
        if n is None:
            raise KeyError(f"节点不存在: {node_id}")
        return n

    def node_props(self, node_id) -> dict:
        return self.node(node_id)["props"]

    def node_type(self, node_id) -> str:
        return self.node(node_id)["type"]

    def node_ids(self, node_type: str = None):
        with self._lock:
            if node_type is None:
                rows = self._conn.execute("SELECT id FROM nodes").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id FROM nodes WHERE type=?", (node_type,)).fetchall()
        return [r["id"] for r in rows]

    def iter_nodes(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM nodes").fetchall()
        return iter((r["id"], self._decode(r)) for r in rows)

    def get_edge(self, src, etype, dst):
        with self._lock:
            r = self._conn.execute(
                "SELECT props FROM edges WHERE src=? AND type=? AND dst=?",
                (src, etype, dst)).fetchone()
        return json.loads(r["props"]) if r else None

    def out_edges(self, src, etype=None):
        with self._lock:
            if etype is None:
                rows = self._conn.execute(
                    "SELECT type, dst, props FROM edges WHERE src=?",
                    (src,)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT type, dst, props FROM edges WHERE src=? AND type=?",
                    (src, etype)).fetchall()
        return [(r["type"], r["dst"], json.loads(r["props"])) for r in rows]

    def in_edges(self, dst, etype=None):
        with self._lock:
            if etype is None:
                rows = self._conn.execute(
                    "SELECT type, src, props FROM edges WHERE dst=?",
                    (dst,)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT type, src, props FROM edges WHERE dst=? AND type=?",
                    (dst, etype)).fetchall()
        return [(r["type"], r["src"], json.loads(r["props"])) for r in rows]

    def subject_of(self, node_id) -> str:
        """记忆主体归一：Account → HAS_HANDLE 起点；Subject → 自身；
        其余/无主体账号 → 自身（与内存后端语义一致，见 kb/store.py）。"""
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
        subj = self.subject_of(node_id)
        for _, dst, _ in self.out_edges(subj, "HAS_PERSONA"):
            return dst
        raise KeyError(f"主体未绑定人设: {node_id} → {subj}")

    def accounts_of(self, subject_id) -> list:
        return [d for _, d, _ in self.out_edges(subject_id, "HAS_HANDLE")]

    def stats(self) -> dict:
        with self._lock:
            c = self._conn.execute
            return {"nodes": c("SELECT count(*) AS n FROM nodes"
                               ).fetchone()["n"],
                    "edges": c("SELECT count(*) AS n FROM edges"
                               ).fetchone()["n"],
                    "events": c("SELECT count(*) AS n FROM events"
                                ).fetchone()["n"]}

    # ---------------- 写（单事务：事件幂等 + 物化更新） ----------------
    def apply(self, ev: Event) -> str:
        validate_event(ev)
        with self._lock:
            try:
                with self._conn:              # 单事务：失败整体回滚
                    cur = self._conn.execute(
                        "INSERT OR IGNORE INTO events "
                        "(event_id, partition_key, kind, payload, ts) "
                        "VALUES (?,?,?,?,?)",
                        (ev.event_id, ev.partition, ev.kind,
                         json.dumps(ev.payload, ensure_ascii=False), ev.ts))
                    if cur.rowcount == 0:
                        return "duplicate"     # 跨进程/重试重复：零副作用
                    return self._apply_kind(ev)
            except sqlite3.OperationalError:
                raise                          # 锁超时等由调用方重试策略处理

    def _apply_kind(self, ev: Event) -> str:
        p, k = ev.payload, ev.kind
        if k == "upsert_node":
            return self._upsert_node(p, ev)
        if k == "set_prop":
            return self._set_prop(p, ev)
        if k == "upsert_edge":
            return self._upsert_edge(p, ev)
        if k == "incr_edge":
            return self._incr_edge(p, ev)
        raise ValueError(f"未知事件类型: {k}")

    def _upsert_node(self, p, ev):
        r = self._conn.execute(
            "SELECT * FROM nodes WHERE id=?", (p["id"],)).fetchone()
        incoming = {k: v for k, v in (p.get("props") or {}).items()
                    if v is not None}
        if r is None:
            props, history, version = {}, {}, 0
            props.update(incoming)
            version = 1
            self._conn.execute(
                "INSERT INTO nodes (id, type, version, history, props) "
                "VALUES (?,?,?,?,?)",
                (p["id"], p["type"], version, "{}",
                 json.dumps(props, ensure_ascii=False)))
            return "created"
        cur = self._decode(r)
        props, history = dict(cur["props"]), cur["history"]
        for key, val in incoming.items():
            self._set_one(props, history, key, val, ev.ts)
        self._conn.execute(
            "UPDATE nodes SET version=?, history=?, props=? WHERE id=?",
            (cur["version"] + 1, json.dumps(history, ensure_ascii=False),
             json.dumps(props, ensure_ascii=False), p["id"]))
        return "updated"

    def _set_prop(self, p, ev):
        r = self._conn.execute(
            "SELECT * FROM nodes WHERE id=?", (p["id"],)).fetchone()
        if r is None:
            raise KeyError(f"节点不存在: {p['id']}")
        cur = self._decode(r)
        props, history = dict(cur["props"]), cur["history"]
        self._set_one(props, history, p["prop"], p["value"], ev.ts)
        self._conn.execute(
            "UPDATE nodes SET version=?, history=?, props=? WHERE id=?",
            (cur["version"] + 1, json.dumps(history, ensure_ascii=False),
             json.dumps(props, ensure_ascii=False), p["id"]))
        return "updated"

    def _upsert_edge(self, p, ev):
        self._check_endpoints(p)
        r = self._conn.execute(
            "SELECT props FROM edges WHERE src=? AND type=? AND dst=?",
            (p["src"], p["type"], p["dst"])).fetchone()
        meta = {"ts": ev.ts, "confidence": p.get("confidence", 1.0),
                "source": p.get("source", ""), "event_id": ev.event_id}
        if r is None:
            props = dict(p.get("props") or {})
            props.update(meta)
            self._conn.execute(
                "INSERT INTO edges (src, type, dst, props) VALUES (?,?,?,?)",
                (p["src"], p["type"], p["dst"],
                 json.dumps(props, ensure_ascii=False)))
            return "created"
        cur = json.loads(r["props"])
        if (ev.ts, meta["confidence"]) >= (cur.get("ts", 0),
                                           cur.get("confidence", 0)):
            for k2, v2 in (p.get("props") or {}).items():
                if v2 is not None:
                    cur[k2] = v2
            cur.update(meta)
            self._conn.execute(
                "UPDATE edges SET props=? WHERE src=? AND type=? AND dst=?",
                (json.dumps(cur, ensure_ascii=False),
                 p["src"], p["type"], p["dst"]))
            return "updated"
        return "conflict_ignored"

    def _incr_edge(self, p, ev):
        self._check_endpoints(p)
        r = self._conn.execute(
            "SELECT props FROM edges WHERE src=? AND type=? AND dst=?",
            (p["src"], p["type"], p["dst"])).fetchone()
        if r is None:
            cur = {"count": 0, "weight": 0.0, "confidence": 1.0}
        else:
            cur = json.loads(r["props"])
        cur["count"] = cur.get("count", 0) + p.get("n", 1)
        cur["last_active"] = ev.ts
        cur["ts"] = ev.ts
        cur["source"] = p.get("source", cur.get("source", ""))
        self._conn.execute(
            "INSERT INTO edges (src, type, dst, props) VALUES (?,?,?,?) "
            "ON CONFLICT(src, type, dst) DO UPDATE SET props=excluded.props",
            (p["src"], p["type"], p["dst"],
             json.dumps(cur, ensure_ascii=False)))
        return "updated"

    def _check_endpoints(self, p):
        src_t, dst_t = EDGE_TYPES[p["type"]]
        for nid, expect in ((p["src"], src_t), (p["dst"], dst_t)):
            r = self._conn.execute(
                "SELECT type FROM nodes WHERE id=?", (nid,)).fetchone()
            if r is None:
                raise KeyError(f"边端点不存在: {nid}")
            if expect != "*" and r["type"] != expect:
                raise ValueError(
                    f"边端点类型不匹配: {nid} 期望 {expect} 实际 {r['type']}")

    # ------------------------------ 编解码 ------------------------------
    @staticmethod
    def _decode(r) -> dict:
        return {"type": r["type"], "version": r["version"],
                "history": json.loads(r["history"]),
                "props": json.loads(r["props"])}

    @staticmethod
    def _set_one(props, history, prop, value, ts):
        old = props.get(prop)
        if prop in props and old != value:
            hist = history.setdefault(prop, [])
            hist.append({"ts": ts, "value": old})
            del hist[:-HISTORY_KEEP]
        props[prop] = value
