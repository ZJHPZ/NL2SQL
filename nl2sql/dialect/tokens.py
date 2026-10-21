"""SQL 词法工具：分词、语句切分、只读判定、标识符提取。

为什么不用正则
--------------
判断"是否只读语句"用正则会在这些地方翻车：

    SELECT 'DROP TABLE x'        ← 字符串字面量里有 DROP
    /* DROP TABLE x */ SELECT 1  ← 注释里有 DROP
    SELECT 1; DROP TABLE x       ← 多语句注入
    WITH c AS (...) DELETE ...   ← CTE 开头但整体是写操作

所以这里做一个**最小的 SQL 词法扫描器**：只处理字符串、引号标识符、
行注释、块注释（含 MySQL 嵌套）、分号。它不理解 SQL 语义，
但足以把"关键字出现在值/注释里"和"真的出现在语句位置"区分开。

这不是 SQL 解析器，也刻意不做成解析器——P0 只需要拦住写操作与多语句。
"""
from __future__ import annotations

import re

__all__ = [
    "tokenize",
    "split_statements",
    "is_read_only",
    "first_keywords",
    "extract_identifiers",
    "strip_comments",
]

Token = tuple[str, str]  # (kind, text)；kind: word | string | quoted | punct | number

_QUOTES = {"'": "'", '"': '"', "`": "`"}
# 语句起始处的只读关键字
_READ_ONLY_STARTS = {"select", "with", "explain", "pragma", "show", "describe", "desc", "values"}
# 任何位置出现都视为写操作的关键字
_WRITE_KEYWORDS = {
    "insert", "update", "delete", "drop", "create", "alter", "truncate",
    "replace", "merge", "grant", "revoke", "attach", "detach", "vacuum",
    "reindex", "analyze", "call", "exec", "execute", "load", "import",
    "copy", "set", "begin", "commit", "rollback", "savepoint", "lock",
    "unlock", "kill", "shutdown", "into",  # SELECT ... INTO 在 MySQL 会写文件
}
# SQLite 只读 PRAGMA 白名单之外，PRAGMA 可能写库；P0 只允许这些
_SAFE_PRAGMA_PREFIXES = (
    "table_info", "table_xinfo", "table_list", "foreign_key_list",
    "index_list", "index_info", "database_list", "collation_list",
    "compile_options", "function_list", "module_list", "pragma_list",
)

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def strip_comments(sql: str) -> str:
    """去掉注释，但**保留字符串字面量原文**。

    用于展示与调试；判定逻辑仍走 `tokenize`。
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            i = n if j < 0 else j
            continue
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth += 1
                    i += 2
                elif sql.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            continue
        if ch in _QUOTES:
            q = _QUOTES[ch]
            j = i + 1
            while j < n:
                if sql[j] == q:
                    if j + 1 < n and sql[j + 1] == q:  # 转义的双引号
                        j += 2
                        continue
                    break
                j += 1
            out.append(sql[i:j + 1])
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def tokenize(sql: str) -> list[Token]:
    """最小词法扫描。字符串/引号标识符/注释都不会被当作关键字。"""
    tokens: list[Token] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]

        # 行注释
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            continue

        # 块注释（支持 MySQL 嵌套）
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth += 1
                    i += 2
                elif sql.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            continue

        # 字符串 / 引号标识符
        if ch in _QUOTES:
            q = _QUOTES[ch]
            j = i + 1
            while j < n:
                if sql[j] == q:
                    if j + 1 < n and sql[j + 1] == q:
                        j += 2
                        continue
                    break
                if sql[j] == "\\" and q != "`":  # MySQL 反斜杠转义
                    j += 2
                    continue
                j += 1
            kind = "string" if q == "'" else "quoted"
            tokens.append((kind, sql[i + 1:j]))
            i = j + 1
            continue

        # 标识符 / 关键字
        m = _WORD.match(sql, i)
        if m:
            tokens.append(("word", m.group(0)))
            i = m.end()
            continue

        # 数字
        m = _NUMBER.match(sql, i)
        if m:
            tokens.append(("number", m.group(0)))
            i = m.end()
            continue

        if ch.isspace():
            i += 1
            continue

        tokens.append(("punct", ch))
        i += 1
    return tokens


def split_statements(sql: str) -> list[str]:
    """按分号切分语句（字符串与注释内的分号不算）。"""
    stmts: list[str] = []
    buf: list[str] = []
    depth = 0
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            end = n if j < 0 else j + 1
            buf.append(sql[i:end])
            i = end
            continue
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            d, j = 1, i + 2
            while j < n and d:
                if sql.startswith("/*", j):
                    d += 1
                    j += 2
                elif sql.startswith("*/", j):
                    d -= 1
                    j += 2
                else:
                    j += 1
            buf.append(sql[i:j])
            i = j
            continue
        if ch in _QUOTES:
            q = _QUOTES[ch]
            j = i + 1
            while j < n:
                if sql[j] == q:
                    if j + 1 < n and sql[j + 1] == q:
                        j += 2
                        continue
                    break
                if sql[j] == "\\" and q != "`":
                    j += 2
                    continue
                j += 1
            buf.append(sql[i:j + 1])
            i = j + 1
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                stmts.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts


def first_keywords(sql: str, k: int = 3) -> list[str]:
    """取语句位置上的前 k 个关键字（跳过字符串/引号/注释）。"""
    out: list[str] = []
    for kind, text in tokenize(sql):
        if kind == "word":
            out.append(text.lower())
            if len(out) >= k:
                break
    return out


def is_read_only(sql: str) -> bool:
    """判断是否为只读语句。

    规则：
        1. 首个关键字必须在只读集合内（字符串/注释里的不算）
        2. 语句中任何位置出现写关键字即拒绝（能抓到 `WITH ... DELETE`）
        3. PRAGMA 只允许白名单（部分 PRAGMA 会写库）

    注意：`SELECT ... INTO` 在 MySQL 会写文件，因此 `into` 也在写关键字里。
    """
    kws = first_keywords(sql, k=1)
    if not kws:
        return False
    first = kws[0]
    if first not in _READ_ONLY_STARTS:
        return False

    if first == "pragma":
        return _is_safe_pragma(sql)
    if first == "show" or first in ("describe", "desc"):
        return True

    # 全量扫描：写关键字不能出现在语句位置
    for kind, text in tokenize(sql):
        if kind != "word":
            continue
        low = text.lower()
        if low in _WRITE_KEYWORDS:
            # EXPLAIN 后面跟的也可能是写语句，同样应拒绝
            return False
    return True


def _is_safe_pragma(sql: str) -> bool:
    words = [t.lower() for k, t in tokenize(sql) if k == "word"]
    if len(words) < 2:
        return False
    target = words[1].split("(")[0]
    return any(target.startswith(p) for p in _SAFE_PRAGMA_PREFIXES)


def extract_identifiers(sql: str) -> list[str]:
    """提取可能的标识符（表名/列名/别名），用于存在性检查与候选提示。

    刻意保守：只返回长度 >= 2 的标识符，并排除 SQL 关键字；
    结果可能包含别名，因此调用方只应把它当作**候选提示**而非硬判定。
    """
    keywords = _SQL_KEYWORDS
    out: list[str] = []
    for kind, text in tokenize(sql):
        if kind in ("string", "number", "punct"):
            continue
        if kind == "quoted":
            out.append(text)
            continue
        low = text.lower()
        if low in keywords or len(text) < 2:
            continue
        out.append(text)
    # 去重保序
    seen: dict[str, None] = {}
    for i in out:
        seen.setdefault(i, None)
    return list(seen)


_SQL_KEYWORDS = {
    "select", "from", "where", "group", "by", "order", "having", "limit",
    "offset", "join", "left", "right", "inner", "outer", "full", "cross",
    "on", "as", "and", "or", "not", "in", "is", "null", "between", "like",
    "case", "when", "then", "else", "end", "distinct", "all", "union",
    "with", "over", "partition", "rows", "range", "unbounded", "preceding",
    "following", "current", "row", "asc", "desc", "cast", "count", "sum",
    "avg", "min", "max", "round", "coalesce", "ifnull", "nullif", "abs",
    "strftime", "date_format", "extract", "year", "month", "day", "week",
    "quarter", "explain", "pragma", "show", "describe", "values", "exists",
    "any", "some", "using", "natural", "recursive", "filter", "within",
    "rank", "dense_rank", "row_number", "lag", "lead", "first_value",
    "last_value", "ntile", "percent_rank", "cume_dist",
}
