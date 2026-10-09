"""
存储工厂：按环境切换内存 / SQLite / Neo4j 后端。

环境变量：
  CALLFANS_STORE=memory|sqlite|neo4j    （默认 memory：零依赖离线模式）
  CALLFANS_SQLITE_PATH=~/.callfans/callfans.db   （sqlite 后端）
  CALLFANS_NEO4J_URI=bolt://localhost:7687        （neo4j 后端）
  CALLFANS_NEO4J_USER=neo4j
  CALLFANS_NEO4J_PASSWORD=...

后端选型（docs/04）：memory=测试/CI；sqlite=桌面单机（嵌入式，随 App 分发）；
neo4j=服务器多进程/团队共享。

用法：
  store, queue = open_kb()           # 与 build_seed(store, queue) 组合
"""
from __future__ import annotations

import os


def open_store(env=None):
    env = env or dict(os.environ)
    backend = env.get("CALLFANS_STORE", "memory")
    if backend == "neo4j":
        from .store_neo4j import Neo4jKnowledgeStore   # 懒加载：neo4j 可选依赖
        return Neo4jKnowledgeStore(uri=env.get("CALLFANS_NEO4J_URI"),
                                   user=env.get("CALLFANS_NEO4J_USER"),
                                   password=env.get("CALLFANS_NEO4J_PASSWORD"))
    if backend == "sqlite":
        from .store_sqlite import SQLiteKnowledgeStore  # stdlib：零依赖
        return SQLiteKnowledgeStore(path=env.get("CALLFANS_SQLITE_PATH"))
    if backend == "memory":
        from .store import KnowledgeStore
        return KnowledgeStore()
    raise ValueError(f"未知存储后端: {backend}（可选 memory|sqlite|neo4j）")


def open_queue(store):
    from .store import WriteQueue
    return WriteQueue(store)


def open_kb(env=None):
    """返回 (store, queue)。"""
    store = open_store(env)
    return store, open_queue(store)
