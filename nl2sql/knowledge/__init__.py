"""知识库：条目管理 + 多路召回 + 自动生成。

个人场景：**免审，但保留版本、停用开关、测试检索**。

为什么"测试检索"是一等能力
--------------------------
没有它，知识库的错误是**静默的**——用户上传了文档、以为生效了，
但检索可能永远命中不到，或命中到错误条目。所以每次入库都应能
"输入一个问题，看到实际命中了哪几条、为什么命中"。
"""
from __future__ import annotations

from typing import Any

from ..contracts.knowledge import (
    KnowledgeEntry,
    KnowledgeType,
    RetrievalHit,
    RetrievalQuery,
)
from ..contracts.source import TableMeta
from ..errors import KnowledgeError
from .naming import stable_key

__all__ = [
    "KnowledgeService",
    "Bm25Retriever",
    "VectorRetriever",
    "rrf_fuse",
    "entries_from_table_meta",
    "stable_key",
    "seed_from_golden",
    "split",
    "SEED_CATEGORIES",
    "HOLDOUT_CATEGORIES",
]

from .seed import (  # noqa: E402  (放在 __all__ 之后，seed 依赖 naming 而非本模块)
    HOLDOUT_CATEGORIES,
    SEED_CATEGORIES,
    seed_from_golden,
    split,
)

# RRF（Reciprocal Rank Fusion）常数。60 是原论文的常用取值：
# 它对名次靠前的差异更敏感，同时避免单路召回的绝对分数主导融合结果。
_RRF_K = 60


