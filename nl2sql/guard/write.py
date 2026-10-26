"""受控写守门（P1，SQLite 测试库）：只放行「单条、单表、带条件」的写语句。

与只读 `Guard` 完全分离：问答链路与只读执行继续走 `Guard`，写路径只走这里。
为什么不用现有 `Guard`：只读守门按「拒绝一切写」设计，两者目标相反；
共用 `dialect/tokens` 的词法扫描就够了。

规则（宁可少放，不可错放）
---------------------------
1. 单条语句；首关键字 ∈ INSERT / UPDATE / DELETE；
2. 任何位置出现 DDL / 事务 / ATTACH / PRAGMA / REPLACE / MERGE 等关键字即拒；
3. 只允许一张表，表名不能是 sqlite_ 内部表、也不能在项目禁用表里；
4. UPDATE / DELETE 必须带 WHERE，且拒绝 `1=1` / `true` / 恒真 OR 这类写法；
5. INSERT 只允许 `INSERT INTO t (列…) VALUES (…)`，拒绝 `INSERT ... SELECT`；
6. 影响行数上限由执行层（事务里真的执行一次再回滚）核对，这里只做语句校验。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..dialect.tokens import first_keywords, split_statements, tokenize

__all__ = ["WriteCheck", "WriteGuard", "MAX_WRITE_SQL_CHARS"]

MAX_WRITE_SQL_CHARS = 20000

_WRITE_STARTS = {"insert", "update", "delete"}

# 任何位置出现都拒绝：DDL、事务控制、文件/库挂载、外部调用。
_FORBIDDEN = {
    "drop", "create", "alter", "truncate", "attach", "detach", "vacuum",
    "reindex", "pragma", "begin", "commit", "rollback", "savepoint",
    "release", "replace", "merge", "grant", "revoke", "call", "exec",
    "execute", "load", "import", "copy", "lock", "unlock", "kill",
    "shutdown", "returning", "union", "intersect", "except",
}

_TABLE_PART = r'(?:"[^"]+"|`[^`]+`|[A-Za-z_][A-Za-z0-9_$]*)'
_TABLE_RE = rf"(?:{_TABLE_PART}\s*\.\s*)?({_TABLE_PART})"

_INSERT_RE = re.compile(
    rf"^\s*insert\s+into\s+{_TABLE_RE}\s*\(", re.IGNORECASE | re.DOTALL
)
_UPDATE_RE = re.compile(
    rf"^\s*update\s+{_TABLE_RE}\s+set\b", re.IGNORECASE | re.DOTALL
)
_DELETE_RE = re.compile(
    rf"^\s*delete\s+from\s+{_TABLE_RE}\b", re.IGNORECASE | re.DOTALL
)
_WHERE_RE = re.compile(r"\bwhere\b", re.IGNORECASE)
# 恒真 / 没有列条件的 WHERE 一律拒绝
_TAUTOLOGY_RE = re.compile(
    r"^\s*(?:1\s*=\s*1|1|true|'1'\s*=\s*'1'|\"1\"\s*=\s*\"1\")\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class WriteCheck:
    """一条写语句的校验结果（被拒时给可展示原因与规则名）。"""

    allowed: bool
    sql: str = ""
    kind: str = ""
    table: str = ""
    where_sql: str = ""
    rule: str = ""
    reason: str = ""


def _deny(rule: str, reason: str, *, sql: str = "") -> WriteCheck:
    return WriteCheck(allowed=False, sql=sql, rule=rule, reason=reason)


def _unquote(identifier: str) -> str:
    text = identifier.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "`"):
        inner = text[1:-1]
        if text[0] == '"':
            inner = inner.replace('""', '"')
        else:
            inner = inner.replace("``", "`")
        return inner
    return text


def mask_literals(sql: str) -> str:
    """把字符串字面量与注释替换成等长空格，保留引号标识符与标点位置。

    目的：在拼接/注释里出现的 `where`、`1=1` 不会骗过规则；长度不变，
    所以可以根据掩码里的下标回原 SQL 取原文。
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            end = n if j < 0 else j
            out.append(" " * (end - i))
            i = end
            continue
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            depth, j = 1, i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth += 1
                    j += 2
                elif sql.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            out.append(" " * (j - i))
            i = j
            continue
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                if sql[j] == "\\":
                    j += 2
                    continue
                j += 1
            end = min(j + 1, n)
            out.append(" " * (end - i))
            i = end
            continue
        out.append(ch)
        i += 1
    return "".join(out)


