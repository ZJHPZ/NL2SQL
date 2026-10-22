"""内置工具集：agent 的能力半径，也是用户写插件时的参考模板。

为什么工具要这样设计
------------------
每个工具都遵守三条纪律，用户照着写就不会踩坑：
    1. `run` **不抛异常** —— 失败返回 `ToolResult(ok=False, hint=...)`。
       因为 agent 循环要看到失败并据此决策，异常会打断循环。
    2. 失败信息必须**可行动**：不只说"字段不存在"，还要说"候选是 XXX"。
       这是 agent 能自我纠正的前提。
    3. 依赖全部来自 `ToolContext`，不 import 核心私有模块。
       所以这批工具可以整体搬到 `plugins/builtin/` 而无需改动。

工具清单（覆盖"能自己解决问题"的最小闭环）：
    semantic.list_metrics   探索：有哪些指标可回答这个问题
    semantic.describe       探索：某指标的准确口径
    schema.describe         探索：表结构、字段、取值域（纠错时最需要）
    knowledge.search        探索：知识库里有没有相关口径/示例
    sql.solve               主力：问题 → SQL + 结果（可整体调用）
    sql.plan / sql.compile / sql.execute   分步控制（agent 可只要 SQL 不执行）
    clarify.ask             追问：信息不足时向用户提问
"""
from __future__ import annotations

from typing import Any

from ..contracts.tool import ToolContext, ToolResult, ToolSpec
__all__ = ["build_builtin_tools", "BuiltinDeps", "builtin_tool_specs"]


class BuiltinDeps:
    """内置工具的依赖包。

    用显式依赖对象而不是让工具去摸全局：这样工具可以在测试里
    被单独构造，也保证了"插件不得访问核心内部"这条规则的一致性。
    """

    __slots__ = ("semantic_model", "compiler", "guard", "adapter", "cache",
                 "catalog", "knowledge", "planner", "session_state",
                 "skill_catalog", "task_store", "project_info")

    def __init__(self, *, semantic_model: Any = None, compiler: Any = None,
                 guard: Any = None, adapter: Any = None, cache: Any = None,
                 catalog: Any = None, knowledge: Any = None,
                 planner: Any = None, skill_catalog: Any = None,
                 task_store: Any = None, project_info: Any = None) -> None:
        self.semantic_model = semantic_model
        self.compiler = compiler
        self.guard = guard
        self.adapter = adapter
        self.cache = cache
        self.catalog = catalog
        self.knowledge = knowledge
        self.planner = planner
        self.skill_catalog = skill_catalog
        # M7：任务库（None = 不启用任务工具）与项目信息（callable 或 dict）。
        self.task_store = task_store
        self.project_info = project_info


# ===================================================================== 工具
class ListMetricsTool:
    spec = ToolSpec(
        name="semantic.list_metrics",
        description=(
            "列出语义层中所有可用指标与维度。"
            "**不确定有哪些指标能回答用户问题时，先调用它**，"
            "而不是凭空猜测指标名。"
        ),
        params={"type": "object", "properties": {}, "required": []},
        requires=("semantic.read",),
        kind="none",
        examples=({},),
    )

    def __init__(self, deps: BuiltinDeps) -> None:
        self._deps = deps

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        model = ctx.get("semantic_model", "semantic.read")
        if model is None:
            return ToolResult(ok=False, error="语义模型未装配",
                              hint="检查数据源的语义配置")
        metrics = [
            {
                "name": n,
                "label": model.metrics[n].label or n,
                "description": model.metrics[n].description,
                "window": model.metrics[n].window,
                "synonyms": list(model.metrics[n].synonyms),
            }
            for n in model.metric_names()
        ]
        dims = [
            {
                "name": n,
                "description": model.dimensions[n].description,
                "synonyms": list(model.dimensions[n].synonyms),
            }
            for n in model.dimension_names()
            if n not in _INTERNAL_DIMS
        ]
        return ToolResult(ok=True, data={"metrics": metrics, "dimensions": dims})


