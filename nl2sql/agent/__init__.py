"""编排层：问题 → 结果。

职责：把"问题"变成"结果"。所有副作用（规划、编译、执行、缓存）在此汇聚。
节点之间只通过 state 通信；依赖全部构造注入。

设计取舍
--------
1. **默认用显式顺序管线，不先引入 LangGraph**。
   理由：P0 流程是线性的（规划 → 编译 → 查缓存 → 守门 → 执行 → 写缓存）。
   LangGraph 的价值在分支/循环/多 Agent，现在引入只增加抽象成本。
   `GraphSpec` 的数据结构已支持任意节点与边，需要时把它编译成 LangGraph 图即可
   ——所以这一步不会成为返工点。
2. **降级链显式**：品语义规划失败 → 自由生成 → 再失败才报错。
   每一级降级都在 `outcome.notes` 里留痕，不静默。
3. **缓存不参与正确性判断**：读不到就正常执行；写失败也不影响返回。
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime
from typing import Any, Callable

from ..contracts.llm import Planner, PlannerContext
from ..contracts.pipeline import Edge, GraphSpec, NodeSpec, QueryOutcome
from ..contracts.semantic import LogicalPlan, SemanticResolutionError

__all__ = ["AgentGraph", "build_default_graph", "DEFAULT_GRAPH_NAME"]

DEFAULT_GRAPH_NAME = "default"


def build_default_graph() -> GraphSpec:
    """P0 默认管线（线性）。

    plan → compile → cache_lookup → guard → execute → cache_store → outcome

    `cache_lookup` 安排在 `plan`/`compile` 之后：缓存键是**逻辑计划指纹**，
    问题换个说法只要计划相同就应命中。这与"按问题文本缓存"有本质区别——
    后者判断错了没人会发现。
    """
    nodes = [NodeSpec(name=n, kind=k, label=lb) for n, k, lb in [
        ("plan", "plan", "规划"), ("compile", "compile", "编译"),
        ("cache_lookup", "cache", "查缓存"), ("guard", "guard", "安全校验"),
        ("execute", "execute", "执行"), ("cache_store", "cache", "写缓存"),
        ("outcome", "outcome", "结果组装"),
    ]]
    edges = [Edge(source=nodes[i].name, target=nodes[i + 1].name)
             for i in range(len(nodes) - 1)]
    return GraphSpec(name=DEFAULT_GRAPH_NAME, nodes=nodes, edges=edges, version="v1")


class AgentGraph:
    """顺序管线执行器。

    依赖通过构造参数显式注入，运行时只传 state。
    `planner` 可以是 `RulePlanner` 或 `LLMPlanner`——管线不感知差别，
    这正是"模型只是可替换组件"的落地方式。
    """

    def __init__(self, spec: GraphSpec, *,
                 planner: Planner,
                 compiler: Any,
                 guard: Any,
                 adapter: Any,
                 semantic_model: Any,
                 cache: Any = None,
                 validator: Any = None,
                 history: Any = None,
                 knowledge: Any = None,
                 freeform: Callable[[str, PlannerContext], LogicalPlan | None] | None = None,
                 max_rows: int = 1000,
                 timeout_s: float = 30.0) -> None:
        self.spec = spec
        self._planner = planner
        self._compiler = compiler
        self._guard = guard
        self._adapter = adapter
        self._model = semantic_model
        self._cache = cache
        self._validator = validator
        self._history = history
        self._knowledge = knowledge
        self._freeform = freeform
        self._max_rows = max_rows
        self._timeout_s = timeout_s

    @property
    def planner_name(self) -> str:
        return getattr(self._planner, "name", "unknown")

    # ------------------------------------------------------------ 主流程
    def run(self, question: str, *, source_id: str,
            now: str | None = None, use_cache: bool | None = None,
            knowledge: list[str] | None = None) -> QueryOutcome:
        t0 = time.perf_counter()
        ctx = PlannerContext(source_id=source_id, now=now,
                             knowledge=list(knowledge or []))
        trace: dict[str, int] = {}
        notes: list[str] = []

        def mark(label: str, started: float) -> None:
            trace[label] = int((time.perf_counter() - started) * 1000)

        # ---- 0) 知识召回（few-shot 示例）
        # 这是知识库对准确率的主要贡献路径：把相似的历史 (问题, SQL) 对
        # 交给规划器参考，模型照着改，而不是从零推导。
        if self._knowledge is not None and not ctx.examples:
            s = time.perf_counter()
            try:
                ctx.examples = self._knowledge.few_shot_examples(
                    question, source_id=source_id, top_k=3,
                )
            except Exception:  # noqa: BLE001 - 召回失败不应阻断问答
                ctx.examples = []
            mark("retrieve_ms", s)
            if ctx.examples:
                notes.append(f"召回 {len(ctx.examples)} 条相似查询示例")

        # ---- 1) 规划
        s = time.perf_counter()
        plan: LogicalPlan | None = None
        planner_error: Exception | None = None
        try:
            plan = self._planner.resolve(question, ctx)
        except SemanticResolutionError as e:
            planner_error = e
        except Exception as e:  # noqa: BLE001 - 规划器内部异常也应可降级
            planner_error = e
        mark("plan_ms", s)

        if plan is None and self._freeform is not None:
            s = time.perf_counter()
            try:
                plan = self._freeform(question, ctx)
            except Exception as e:  # noqa: BLE001
                planner_error = e
            mark("freeform_ms", s)
            if plan is not None:
                notes.append("语义层未覆盖，已降级为自由生成（口径未验证）")

        if plan is None:
            return self._error_outcome(
                question, source_id, code="UNRESOLVED", status="unresolved",
                message="无法把问题映射到语义模型，自由生成也未产出可用查询。",
                suggestions=[
                    "换一种更具体的问法，点明指标与维度",
                    "确认该口径已在语义层（指标文件）中定义",
                ],
                cause=planner_error, trace=trace, notes=notes,
            )
        notes.extend(plan.notes)

        # ---- 2) 编译
        s = time.perf_counter()
        try:
            compiled = self._compiler.compile(plan, self._model)
        except Exception as e:  # noqa: BLE001
            mark("compile_ms", s)
            return self._error_outcome(
                question, source_id, code="COMPILE_FAILED", status="invalid_sql",
                message=f"逻辑计划无法编译为 SQL：{e}",
                suggestions=["该口径可能尚未在语义层完整定义"],
                cause=e, trace=trace, notes=notes,
            )
        mark("compile_ms", s)

        # ---- 3) 查缓存（键 = 逻辑计划指纹）
        use_cache = self._cache.enabled if use_cache is None else use_cache
        cached = (self._cache.lookup(source_id, plan)
                  if (use_cache and self._cache) else None)
        if cached is not None:
            result = self._cache.to_query_result(cached)
            outcome = self._ok_outcome(
                question, source_id, plan=plan, sql=cached.sql or compiled.sql,
                result=result, notes=list(notes), trace=trace,
                elapsed_ms=int((time.perf_counter() - t0) * 1000),
            )
            outcome.cached = True
            outcome.notes.append(
                "结果来自缓存（数据时间："
                + (cached.data_time or cached.created_at or "未知") + "）"
            )
            self._record_history(outcome)
            return outcome

        # ---- 4) 守门
        s = time.perf_counter()
        # 关键耦合：SQL 级 LIMIT 与适配器的探测上限必须都大于展示上限。
        #   适配器收到 limit=N 时会去取 N+1 行来判断"还有没有更多"；
        #   若这里把 SQL 限成 N，数据库先截断，适配器永远看不到更多，
        #   truncated 标记就会静默失效（踩过这个坑）。
        # guard 的职责是"别让查询失控跑到数据库"（安全上限），
        # 判断截断则交给适配器。
        decision = self._guard.check(compiled.sql, limit=self._max_rows + 1)
        mark("guard_ms", s)
        if not decision.allowed:
            return self._error_outcome(
                question, source_id, code="BLOCKED", status="blocked",
                message=decision.message or "查询被安全策略拒绝",
                suggestions=[decision.suggestion] if decision.suggestion else [],
                sql=compiled.sql, trace=trace, notes=notes,
            )
        sql = decision.sql

        # ---- 5) 校验（存在性 / 方言）
        if self._validator is not None:
            vres = self._validator.validate(sql, max_rows=self._max_rows)
            if vres.candidates:
                notes.append("字段候选：" + "、".join(vres.candidates[:5]))
            if not vres.ok:
                # 不直接判死：存在性检查会因别名产生噪声，最终以执行结果为准
                notes.append("静态校验提示：" + "；".join(vres.errors[:2]))

        # ---- 6) 执行
        s = time.perf_counter()
        try:
            result = self._adapter.execute(
                # 适配器内部会多读一行以判断截断，这里传展示上限即可
                sql, limit=self._max_rows, timeout_s=self._timeout_s,
            )
        except Exception as e:  # noqa: BLE001
            mark("execute_ms", s)
            return self._execute_error_outcome(
                question, source_id, sql=sql, error=e,
                plan=plan, trace=trace, notes=notes,
            )
        mark("execute_ms", s)
        result.source_id = source_id
        result.sql = sql

        # ---- 7) 写缓存（失败不影响返回）
        if use_cache and self._cache:
            self._cache.store_result(source_id, plan, result)

        outcome = self._ok_outcome(
            question, source_id, plan=plan, sql=sql, result=result,
            notes=notes, trace=trace,
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
        )
        self._record_history(outcome)
        return outcome

    # ------------------------------------------------------------ 结果组装
    def _ok_outcome(self, question: str, source_id: str, *,
                    plan: LogicalPlan, sql: str, result: Any,
                    notes: list[str], trace: dict[str, int],
                    elapsed_ms: int) -> QueryOutcome:
        status = "truncated" if result.truncated else (
            "empty" if result.row_count == 0 else "ok"
        )
        suggestions: list[str] = []
        if status == "truncated":
            notes.append(f"结果被截断，仅显示前 {result.row_count} 行")
            suggestions.append("缩小时间范围或增加过滤条件以获得完整结果")
        if status == "empty":
            suggestions.append("查询执行成功但没有匹配数据；可放宽过滤条件或检查时间范围")
        if result.warnings:
            notes.extend(result.warnings)

        applied = describe_plan_filters(plan)
        if status == "empty" and applied:
            notes.append("实际生效的过滤条件：" + "；".join(applied))

        return QueryOutcome(
            status=status, question=question, source_id=source_id,
            sql=sql, result=result, plan=plan, notes=notes,
            suggestions=suggestions, applied_filters=applied,
            plan_fingerprint=plan.fingerprint(), elapsed_ms=elapsed_ms,
            data_time=result.data_time, trace=trace,
        )

    def _execute_error_outcome(self, question: str, source_id: str, *,
                               sql: str, error: Exception, plan: LogicalPlan,
                               trace: dict[str, int],
                               notes: list[str]) -> QueryOutcome:
        """把执行错误翻译成**可操作**的反馈，而不是抛数据库原文。

        在"只给 SQL + 结果表"的方案里，这段就是产品体验本身。
        """
        msg = str(error)
        code = "EXEC_ERROR"
        status = "error"
        suggestions = ["可尝试简化问题，或先在数据库客户端验证该查询"]

        if "timeout" in type(error).__name__.lower() or "超过" in msg:
            code, status = "TIMEOUT", "timeout"
            suggestions = ["缩小时间范围，或加上地区/品类等维度限制后再查"]
        elif "no such column" in msg.lower() or "unknown column" in msg.lower():
            code, status = "UNKNOWN_COLUMN", "invalid_sql"
            bad = msg.split(":")[-1].strip()
            suggestions = [f"字段 {bad} 不存在，请确认语义层定义或改用相近字段"]
        elif "no such table" in msg.lower() or "doesn't exist" in msg.lower():
            code, status = "UNKNOWN_TABLE", "invalid_sql"
            suggestions = ["表不存在，可能元数据快照已过期，建议刷新元数据"]
        elif "syntax" in msg.lower():
            code, status = "SYNTAX_ERROR", "invalid_sql"

        return QueryOutcome(
            status=status, question=question, source_id=source_id, sql=sql,
            plan=plan, error_code=code,
            message=f"查询执行失败：{msg}", suggestions=suggestions,
            notes=notes, plan_fingerprint=plan.fingerprint(), trace=trace,
        )

    def _error_outcome(self, question: str, source_id: str, *, code: str,
                       status: str, message: str, suggestions: list[str],
                       cause: Exception | None = None, sql: str | None = None,
                       trace: dict[str, int], notes: list[str]) -> QueryOutcome:
        if cause is not None:
            notes.append(f"原因：{type(cause).__name__}: {cause}")
        return QueryOutcome(
            status=status, question=question, source_id=source_id, sql=sql,
            error_code=code, message=message, suggestions=suggestions,
            notes=notes, trace=trace,
        )

    # ------------------------------------------------------------ 历史
    def _record_history(self, outcome: QueryOutcome) -> None:
        if self._history is None:
            return
        try:
            from ..contracts.storage import HistoryRecord

            self._history.add_history(HistoryRecord(
                id=uuid.uuid4().hex[:16],
                source_id=outcome.source_id,
                question=outcome.question,
                plan_fingerprint=outcome.plan_fingerprint or "",
                sql=outcome.sql or "",
                status=outcome.status,
                row_count=outcome.result.row_count if outcome.result else 0,
                elapsed_ms=outcome.elapsed_ms,
                created_at=datetime.now().replace(microsecond=0).isoformat(),
                error_code=outcome.error_code,
                error_message=outcome.message,
                meta={"planner": self.planner_name, "cached": outcome.cached},
            ))
        except Exception:  # noqa: BLE001 - 历史写入失败不应影响查询
            pass


def describe_plan_filters(plan: LogicalPlan) -> list[str]:
    """把计划里的过滤条件渲染成人话，用于"结果为空"时回显。"""
    out: list[str] = []
    for f in plan.filters:
        if isinstance(f.value, (list, tuple)):
            val_str = "、".join(str(v) for v in f.value)
        else:
            val_str = str(f.value)
        out.append(f"{f.field} {f.op} {val_str}")
    tr = plan.time_range
    if tr and (tr.start or tr.end):
        out.append(f"时间 {tr.start or '*'} ~ {tr.end or '*'}")
    return out
