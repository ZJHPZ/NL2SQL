"""列名候选与"相似字段建议"。

用途：当生成的 SQL 引用了不存在的列时，**不要抛数据库原始错误**，
而是给出可用字段候选。这是"只给结果表"方案里错误体验的关键一环。
"""
from __future__ import annotations

__all__ = ["suggest_columns"]


def suggest_columns(bad: str, available: list[str], *, top_k: int = 5) -> list[str]:
    """TODO(S4): 用 difflib 或编辑距离给出候选列名。"""
    import difflib

    return difflib.get_close_matches(bad, available, n=top_k, cutoff=0.5)