class DescribeMetricTool:
    spec = ToolSpec(
        name="semantic.describe",
        description=(
            "查看某个指标的准确定义与计算口径（表达式）。"
            "当你不确定某指标是否包含退款、是否含税等口径问题时调用它。"
        ),
        params={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "指标名，如 net_revenue"},
            },
            "required": ["name"],
        },
        requires=("semantic.read",),
        kind="none",
    )

    def __init__(self, deps: BuiltinDeps) -> None:
        self._deps = deps

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        model = ctx.get("semantic_model", "semantic.read")
        name = str(args.get("name") or "").strip()
        if not name:
            return ToolResult(ok=False, error="缺少参数 name",
                              hint="传入指标名，例如 net_revenue")
        metric = model.metrics.get(name)
        if metric is None:
            near = _suggest(name, model.metric_names())
            if not near:
                # 相似度太低时给出全部指标名：比"什么都不给"有用得多，
                # agent 至少知道下一步该从哪些名字里挑
                near = model.metric_names()[:12]
            return ToolResult(
                ok=False, error=f"未定义指标: {name}",
                hint="候选是语义层中已定义的指标；也可调用 "
                     "semantic.list_metrics 查看完整清单与说明",
                candidates=near,
            )
        return ToolResult(ok=True, data={
            "name": name,
            "label": metric.label,
            "description": metric.description,
            "expr": metric.expr,
            "window": metric.window,
            "decimals": metric.decimals,
            "synonyms": list(metric.synonyms),
        })


class SkillLoadTool:
    """加载一份技能（playbook）的完整正文。

    为什么是一个**工具**而不是知识库条目
    ------------------------------------
    知识库检索按分数返回 `top_k` 条，会**截断**；技能是流程，
    被切一半就不成立了。所以技能走"按 description 命中一个、整份加载"。

    为什么它有正文长度上限且**超限拒绝**而不是截断：同上——
    截断的流程不成立，宁可拒绝并告诉作者拆文件。

    `description` 在装配期会被替换成"基础说明 + 当前技能目录"
    （见 `build_builtin_tools`）——目录必须让模型看见，
    否则它不知道有哪些技能可加载。
    """

    spec = ToolSpec(
        name="skill.load",
        description=(
            "加载一份**技能**（playbook）的完整步骤说明。"
            "技能是「这类任务该怎么做」的流程，不是事实检索。"
            "参数 name 必须是可用技能之一；可用技能见下方目录。"
        ),
        params={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "技能名"},
            },
            "required": ["name"],
        },
        requires=("knowledge.read",),
        kind="none",
    )

    def __init__(self, deps: BuiltinDeps) -> None:
        self._deps = deps

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        catalog = ctx.get("skill_catalog", "knowledge.read")
        if catalog is None:
            return ToolResult(
                ok=False, error="技能目录未装配",
                hint="检查 config.skills.enabled 与技能目录是否存在",
            )
        name = str(args.get("name") or "").strip()
        available = catalog.names()
        if not name:
            return ToolResult(
                ok=False, error="缺少参数 name",
                hint="传入技能名；可用技能见本工具说明里的目录",
                candidates=available,
            )
        skill = catalog.get(name)
        if skill is None:
            near = _suggest(name, available)
            if not near:
                near = available[:12]
            return ToolResult(
                ok=False, error=f"没有这个技能: {name}",
                hint="用目录里的技能名重试；也可先看目录了解每个技能何时适用",
                candidates=near,
            )
        if not skill.matches_scope(ctx.source_id):
            return ToolResult(
                ok=False,
                error=f"技能 {name} 不适用于数据源 {ctx.source_id or '（未指定）'}"
                      f"（它的 scope 是 {skill.scope}）",
                hint="换一个通用技能，或直接按已授予的能力作答",
            )
        # 正文**整份**回灌，不做截断。
        #
        # 刻意返回**字符串**而不是 dict：`ToolResult.to_observation()` 对
        # 字符串走的是原样输出，对 dict 会过 `_compact()`——那里有 1200 字符
        # 上限。也就是说返回 dict 会让"20KB 的技能"被悄悄砍成 1.2KB。
        # 而截断的流程不成立，这正是本模块最不能犯的错。
        return ToolResult(ok=True, data=(
            f"# 技能：{skill.name}\n"
            f"（{skill.description}）\n\n"
            f"{skill.body}"
        ))


