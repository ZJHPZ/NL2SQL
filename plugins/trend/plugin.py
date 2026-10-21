"""环比趋势分析插件。

写这个插件的目的是**当一次真正的插件作者**，检验"可插拔"是否成立。
它是真实有用的能力：内置工具里没有"对比连续两期并给出变化率"的形态，
而这正是日常分析最常问的一类问题（"这个月比上个月怎么样"）。

补一句设计上的自觉：我**没有** import 任何 `nl2sql` 内部模块，
只用 `nl2sql.plugins` 暴露的契约。如果写着写着发现"必须去看核心源码
才能写对"，那说明插件接口不合格——这是本次要验证的事。
"""
from __future__ import annotations

from typing import Any

from nl2sql.plugins import ToolContext, ToolResult, ToolSpec


def _safe_ident(name: str) -> bool:
    """标识符白名单。

    标识符**无法用 SQL 参数占位符**（`?` 只能用于值），所以必须自己校验。
    不校验就是注入入口——插件和核心遵守同一条底线。
    """
    if not name or len(name) > 64:
        return False
    return all(ch.isalnum() or ch == "_" or "\u4e00" <= ch <= "\u9fff"
               for ch in name)


class ComparePeriodsTool:
    """对比连续两个时间段的某个指标，给出变化量与变化率。

    设计取舍：**只接受"已支付"口径的金额与件数**，不接受任意指标名。
    因为插件拿不到语义层的指标定义（只有 `source.read` 权限），
    硬编码别的口径会产生错误数字——宁可把能力范围说清楚，
    也不假装能算任意指标。
    """

    spec = ToolSpec(
        name="trend.compare_periods",
        description=(
            "对比连续日历月的指标变化，输出两期数值、变化量与变化率。"
            "**当用户问\"这个月比上个月怎么样\"、\"环比如何\"、\"有没有增长\"时用它。**"
            "目前支持 metric 取 '实收金额' 或 '销量'（口径：已支付订单，金额减优惠）。"
        ),
        params={
            "type": "object",
            "properties": {
                "current_month": {
                    "type": "string",
                    "description": "本期月份，格式 YYYY-MM，例如 2024-06",
                },
                "metric": {
                    "type": "string",
                    "enum": ["实收金额", "销量"],
                    "description": "要对比的指标，默认实收金额",
                },
                "group_by": {
                    "type": "string",
                    "description": "（可选）按某列分组对比，例如 channel_id、category",
                },
            },
            "required": ["current_month"],
        },
        requires=("source.read",),
        kind="read",
        examples=({"current_month": "2024-06"},),
    )

    # 允许的指标 → **行级**表达式（不含聚合函数）。
    #
    # 为什么是行级：SQL 里不能写 `CASE WHEN ... THEN SUM(x) END`
    # ——聚合函数不能出现在 CASE 内部（`misuse of aggregate`）。
    # 正确形态是 `SUM(CASE WHEN ... THEN x ELSE 0 END)`，
    # 所以这里只给"x"，SUM 由外层包。
    # 这也是插件作者最容易踩的坑，我连踩两次，记在文档里。
    _METRICS: dict[str, str] = {
        "实收金额": "(amount - discount_amount)",
        "销量": "quantity",
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        current = str(args.get("current_month") or "").strip()
        if not current:
            return ToolResult(
                ok=False, error="缺少参数 current_month",
                hint="传入本期月份，格式 YYYY-MM，例如 2024-06",
            )
        prev = _prev_month(current)
        if prev is None:
            return ToolResult(
                ok=False, error=f"月份格式不对: {current}",
                hint="需要 YYYY-MM 格式，例如 2024-06",
            )

        metric = str(args.get("metric") or "实收金额").strip()
        expr = self._METRICS.get(metric)
        if expr is None:
            return ToolResult(
                ok=False, error=f"不支持的指标: {metric}",
                hint=f"目前只支持: {'、'.join(self._METRICS)}",
                candidates=list(self._METRICS),
            )

        group_by = str(args.get("group_by") or "").strip()
        if group_by and not _safe_ident(group_by):
            return ToolResult(
                ok=False, error=f"group_by 含非法字符: {group_by}",
                hint="列名只允许字母、数字、下划线与中文",
            )

        adapter = ctx.get("adapter", "source.read")
        # 聚合表达式：SUM 包在 CASE 外面（另见 _METRICS 的说明）
        agg = (f"SUM(CASE WHEN substr(order_date,1,7) = '{{m}}'"
               f" AND order_status = '已支付' THEN {expr} ELSE 0 END)")
        cur_expr = agg.format(m=current)
        prev_expr = agg.format(m=prev)

        if group_by:
            sql = (
                f"SELECT {group_by} AS 分组,"
                f"ROUND({prev_expr}, 2) AS 上期,"
                f"ROUND({cur_expr}, 2) AS 本期"
                f" FROM fact_order"
                f" WHERE substr(order_date,1,7) IN ('{prev}', '{current}')"
                f" GROUP BY {group_by} ORDER BY {group_by}"
            )
        else:
            sql = (
                f"SELECT ROUND({prev_expr}, 2) AS 上期,"
                f"ROUND({cur_expr}, 2) AS 本期"
                f" FROM fact_order"
                f" WHERE substr(order_date,1,7) IN ('{prev}', '{current}')"
            )

        # 注意：SQL 走的是**数据源适配器**，而适配器打开的是只读连接。
        # 因此这里即便拼接了字符串，也拿不到写权限——两道防线都在。
        try:
            r = adapter.execute(sql, limit=200, timeout_s=30)
        except Exception as e:  # noqa: BLE001
            return ToolResult(
                ok=False, error=str(e),
                hint="表 fact_order 或字段名可能不符；本插件按固定表结构实现",
            )

        rows: list[dict[str, Any]] = []
        for row in r.rows:
            if group_by:
                key, before, after = row[0], float(row[1] or 0), float(row[2] or 0)
            else:
                key, before, after = "全部", float(row[0] or 0), float(row[1] or 0)
            delta = round(after - before, 2)
            rate = (round(delta / before, 4) if before else None)
            rows.append({
                "分组": key, "上期": before, "本期": after,
                "变化量": delta, "变化率": rate,
            })

        if not rows:
            return ToolResult(
                ok=True, data={"periods": [prev, current], "rows": [],
                               "note": "两个区间都没有数据"},
                hint=f"数据里可能没有 {prev} 或 {current} 的订单",
            )

        total_before = sum(r["上期"] for r in rows)
        total_after = sum(r["本期"] for r in rows)
        return ToolResult(ok=True, data={
            "metric": metric,
            "periods": {"上期": prev, "本期": current},
            "rows": rows,
            "总体": {
                "上期": round(total_before, 2),
                "本期": round(total_after, 2),
                "变化量": round(total_after - total_before, 2),
                "变化率": (round((total_after - total_before) / total_before, 4)
                           if total_before else None),
            },
            "口径": f"{metric}；已支付订单，金额取 amount - discount_amount",
        })


def _prev_month(ym: str) -> str | None:
    """上一个自然月。格式不对返回 None（由调用方给出可行动的提示）。"""
    parts = ym.split("-")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    y, m = int(parts[0]), int(parts[1])
    if not (1 <= m <= 12):
        return None
    m -= 1
    if m == 0:
        y, m = y - 1, 12
    return f"{y:04d}-{m:02d}"


def register() -> list[Any]:
    """插件入口。核心会校验每个工具声明的能力是否都在 plugin.toml 的 requires 里。"""
    return [ComparePeriodsTool()]
