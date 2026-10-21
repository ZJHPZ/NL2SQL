"""强制 LIMIT 的实现。

策略（刻意保守）
----------------
* **已有 LIMIT → 一律不动**。因为无法可靠判断这个 LIMIT 属于最外层还是子查询，
  改写有把语义改错的风险。宁可不动，也不猜。
* 没有 LIMIT → 在末尾追加 `LIMIT n`。
* 方言不支持 LIMIT（Oracle 用 ROWNUM）→ 交给上层处理，这里只标注。

为什么不做"收紧已有 LIMIT"：
    若生成的 SQL 是 `SELECT * FROM (SELECT ... LIMIT 5000) LIMIT 10`，
    盲目替换第一个 LIMIT 会改掉子查询语义。语义正确优先于省几行内存——
    适配器本身也会按 `max_rows + 1` 截断读取，内存风险已被兜住。
"""
from __future__ import annotations

import re

from ..contracts.source import SourceCapabilities
from ..dialect.tokens import tokenize

__all__ = ["apply_limit", "has_limit", "LimitResult"]

_TRAILING_SEMICOLON = re.compile(r";\s*$")


class LimitResult:
    __slots__ = ("sql", "changed", "note")

    def __init__(self, sql: str, changed: bool, note: str = "") -> None:
        self.sql = sql
        self.changed = changed
        self.note = note


def has_limit(sql: str) -> bool:
    """检测顶层是否已有 LIMIT。

    只看**未被括号包裹**的 LIMIT，避免把子查询里的 LIMIT 误认为顶层限额。
    Oracle 的 `ROWNUM` 也一并识别。
    """
    depth = 0
    words: list[tuple[int, str]] = []
    for kind, text in tokenize(sql):
        if kind == "punct":
            if text == "(":
                depth += 1
            elif text == ")":
                depth -= 1
            continue
        if kind == "word":
            words.append((depth, text.lower()))
    return any(d == 0 and w in ("limit", "rownum", "fetch") for d, w in words)


def apply_limit(sql: str, max_rows: int,
                capabilities: SourceCapabilities) -> LimitResult:
    clean = _TRAILING_SEMICOLON.sub("", sql.rstrip())
    if max_rows <= 0:
        return LimitResult(clean, False, "")

    if has_limit(clean):
        return LimitResult(clean, False, "已有限额，未改动（避免破坏子查询语义）")

    if capabilities.pagination == "rownum":
        wrapped = (f"SELECT * FROM (\n{clean}\n) WHERE ROWNUM <= {max_rows}")
        return LimitResult(wrapped, True, f"已包裹 ROWNUM <= {max_rows}")

    if capabilities.pagination == "none":
        return LimitResult(clean, True, "该数据源不支持分页，未强制限额")

    return LimitResult(f"{clean}\nLIMIT {max_rows}", True, f"已追加 LIMIT {max_rows}")
