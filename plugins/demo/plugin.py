"""示例插件：演示怎么写一个自定义工具。

它本身也有实际用处——这两个工具补的是"数据质量探查"，
是内置工具集里没有、但排查数据问题时很常用的能力。

写插件的三条纪律（照着做就不会踩坑）
------------------------------------
1. **只 import `nl2sql.plugins`**
   不得 import 核心其它子模块（如 `nl2sql.sources.sqlite`）。
   层检查会拦住违规——因为那会让核心无法演进、也让沙箱失效。

2. **`run` 不抛异常，失败返回 `ToolResult(ok=False, hint=...)`**
   agent 循环需要*看到*失败并据此决策；抛异常会打断循环、丢失上下文。

3. **失败信息要可行动**
   不只说"错了"，要说"候选是什么""下一步该调什么工具"。
   这是 agent 能自我纠正的前提。

依赖通过 `ctx.get(资源名, 能力名)` 取用，**能力名必须声明**，
否则运行时会拒绝——这不是麻烦，而是权限边界的落地方式。
"""
from __future__ import annotations

import json
from typing import Any

from nl2sql.plugins import ToolContext, ToolResult, ToolSpec


class ProfileColumnTool:
    """探查某列的分布：空值率、去重值数、Top 取值。

    用途：模型常猜错取值（写 'paid' 而库里是 '已支付'）。
    先探查再写 SQL，比反复试错省得多。
    """

    spec = ToolSpec(
        name="demo.profile_column",
        description=(
            "探查某一列的数据分布：总行数、空值数、去重值数、出现最多的取值。"
            "**当你不确定某列的取值长什么样时用它**，"
            "例如怀疑订单状态该写中文还是英文。"
        ),
        params={
            "type": "object",
            "properties": {
                "table": {"type": "string", "description": "表名"},
                "column": {"type": "string", "description": "列名"},
                "top_k": {"type": "integer", "description": "返回前几个高频取值，默认 5"},
            },
            "required": ["table", "column"],
        },
        requires=("source.read",),
        kind="read",
        examples=({"table": "fact_order", "column": "order_status"},),
    )

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        table = str(args.get("table") or "").strip()
        column = str(args.get("column") or "").strip()
        if not table or not column:
            return ToolResult(
                ok=False, error="缺少参数 table 或 column",
                hint="例如 {\"table\": \"fact_order\", \"column\": \"order_status\"}",
            )

        # 标识符不能参数化，只能白名单校验后拼接。
        # **必须校验**——否则这就是一个 SQL 注入入口。
        if not _safe_ident(table) or not _safe_ident(column):
            return ToolResult(
                ok=False,
                error=f"表名或列名含非法字符: {table}.{column}",
                hint="标识符只允许字母、数字、下划线和中文",
            )

        adapter = ctx.get("adapter", "source.read")
        top_k = int(args.get("top_k") or 5)
        base = f'"{table}"'
        col = f'"{column}"'

        stats_sql = (
            f"SELECT COUNT(*) AS total, "
            f"SUM(CASE WHEN {col} IS NULL THEN 1 ELSE 0 END) AS nulls, "
            f"COUNT(DISTINCT {col}) AS distinct_values "
            f"FROM {base}"
        )
        try:
            stats = adapter.execute(stats_sql, limit=10, timeout_s=15)
        except Exception as e:  # noqa: BLE001
            return ToolResult(
                ok=False, error=str(e),
                hint="表名或列名可能不对；可用 schema.describe 确认真实名称",
            )

        top_sql = (
            f"SELECT {col} AS v, COUNT(*) AS n FROM {base} "
            f"WHERE {col} IS NOT NULL GROUP BY {col} "
            f"ORDER BY n DESC LIMIT {top_k}"
        )
        try:
            tops = adapter.execute(top_sql, limit=top_k, timeout_s=15)
        except Exception:  # noqa: BLE001
            tops = None

        total, nulls, distinct = (stats.rows[0] if stats.rows else (0, 0, 0))
        return ToolResult(ok=True, data={
            "table": table,
            "column": column,
            "total_rows": total,
            "null_rows": nulls,
            "distinct_values": distinct,
            "top_values": (
                [{"value": r[0], "count": r[1]} for r in tops.rows]
                if tops else []
            ),
            "_note": "distinct_values 较小时，top_values 基本就是该列的完整取值域",
        })


