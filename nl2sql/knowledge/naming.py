"""知识条目的稳定命名。

单独成模块的原因：`seed.py` 和 `__init__.py` 都要用它，
放在 `__init__.py` 里会造成循环导入。
"""
from __future__ import annotations

__all__ = ["stable_key"]

# key 中允许出现的字符；其余替换为下划线，保证 key 稳定且可读
_ALLOWED = set("_-.:@abcdefghijklmnopqrstuvwxyz"
               "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")


def stable_key(entry_type: str, identifier: str) -> str:
    """生成稳定的知识条目 key，便于幂等入库。

    形如 `schema:fact_order` / `sql_pair:q_A1`。

    稳定性很重要：自动生成（库表注释 → 条目）靠它做幂等判断，
    key 一变就会重复插入、并覆盖用户已有的编辑。
    """
    safe = "".join(ch if ch in _ALLOWED else "_" for ch in identifier.strip())
    return f"{entry_type}:{safe}"