def rrf_fuse(rankings: list[list[str]], *, k: int = _RRF_K) -> dict[str, float]:
    """多路召回融合（Reciprocal Rank Fusion）。

    为什么用 RRF 而不是分数加权：
        BM25 的分数与向量余弦相似度量纲完全不同，加权需要归一化，
        而归一化参数在不同数据集上要反复调。RRF 只用**名次**，
        无需跨路可比，鲁棒且无需调参。
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
    return scores


def stable_key_compat(entry_type: KnowledgeType, identifier: str) -> str:
    """兼容包装，保留给外部调用方。真实实现见 `naming.stable_key`。"""
    return stable_key(entry_type, identifier)

class Bm25Retriever:
    """基于 SQLite FTS5 的 BM25 召回。

    中文切分由 `storage/text.py` 处理（CJK 二元组）——
    直接用 unicode61 会让中文检索**静默失效**，详见该文件说明。
    """

    name = "bm25"

    def __init__(self, store: Any) -> None:
        self._store = store

    def retrieve(self, query: RetrievalQuery) -> list[tuple[str, float]]:
        return self._store.search_fts(
            query.text, source_id=query.source_id,
            types=query.types, top_k=query.top_k * 3,
        )


class VectorRetriever:
    """基于 VectorStore 的向量召回。

    默认 `HashEmbedder` 是**免依赖占位实现**（字符 n-gram），
    不是语义向量——它的存在是让 P0 不被模型选型阻塞。
    换成 embedding 模型后，这一路的质量才有实质提升。
    """

    name = "vector"

    def __init__(self, vector_store: Any, store: Any) -> None:
        self._vector = vector_store
        self._store = store

    def retrieve(self, query: RetrievalQuery) -> list[tuple[str, float]]:
        if self._vector is None:
            return []
        where: dict[str, Any] = {}
        if query.source_id:
            where["source_id"] = query.source_id
        hits = self._vector.search(query.text, top_k=query.top_k * 3,
                                   where=where or None)
        # 过滤掉已停用的条目（向量库不知道 enabled 状态）
        out: list[tuple[str, float]] = []
        for key, score in hits:
            entry = self._store.get(key)
            if entry is not None and entry.enabled:
                if query.types and entry.type not in query.types:
                    continue
                out.append((key, score))
        return out


class KnowledgeService:
    """知识库门面：管理 + 检索 + 自动生成。

    设计要点：
    * **只有 enabled 的条目参与检索**（硬规则）
    * `test_retrieve` 返回命中原因，供 UI 展示
    * 写入一律留修订；删除是软删（保留历史）
    """

    def __init__(self, store: Any, *, vector_store: Any = None,
                 hybrid: bool = True) -> None:
        self._store = store
        self._vector = vector_store
        self._hybrid = hybrid
        self._bm25 = Bm25Retriever(store)
        self._vec = VectorRetriever(vector_store, store)

    # ------------------------------------------------------------ 管理
    def upsert(self, entry: KnowledgeEntry, *, note: str = "") -> KnowledgeEntry:
        saved = self._store.upsert(entry, note=note)
        self._index([saved])
        return saved

    def upsert_many(self, entries: list[KnowledgeEntry], *,
                    note: str = "") -> list[KnowledgeEntry]:
        saved = [self._store.upsert(e, note=note) for e in entries]
        self._index(saved)
        return saved

    def get(self, key: str) -> KnowledgeEntry | None:
        return self._store.get(key)

    def list(self, *, source_id: str | None = None,
             types: tuple[KnowledgeType, ...] = (),
             include_disabled: bool = False) -> list[KnowledgeEntry]:
        return self._store.list(source_id=source_id, types=types,
                                include_disabled=include_disabled)

    def set_enabled(self, key: str, enabled: bool) -> None:
        """停用/启用。怀疑某条污染结果时可立即下线。"""
        self._store.set_enabled(key, enabled)
        entry = self._store.get(key)
        if entry is not None:
            self._index([entry])

    def delete(self, key: str) -> None:
        """软删：停用并移出检索，历史保留。"""
        self._store.delete(key)
        if self._vector is not None:
            self._vector.delete([key])

    def history(self, key: str) -> list[Any]:
        return self._store.history(key)

    def rollback(self, key: str, version: int) -> KnowledgeEntry:
        entry = self._store.rollback(key, version)
        self._index([entry])
        return entry

    # ------------------------------------------------------------ 索引
    def _index(self, entries: list[KnowledgeEntry]) -> None:
        if self._vector is None or not entries:
            return
        ids, texts, metas = [], [], []
        for e in entries:
            if not e.enabled:
                continue
            ids.append(e.key)
            # 标题 + 正文 + 标签一起入向量，提升召回
            texts.append(f"{e.title}\n{e.body}\n{' '.join(e.tags)}")
            metas.append({"source_id": e.source_id or "",
                          "type": e.type, "key": e.key})
        if ids:
            self._vector.upsert(ids, texts, metas)

    def reindex(self) -> int:
        """全量重建向量索引。换 embedding 模型后必须调用。"""
        if self._vector is None:
            return 0
        entries = self.list(include_disabled=False)
        if self._vector is not None:
            self._vector.delete([e.key for e in entries])
        self._index(entries)
        return len(entries)

    # ------------------------------------------------------------ 检索
    def retrieve(self, query: RetrievalQuery) -> list[RetrievalHit]:
        """多路召回 + RRF 融合。

        返回的 `why` 字段说明命中来源与名次，供测试检索页展示。
        """
        if not query.text.strip():
            return []

        bm25_hits = self._bm25.retrieve(query)
        vec_hits = self._vec.retrieve(query) if self._hybrid else []

        bm25_rank = [k for k, _ in bm25_hits]
        vec_rank = [k for k, _ in vec_hits]
        fused = rrf_fuse([r for r in (bm25_rank, vec_rank) if r])

        if not fused:
            return []

        bm25_pos = {k: i for i, k in enumerate(bm25_rank)}
        vec_pos = {k: i for i, k in enumerate(vec_rank)}

        hits: list[RetrievalHit] = []
        for key, score in sorted(fused.items(), key=lambda kv: -kv[1]):
            entry = self._store.get(key)
            if entry is None or not entry.enabled:
                continue
            if query.source_id and entry.source_id not in (None, query.source_id):
                continue
            if query.types and entry.type not in query.types:
                continue
            why_parts = []
            if key in bm25_pos:
                why_parts.append(f"bm25#{bm25_pos[key] + 1}")
            if key in vec_pos:
                why_parts.append(f"vector#{vec_pos[key] + 1}")
            hits.append(RetrievalHit(
                entry=entry, score=round(score, 6),
                why="+".join(why_parts) or "fused",
            ))
            if len(hits) >= query.top_k:
                break
        return hits

    def test_retrieve(self, text: str, *, source_id: str | None = None,
                      types: tuple[KnowledgeType, ...] = (),
                      top_k: int = 8) -> list[RetrievalHit]:
        """测试检索：查看某问题实际命中了哪些条目、为什么命中。

        这是知识库可用性的唯一可靠验证方式——没有它，
        上传的知识是否生效只能靠猜。
        """
        return self.retrieve(RetrievalQuery(
            text=text, source_id=source_id, types=types, top_k=top_k,
        ))

    def few_shot_examples(self, question: str, *, source_id: str | None = None,
                          top_k: int = 3) -> list[tuple[str, str]]:
        """取相似的历史 (问题, SQL) 对，供规划 Prompt 使用。

        **只召回 sql_pair 类型**，且只有 enabled 的条目——
        被停用的错误示例不会污染 Prompt。
        """
        hits = self.retrieve(RetrievalQuery(
            text=question, source_id=source_id, types=("sql_pair",),
            top_k=top_k,
        ))
        out: list[tuple[str, str]] = []
        for h in hits:
            sql = h.entry.extra.get("sql") or ""
            if not sql:
                continue
            out.append((h.entry.extra.get("question") or h.entry.title, sql))
        return out

    # ------------------------------------------------------------ 自动生成
    def auto_generate_from_comments(self, source_id: str,
                                    tables: list[TableMeta]) -> list[KnowledgeEntry]:
        """从数据源库表注释生成初版知识条目（幂等）。

        幂等的关键：**已存在的 key 不覆盖**，否则用户对条目的编辑会被
        每次同步冲掉。这让"自动生成"成为一次性的起点，而不是持续覆盖。
        """
        created: list[KnowledgeEntry] = []
        for entry in entries_from_table_meta(tables, source_id=source_id):
            if self._store.get(entry.key) is not None:
                continue
            created.append(self._store.upsert(entry, note="自动生成（库表注释）"))
        self._index(created)
        return created


def entries_from_table_meta(tables: list[TableMeta], *,
                            source_id: str) -> list[KnowledgeEntry]:
    """库表元数据 → schema 类知识条目（每表一条）。

    这是降低上手门槛的关键：用户连上数据源后不必面对空白知识库，
    而是在"已填好的初稿"上编辑。
    """
    out: list[KnowledgeEntry] = []
    for t in tables:
        lines = []
        if t.comment:
            lines.append(t.comment)
        if t.row_estimate is not None:
            lines.append(f"约 {t.row_estimate} 行")
        col_desc = []
        for c in t.columns:
            piece = f"{c.name} {c.data_type}".strip()
            if c.comment:
                piece += f"（{c.comment}）"
            if c.is_primary_key:
                piece += " [主键]"
            if c.generated:
                piece += " [生成列]"
            col_desc.append(piece)
        body = "\n".join(lines)
        if col_desc:
            body += ("\n\n字段：\n" + "\n".join(f"- {c}" for c in col_desc))
        if t.foreign_keys:
            fks = "\n".join(f"- {a} → {b}.{c}" for a, b, c in t.foreign_keys)
            body += f"\n\n外键：\n{fks}"
        out.append(KnowledgeEntry(
            key=stable_key("schema", t.name),
            type="schema",
            title=t.name,
            body=body.strip(),
            source_id=source_id,
            origin="auto:comments",
            source_ref=t.name,
            tags=["表结构", t.name],
        ))
    return out