class DescribeSchemaTool:
    spec = ToolSpec(
        name="schema.describe",
        description=(
            "查看数据源的表结构：表名、字段、类型、取值域。"
            "**当 SQL 报错说字段或表不存在时，先调用它确认真实字段名**，"
            "不要反复猜。也可用于了解有哪些取值（如订单状态有哪些）。"
        ),
        params={
            "type": "object",
            "properties": {
                "table": {"type": "string", "description": "表名，留空列出全部表"},
                "with_values": {
                    "type": "boolean",
                    "description": "是否采样低基数列的取值（如状态、地区），默认 false",
                },
            },
            "required": [],
        },
        requires=("source.read",),
        kind="read",
    )

    def __init__(self, deps: BuiltinDeps) -> None:
        self._deps = deps

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        catalog = ctx.get("catalog", "source.read")
        tables = catalog.tables(ctx.source_id)
        if not tables:
            return ToolResult(
                ok=False, error="元数据快照为空",
                hint="可能需要先刷新元数据快照（/api/snapshot）",
            )
        want = str(args.get("table") or "").strip().lower()
        if want:
            hit = next((t for t in tables if t.name.lower() == want), None)
            if hit is None:
                return ToolResult(
                    ok=False, error=f"表不存在: {args.get('table')}",
                    hint="调用 schema.describe（不带 table 参数）查看全部表",
                    candidates=[t.name for t in tables],
                )
            tables = [hit]

        out: list[dict[str, Any]] = []
        with_values = bool(args.get("with_values"))
        for t in tables:
            item: dict[str, Any] = {
                "table": t.name,
                "rows": t.row_estimate,
                "columns": [
                    {
                        "name": c.name,
                        "type": c.data_type,
                        "pk": c.is_primary_key,
                        "generated": c.generated,
                    }
                    for c in t.columns
                ],
            }
            if with_values:
                item["value_samples"] = self._sample_values(ctx, t)
            out.append(item)
        return ToolResult(ok=True, data=out if len(out) > 1 else out[0])

    def _sample_values(self, ctx: ToolContext, table: Any,
                       max_cardinality: int = 30) -> dict[str, list[Any]]:
        """采样低基数列的取值。这是极其实用的能力：
        模型常常猜错取值（写 'paid' 而库里是 '已支付'）。"""
        adapter = ctx.maybe("adapter")
        if adapter is None:
            return {}
        out: dict[str, list[Any]] = {}
        for col in table.columns:
            if col.data_type.upper() not in ("TEXT", "VARCHAR", "CHAR", ""):
                continue
            sql = (f'SELECT DISTINCT "{col.name}" AS v FROM "{table.name}" '
                   f'WHERE "{col.name}" IS NOT NULL LIMIT {max_cardinality + 1}')
            try:
                r = adapter.execute(sql, limit=max_cardinality + 1, timeout_s=10)
            except Exception:  # noqa: BLE001 - 采样失败不影响主流程
                continue
            values = [row[0] for row in r.rows]
            if 0 < len(values) <= max_cardinality:
                out[col.name] = values
        return out


class KnowledgeSearchTool:
    spec = ToolSpec(
        name="knowledge.search",
        description=(
            "检索知识库，查找相关的口径定义、业务规则或历史相似查询。"
            "当你不确定某个业务术语的含义时调用它。"
        ),
        params={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索词，通常就是用户的问题"},
                "top_k": {"type": "integer", "description": "返回条数，默认 5"},
            },
            "required": ["query"],
        },
        requires=("knowledge.read",),
        kind="none",
    )

    def __init__(self, deps: BuiltinDeps) -> None:
        self._deps = deps

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        kb = ctx.get("knowledge", "knowledge.read")
        if kb is None:
            return ToolResult(ok=False, error="知识库未启用",
                              hint="检查 config.knowledge.enabled")
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult(ok=False, error="缺少参数 query",
                              hint="传入检索词")
        top_k = int(args.get("top_k") or 5)
        hits = kb.test_retrieve(query, source_id=ctx.source_id, top_k=top_k)
        return ToolResult(ok=True, data=[
            {"type": h.entry.type, "title": h.entry.title,
             "body": h.entry.body[:400], "why": h.why}
            for h in hits
        ])


