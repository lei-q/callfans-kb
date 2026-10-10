"""
记忆检索：中文 bigram 倒排索引 + 分层下钻。

设计（docs/03 第 3 节）：
- 只索引"摘要层"（label/digest/summary_*/fact/narrative），原始层不付检索成本
- bigram 分词：零依赖的中文语义粗召回（"防晒"→ 防/晒/防晒）
- 分层下钻：年/月摘要定位 → 日摘要 → 必要时 source_uri 原始层（rollup 红利）
"""
from __future__ import annotations

INDEXED_EXACT = ("label", "name", "digest", "fact", "narrative", "summary")
INDEXED_PREFIX = ("summary_day:", "summary_week:", "summary_month:",
                  "summary_year:", "fact:")


def _bigrams(text: str) -> set:
    text = "".join(str(text).split())          # 去空白
    if len(text) <= 1:
        return {text} if text else set()
    return {text[i:i + 2] for i in range(len(text) - 1)} | {c for c in text}


class InvertedIndex:
    """进程内倒排索引。store 变化后需 rebuild（KBTools 内做了版本缓存）。"""

    def __init__(self):
        self._postings = {}                    # bigram -> {node_id: weight}
        self._docs = {}                        # node_id -> (type, snippet)

    @classmethod
    def build(cls, store) -> "InvertedIndex":
        idx = cls()
        for nid, n in store.iter_nodes():
            fields = [v for k, v in n["props"].items()
                      if (k in INDEXED_EXACT or k.startswith(INDEXED_PREFIX))
                      and isinstance(v, str)]
            if not fields:
                continue
            blob = " ".join(fields)
            idx._docs[nid] = (n["type"], blob[:80])
            for g in _bigrams(blob):
                idx._postings.setdefault(g, {}).setdefault(nid, 0)
                idx._postings[g][nid] += 1
        return idx

    def search(self, query, k=5) -> list:
        """返回 [{node_id, type, score, snippet}]，按 bigram 重合度排序。"""
        qgrams = _bigrams(query)
        if not qgrams:
            return []
        scores = {}
        for g in qgrams:
            for nid, w in self._postings.get(g, {}).items():
                scores[nid] = scores.get(nid, 0) + w
        ranked = sorted(scores.items(), key=lambda x: -x[1])[:k]
        return [{"node_id": nid, "type": self._docs[nid][0],
                 "score": round(s / max(1, len(qgrams)), 2),
                 "snippet": self._docs[nid][1]} for nid, s in ranked]


def drill_down(store, account_id, level, period) -> dict:
    """分层下钻：给定周期 → 返回其子级摘要（月→周/日，周→日）。

    年度检索命中 200 字月摘要即可定位时间段，不必扫描 365 天流水。
    """
    props = store.node_props(account_id)
    if level == "year":
        keys = [k for k in props if k.startswith("summary_month:")
                and k.split(":", 1)[1].startswith(period)]
    elif level == "month":
        keys = [k for k in props if k.startswith("summary_week:")
                and k.split(":", 1)[1][:4] == period[:4]]
        days = [k for k in props if k.startswith("summary_day:")
                and k.split(":", 1)[1].startswith(period)]
        keys += days
    else:                                       # week → 展开 7 天
        from .rollup import _period_of
        keys = [k for k in props if k.startswith("summary_day:")
                and _period_of(k.split(":", 1)[1], "week") == period]
    return {"account_id": account_id, "level": level, "period": period,
            "children": {k.split(":", 1)[1]: props[k] for k in sorted(keys)}}