class CheckNullRateTool:
    """批量检查若干列的空值率，快速找出"看起来很关键但实际大量为空"的字段。

    这类字段是 SQL 写错的高发区：模型会基于列名语义写过滤条件，
    而实际上该列大部分是 NULL，结果自然是空的。
    """

    spec = ToolSpec(
        name="demo.check_null_rate",
        description=(
            "检查一张表多个列的空值率，找出实际数据稀疏的字段。"
            "当查询返回空结果、怀疑某字段实际没数据时用它。"
        ),
        params={
            "type": "object",
            "properties": {
                "table": {"type": "string", "description": "表名"},
                "columns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要检查的列名；留空则检查该表全部列",
                },
            },
            "required": ["table"],
        },
        requires=("source.read",),
        kind="read",
    )

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        table = str(args.get("table") or "").strip()
        if not _safe_ident(table):
            return ToolResult(ok=False, error=f"非法表名: {table}",
                              hint="标识符只允许字母、数字、下划线和中文")

        columns = args.get("columns") or []
        if not isinstance(columns, list):
            columns = []
        columns = [str(c) for c in columns]

        if not columns:
            catalog = ctx.get("catalog", "source.read")
            for t in catalog.tables(ctx.source_id):
                if t.name == table:
                    columns = [c.name for c in t.columns]
                    break
            if not columns:
                return ToolResult(
                    ok=False, error=f"表不存在或无法取得列清单: {table}",
                    hint="调用 schema.describe 查看全部表",
                )

        bad = [c for c in columns if not _safe_ident(c)]
        if bad:
            return ToolResult(ok=False, error=f"非法列名: {bad}",
                              hint="标识符只允许字母、数字、下划线和中文")

        adapter = ctx.get("adapter", "source.read")
        parts = ["COUNT(*) AS total_rows"] + [
            f'SUM(CASE WHEN "{c}" IS NULL THEN 1 ELSE 0 END) AS "n_{c}"'
            for c in columns
        ]
        sql = f'SELECT {", ".join(parts)} FROM "{table}"'
        try:
            r = adapter.execute(sql, limit=10, timeout_s=20)
        except Exception as e:  # noqa: BLE001
            return ToolResult(ok=False, error=str(e),
                              hint="可用 schema.describe 确认表与列名")

        if not r.rows:
            return ToolResult(ok=True, data={"table": table, "columns": []})

        row = r.rows[0]
        names = list(r.columns)
        total = int(row[0]) if row else 0
        out = []
        for i, cname in enumerate(columns):
            nulls = int(row[i + 1]) if i + 1 < len(row) else 0
            out.append({
                "column": cname,
                "null_rows": nulls,
                "null_rate": round(nulls / total, 4) if total else None,
            })
        out.sort(key=lambda x: -(x["null_rate"] or 0))

        sparse = [c["column"] for c in out if (c["null_rate"] or 0) > 0.5]
        return ToolResult(
            ok=True,
            data={"table": table, "total_rows": total, "columns": out,
                  "sparse_columns": sparse},
            hint=(f"注意这些列超过一半为空: {'、'.join(sparse)}"
                  if sparse else ""),
        )


def _safe_ident(name: str) -> bool:
    """标识符白名单校验。

    标识符无法用 SQL 参数占位符（`?` 只能用于值），因此必须自己校验。
    **不做这一步就是一个 SQL 注入入口**——插件和核心遵守同一条底线。
    """
    if not name or len(name) > 64:
        return False
    return all(ch.isalnum() or ch == "_" or "\u4e00" <= ch <= "\u9fff"
               for ch in name)


def register() -> list[Any]:
    """插件入口：返回本插件提供的工具。

    核心会检查每个工具声明的能力是否都在 plugin.toml 的 requires 里，
    未声明的一律拒绝注册——能力必须显式声明才能被授予。
    """
    return [ProfileColumnTool(), CheckNullRateTool()]