class ClarifyAskTool:
    """向用户追问。**回合制**：立即结束本轮，等下一轮带上下文继续。"""

    spec = ToolSpec(
        name="clarify.ask",
        description=(
            "向用户提问以获取继续所需的关键信息。"
            "**只在确实无法通过其它工具自行推断时才用**——"
            "能查表、能查知识库解决的，不要打断用户。"
            "典型场景：时间范围不明确、业务术语有歧义、需要用户确认口径。"
        ),
        params={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "要问用户的问题"},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选项，便于用户快速回答",
                },
                "reason": {"type": "string", "description": "为什么必须问"},
            },
            "required": ["question"],
        },
        requires=("clarify.ask",),
        kind="none",
        feed_back=False,
    )

    def __init__(self, deps: BuiltinDeps) -> None:
        self._deps = deps

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        question = str(args.get("question") or "").strip()
        if not question:
            return ToolResult(ok=False, error="缺少参数 question",
                              hint="给出要问用户的完整问题")
        options = args.get("options") or []
        return ToolResult(
            ok=True, needs_clarification=True, question=question,
            options=[str(o) for o in options] if isinstance(options, list) else [],
        )


class SqlPlanTool:
    spec = ToolSpec(
        name="sql.plan",
        description=(
            "把自然语言问题转换成逻辑查询计划（指标 + 维度 + 过滤 + 时间范围），"
            "但**不生成 SQL、不执行**。用于先确认理解是否正确。"
        ),
        params={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "用户的业务问题"},
            },
            "required": ["question"],
        },
        requires=("semantic.read",),
        kind="none",
    )

    def __init__(self, deps: BuiltinDeps) -> None:
        self._deps = deps

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from ..contracts.llm import PlannerContext

        planner = ctx.get("planner", "semantic.read")
        question = str(args.get("question") or ctx.question).strip()
        if not question:
            return ToolResult(ok=False, error="缺少参数 question")
        try:
            plan = planner.resolve(
                question, PlannerContext(source_id=ctx.source_id, now=ctx.now))
        except Exception as e:  # noqa: BLE001
            return ToolResult(
                ok=False, error=f"无法生成逻辑计划: {e}",
                hint=("可先调用 semantic.list_metrics 查看可用指标，"
                      "或 knowledge.search 查有没有类似问题的示例"),
            )
        return ToolResult(ok=True, data={
            "measures": [m.metric for m in plan.measures],
            "dimensions": [d.dimension for d in plan.dimensions],
            "filters": [f"{f.field} {f.op} {f.value}" for f in plan.filters],
            "time_range": (f"{plan.time_range.start}~{plan.time_range.end}"
                           if plan.time_range else None),
            "fingerprint": plan.fingerprint(),
        })


class SqlCompileTool:
    spec = ToolSpec(
        name="sql.compile",
        description="把逻辑计划编译成方言 SQL，但不执行。需要先用 sql.plan 拿到计划。",
        params={
            "type": "object",
            "properties": {
                "question": {"type": "string",
                             "description": "用户问题（内部会重新规划以拿到计划）"},
            },
            "required": ["question"],
        },
        requires=("semantic.read",),
        kind="none",
    )

    def __init__(self, deps: BuiltinDeps) -> None:
        self._deps = deps

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from ..contracts.llm import PlannerContext

        planner = ctx.get("planner", "semantic.read")
        compiler = ctx.get("compiler", "semantic.read")
        model = ctx.get("semantic_model", "semantic.read")
        question = str(args.get("question") or ctx.question).strip()
        try:
            plan = planner.resolve(
                question, PlannerContext(source_id=ctx.source_id, now=ctx.now))
            compiled = compiler.compile(plan, model)
        except Exception as e:  # noqa: BLE001
            return ToolResult(
                ok=False, error=f"编译失败: {e}",
                hint="该口径可能尚未在语义层完整定义；可试 semantic.list_metrics",
            )
        return ToolResult(ok=True, data={
            "sql": compiled.sql,
            "tables_used": compiled.tables_used,
            "fingerprint": plan.fingerprint(),
        })


