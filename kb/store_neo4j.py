"""
Neo4j 持久化后端：与 kb.store.KnowledgeStore 同接口的实现。

架构定位（docs/04）：
- Neo4j 是**跨进程共享的物化层**：cron 维护进程与业务执行进程连同一实例
- 事件仍是唯一写入路径：apply() 在同一事务里写 KBEvent 节点（唯一约束
  承担跨进程幂等去重）+ 更新图状态
- 图结构原生（节点/边/关系类型/ID），属性载荷以 JSON 存储——Neo4j 属性
  不支持嵌套 dict（如 Rule.params / drift_detail），JSON 保证任意 props
  无损往返；需要 Cypher 直接过滤热属性时，v2 可把该字段提升为原生属性

与内存版语义对齐：
- apply() 返回 created / updated / conflict_ignored / duplicate
- 属性历史（10 条）、边仲裁（ts 大者胜、同 ts 比 confidence）逻辑一致
- 事件重复（跨进程）→ duplicate，不重复应用
"""
from __future__ import annotations

import json

from .schema import EDGE_TYPES, Event, validate_event
from .store import HISTORY_KEEP

META_KEYS = ("id", "_type", "_version", "_history")


class Neo4jKnowledgeStore:
    """接口与 KnowledgeStore 一致（见 kb/store.py 读方法列表）。"""

    def __init__(self, uri=None, user=None, password=None):
        from neo4j import GraphDatabase          # 懒加载：内存模式零依赖
        import os
        uri = uri or os.environ.get("CALLFANS_NEO4J_URI", "bolt://localhost:7687")
        user = user or os.environ.get("CALLFANS_NEO4J_USER", "neo4j")
        password = password or os.environ.get("CALLFANS_NEO4J_PASSWORD", "")
        self._driver = GraphDatabase.driver(uri, auth=(user, password))
        self._init_schema()

    def close(self):
        self._driver.close()

    # ------------------------------ schema ------------------------------
    def _init_schema(self):
        with self._driver.session() as s:
            s.run("CREATE CONSTRAINT kb_node_id IF NOT EXISTS "
                  "FOR (n:KBNode) REQUIRE n.id IS UNIQUE")
            s.run("CREATE CONSTRAINT kb_event_id IF NOT EXISTS "
                  "FOR (e:KBEvent) REQUIRE e.event_id IS UNIQUE")

    def wipe(self):
        """清空全部数据（Demo/测试用）。"""
        with self._driver.session() as s:
            s.run("MATCH (n) DETACH DELETE n")

    # ------------------------------ 读 ------------------------------
    def has_node(self, node_id: str) -> bool:
        with self._driver.session() as s:
            return s.run("MATCH (n:KBNode {id: $id}) RETURN 1 AS x",
                         id=node_id).single() is not None

    def get_node(self, node_id):
        with self._driver.session() as s:
            rec = s.run("MATCH (n:KBNode {id: $id}) RETURN n", id=node_id).single()
        return self._decode(rec["n"]) if rec else None

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
        q = ("MATCH (n:KBNode) RETURN n.id AS id"
             if node_type is None else
             "MATCH (n:KBNode {_type: $t}) RETURN n.id AS id")
        with self._driver.session() as s:
            if node_type is None:
                return [r["id"] for r in s.run(q)]
            return [r["id"] for r in s.run(q, t=node_type)]

    def iter_nodes(self):
        with self._driver.session() as s:
            rows = [(r["n"]["id"], self._decode(r["n"]))
                    for r in s.run("MATCH (n:KBNode) RETURN n")]
        return iter(rows)

    def get_edge(self, src, etype, dst):
        with self._driver.session() as s:
            rec = s.run(
                f"MATCH (:KBNode {{id: $s}})-[r:`{etype}`]->(:KBNode {{id: $d}}) "
                "RETURN r", s=src, d=dst).single()
        return self._decode_edge(rec["r"]) if rec else None

    def out_edges(self, src, etype=None):
        q = ("MATCH (:KBNode {id: $s})-[r]->(b:KBNode) "
             "RETURN type(r) AS t, b.id AS dst, r AS r")
        with self._driver.session() as s:
            rows = [(r["t"], r["dst"], self._decode_edge(r["r"]))
                    for r in s.run(q, s=src)]
        if etype is None:
            return rows
        return [x for x in rows if x[0] == etype]

    def in_edges(self, dst, etype=None):
        q = ("MATCH (a:KBNode)-[r]->(:KBNode {id: $d}) "
             "RETURN type(r) AS t, a.id AS src, r AS r")
        with self._driver.session() as s:
            rows = [(r["t"], r["src"], self._decode_edge(r["r"]))
                    for r in s.run(q, d=dst)]
        if etype is None:
            return rows
        return [x for x in rows if x[0] == etype]

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
        with self._driver.session() as s:
            nodes = s.run("MATCH (n:KBNode) RETURN count(n) AS c").single()["c"]
            edges = s.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
            events = s.run("MATCH (e:KBEvent) RETURN count(e) AS c").single()["c"]
        return {"nodes": nodes, "edges": edges, "events": events}

    # ---------------- 写（事务：事件唯一约束去重 + 图更新） ----------------
    def apply(self, ev: Event) -> str:
        validate_event(ev)
        try:
            with self._driver.session() as s:
                return s.execute_write(self._apply_tx, ev)
        except Exception as e:
            if "ConstraintValidation" in str(e) or "already exists" in str(e):
                return "duplicate"        # 跨进程重复投递：事务整体回滚，零副作用
            raise

    def _apply_tx(self, tx, ev: Event) -> str:
        # 1. 事件先落库（唯一约束 = 跨进程幂等）
        hit = tx.run("MATCH (e:KBEvent {event_id: $eid}) RETURN e.event_id",
                     eid=ev.event_id).single()
        if hit is not None:
            return "duplicate"
        tx.run("CREATE (e:KBEvent {event_id: $eid, partition: $p, kind: $k, "
               "payload: $payload, ts: $ts})",
               eid=ev.event_id, p=ev.partition, k=ev.kind,
               payload=json.dumps(ev.payload, ensure_ascii=False), ts=ev.ts)

        p, k = ev.payload, ev.kind
        if k == "upsert_node":
            return self._tx_upsert_node(tx, p, ev)
        if k == "set_prop":
            return self._tx_set_prop(tx, p, ev)
        if k == "upsert_edge":
            return self._tx_upsert_edge(tx, p, ev)
        if k == "incr_edge":
            return self._tx_incr_edge(tx, p, ev)
        raise ValueError(f"未知事件类型: {k}")

    # ---- 各 kind 的事务实现（读-改-写，Neo4j 行锁保证同节点串行） ----
    def _tx_upsert_node(self, tx, p, ev):
        rec = tx.run("MATCH (n:KBNode {id: $id}) RETURN n", id=p["id"]).single()
        if rec is None:
            props = self._merge_props({"_type": p["type"], "_version": 0,
                                       "_history": {}},
                                      p.get("props") or {}, ev.ts)
            props["id"] = p["id"]
            tx.run("CREATE (n:KBNode $props)", props=self._encode(props))
            return "created"
        cur = self._decode(rec["n"])
        props = self._merge_props(
            {**{"_type": cur["type"], "_version": cur["version"],
                "_history": cur["history"]}},
            p.get("props") or {}, ev.ts)
        props["id"] = p["id"]
        tx.run("MATCH (n:KBNode {id: $id}) SET n = $props",
               id=p["id"], props=self._encode(props))
        return "updated"

    def _tx_set_prop(self, tx, p, ev):
        rec = tx.run("MATCH (n:KBNode {id: $id}) RETURN n", id=p["id"]).single()
        if rec is None:
            raise KeyError(f"节点不存在: {p['id']}")
        cur = self._decode(rec["n"])
        props = {"_type": cur["type"], "_version": cur["version"],
                 "_history": cur["history"], **cur["props"]}
        self._set_one(props, p["prop"], p["value"], ev.ts)
        props["_version"] = cur["version"] + 1
        props["id"] = p["id"]
        tx.run("MATCH (n:KBNode {id: $id}) SET n = $props",
               id=p["id"], props=self._encode(props))
        return "updated"

    def _tx_upsert_edge(self, tx, p, ev):
        self._tx_check_endpoints(tx, p)
        rec = tx.run(
            f"MATCH (:KBNode {{id: $s}})-[r:`{p['type']}`]->(:KBNode {{id: $d}}) "
            "RETURN r", s=p["src"], d=p["dst"]).single()
        meta = {"ts": ev.ts, "confidence": p.get("confidence", 1.0),
                "source": p.get("source", ""), "event_id": ev.event_id}
        if rec is None:
            props = dict(p.get("props") or {})
            props.update(meta)
            tx.run(f"MATCH (a:KBNode {{id: $s}}), (b:KBNode {{id: $d}}) "
                   f"CREATE (a)-[r:`{p['type']}` $props]->(b)",
                   s=p["src"], d=p["dst"], props=self._encode_edge(props))
            return "created"
        cur = self._decode_edge(rec["r"])
        if (ev.ts, meta["confidence"]) >= (cur.get("ts", 0), cur.get("confidence", 0)):
            for k2, v2 in (p.get("props") or {}).items():
                if v2 is not None:
                    cur[k2] = v2
            cur.update(meta)
            tx.run(f"MATCH (:KBNode {{id: $s}})-[r:`{p['type']}`]->"
                   f"(:KBNode {{id: $d}}) SET r = $props",
                   s=p["src"], d=p["dst"], props=self._encode_edge(cur))
            return "updated"
        return "conflict_ignored"

    def _tx_incr_edge(self, tx, p, ev):
        self._tx_check_endpoints(tx, p)
        rec = tx.run(
            f"MATCH (:KBNode {{id: $s}})-[r:`{p['type']}`]->(:KBNode {{id: $d}}) "
            "RETURN r", s=p["src"], d=p["dst"]).single()
        if rec is None:
            props = {"count": 0, "weight": 0.0, "confidence": 1.0}
            tx.run(f"MATCH (a:KBNode {{id: $s}}), (b:KBNode {{id: $d}}) "
                   f"CREATE (a)-[r:`{p['type']}` $props]->(b)",
                   s=p["src"], d=p["dst"], props=self._encode_edge(props))
            rec = tx.run(
                f"MATCH (:KBNode {{id: $s}})-[r:`{p['type']}`]->"
                f"(:KBNode {{id: $d}}) RETURN r", s=p["src"], d=p["dst"]).single()
        cur = self._decode_edge(rec["r"])
        cur["count"] = cur.get("count", 0) + p.get("n", 1)
        cur["last_active"] = ev.ts
        cur["ts"] = ev.ts
        cur["source"] = p.get("source", cur.get("source", ""))
        tx.run(f"MATCH (:KBNode {{id: $s}})-[r:`{p['type']}`]->"
               f"(:KBNode {{id: $d}}) SET r = $props",
               s=p["src"], d=p["dst"], props=self._encode_edge(cur))
        return "updated"

    def _tx_check_endpoints(self, tx, p):
        src_t, dst_t = EDGE_TYPES[p["type"]]
        for nid, expect in ((p["src"], src_t), (p["dst"], dst_t)):
            rec = tx.run("MATCH (n:KBNode {id: $id}) RETURN n._type AS t",
                         id=nid).single()
            if rec is None:
                raise KeyError(f"边端点不存在: {nid}")
            if expect != "*" and rec["t"] != expect:
                raise ValueError(
                    f"边端点类型不匹配: {nid} 期望 {expect} 实际 {rec['t']}")

    # ------------------------------ 编解码 ------------------------------
    @staticmethod
    def _encode(props: dict) -> dict:
        """节点属性 → Neo4j 原生属性（载荷 JSON 化）。"""
        out = {"id": props["id"], "_type": props["_type"],
               "_version": props["_version"],
               "_history": json.dumps(props["_history"], ensure_ascii=False)}
        payload = {k: v for k, v in props.items() if k not in META_KEYS}
        out["props"] = json.dumps(payload, ensure_ascii=False)
        return out

    @staticmethod
    def _decode(n) -> dict:
        return {"type": n["_type"], "version": n["_version"],
                "history": json.loads(n.get("_history") or "{}"),
                "props": json.loads(n.get("props") or "{}")}

    @staticmethod
    def _encode_edge(props: dict) -> dict:
        return {"props": json.dumps(props, ensure_ascii=False)}

    @staticmethod
    def _decode_edge(r) -> dict:
        return json.loads(r.get("props") or "{}")

    # ---- 属性合并 / 历史（与内存版 _set_prop 语义一致） ----
    @staticmethod
    def _set_one(props, prop, value, ts):
        old = props.get(prop)
        if prop in props and old != value:
            hist = props["_history"].setdefault(prop, [])
            hist.append({"ts": ts, "value": old})
            del hist[:-HISTORY_KEEP]
        props[prop] = value

    def _merge_props(self, base, incoming, ts):
        props = dict(base)
        props["_history"] = dict(props.get("_history") or {})
        for k, v in incoming.items():
            if v is not None:
                self._set_one(props, k, v, ts)
        props["_version"] = (props.get("_version") or 0) + 1
        return props
