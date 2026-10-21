"""执行守门：SQL 落地前的最后一道确定性校验。

原则：**授权与安全由网关确定性校验，绝不交给模型或 Prompt 判断。**

个人场景精简为四件事：
    1. 只读（拒绝 DDL/DML）
    2. 禁多语句
    3. 强制 LIMIT（补或收紧）
    4. 执行超时（由适配器落实，这里只传参）

实现方式：用 `dialect/tokens.py` 的词法扫描器判定语句类型，**不用正则**。
正则判只读会在注释、字符串字面量、CTE 上翻车（详见 tokens.py 说明）。
"""
from __future__ import annotations

from ..contracts.source import SourceCapabilities
from ..dialect.tokens import is_read_only, split_statements
from .limit import apply_limit, has_limit

__all__ = ["Guard", "GuardDecision"]


class GuardDecision:
    """守门结果。被拒时带可展示的原因与建议，而不是抛异常。"""

    __slots__ = ("allowed", "sql", "rule", "message", "suggestion")

    def __init__(self, allowed: bool, sql: str = "", rule: str = "",
                 message: str = "", suggestion: str = "") -> None:
        self.allowed = allowed
        self.sql = sql
        self.rule = rule
        self.message = message
        self.suggestion = suggestion

    def __repr__(self) -> str:
        state = "ALLOW" if self.allowed else "DENY"
        return f"<GuardDecision {state} rule={self.rule or '-'}>"


class Guard:
    """执行前校验 + 限额补齐。"""

    def __init__(self, capabilities: SourceCapabilities, *, max_rows: int,
                 timeout_s: float, forbid_multiple: bool = True,
                 forbid_writes: bool = True) -> None:
        self._cap = capabilities
        self._max_rows = max_rows
        self._timeout_s = timeout_s
        self._forbid_multiple = forbid_multiple
        self._forbid_writes = forbid_writes

    @property
    def timeout_s(self) -> float:
        return self._timeout_s

    def check(self, sql: str, *, limit: int | None = None) -> GuardDecision:
        """校验并限额。

        Args:
            limit: SQL 级 LIMIT 的取值。默认 `max_rows`；调用方若需要
                判断"结果是否被截断"，应传入 `max_rows + 1`——
                多出的那一行是**截断检测**用的，适配器读满它会标记 truncated。
        """
        if not sql or not sql.strip():
            return GuardDecision(
                False, rule="empty", message="SQL 为空",
                suggestion="重新生成查询",
            )

        # 1) 多语句
        if self._forbid_multiple:
            stmts = split_statements(sql)
            if len(stmts) > 1:
                return GuardDecision(
                    False, rule="multiple_statements",
                    message=f"检测到 {len(stmts)} 条语句，只允许单条只读查询",
                    suggestion="请缩小问题范围，一次只查一件事",
                )
            sql = stmts[0] if stmts else sql

        # 2) 只读
        if self._forbid_writes and not is_read_only(sql):
            return GuardDecision(
                False, rule="not_read_only",
                message="只允许只读查询（SELECT / WITH / EXPLAIN / 只读 PRAGMA）",
                suggestion="本工具不执行任何写操作；如需变更数据请使用专门工具",
            )

        # 3) 强制 LIMIT：已有限额则收紧，没有则补齐
        limited = apply_limit(sql, limit if limit is not None else self._max_rows,
                              self._cap)
        return GuardDecision(True, sql=limited.sql, rule="ok",
                             message=limited.note)

    # ------------------------------------------------------------ 辅助
    def would_truncate(self, sql: str) -> bool:
        return has_limit(sql) and self._max_rows > 0
