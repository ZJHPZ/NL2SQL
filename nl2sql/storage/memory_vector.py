"""内存向量库 + 免依赖向量化器（默认实现）。

用途：P0 零依赖起步。用户不配任何模型也能用知识库的 BM25 部分，
向量部分用哈希向量占位，接口先跑通。

进阶实现（sqlite-vec / lancedb / 本地 embedding 模型）走同一 `VectorStore` 契约，
替换时不需要改上层。
"""
from __future__ import annotations

import hashlib
import math
from typing import Any

DIM = 256


class HashEmbedder:
    """字符 n-gram 哈希向量化。**免模型依赖的占位实现**。

    重要：这不是语义向量。它只能做"字面相似"的近似，
    真正语义检索请换成本地模型或远端 API。

    存在的价值：让 P0 不被模型选型阻塞，且 `Embedder` 接口先跑通。
    """

    name = "hash"
    dim = DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def embed_one(self, text: str) -> list[float]:
        return self._one(text)

    @staticmethod
    def _one(text: str) -> list[float]:
        vec = [0.0] * DIM
        for n in (1, 2, 3):
            for i in range(len(text) - n + 1):
                gram = text[i:i + n]
                h = int(hashlib.md5(gram.encode("utf-8")).hexdigest()[:8], 16)
                vec[h % DIM] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def _matches(meta: dict[str, Any], where: dict[str, Any] | None) -> bool:
    if not where:
        return True
    for k, v in where.items():
        if meta.get(k) != v:
            return False
    return True


class MemoryVectorStore:
    """实现 `VectorStore` 契约：余弦相似度 + 元数据等值过滤。

    规模上限：个人场景（知识条目数百到数千）足够。
    超过万级应换 sqlite-vec / lancedb——它们同样走这个契约。
    """

    name = "memory"

    def __init__(self, embedder: Any) -> None:
        self._embedder = embedder
        self._items: dict[str, tuple[list[float], dict[str, Any]]] = {}

    def upsert(self, ids: list[str], texts: list[str],
               metadatas: list[dict[str, Any]] | None = None) -> None:
        metas = metadatas or [{} for _ in ids]
        vectors = self._embedder.embed(texts)
        for i, _id in enumerate(ids):
            self._items[_id] = (vectors[i], metas[i] if i < len(metas) else {})

    def search(self, text: str, *, top_k: int = 8,
               where: dict[str, Any] | None = None) -> list[tuple[str, float]]:
        if not self._items or not text.strip():
            return []
        query = self._embedder.embed([text])[0]
        scored: list[tuple[str, float]] = []
        for _id, (vec, meta) in self._items.items():
            if not _matches(meta, where):
                continue
            scored.append((_id, _cosine(query, vec)))
        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored[:top_k]

    def delete(self, ids: list[str]) -> None:
        for _id in ids:
            self._items.pop(_id, None)

    def __len__(self) -> int:
        return len(self._items)
