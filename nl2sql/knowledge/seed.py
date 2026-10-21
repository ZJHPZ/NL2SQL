"""把黄金集里的规范查询写成知识库的 few-shot 示例。

为什么要有留出集
----------------
如果把 30 题的 (问题, SQL) 全部入库，再拿这 30 题评测，就是**答案泄露**——
模型只要检索到原题就必然答对，测出来的不是"知识库有没有用"，
而是"检索准不准"。

因此本模块提供 `split()`：
    入库集（seed）  = A/B/C 三类共 15 题
    留出集（holdout）= D/E/F 三类共 15 题
只在入库集上做知识种子，只在留出集上评测。

示例的 SQL 来自**人工构造**（两种规划器都答对或都答错的标准写法），
不是模型生成的——否则会把模型的错误固化成"标准答案"。
"""
from __future__ import annotations

import json
import os
from typing import Any

from ..contracts.knowledge import KnowledgeEntry
from .naming import stable_key

__all__ = ["seed_from_golden", "split", "SEED_CATEGORIES", "HOLDOUT_CATEGORIES"]

# A/B/C 三类用于入库（基础过滤、分组聚合、时间趋势）
SEED_CATEGORIES = ("基础过滤与聚合", "分组聚合", "时间趋势")
# D/E/F 三类留作评测（多表关联、窗口与排名、子查询与CTE）
HOLDOUT_CATEGORIES = ("多表关联", "窗口与排名", "子查询与CTE")


def _load_baseline(golden_dir: str) -> list[dict[str, Any]]:
    path = os.path.join(golden_dir, "baseline.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)["cases"]


def split(golden_dir: str) -> tuple[list[dict], list[dict]]:
    """按类别切分为 (入库集, 留出集)。"""
    cases = _load_baseline(golden_dir)
    seed = [c for c in cases if c.get("category") in SEED_CATEGORIES]
    holdout = [c for c in cases if c.get("category") in HOLDOUT_CATEGORIES]
    return seed, holdout


def seed_from_golden(golden_dir: str, *,
                     only_categories: tuple[str, ...] | None = None,
                     max_items: int | None = None) -> list[KnowledgeEntry]:
    """把黄金集的规范 (问题, SQL) 转成 `sql_pair` 知识条目。

    Args:
        only_categories: 只取这些类别的题；None 表示全部（**评测时不要用 None**，
            会造成答案泄露）。
        max_items: 上限，便于小规模试验。

    条目正文（进入检索）包含：问题 + 类别 + 语义提示；
    标准 SQL 放在 `extra["sql"]`，只在拼 Prompt 时使用，
    避免 SQL 文本参与向量检索而干扰问题匹配。
    """
    cases = _load_baseline(golden_dir)
    if only_categories is not None:
        cases = [c for c in cases if c.get("category") in only_categories]
    if max_items:
        cases = cases[:max_items]

    entries: list[KnowledgeEntry] = []
    for c in cases:
        qid, question = c["id"], c["question"]
        tags = list(c.get("tags") or [])
        body_parts = [
            f"问题：{question}",
            f"类别：{c.get('category', '')}",
        ]
        if tags:
            body_parts.append("要点：" + "、".join(tags))
        entries.append(KnowledgeEntry(
            key=stable_key("sql_pair", f"q_{qid}"),
            type="sql_pair",
            title=question,
            body="\n".join(body_parts),
            source_id=c.get("source_id") or None,
            origin="golden:baseline",
            source_ref=qid,
            tags=["示例", c.get("category", ""), *tags],
            extra={
                "question": question,
                "sql": c["sql"],
                "golden_id": qid,
                "category": c.get("category", ""),
            },
        ))
    return entries