class WriteGuard:
    """受控写语句校验器。`blocked_tables` 给项目级禁用表留口子。"""

    def __init__(self, *, max_affected: int = 100,
                 blocked_tables: list[str] | tuple[str, ...] = ()) -> None:
        self.max_affected = max(1, int(max_affected))
        self.blocked_tables = {str(t).lower() for t in blocked_tables}

    # ------------------------------------------------------------------
    def check(self, sql: str) -> WriteCheck:
        text = (sql or "").strip()
        if not text:
            return _deny("empty", "写语句不能为空")
        if len(text) > MAX_WRITE_SQL_CHARS:
            return _deny(
                "too_long",
                f"写语句太长（{len(text)} 字符，上限 {MAX_WRITE_SQL_CHARS}）",
            )
        statements = [s for s in split_statements(text) if s.strip()]
        if not statements:
            return _deny("empty", "写语句不能为空（只包含注释）")
        if len(statements) > 1:
            return _deny(
                "multi_statement",
                f"写操作只允许一条语句（检测到 {len(statements)} 条）",
            )
        statement = statements[0]

        kws = first_keywords(statement, k=1)
        first = kws[0] if kws else ""
        if first not in _WRITE_STARTS:
            return _deny(
                "not_write",
                "写任务只允许 INSERT / UPDATE / DELETE 单条语句",
            )
        for kind, token_text in tokenize(statement):
            if kind == "word" and token_text.lower() in _FORBIDDEN:
                return _deny(
                    "forbidden_keyword",
                    f"写语句不能包含 {token_text.upper()}（禁止 DDL / 事务 / "
                    "多语句拼接等写法）",
                )

        masked = mask_literals(statement)
        if first == "insert":
            check = self._check_insert(statement, masked)
        elif first == "update":
            check = self._check_update(statement, masked)
        else:
            check = self._check_delete(statement, masked)
        return check

    # ------------------------------------------------------------------
    def _table_name(self, match: re.Match[str]) -> str:
        return _unquote(match.group(1))

    def _table_allowed(self, name: str) -> WriteCheck | None:
        low = name.lower()
        if not low:
            return _deny("bad_table", "写语句里的表名不能为空")
        if low.startswith("sqlite_"):
            return _deny("internal_table", f"不能写 SQLite 内部表：{name}")
        if low in self.blocked_tables:
            return _deny("blocked_table", f"项目已禁用对表 {name} 的写入")
        return None

    def _check_insert(self, statement: str, masked: str) -> WriteCheck:
        match = _INSERT_RE.match(masked)
        if not match:
            return _deny(
                "insert_shape",
                "INSERT 只允许 INSERT INTO 表 (列…) VALUES (…)，"
                "不支持 INSERT ... SELECT / DEFAULT VALUES",
            )
        table = self._table_name(match)
        if (bad := self._table_allowed(table)) is not None:
            return bad
        low_words = [t.lower() for k, t in tokenize(statement) if k == "word"]
        if "select" in low_words:
            return _deny("insert_select", "不支持 INSERT ... SELECT（跨语句写）")
        if "values" not in low_words:
            return _deny("insert_shape", "INSERT 必须使用 VALUES 子句")
        if low_words.count("values") > 0 and "(" not in masked[match.end():]:
            return _deny("insert_shape", "INSERT 的列清单或 VALUES 不完整")
        return WriteCheck(allowed=True, sql=statement, kind="insert",
                          table=table)

    def _check_update(self, statement: str, masked: str) -> WriteCheck:
        match = _UPDATE_RE.match(masked)
        if not match:
            return _deny("update_shape", "UPDATE 只允许 UPDATE 表 SET 列=… 形式")
        table = self._table_name(match)
        if (bad := self._table_allowed(table)) is not None:
            return bad
        where = self._extract_where(statement, masked)
        if where is None:
            return _deny("missing_where", "UPDATE 必须带 WHERE 条件（禁止全表更新）")
        return WriteCheck(allowed=True, sql=statement, kind="update",
                          table=table, where_sql=where)

    def _check_delete(self, statement: str, masked: str) -> WriteCheck:
        match = _DELETE_RE.match(masked)
        if not match:
            return _deny("delete_shape", "DELETE 只允许 DELETE FROM 表 WHERE … 形式")
        table = self._table_name(match)
        if (bad := self._table_allowed(table)) is not None:
            return bad
        where = self._extract_where(statement, masked)
        if where is None:
            return _deny("missing_where", "DELETE 必须带 WHERE 条件（禁止全表删除）")
        return WriteCheck(allowed=True, sql=statement, kind="delete",
                          table=table, where_sql=where)

    def _extract_where(self, statement: str, masked: str) -> str | None:
        match = _WHERE_RE.search(masked)
        if match is None:
            return None
        original = statement[match.start():].strip()
        masked_tail = masked[match.end():].strip()
        if not masked_tail:
            return None
        if _TAUTOLOGY_RE.match(masked_tail):
            return None
        # WHERE 1=1 OR … / WHERE true OR … 这类恒真写法也拒绝
        if re.search(r"\b1\s*=\s*1\b", masked_tail, re.IGNORECASE):
            return None
        if re.match(r"^\s*true\b", masked_tail, re.IGNORECASE):
            return None
        return original