class SqlExecuteTool:
    spec = ToolSpec(
        name="sql.execute",
        description=(
            "执行 SQL 并返回结果表。**必须经过只读守门**（禁写操作、强制行数上限）。"
            "若执行失败，请仔细读错误信息：字段名错就调 schema.describe 确认真实字段，"
            "不要用同样的参数重试。"
        ),
        params={
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "要执行的只读 SQL"},
            },
            "required": ["sql"],
        },
        requires=("source.read",),
        kind="read",
    )

    def __init__(self, deps: BuiltinDeps) -> None:
        self._deps = deps

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        guard = ctx.get("guard", "source.read")
        adapter = ctx.get("adapter", "source.read")
        sql = str(args.get("sql") or "").strip()
        if not sql:
            return ToolResult(ok=False, error="缺少参数 sql",
                              hint="传入一条只读 SELECT 语句")

        max_rows = int(ctx.maybe("max_rows") or 1000)
        decision = guard.check(sql, limit=max_rows)
        if not decision.allowed:
            return ToolResult(
                ok=False, error=f"SQL 被安全策略拒绝: {decision.message}",
                hint=decision.suggestion or "本工具只允许只读查询",
            )
        try:
            result = adapter.execute(decision.sql, limit=max_rows, timeout_s=30.0)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            hint = "检查字段名与语法；可用 schema.describe 确认真实字段"
            candidates: list[str] = []
            if "no such column" in msg.lower() or "unknown column" in msg.lower():
                bad = msg.split(":")[-1].strip().strip('"').strip("'")
                catalog = ctx.maybe("catalog")
                if catalog is not None:
                    cols = catalog.column_names(ctx.source_id)
                    candidates = _suggest(bad, cols)
                    if not candidates:
                        # 相似度太低时给出**真实存在的列名**做兜底。
                        # 空候选会让 agent 无从下手、只能盲试，那正是要避免的。
                        candidates = cols[:20]
                    hint = (f"字段 {bad} 不存在。真实字段（部分）: "
                            f"{'、'.join(candidates[:12])}。"
                            f"可用 schema.describe 查看完整表结构")
            elif "no such table" in msg.lower():
                hint = "表不存在；调用 schema.describe 查看全部表名"
            return ToolResult(ok=False, error=msg, hint=hint,
                              candidates=candidates)

        return ToolResult(ok=True, data={
            "columns": list(result.columns),
            "rows": [list(r) for r in result.rows],
            "row_count": result.row_count,
            "truncated": result.truncated,
            "sql": decision.sql,
        })


class Nl2SqlSolveTool:
    """把原有线性流水线整体包装成一个工具。

    这是迁移策略的关键：agent 可以选择"用老办法一把梭"，
    也可以在它失败后改走"先探索再重试"的路径。
    **旧能力不丢，只是从"唯一路径"变成"其中一个选项"。**
    """

    spec = ToolSpec(
        name="nl2sql.solve",
        description=(
            "一步到位：把问题直接转成 SQL 并执行，返回结果表。"
            "**问题表述清晰、你确信语义层已覆盖时用它最快。**"
            "若它返回失败，不要重复调用，改为先探索"
            "（semantic.list_metrics / schema.describe / knowledge.search）再重试。"
        ),
        params={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "用户的业务问题"},
            },
            "required": ["question"],
        },
        requires=("semantic.read", "source.read"),
        kind="read",
    )

    def __init__(self, deps: BuiltinDeps) -> None:
        self._deps = deps

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pipeline = ctx.maybe("pipeline")
        if pipeline is None:
            return ToolResult(ok=False, error="内部流水线不可用",
                              hint="这是运行时的装配问题，不是你的调用问题")
        question = str(args.get("question") or ctx.question).strip()
        if not question:
            return ToolResult(ok=False, error="缺少参数 question")
        outcome = pipeline(question, ctx)
        if outcome.ok and outcome.result is not None:
            return ToolResult(ok=True, data={
                "sql": outcome.sql,
                "columns": list(outcome.result.columns),
                "rows": [list(r) for r in outcome.result.rows],
                "row_count": outcome.result.row_count,
                "truncated": outcome.result.truncated,
            })
        return ToolResult(
            ok=False,
            error=outcome.message or f"未能完成（{outcome.error_code or outcome.status}）",
            hint=("可先调用 semantic.list_metrics 看有哪些指标，"
                  "或 schema.describe 确认字段，再重试"),
            candidates=list(outcome.candidates or []),
        )


_INTERNAL_DIMS = {
    "order_status", "amount", "discount_amount",
    "quantity", "unit_cost", "customer_id",
}


def _registry_classes() -> tuple[type, ...]:
    """内置工具类集合。

    顺序即界面展示顺序：先探索（了解有什么）→ 后执行（取数）→ 最后追问。
    单独抽出来是为了能在**不实例化**的情况下枚举规格。

    注意 `SkillLoadTool` 也在这里：管理界面的"工具全集"要能看到它
    （用户得知道有这么一个工具），但它**按需注册**——只有目录里有技能时
    才真正进运行时（见 `build_builtin_tools`）。M7 的项目/任务工具同理。
    """
    from .tool_tasks import task_tool_classes

    return (
        ListMetricsTool,
        DescribeMetricTool,
        DescribeSchemaTool,
        KnowledgeSearchTool,
        SkillLoadTool,
        SqlPlanTool,
        SqlCompileTool,
        SqlExecuteTool,
        Nl2SqlSolveTool,
        ClarifyAskTool,
    ) + task_tool_classes()


def builtin_tool_specs() -> list[ToolSpec]:
    """返回内置工具的规格清单（**全集**）。

    管理界面靠它显示"有哪些工具可启用"，包括当前被停用或未授权
    （因而未注册）的工具——否则用户看不到它们，也就无从启用。
    """
    return [cls.spec for cls in _registry_classes()]


def _suggest(bad: str, available: list[str], top_k: int = 5) -> list[str]:
    import difflib

    return difflib.get_close_matches(bad, available, n=top_k, cutoff=0.5)


def build_builtin_tools(deps: BuiltinDeps) -> list[Any]:
    """构造内置工具实例。返回列表以便交给 `ToolRegistry` 注册。

    `skill.load` 是**条件注册**的：只有目录里真的有技能时才注册它。

    理由与"能力未授予的工具不注册"完全一致——一个只会回
    "没有可用技能"的工具留在清单里，等于让模型**白花一步**去试它。
    副作用是我们能保证："零技能"配置下工具清单与加技能机制之前**逐字相同**，
    因此黄金集 30 题与 agent 模式的行为都不受影响。
    """
    tools: list[Any] = [
        ListMetricsTool(deps),
        DescribeMetricTool(deps),
        DescribeSchemaTool(deps),
        KnowledgeSearchTool(deps),
        SqlPlanTool(deps),
        SqlCompileTool(deps),
        SqlExecuteTool(deps),
        Nl2SqlSolveTool(deps),
        ClarifyAskTool(deps),
    ]
    catalog = getattr(deps, "skill_catalog", None)
    dynamic = skill_load_tool_spec(catalog)
    if dynamic is not None:
        tool = SkillLoadTool(deps)
        # 把**渲染好目录的规格**装到实例上（覆盖类属性）。
        # 不做这一步的话，模型看到的只有一句"参数必须是可用技能之一"，
        # 却不知道有哪些技能——那等于让它盲猜名字。
        tool.spec = dynamic
        tools.append(tool)

    # M7：项目/任务工具按装配情况条件注册（无任务库就不给模型白花一步的机会）。
    from .tool_tasks import build_task_tools

    tools.extend(build_task_tools(deps))
    return tools


def skill_load_tool_spec(catalog: Any) -> ToolSpec | None:
    """装配期生成 `skill.load` 的规格：**基础说明 + 当前技能目录**。

    目录必须让模型看见，否则它不知道有哪些技能可加载、各自何时适用——
    渐进披露的"目录常驻、正文按需"就是在这一步落地的。
    没有技能时返回 None（不注册，见 `build_builtin_tools`）。
    """
    if catalog is None or not catalog.names():
        return None
    lines = catalog.catalog_text()
    desc = (
        f"{SkillLoadTool.spec.description}\n\n"
        f"当前可用技能（命中其中一条时先加载它，再动手）：\n{lines}"
    )
    from dataclasses import replace

    return replace(SkillLoadTool.spec, description=desc)
