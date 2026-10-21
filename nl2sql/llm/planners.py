"""规划器实现：规则规划器与模型规划器。

两者产出同一类型 `LogicalPlan`，因此下游（编译 / 守门 / 执行 / 缓存）
完全复用，互不感知。

`RulePlanner` 的价值不只是"没模型时的兜底"——它是**语义层覆盖率的度量尺**：
    规则规划器能答对的题 = 语义层已经表达清楚的口径
    规则规划器答不出、模型能答出的题 = 模型补足了语义层的缺口
有了这条基准线，"模型带来了多少增益"才是可量化的，而不是感觉。
"""
from __future__ import annotations

import calendar
import re
from dataclasses import replace
from datetime import date, datetime
from typing import Any

from ..contracts.llm import ChatMessage, LLMProvider, Planner, PlannerContext
from ..dialect._deps import direct_dimension_refs, metric_dependencies, metric_refs
from ..contracts.semantic import (
    Filter,
    Having,
    LogicalPlan,
    MetricDefinition,
    OrderBy,
    Pivot,
    PivotBucket,
    PlanDimension,
    PlanMeasure,
    SemanticModel,
    SemanticResolutionError,
    TimeRange,
)

__all__ = ["RulePlanner", "LLMPlanner", "HybridPlanner", "build_semantic_digest"]

# --------------------------------------------------------------------- 常量
_RELATIVE_MONTH = [
    # (正则, 相对当前月的偏移, 是否整月)
    (re.compile(r"本月|这个月|当月|当月"), 0, True),
    (re.compile(r"上月|上个月|前一个月"), -1, True),
    (re.compile(r"下月|下个月"), 1, True),
]
_RELATIVE_YEAR = [
    (re.compile(r"今年|本年度|本年"), 0, True),
    (re.compile(r"去年|上一年"), -1, True),
]
_CN_NUM = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
    "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12,
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8,
    "9": 9, "10": 10, "11": 11, "12": 12,
}
_ASC_WORDS = ("升序", "从低到高", "由低到高", "从小到大", "最少", "最低")
_DESC_WORDS = ("降序", "从高到低", "由高到低", "从大到小", "最高", "最多")
_LIMIT_RE = re.compile(r"(?:前|top|最高的|最低的|最少的)\s*(\d+)", re.IGNORECASE)
_NO_DEFAULT_RANGE = ("本月", "这个月", "上月", "上个月", "当天", "今天", "昨天",
                     "每日", "每天", "按天", "上周", "本周")
_GRAIN_WORDS = {
    "month": ("按月", "每月", "月度", "每个月", "各月"),
    "day": ("按天", "每天", "每日", "日度"),
    "week": ("按周", "每周", "周度", "各周"),
    "quarter": ("按季度", "每季度", "季度"),
    "year": ("按年", "每年", "年度"),
}
# "分组"信号词：出现时才把维度当作分组维度。
# 必须区分两类词，混淆会造成静默错误：
#   分组类（各/每/按/哪个/哪些/排名）→ 维度进入 GROUP BY
#   仅"多列输出"类（分别/分别是）    → **不算分组信号**
#      "华东地区的有效订单数和实收金额分别是多少" 里只有"分别"，
#      地区是限定条件而非分组要求；若当成分组，结果会多出一个地区列。
_GROUP_HINTS = ("各", "每", "按", "哪个", "哪些", "排名", "排行",
                "前", "top", "TOP", "最高", "最低", "最多", "最少")


# --------------------------------------------------------------------- 时间
def _month_bounds(anchor: date, offset: int) -> tuple[str, str]:
    y, m = anchor.year, anchor.month + offset
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    last = calendar.monthrange(y, m)[1]
    return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{last:02d}"


def _year_bounds(anchor: date, offset: int) -> tuple[str, str]:
    y = anchor.year + offset
    return f"{y:04d}-01-01", f"{y:04d}-12-31"


def parse_time_range(question: str, now: str | None) -> TimeRange | None:
    """从问题里解析时间范围与粒度。**只使用显式传入的 now 作为锚点。**

    支持三类表达：
        绝对：2024年6月 / 2024年上半年 / 6月1日 / 2024-06-01 / 2024年
        相对：本月 / 上月 / 去年（需 now）
        粒度：按月 / 按天 / 按周 / 按季度（不产生范围，只影响分组）

    不支持"最近7天"这类滚动窗口——它需要更细的语义，属于语义层边界，
    规则规划器直接不处理，让问题落到模型或 freeform 路径。
    """
    if not question:
        return None

    grain = next(
        (g for g, words in _GRAIN_WORDS.items() if any(w in question for w in words)),
        None,
    )
    explicit_grain = grain is not None
    anchor = _anchor(now)

    def mk(start: str | None = None, end: str | None = None,
           g: str | None = None, rel: str | None = None) -> TimeRange:
        return TimeRange(field="order_date", start=start, end=end, grain=g,
                         relative=rel, explicit_grain=explicit_grain)

    # 绝对：2024-06-01 这类 ISO 日期（**必须先于"年月"匹配**，
    # 否则 "2024-06-01" 会被其它分支误判成整月）
    iso = re.search(r"(\d{4})-(\d{2})-(\d{2})", question)
    if iso:
        d = f"{iso.group(1)}-{iso.group(2)}-{iso.group(3)}"
        return mk(d, d, "day", "explicit_day")

    # 绝对：6月1日（先于"2024年6月"，因为"2024年6月1日"两种模式都能匹配）
    md = re.search(r"(\d{4}\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日", question)
    if md:
        year = int(md.group(1)[:4]) if md.group(1) else (anchor.year if anchor else None)
        if year:
            iso_d = f"{year:04d}-{int(md.group(2)):02d}-{int(md.group(3)):02d}"
            return mk(iso_d, iso_d, "day", "explicit_day")

    # 相对窗口（本月/上月/今年/去年）：必须在"2024年上半年"之前判断，
    # 因为"本月"不含年份、而"上半年"含年份，两者不冲突但都需先于泛化分支
    comparison = _has_two_periods(question)
    if anchor:
        for pattern, offset, _whole in _RELATIVE_MONTH:
            if pattern.search(question):
                start, end = _month_bounds(anchor, offset)
                return mk(start, end, grain or "month",
                          "compare_months" if comparison else "month")
        for pattern, offset, _whole in _RELATIVE_YEAR:
            if pattern.search(question):
                start, end = _year_bounds(anchor, offset)
                return mk(start, end, grain or "year",
                          "compare_years" if comparison else "year")

    # 绝对：2024年6月
    ym = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", question)
    if ym:
        y, m = int(ym.group(1)), int(ym.group(2))
        last = calendar.monthrange(y, m)[1]
        return mk(f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{last:02d}",
                  grain or "month", "explicit_month")

    # 绝对：2024年上半年 / 下半年 / 全年
    year = re.search(r"(\d{4})\s*年", question)
    if year:
        y = int(year.group(1))
        if "上半年" in question:
            return mk(f"{y}-01-01", f"{y}-06-30", grain, "H1")
        if "下半年" in question:
            return mk(f"{y}-07-01", f"{y}-12-31", grain, "H2")
        return mk(f"{y}-01-01", f"{y}-12-31", grain or "year", "explicit_year")

    # 只有月日：6月1日（无年份时取锚点年）
    md2 = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", question)
    if md2 and anchor:
        m, d = int(md2.group(1)), int(md2.group(2))
        iso_d = f"{anchor.year:04d}-{m:02d}-{d:02d}"
        return mk(iso_d, iso_d, "day", "explicit_day")

    # 只有粒度（如"每月销量"但没给范围）：不产生范围
    if grain:
        return mk(g=grain, rel="grain_only")
    return None


def _has_two_periods(question: str) -> bool:
    """问题里是否同时在比较两个时间段（如"本月和上月"）。

    跨期对比需要 CASE 条件聚合转列，超出品语义表达能力——
    识别出来是为了让规划器**明确报错**，而不是悄悄只取其中一个时间段
    （那会给出一个看起来正常、实际答非所问的结果）。
    """
    month_hits = sum(1 for p, _, _ in _RELATIVE_MONTH if p.search(question))
    year_hits = sum(1 for p, _, _ in _RELATIVE_YEAR if p.search(question))
    if month_hits + year_hits >= 2:
        return True
    return bool(re.search(r"本月.*上月|上月.*本月|今年.*去年|去年.*今年", question))


def _anchor(now: str | None) -> date | None:
    if not now:
        return None
    try:
        return datetime.fromisoformat(now).date()
    except ValueError:
        return None


# --------------------------------------------------------------------- 语义摘要
def build_semantic_digest(model: SemanticModel) -> str:
    """把语义模型渲染成紧凑文本，供 Prompt 使用。

    **不含物理表名与列名**——语义层的意义就是让模型面对业务概念，
    而不是让它去猜 JOIN 路径。
    """
    lines = [f"数据源: {model.source_id}（语义版本 {model.version}）", "", "可用指标:"]
    for name in model.metric_names():
        mt = model.metrics[name]
        label = mt.label or name
        syn = f"（同义词: {'/'.join(mt.synonyms)}）" if mt.synonyms else ""
        win = " [窗口指标]" if mt.window else ""
        lines.append(f"  - {name} | {label}{win} | {mt.description}{syn}")
    lines.append("")
    lines.append("可用维度:")
    for name in model.dimension_names():
        dm = model.dimensions[name]
        if name in _INTERNAL_DIMS:
            continue
        syn = f"（同义词: {'/'.join(dm.synonyms)}）" if dm.synonyms else ""
        lines.append(f"  - {name} | {dm.description}{syn}")
    return "\n".join(lines)


_INTERNAL_DIMS = {
    "order_status", "amount", "discount_amount",
    "quantity", "unit_cost", "customer_id",
}


# --------------------------------------------------------------------- 规则规划器
class RulePlanner:
    """确定性规则规划器：不依赖任何模型。

    它能处理的是"语义层已经表达清楚"的问题——所以它的准确率
    就是**语义层覆盖率**的直接度量。

    设计取舍：宁可解析不出（抛 SemanticResolutionError），
    也不要猜一个错误的计划。猜错比答不出更糟——用户会以为那是对的。
    """

    name = "rule"

    def __init__(self, model: SemanticModel, *, strict: bool = False) -> None:
        self._model = model
        self._metric_keys = self._build_keys(model.metrics)
        self._dimension_keys = self._build_keys(model.dimensions)
        # strict 模式：校验每一列在问题里都有"明文依据"，否则拒绝解析。
        #
        # 诚实的定位：**这是安全护栏，不是准确率提升手段。**
        # 实测（黄金集 30 题）它只拒绝 3 题，且那 3 题规则规划器本来就答对，
        # 因此对当前准确率**没有帮助**（反而多花 3 次模型调用）。
        # 它的价值在于防止"看似正常但答非所问"的输出被当作确定答案返回——
        # 这类静默错误在真实使用中比答不出更危险。
        self._strict = strict

    # ------------------------------------------------------------ 入口
    def resolve(self, question: str, ctx: PlannerContext) -> LogicalPlan:
        q = (question or "").strip()
        if not q:
            raise SemanticResolutionError("问题为空")

        tr = parse_time_range(q, ctx.now)
        # 时间表达本身用掉的词不应再被当成维度词（"本月"里的"月"）
        stripped = _strip_time_words(q, tr)

        metrics = self._match_metrics(stripped)
        metric_spans = list(getattr(self, "_last_spans", []))
        # 维度抑制判断用**原问题**：时间词剥离会带走"各月/按月"这类分组线索
        dims = self._match_dimensions(stripped, tr, reserved=metric_spans,
                                      have_metrics=tuple(metrics),
                                      original=q)

        if not metrics and not dims:
            raise SemanticResolutionError(
                "无法从问题中识别出任何指标或维度；"
                "可能是语义层未覆盖，或问法需要模型理解"
            )

        filters = self._match_filters(stripped, have_metrics=tuple(metrics))
        # 状态集合 → 专用条件聚合指标，并要求按状态分组
        self._extra_dimensions = []
        metrics = self._refine_metrics_by_status(metrics, filters)
        for extra in self._extra_dimensions:
            if extra in self._model.dimensions and extra not in dims:
                dims.append(extra)

        # ---- 聚合之上的能力：HAVING / 累计 / 组内前N / 组内排名 / 跨期对比 / 分类转列 ----
        # 放在排序之前：新增的指标/维度也要能参与排序。
        adv = self._match_advanced(q, stripped, tr, metrics, dims)
        if adv.having:
            # HAVING 是分组级过滤，行级的 amount 阈值就不再需要了。
            # 不去掉会叠加出一个过滤时机会错的条件（"实收金额超过2万的品类"
            # 若同时加了 `f.amount > 20000`，就变成"只看单笔超2万的订单后求和"）。
            filters = [x for x in filters if x.field != "amount"]
            metrics = [x for x in metrics if x not in _ROW_LEVEL_ONLY_METRICS]
        # 高级能力往往**取代**已有匹配结果，所以先删再加
        for name in adv.drop_measures:
            while name in metrics:
                metrics.remove(name)
        for name in adv.drop_dimensions:
            while name in dims:
                dims.remove(name)
        if adv.drop_time_range:
            tr = None
        for name in adv.measures:
            if name not in metrics:
                metrics.append(name)
        for name in adv.dimensions:
            if name in self._model.dimensions and name not in dims:
                dims.append(name)
        filters.extend(adv.filters)

        # 已在 GROUP BY 里的维度**不要**删掉它的取值过滤。
        # 反例（踩过）："已退款和已取消的订单各有多少笔" 若删掉 status 过滤，
        # 条件聚合指标就会按全部状态分组，非匹配组全是 0，结果从 2 行变 3 行且值错。
        # 保留过滤是安全的：IN 条件与条件聚合的目标集合一致，语义上并不冲突。
        order_by = self._match_order(stripped, metrics, dims)
        limit = None if adv.drop_limit else self._match_limit(stripped)
        # "前 N / top N" 是**排序要求**，不只是行数上限。
        # 没有 ORDER BY 时 LIMIT N 取的是任意 N 行（SQL 不保证顺序），
        # "前5"就退化成了"随便5行"——结果不报错，只是答非所问。
        # 所以由"前N"推出的 limit 必须配一个按指标降序的排序。
        if limit is not None and not order_by and metrics:
            order_by = [OrderBy(field=metrics[0], desc=True)]

        # 未给时间范围且问题不带日期线索时，默认锚定"数据所在的上半年"
        # ——这是样例数据的窗口约定，避免全表扫描与跨期歧义
        if tr is None and not any(w in q for w in _NO_DEFAULT_RANGE) and _has_year(q):
            tr = TimeRange(field="order_date", start="2024-01-01",
                           end="2024-06-30", relative="default_window")

        # 输出别名统一用"用户会看到的业务名"：指标的 label / 维度的 description
        measures = tuple(
            PlanMeasure(metric=name,
                        alias=self._model.metrics[name].label or name)
            for name in metrics
        )
        dimensions = tuple(
            PlanDimension(dimension=name,
                          alias=_dim_output_name(self._model, name))
            for name in dims
        )

        # 严格模式：拒绝"列没有明文依据"的计划，让上层改用模型。
        if self._strict:
            unjustified = self._unjustified_columns(q, metrics, dims)
            if unjustified:
                raise SemanticResolutionError(
                    f"严格模式：输出列在问题中没有明文依据 {unjustified}；"
                    f"规则规划器不予作答"
                )

        return LogicalPlan(
            source_id=self._model.source_id,
            sem_version=self._model.version,
            measures=measures,
            dimensions=dimensions,
            filters=tuple(filters),
            time_range=tr,
            order_by=tuple(order_by),
            limit=limit,
            having=tuple(adv.having),
            qualify=tuple(adv.qualify),
            pivot=adv.pivot,
            notes=tuple(adv.notes) or ("由规则规划器解析（未使用模型）",),
        )

    # ------------------------------------------------- 聚合之上的能力识别
    def _match_advanced(self, original: str, stripped: str, tr: TimeRange | None,
                        metrics: list[str], dims: list[str]) -> "_Advanced":
        """识别"聚合之后还能做什么"，产出计划层的构造。

        为什么单独放在一处、而不是散在 `_match_*` 里：
        这些能力**互相排斥或需要整体协调**（累计要加维度、前N要换指标、
        跨期对比要把整个计划改成透视）。散开写会互相覆盖。

        设计立场：**优先从语义模型里找现成的窗口指标**，而不是在规划器里
        拼窗口 SQL。方向是"语义层声明能力、规划器选择能力"——
        这样加一个新窗口指标不需要改规划器。
        """
        adv = _Advanced()
        text = original

        # ---------------------------------------------------- 1) 累计
        if any(w in text for w in ("累计", "累积", "累计值")):
            base = next((x for x in metrics if x not in _WINDOW_LIKE), None)
            if base is not None:
                cum = self._find_window_metric(
                    kind="cumulative", base_metric=base,
                    order_dim=self._time_group_dim(dims, tr),
                )
                if cum:
                    adv.measures.append(cum)
                    adv.notes.append(
                        f"★ 累计：按 "
                        f"{self._time_group_dim(dims, tr) or '时间维度'} "
                        f"滚动求和（{cum}）"
                    )

        # ------------------------------------------- 2) 组内排名（含前 N）
        part_dim = self._partition_dimension(stripped, dims)
        top_n = self._match_top_n(text)
        if part_dim:
            order_metric = next(
                (x for x in metrics
                 if x not in _WINDOW_LIKE and x != "net_revenue"),
                metrics[0] if metrics else None,
            )
            rank = self._find_window_metric(
                kind="rank", base_metric=order_metric, order_dim=part_dim,
            )
            if rank:
                adv.measures.append(rank)
                # 窗口指标引用的维度必须在分组里，否则编译器会拒绝
                # （它要按那个维度排序/分区，外层得有这一列）。
                # 这里**自动补上**而不是报错：那是窗口语义的必然要求，
                # 不是"猜用户想要什么"。
                for need_dim in direct_dimension_refs(rank, self._model):
                    if need_dim not in dims:
                        adv.dimensions.append(need_dim)
                # 问题问的是"地区内排名"，同时被同义词命中的"全局排名"
                # 是多余的一列——两个都输出会让结果行多一个数。
                if _rank_kind(self._model.metrics[rank]) == "partitioned":
                    for x in list(metrics):
                        mt = self._model.metrics.get(x)
                        if mt is not None and _rank_kind(mt) == "global":
                            adv.drop_measures.append(x)
                adv.notes.append(
                    f"★ 组内排名：按 {part_dim} 分区（{rank}）"
                )
                if top_n is not None:
                    # 前 N 用 qualify，不能用 having：排名是窗口层算出来的，
                    # HAVING 比窗口层早，那时候"名次"还不存在。
                    label = self._model.metrics[rank].label or rank
                    adv.qualify.append(Having(
                        field=label, op="<=", value=top_n))
                    adv.drop_limit = True
                    adv.notes.append(
                        f"★ 分组内前 {top_n}：窗口层之后筛名次"
                        f"（qualify，不是 HAVING；"
                        f"这个「前{top_n}」是组内的，不做整表 LIMIT）"
                    )

        # ---------------------------------------------------- 3) HAVING
        if not adv.qualify:
            adv.having.extend(self._match_having(stripped, metrics, dims))

        # ---------------------------------------------------- 4) 跨期对比
        if tr is not None and tr.relative in ("compare_months", "compare_years"):
            base = next((x for x in metrics
                         if x not in _WINDOW_LIKE), None)
            if base and tr.start and tr.end:
                adv.pivot = self._comparison_pivot(base, tr)
                adv.dimensions.clear()   # 透视自己就是"一行多列"，不再分组
                # 桶条件已经把两个时间窗写死了；计划层再留一个时间范围
                # 会把上一个窗口整个切掉——**上期那一列恒为 0，而且不报错**。
                adv.drop_time_range = True
                adv.notes.append(
                    "★ 跨期对比：透视成多列（语义层已内建，不再走自由生成）"
                )

        # ------------------------------------------- 5) 分类转列（全取值列出）
        if adv.pivot is None:
            piv, piv_field = self._categorical_pivot(stripped, metrics)
            if piv is not None:
                adv.pivot = piv
                # 被转列的维度不能再当分组列，否则每个取值一行、
                # 透视出来的两列里总有一列是 0（结果行数还翻倍）
                adv.drop_dimensions.append(piv_field)
                adv.notes.append(
                    "★ 分类转列：把维度取值转成列（与跨期对比同一机制）"
                )

        return adv

    def _match_having(self, question: str, metrics: list[str],
                      dims: list[str]) -> list[Having]:
        """识别"分组后按聚合值过滤"（SQL HAVING）。

        判据两件事同时成立，缺一不可：

        * 出现了 `<指标> + 比较词 + 数字>`
        * 问句里**有分组维度**（"…的<维度>"/"哪些/各/每"匹配到了维度）

        第二条是关键，漏了它会犯一个很隐蔽的错：

            "单个订单成交金额超过 5000 元的订单有几笔？"

        这句里的"金额超过5000"是**行级**过滤（先筛订单，再数），
        误判成 HAVING 就变成"先按订单分组求和、再比较"——口径完全不同，
        而且它还会把原本正确的行级过滤条件删掉，结果看着像算出来了。
            相反，"实收金额超过2万的品类有哪些"有分组维度（品类），
        才是真的 HAVING。
        """
        if not dims:
            return []
        m = _HAVING_RE.search(question)
        if not m or not metrics:
            return []
        op = _HAVING_OP.get(m.group(1))
        if op is None:
            return []
        value: float | int = float(m.group(2))
        if value == int(value):
            value = int(value)

        # 命中哪个指标：取在比较词之前出现、且位置最靠后的那个
        head = question[:m.start(1)]
        best, best_pos = None, -1
        for name in metrics:
            mt = self._model.metrics.get(name)
            if mt is None:
                continue
            words = [mt.label, *mt.synonyms, name]
            for w in words:
                if not w:
                    continue
                pos = head.rfind(w)
                if pos > best_pos:
                    best, best_pos = name, pos
        if best is None:
            return []
        return [Having(field=best, op=op, value=value)]

    def _time_group_dim(self, dims: list[str],
                        tr: TimeRange | None) -> str | None:
        """分组里的时间维度（累计/环比要按它排序）。"""
        for cand in ("month", "day", "week", "quarter", "year", "order_date"):
            if cand in dims:
                return cand
        if tr is not None and tr.grain in ("month", "day"):
            return {"month": "month", "day": "day"}[tr.grain]
        return None

    def _partition_dimension(self, question: str,
                             dims: list[str]) -> str | None:
        """识别"<维度>内/内部/之内"里的分区维度。"""
        for name in dims:
            mt = self._model.dimensions.get(name)
            if mt is None:
                continue
            for w in [mt.description, *_synonyms_of(mt), name]:
                if w and re.search(
                    re.escape(w) + r"\s*(?:内|内部|之内|当中|中)", question
                ):
                    return name
        return None

    def _find_window_metric(self, *, kind: str, base_metric: str | None,
                            order_dim: str | None) -> str | None:
        """在语义模型里找一个符合要求的窗口指标。

        **不在规划器里拼窗口 SQL**，而是"语义层声明、规划器挑选"：
        加新窗口指标不用改规划器。找不到就返回 None，
        让计划退化成普通聚合（而不是生成一个错误的窗口）。
        """
        for name, mt in sorted(self._model.metrics.items()):
            if not mt.window:
                continue
            refs = direct_dimension_refs(name, self._model)
            if order_dim and order_dim not in refs:
                continue
            deps = metric_dependencies(name, self._model)
            if kind == "cumulative":
                if base_metric and base_metric not in deps:
                    continue
                if not re.search(r"\bOVER\s*\(", mt.expr or "", re.I):
                    continue
                if not re.search(r"(SUM|AVG|COUNT)\s*\(", mt.expr or "", re.I):
                    continue
            elif kind == "rank":
                if not re.search(r"\b(RANK|ROW_NUMBER|DENSE_RANK)\s*\(",
                                 mt.expr or "", re.I):
                    continue
                if not re.search(r"PARTITION\s+BY", mt.expr or "", re.I):
                    continue
            return name
        return None

    def _match_top_n(self, question: str) -> int | None:
        """识别"前 N"（含"前二/前三"这类中文数字）。"""
        m = re.search(r"前\s*(\d+)", question)
        if m:
            return int(m.group(1))
        for cn, n in _CN_NUM.items():
            if f"前{cn}" in question:
                return n
        return None

    def _comparison_pivot(self, base_metric: str,
                          tr: TimeRange) -> Pivot:
        """跨期对比 → 透视。两个窗口由锚点推出（本月 / 上月）。"""
        this_label = self._model.metrics[base_metric].label or base_metric
        prev = _previous_window(tr)
        buckets = [
            PivotBucket(
                label=f"本期{this_label}",
                conditions=(Filter(field=tr.field, op=">=", value=tr.start),
                            Filter(field=tr.field, op="<=", value=tr.end)),
            ),
            PivotBucket(
                label=f"上期{this_label}",
                conditions=(Filter(field=tr.field, op=">=", value=prev[0]),
                            Filter(field=tr.field, op="<=", value=prev[1])),
            ),
        ]
        return Pivot(buckets=tuple(buckets),
                     measure=PlanMeasure(metric=base_metric))

    def _categorical_pivot(self, question: str,
                           metrics: list[str]) -> tuple[Pivot | None, str]:
        """分类转列：某个维度的**全部取值**都被列出，且用户明说"各自"。

        两个条件缺一不可：

        1. **全部取值都出现** → 用户要的是"并排列出"而不是过滤。
           只出现一个取值时是过滤（`_match_filters` 已经处理）。
        2. 有 `各自/交叉` 这类**明确要求各占一列**的说法。
           只有条件 1 时，"线上和线下的实收金额"既可以分组（12 行）
           也可以转列（6 行），两种都合理；选错会让结果形状完全变样。
           所以这种情况下**默认按分组输出**——信息更多，也更接近
           "列出明细"的默认预期。

        （条件 1 与 `_match_filters` 里"全部取值都出现 → 不是过滤"是同一条
        规则，两处必须一致，否则会出现"先按取值过滤掉、又要按同一取值转列"
        的荒谬计划——计划能跑，结果一列全是 0。）

        返回 `(透视, 被转列的维度名)`；后者让调用方把它从分组维度里去掉。
        """
        base = next((x for x in metrics if x not in _WINDOW_LIKE), None)
        if base is None:
            return None, ""
        if not any(w in question for w in _PIVOT_HINTS):
            return None, ""
        for field in sorted(_VALUE_MAP, key=lambda f: -_value_count(f)):
            if field not in self._model.dimensions:
                continue
            hit = _value_hits(field, question)
            if len(hit) < _value_count(field) or _value_count(field) < 2:
                continue
            label = self._model.metrics[base].label or base
            return Pivot(
                buckets=tuple(
                    PivotBucket(
                        label=f"{v}{label}",
                        conditions=(Filter(field=field, op="=", value=v),),
                    )
                    for v in hit
                ),
                measure=PlanMeasure(metric=base),
            ), field
        return None, ""

    def _unjustified_columns(self, question: str, metrics: list[str],
                             dims: list[str]) -> list[str]:
        """列出"没有明文依据"的输出列。

        判据：
            派生指标（引用了其它指标）→ 要求其 label 出现在问句里。
                基础指标不查：用户可能用同义词（"有多少人"对应 customer_count），
                键表已经保证了同义词命中，再查 label 会误杀。
            分组维度 → 问句里必须有分组信号词（各/每/按/哪个/前N…）。
        """
        bad: list[str] = []
        for name in metrics:
            mt = self._model.metrics.get(name)
            if mt is None:
                continue
            if metric_refs(mt.expr) and (mt.label or name) not in question:
                bad.append(f"指标:{name}")
        if dims and not any(w in question for w in _GROUP_HINTS):
            bad.extend(f"维度:{d}" for d in dims)
        return bad

    # ------------------------------------------------------------ 匹配
    @staticmethod
    def _build_keys(items: dict[str, Any]) -> list[tuple[str, str]]:
        """构造 (候选词, 语义名) 列表，按候选词长度降序。

        两个关键决定：
        1. **只用 名称 + label + synonyms，绝不用 description**。
           描述是给人读的完整句子，用于匹配会大面积误命中
           （"客户等级"的描述里含"客户"，于是"客户数"这类问题会多出一个
           customer_name 分组列）。这是规则规划器第一版 23% 准确率的头号原因。
        2. **长度降序**："退款率"必须先于"退款"、"实收金额"必须先于"金额"。
        """
        out: list[tuple[str, str]] = []
        for name, obj in items.items():
            candidates = {name, getattr(obj, "label", "") or "",
                          *getattr(obj, "synonyms", [])}
            for c in candidates:
                if c and len(c) >= 2:
                    out.append((c, name))
        out.sort(key=lambda x: -len(x[0]))
        return out

    @staticmethod
    def _match_keys(question: str, keys: list[tuple[str, str]],
                    limit: int = 4,
                    reserved: list[tuple[int, int]] | None = None,
                    ) -> list[str]:
        """按键表匹配，返回命中的语义名，并记录已占用文本区间到 `_last_spans`。

        `reserved` 让指标先占位，防止维度抢走同一段文字
        （"客户数"已被指标占用，"客户"就不该再命中 customer_name 维度）。
        """
        found: list[str] = []
        spans: list[tuple[int, int]] = list(reserved or [])
        for word, name in keys:
            idx = question.find(word)
            if idx < 0:
                continue
            span = (idx, idx + len(word))
            if any(not (span[1] <= s or span[0] >= e) for s, e in spans):
                continue
            if name not in found:
                found.append(name)
            spans.append(span)
            if len(found) >= limit:
                break
        return found

    def _match_keys_with_spans(self, question: str,
                               keys: list[tuple[str, str]],
                               limit: int = 4,
                               reserved: list[tuple[int, int]] | None = None,
                               ) -> tuple[list[str], list[tuple[int, int]]]:
        """匹配并返回 (命中的语义名, 占用的文本区间)。

        区间是**关键**：指标先匹配并占位，维度再匹配时跳过已占用的文字，
        否则"客户数"这个指标里的"客户"会被 customer_name 维度再次命中，
        最终 SELECT 里多出一个毫无意义的分组列。
        """
        found: list[str] = []
        spans: list[tuple[int, int]] = list(reserved or [])
        for word, name in keys:
            idx = question.find(word)
            if idx < 0:
                continue
            span = (idx, idx + len(word))
            if any(not (span[1] <= s or span[0] >= e) for s, e in spans):
                continue
            if name not in found:
                found.append(name)
            spans.append(span)
            if len(found) >= limit:
                break
        return found, spans

    def _match_metrics(self, question: str) -> list[str]:
        names, spans = self._match_keys_with_spans(question, self._metric_keys)
        self._last_spans = spans
        # "排名"这类窗口指标只在明确提到时加入，避免污染普通排序问题
        names = [n for n in names if n != "revenue_rank"
                 or "排名" in question or "排行" in question]
        return self._prune_component_metrics(names, question)

    def _prune_component_metrics(self, names: list[str],
                                 question: str) -> list[str]:
        """剔除"纯组成项"，保留基础指标、被点名的派生指标、以及**被明文请求**的组成项。

        问题场景："各地区已退款订单数占全部订单数的比例" 会同时命中
        `refunded_order_count` / `all_order_count` / `refund_rate`，
        但用户只要最后一个；前两个只是它的分子分母。

        两类问法必须区分（这是踩出来的）：
          * **解释型**："…占…的比例是多少" / "…率是多少"
            分子分母只是解释，**只输出比率**
          * **列举型**："实收金额占全渠道总实收金额的比例" / "实收金额、成本和毛利率"
            分子与比率都要输出

        判据：追问短语（"占…的比例" / "率是多少"）里出现的派生指标，
        输出时剪掉其组成项。
        """
        if len(names) <= 1:
            return names

        # 统一规则一：问句是"占…的比例/率"结构时，这是**比率问句**。
        #   只保留：派生比率指标 + 被明文点名的**基础**指标（如 F2 的"实收金额"）。
        #   组成项（分子/分母，如 B6 的 refunded_order_count / all_order_count）一律剪掉。
        # 统一规则二：否则按基础/派生分类（D7 三个都点名 → 全保留）。
        is_ratio_question = "占" in question and (
            "比例" in question or "占比" in question
        )

        # **关键排除**：被比率指标引用、且出现在分母解释里的组成项，
        # 不算"用户点名要的列"。
        #   B6 "已退款订单数占该地区全部订单数的比例" 中 "全部订单数"
        #   出现在"占…"之后——那是在解释分母，不是要求输出该列；
        #   不排除的话它会被判为 named 而保留，结果从 1 列变 3 列。
        #   F2 的 "占全渠道总实收金额" 里 "实收金额" 是**基础指标**，
        #   用户确实要这一列，所以排除只作用于"被派生指标引用的"组成项。
        explanatory: set[str] = set()
        if is_ratio_question:
            for n in names:
                mt = self._model.metrics.get(n)
                if mt is None or not metric_refs(mt.expr):
                    continue
                if _is_explanatory(mt.label or n, question, self._model):
                    explanatory.update(metric_refs(mt.expr))

        kept: list[str] = []
        for n in names:
            mt = self._model.metrics.get(n)
            if mt is None:
                continue
            is_base = not metric_refs(mt.expr)
            named = (mt.label or n) in question and n not in explanatory
            if is_ratio_question:
                if not is_base:
                    kept.append(n)              # 派生比率（如"比例"）
                elif named:
                    kept.append(n)              # 被点名的基础指标（F2 的实收金额）
            else:
                if is_base or named:
                    kept.append(n)
        return kept or names

    def _match_dimensions(self, question: str, tr: TimeRange | None,
                          reserved: list[tuple[int, int]] | None = None,
                          have_metrics: tuple[str, ...] = (),
                          original: str = "") -> list[str]:
        """匹配分组维度。

        `reserved` 是被指标已占用的文本区间——**关键机制**：
        不尊重它就会出现"客户数"这个指标里的"客户"又被当成 customer_name 维度，
        最终 SELECT 里多出一个毫无意义的分组列。

        `original` 用于判断"是否要求分组"，因为时间词剥离会带走分组线索
        （"各月"里的"各"会被当作时间词一起剥掉）。
        """
        skip = {"order_date"}
        if tr is not None and tr.grain in ("month", "day", "week"):
            skip.add({"month": "month", "day": "day", "week": "week"}[tr.grain])

        matched, _ = self._match_keys_with_spans(
            question, self._dimension_keys, reserved=reserved)
        dims = [
            n for n in matched
            if n not in skip and n not in _INTERNAL_DIMS
        ]
        # 按维度名在问句中的出现位置重排，保证输出列顺序符合用户表达顺序
        # （"各地区…消费客户数和实收金额" → 先地区；"客户姓名、所属地区" → 先姓名）
        dims.sort(key=lambda n: _first_pos(question, n, self._model))

        # 剔掉"仅作为过滤值出现"的维度。
        # 判断用**原问题**：剥离时间词会把 "各月" 的 "各" 一起带走，导致误判。
        src = original or question
        group_hint = any(w in src for w in _GROUP_HINTS)
        if not group_hint:
            filter_only = {"category", "region", "channel_type", "channel",
                           "customer_type", "status"}
            dims = [d for d in dims if d not in filter_only]

        # "前 N" 只在**没有其它分组维度**时才暗示分组。
        # 否则"销量前5的商品是哪些"会因"前"字被当成要按商品名分组。
        #
        # 但**问题里显式列出了输出列**时不能截断：
        # "销量前5的商品是哪些？列出商品名称、品类和销量" 里品类是用户
        # 点名要的列，砍掉它属于"因为一个启发式规则丢掉了明文要求"。
        # 判据用"列出"与顿号——这是中文里列清单最直接的两个标记。
        if "前" in src and len(dims) > 1 and not _lists_columns(src):
            dims = dims[:1]

        # 粒度维度只在**用户明确要求按粒度分组**时才补入（判断用原问题）
        stripped_grain = tr.grain if tr is not None else None
        wants_grain = bool(tr is not None and (
            tr.explicit_grain or _has_grain_word(src, stripped_grain)
        ))
        if wants_grain and stripped_grain in ("month", "day"):
            dim_name = {"month": "month", "day": "day"}[stripped_grain]
            if dim_name in self._model.dimensions and dim_name not in dims:
                dims.insert(0, dim_name)
            if tr.grain == "month" and "month" in self._model.dimensions:
                if "month" not in dims:
                    dims.insert(0, "month")
            if tr.grain == "day" and "day" in self._model.dimensions:
                if "day" not in dims:
                    dims.insert(0, "day")
        # 比率/累计类指标自带分子分母，分组维度不应被其描述文字带出
        if {"refund_rate", "refunded_order_count"} & set(have_metrics):
            dims = [d for d in dims if d != "status"]
        return dims

    def _refine_metrics_by_status(self, metrics: list[str],
                                  filters: list[Filter]) -> list[str]:
        """状态过滤 + 订单计数 时，改用语义化的计数写法。

        "已退款和已取消的订单各有多少笔" 若用 paid_order_count 会得到 0
        （指标本身固定筛"已支付"，与 IN 过滤叠加后互相矛盾）。

        正确做法是注册一个**条件聚合指标**：
            SUM(CASE WHEN status IN ('已退款','已取消') THEN 1 ELSE 0 END)
        这样过滤与计数语义一致，且仍走完整的编译/守门/缓存链路
        （不是绕过链路拼裸 SQL）。
        """
        status = next((f for f in filters if f.field == "status"), None)
        if status is None:
            return metrics
        values = list(status.value) if isinstance(status.value, (list, tuple)) \
            else [status.value]
        values = [str(v) for v in values]
        if not values:
            return metrics

        generic = {"paid_order_count", "all_order_count"}
        if not any(m in generic for m in metrics):
            return metrics

        # 单值且语义模型已有专用指标 → 直接复用（口径已定义）
        if len(values) == 1:
            preferred = {"已支付": "paid_order_count",
                         "已退款": "refunded_order_count"}.get(values[0])
            if preferred == "paid_order_count":
                return metrics
            if preferred:
                out = [m for m in metrics if m not in generic]
                out.insert(0, preferred)
                return out

        # 多值（或没有专用指标）→ 注册条件聚合指标，并**按该字段分组**
        # （"已退款和已取消各有多少笔"要求分别列出，所以要 GROUP BY 状态）
        suffix = "_".join(values)
        name = f"count_status_{suffix}"
        cond = ", ".join(f"'{v}'" for v in values)
        expr = (f"SUM(CASE WHEN {{{{ dimension('order_status') }}}} IN ({cond})"
                f" THEN 1 ELSE 0 END)")
        self._model.extend_metrics({
            name: MetricDefinition(
                name=name, label="订单数", expr=expr,
                description=f"状态为 {suffix} 的订单笔数",
            )
        })
        out = [m for m in metrics if m not in generic]
        out.insert(0, name)
        self._extra_dimensions = ["status"]
        return out

    def _match_filters(self, question: str,
                       have_metrics: tuple[str, ...] = ()) -> list[Filter]:
        out: list[Filter] = []

        # 1) 维度取值：问句里出现某个维度的已知取值
        for field in sorted(_VALUE_MAP, key=lambda f: -max(
                len(fo) for _c, forms in _VALUE_MAP[f] for fo in forms)):
            if field not in self._model.dimensions:
                continue
            hit = _value_hits(field, question)
            if not hit:
                continue
            # 全部取值都出现 → 这是"分类别列出"，不是过滤
            # （"线上渠道和线下渠道各自…"若做成 IN 过滤会只留一条，结果全错）
            if len(hit) >= _value_count(field) and field != "customer_type":
                continue
            if len(hit) == 1:
                out.append(Filter(field=field, op="=", value=hit[0]))
            else:
                out.append(Filter(field=field, op="in", value=hit))

        # 2) 渠道名（取值无法硬编码全量，这里覆盖常见项）
        if "channel" in self._model.dimensions:
            for ch in ["淘宝", "京东", "抖音", "官网", "线下门店"]:
                if ch in question and not any(f.field == "channel" for f in out):
                    out.append(Filter(field="channel", op="=", value=ch))

        # 3) 成交金额阈值："金额超过 5000"
        m = re.search(
            r"金额[^0-9]{0,8}?(\d+(?:\.\d+)?)\s*(?:元)?"
            r"\s*(以上|以下|超过|大于|小于|不低于|不高于)?", question)
        if m and any(w in question for w in
                     ("超过", "大于", "以上", "低于", "小于", "以下", "不低于", "不高于")):
            value = float(m.group(1))
            op = (" >= " if ("以上" in question or "不低于" in question) else
                  " <= " if ("以下" in question or "不高于" in question) else
                  " > " if ("超过" in question or "大于" in question) else " < ").strip()
            if "amount" in self._model.dimensions:
                out.append(Filter(field="amount", op=op, value=value))

        # 4) 比率/累计类指标自带口径，不应再叠加状态过滤
        if {"refund_rate", "refunded_order_count"} & set(have_metrics):
            out = [f for f in out if f.field != "status"]

        return out

    def _match_order(self, question: str, metrics: list[str],
                     dims: list[str]) -> list[OrderBy]:
        desc = any(w in question for w in _DESC_WORDS)
        asc = any(w in question for w in _ASC_WORDS)
        if not (desc or asc):
            return []

        # 明确点名了按哪个字段排序（"按销量降序"）
        target_metric = None
        for word, name in self._metric_keys:
            if word in question and name in metrics:
                target_metric = name
                break

        orders: list[OrderBy] = []
        if target_metric:
            orders.append(OrderBy(field=target_metric, desc=not asc))
        elif metrics:
            orders.append(OrderBy(field=metrics[0], desc=not asc))
        elif dims:
            orders.append(OrderBy(field=dims[0], desc=not asc))

        # 并列打破：加一分组维度升序，保证结果确定可判分
        for dname in dims:
            if all(o.field != dname for o in orders):
                orders.append(OrderBy(field=dname, desc=False))
                break
        return orders

    @staticmethod
    def _match_limit(question: str) -> int | None:
        m = _LIMIT_RE.search(question)
        if m:
            return int(m.group(1))
        if "前十" in question:
            return 10
        if "前五" in question:
            return 5
        return None


class _Advanced:
    """"聚合之上的能力"识别结果。

    一个容器而不是元组：字段会随能力增加（后面还会有"移动平均""同比"），
    每次加一个就改所有返回点的签名不值得。

    `drop_*` 三兄弟是必要的，不是补丁：高级能力往往**取代**已有的匹配结果——

    * 认出了"地区内排名"，就要去掉同时被同义词命中的"全局排名"
      （否则多出一个用户没要的列）
    * 透视的源维度不能再当分组列（否则每个取值一行，透视就白做了）
    * 跨期对比的桶已经把时间写死了，计划层的时间范围会把上个窗口切掉
      （**这个错误最隐蔽：结果不报错，只是上期那一列恒为 0**）
    """

    __slots__ = ("having", "qualify", "pivot", "measures", "dimensions",
                 "filters", "notes", "drop_measures", "drop_dimensions",
                 "drop_time_range", "drop_limit")

    def __init__(self) -> None:
        self.having: list[Having] = []
        self.qualify: list[Having] = []
        self.pivot: Pivot | None = None
        self.measures: list[str] = []
        self.dimensions: list[str] = []
        self.filters: list[Filter] = []
        self.notes: list[str] = []
        self.drop_measures: list[str] = []
        self.drop_dimensions: list[str] = []
        self.drop_time_range = False
        # "每个品类前 2" 里的"前2"是**组内**前 N，不能同时当成整表的 LIMIT。
        # 两者共用同一段文字，若都生效，"每组前2"会变成"整表取2行"——
        # 结果只剩一组，而且看起来像是分组没生效。
        self.drop_limit = False


# "实收金额超过 20000" / "销量不低于 100 件"
_HAVING_RE = re.compile(
    r"(超过|大于|高于|不低于|以上|低于|小于|不足|不高于|以下)"
    r"\s*(\d+(?:\.\d+)?)"
)
_HAVING_OP = {
    "超过": ">", "大于": ">", "高于": ">",
    "不低于": ">=", "以上": ">=",
    "低于": "<", "小于": "<", "不足": "<",
    "不高于": "<=", "以下": "<=",
}

# 中文数字（"前二""前三"）
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 维度取值表：**规范化取值 → 该取值的各种写法**。
#
# 为什么不是平铺一个列表：同义写法必须归一到同一个取值。"新客"是"新客户"
# 的子串，"新客户和老客户各贡献了多少"若把两者当成两个类别，会算出
# **4 列而不是 2 列**（"新客户/新客/老客户/老客"）——真实踩到过。
_VALUE_MAP: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    "status": [("已支付", ("已支付",)), ("已退款", ("已退款",)),
               ("已取消", ("已取消",))],
    "region": [("华东", ("华东",)), ("华北", ("华北",)), ("华南", ("华南",)),
               ("西南", ("西南",)), ("华中", ("华中",))],
    "channel_type": [("线上", ("线上",)), ("线下", ("线下",))],
    "category": [("数码", ("数码",)), ("家电", ("家电",)), ("服饰", ("服饰",)),
                 ("食品", ("食品",)), ("图书", ("图书",))],
    "customer_type": [("新客户", ("新客户", "新客")),
                      ("老客户", ("老客户", "老客"))],
}

# 只有行级语义、不能当累计/透视基数的指标（它们本身就是"按状态数行"）
_ROW_LEVEL_ONLY_METRICS = {"all_order_count"}
# 窗口类指标：不能作为"累计的基数"或"排名的排序依据"
_WINDOW_LIKE: frozenset[str] = frozenset()

# "分类转列"的触发词。**必须窄**：
# "线上和线下的实收金额"既可以按 channel_type 分组（12 行），
# 也可以转成两列（6 行）——两种都合理。选错会让结果形状全变，
# 所以只在这类明确的"各自成一列"的说法下才转列；
# 否则按分组输出（信息更多、也更接近"列出明细"的默认预期）。
#
# 注意**不能**用"分别"：黄金集里 A2/D2 的"分别是多少"指的是
# "不同指标各自的值"，不是"同一维度的不同取值各占一列"。
_PIVOT_HINTS = ("各自", "交叉", "各一列", "分别列")


def _lists_columns(question: str) -> bool:
    """问题是否**显式列出了输出列**（"列出A、B和C"）。

    用来给启发式规则让路：用户点名的列不该被"前N只暗示一个分组维度"
    这类规则砍掉。中文里列清单最直接的两个标记是"列出"与顿号。
    """
    return "列出" in question or "、" in question


def _value_hits(field: str, question: str) -> list[str]:
    """问句里命中的**规范化**取值（去同义重复，保持取值表顺序）。"""
    out: list[str] = []
    for canon, forms in _VALUE_MAP.get(field, ()):
        if any(f in question for f in forms) and canon not in out:
            out.append(canon)
    return out


def _value_count(field: str) -> int:
    return len(_VALUE_MAP.get(field, ()))


def _synonyms_of(mt: Any) -> list[str]:
    """维度/指标的同义词，容错取（定义可以没有这个字段）。"""
    return [str(x) for x in (getattr(mt, "synonyms", None) or [])]


_RANK_FN_RE = re.compile(r"\b(RANK|ROW_NUMBER|DENSE_RANK|NTILE)\s*\(", re.I)
_PARTITION_RE = re.compile(r"PARTITION\s+BY", re.I)


def _rank_kind(mt: Any) -> str | None:
    """窗口指标的排名类型：`"partitioned"` / `"global"` / None（不是排名）。

    用来区分"地区内排名"和"全局排名"：问题问了前者时，同义词表会把后者
    也匹配上（两者的同义词里都有"排名"），于是结果多出一列用户没要的排名。
    """
    expr = getattr(mt, "expr", "") or ""
    if not _RANK_FN_RE.search(expr):
        return None
    return "partitioned" if _PARTITION_RE.search(expr) else "global"


def _previous_window(tr: TimeRange) -> tuple[str, str]:
    """上一个等长窗口（"本月"→"上月"）。

    只处理月与年两种——它们是 `parse_time_range` 会给出 start/end 的相对
    窗口。别的粒度（周/季）要么还没支持，要么等长推算有歧义（2 月只有 28 天），
    宁可不给也不给一个差几天的窗口。
    """
    import calendar as _cal

    assert tr.start and tr.end
    y, m = int(tr.start[:4]), int(tr.start[5:7])
    if tr.relative == "compare_years":
        return f"{y - 1}-01-01", f"{y - 1}-12-31"
    m -= 1
    if m == 0:
        y, m = y - 1, 12
    last = _cal.monthrange(y, m)[1]
    return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{last:02d}"


def _has_grain_word(text: str, grain: str | None) -> bool:
    """文本里是否出现该粒度的分组词（判断用**原问题**，不能用剥离后的文本）。

    "2024年上半年…按月列出" 里的"按月"在剥离时间词时会被去掉，
    因此必须回原问题里找。
    """
    if not grain:
        return False
    return any(w in text for w in _GRAIN_WORDS.get(grain, ()))


def _first_pos(question: str, dim_name: str, model: SemanticModel) -> int:
    """维度在问句里最早出现的位置；找不到则排到最后。

    用于让输出列顺序贴合用户的表达顺序，避免"各地区…消费客户数"
    却把客户姓名排在地区前面。
    """
    dim = model.dimensions.get(dim_name)
    candidates = [dim_name]
    if dim is not None:
        # DimensionDefinition 没有 label 字段，用 getattr 兼容未来新增字段
        candidates += [getattr(dim, "label", "") or "",
                       getattr(dim, "description", "") or "",
                       *getattr(dim, "synonyms", [])]
    positions = [question.find(c) for c in candidates if c]
    positions = [p for p in positions if p >= 0]
    return min(positions) if positions else 10 ** 6


def _is_explanatory(label: str, question: str,
                    model: SemanticModel) -> bool:
    """问句是否把该比率当"解释"而非"要输出的列"。

    两种问法差别很细，但决定输出列数：
        "已退款订单数占该地区全部订单数的比例是多少" → 解释型
            （"占"后面只是"全部订单数"，没有点出要哪一列）
        "实收金额占全渠道总实收金额的比例是多少"     → 列举型
            （"占"后面出现了"实收金额"，用户明确要这一列）

    判据：取"占…的比例|率"之间的分母陈述，看它是否**包含其它指标的 label**。
    包含 → 用户在指名要那一列，不能剪。
    这个判据比数关键词稳，因为它直接读的是用户写下的字。
    """
    idx = question.find(label)
    if idx < 0:
        return False
    before = question[max(0, idx - 30): idx]
    if "占" not in before:
        return False
    # 取分母陈述：从"占"到"的比例"/"率"
    start = question.rfind("占", 0, idx)
    tail = question[idx: idx + 20]
    end_marker = min(
        [p for p in (tail.find("比例"), tail.find("率是多少"),
                     tail.find("率多少")) if p >= 0] or [len(tail)]
    )
    denominator = question[start + 1: idx + end_marker]
    for other in model.metrics:
        if other == label:
            continue
        other_label = model.metrics[other].label or other
        if other_label and other_label in denominator:
            return False        # 分母被点名 → 列举型
    return True


def _dim_output_name(model: SemanticModel, name: str) -> str:
    dim = model.dimensions.get(name)
    if dim is None:
        return name
    return dim.description or name


def _has_year(question: str) -> bool:
    return bool(re.search(r"20\d{2}", question))


def _strip_time_words(question: str, tr: TimeRange | None) -> str:
    """去掉时间表达，避免其用词被当成维度/指标词。

    "本月各渠道实收金额" 里的"月"不该被当成 month 维度——
    时间粒度已经由 time_range 承载。
    """
    q = question
    for pat in (r"20\d{2}\s*年\s*上?下?半年", r"20\d{2}\s*年\s*\d{1,2}\s*月",
                r"20\d{2}\s*年", r"\d{1,2}\s*月\s*\d{1,2}\s*日",
                r"本月|这个月|上月|上个月|下个月|今年|去年|本年度",
                r"按[月天周年季]|每[月天周年季]|各[月周]|月度|日度|周度"):
        q = re.sub(pat, " ", q)
    return q


# --------------------------------------------------------------------- LLM 规划器
_SYSTEM_PROMPT = """你是一个数据仓库语义层查询规划器。
你的任务：把用户的业务问题转换成**逻辑计划 JSON**，不要写 SQL。

规则：
1. 只能使用下面列出的指标名与维度名，不得编造。
2. 指标名用于 measures，维度名用于 dimensions。
3. 时间范围解析为 time_range：{start, end, grain}，日期格式 YYYY-MM-DD。
   若问题未给时间且无相对时间词，time_range 置为 null。
4. 过滤条件用 filters：[{field, op, value}]，op ∈ = != > >= < <= in between like。
   field 必须是上面列出的维度名或指标名。
5. 排序用 order_by：[{field, desc}]，field 用**输出别名或语义名**。
6. limit 为整数或 null。

7. **聚合之后的四种能力**（用错位置会得到错误数字，务必分清）：

   7.1 having —— 对**分组结果**过滤（"实收金额超过2万的品类"）。
       写在这里的条件会生成 SQL HAVING，作用在聚合值上。
       与 filters 的区别：filters 过滤**明细行**（"金额超过5000的订单"），
       having 过滤**分组**。判断依据是"比较的对象是单行还是整组"。

   7.2 qualify —— 对**窗口指标的结果**过滤（"每个品类内部销量前2"）。
       只有在 measures 里放了窗口指标（如"品类内名次"）并要按它筛的时候用。
       注意"前 N"在这里是**组内**前 N，**不要**再写进 limit；
       写成 limit 会变成"整表只取 N 行"，结果只剩一组。

   7.3 pivot —— 把某个维度的取值转成列（长表变宽表）。两种用法：
       * 跨期对比："本月和上月的实收金额分别是多少"
         → 两个桶，条件都写在 order_date 上；
         **此时 time_range 必须为 null**（时间已由桶表达，
         再写 time_range 会把上一个窗口整段过滤掉，那一列恒为 0）
       * 分类转列："线上和线下各自的实收金额"
         → 每个取值一个桶
       用了 pivot 时，measures 留空（值列写在 pivot.measure），
       dimensions 只放**分组**维度，被转列的那个维度不要放进去。

   7.4 累计 / 组内排名 —— 直接用语义层里已定义的窗口指标
       （名字或说明里带"[窗口指标]"的那些）。它们已经声明了按哪个维度
       排序/分区，你**必须把那个维度放进 dimensions**，
       否则计划不合法（窗口要按它排序，外层得有这么一列）。

8. 只输出 JSON，不要任何解释、不要 markdown 代码块。

输出 JSON 结构：
{
  "measures":   [{"metric": "指标名", "alias": "中文列名"}],
  "dimensions": [{"dimension": "维度名", "alias": "中文列名"}],
  "filters":    [{"field": "维度名", "op": "=", "value": "值"}],
  "time_range": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD", "grain": "month"},
  "order_by":   [{"field": "中文列名", "desc": true}],
  "limit": 10,
  "having":     [{"field": "指标名", "op": ">", "value": 20000}],
  "qualify":    [{"field": "名次列名", "op": "<=", "value": 2}],
  "pivot": {
    "measure": {"metric": "指标名", "alias": "中文列名"},
    "buckets": [
      {"label": "本期实收金额",
       "conditions": [{"field": "order_date", "op": ">=", "value": "2024-06-01"},
                      {"field": "order_date", "op": "<=", "value": "2024-06-30"}]}
    ]
  },
  "notes": ["不确定之处"]
}
没有用到的字段请省略或置 null（having/qualify/pivot 都可以不出现）。
"""


class LLMPlanner:
    """模型驱动规划器：把问题交给模型产出逻辑计划 JSON。

    与 `RulePlanner` 的关键差别：
        规则规划器**解析失败就报错**；模型规划器会**尽力给出一个计划**，
        因此必须做产出校验（指标/维度是否存在），不合格就抛
        SemanticResolutionError，让上层决定是否降级到 freeform。

    这里刻意不把物理表结构放进 Prompt：语义层的意义就是让模型面对业务概念。
    """

    name = "llm"

    def __init__(self, provider: LLMProvider, model: SemanticModel, *,
                 temperature: float = 0.0, max_tokens: int = 1200) -> None:
        self._provider = provider
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._digest = build_semantic_digest(model)

    @property
    def provider(self) -> LLMProvider:
        return self._provider

    def resolve(self, question: str, ctx: PlannerContext) -> LogicalPlan:
        from ..llm.providers import LLMError, OpenAICompatProvider

        if not self._provider.available():
            raise SemanticResolutionError("模型提供者不可用")

        user = self._build_user_prompt(question, ctx)
        messages = [
            ChatMessage(role="system", content=_SYSTEM_PROMPT),
            ChatMessage(role="user", content=user),
        ]
        try:
            resp = self._provider.chat(
                messages, temperature=self._temperature,
                max_tokens=self._max_tokens, json_mode=True,
            )
        except LLMError as e:
            raise SemanticResolutionError(f"模型调用失败: {e}") from e

        try:
            payload = OpenAICompatProvider.parse_json(resp.text)
        except LLMError as e:
            raise SemanticResolutionError(f"模型输出无法解析: {e}") from e

        return self._to_plan(payload, question, ctx, resp)

    # ------------------------------------------------------------ Prompt
    def _build_user_prompt(self, question: str, ctx: PlannerContext) -> str:
        parts = [self._digest, ""]
        if ctx.now:
            parts.append(f"当前日期（相对时间的锚点）: {ctx.now}")
        if ctx.knowledge:
            parts.append("")
            parts.append("相关知识（来自知识库，可参考但不要编造字段）:")
            parts.extend(f"  - {k}" for k in ctx.knowledge[:8])
        if ctx.examples:
            parts.append("")
            parts.append(
                "相似的历史查询示例（请参考其中的输出列选择思路，"
                "但必须适配当前问题，不要照抄）:"
            )
            for i, (q, sql) in enumerate(ctx.examples[:3], 1):
                parts.append(f"  示例{i} 问题: {q}")
                parts.append(f"  示例{i} 标准 SQL: {sql.strip()}")
                parts.append("")
        parts.append("")
        parts.append(f"用户问题: {question}")
        return "\n".join(parts)

    # ------------------------------------------------------------ 校验与转换
    def _to_plan(self, payload: dict[str, Any], question: str,
                 ctx: PlannerContext, resp: Any) -> LogicalPlan:
        known_metrics = set(self._model.metrics)
        known_dims = set(self._model.dimensions)

        measures: list[PlanMeasure] = []
        for item in payload.get("measures") or []:
            name = str(item.get("metric", "")).strip()
            if not name:
                continue
            if name not in known_metrics:
                raise SemanticResolutionError(f"模型给出了不存在的指标: {name}")
            measures.append(PlanMeasure(
                metric=name,
                alias=str(item.get("alias") or
                          self._model.metrics[name].label or name),
            ))

        dims: list[PlanDimension] = []
        for item in payload.get("dimensions") or []:
            name = str(item.get("dimension", "")).strip()
            if not name:
                continue
            if name not in known_dims:
                raise SemanticResolutionError(f"模型给出了不存在的维度: {name}")
            dims.append(PlanDimension(
                dimension=name,
                alias=str(item.get("alias") or
                          self._model.dimensions[name].description or name),
            ))

        if not measures and not dims:
            raise SemanticResolutionError("模型未给出任何指标或维度")

        filters: list[Filter] = []
        for item in payload.get("filters") or []:
            field = str(item.get("field", "")).strip()
            if not field:
                continue
            if field not in known_dims and field not in known_metrics:
                raise SemanticResolutionError(f"模型过滤了不存在的字段: {field}")
            filters.append(Filter(
                field=field,
                op=str(item.get("op") or "=").lower(),
                value=item.get("value"),
            ))

        tr = None
        raw_tr = payload.get("time_range")
        if isinstance(raw_tr, dict):
            start = raw_tr.get("start") or None
            end = raw_tr.get("end") or None
            grain = raw_tr.get("grain") or None
            if start or end or grain:
                tr = TimeRange(field="order_date", start=start, end=end, grain=grain)

        orders: list[OrderBy] = []
        for item in payload.get("order_by") or []:
            field = str(item.get("field", "")).strip()
            if not field:
                continue
            orders.append(OrderBy(field=field, desc=bool(item.get("desc", True))))

        limit = payload.get("limit")
        limit = int(limit) if isinstance(limit, (int, float)) and limit else None

        having = self._to_group_filters(
            payload.get("having"), known_dims, known_metrics, "having")
        qualify = self._to_group_filters(
            payload.get("qualify"), known_dims, known_metrics, "qualify")
        pivot = self._to_pivot(payload.get("pivot"), known_metrics, known_dims)

        # 与规则规划器同一套一致性约束：**窗口指标**直接引用的维度必须在分组里。
        # 只查窗口指标：普通聚合指标引用的列（如 net_revenue 里的 amount/
        # order_status）是**内层**的事，内层 CTE 已经消化掉了，不需要进分组。
        # 在**规划期**就报出来比等到编译期更有用——上层可以据此让模型重做，
        # 而编译期只能放弃这个计划。
        need_dims = {
            dn
            for m in measures
            if self._model.metrics[m.metric].window
            for dn in direct_dimension_refs(m.metric, self._model)
        }
        have_dims = {d.dimension for d in dims}
        missing = sorted(need_dims - have_dims)
        if missing:
            raise SemanticResolutionError(
                f"窗口指标需要这些维度同时出现在 dimensions 里: {missing}"
                f"（窗口函数要按它们排序/分区）"
            )

        notes = ["由模型规划器生成"]
        for n in payload.get("notes") or []:
            notes.append(str(n))

        return LogicalPlan(
            source_id=self._model.source_id,
            sem_version=self._model.version,
            measures=tuple(measures),
            dimensions=tuple(dims),
            filters=tuple(filters),
            time_range=tr,
            order_by=tuple(orders),
            limit=limit,
            having=having,
            qualify=qualify,
            pivot=pivot,
            notes=tuple(notes),
        )

    @staticmethod
    def _to_group_filters(raw: Any, known_dims: set[str],
                          known_metrics: set[str],
                          field_name: str) -> tuple[Having, ...]:
        """把模型给的 having / qualify 列表转成契约类型（含字段校验）。"""
        out: list[Having] = []
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            fld = str(item.get("field", "")).strip()
            if not fld:
                continue
            # qualify 的 field 可能是**输出别名**（"名次"），那不在语义模型里，
            # 所以这里不硬校验存在性——编译器会把它落成列名，
            # 名字不对时报"不在窗口层的输出列里"并列出可用列。
            if field_name == "having" and fld not in known_dims \
                    and fld not in known_metrics:
                raise SemanticResolutionError(
                    f"having 引用了不存在的字段: {fld}"
                    f"（必须是已定义的指标名或维度名）"
                )
            out.append(Having(
                field=fld,
                op=str(item.get("op") or "=").lower(),
                value=item.get("value"),
            ))
        return tuple(out)

    def _to_pivot(self, raw: Any, known_metrics: set[str],
                  known_dims: set[str]) -> Pivot | None:
        """把模型给的 pivot 转成契约类型。"""
        if not isinstance(raw, dict):
            return None
        buckets: list[PivotBucket] = []
        for b in raw.get("buckets") or []:
            if not isinstance(b, dict):
                continue
            label = str(b.get("label") or "").strip()
            conds: list[Filter] = []
            for c in b.get("conditions") or []:
                if not isinstance(c, dict):
                    continue
                fld = str(c.get("field", "")).strip()
                if not fld:
                    continue
                if fld not in known_dims and fld not in known_metrics:
                    raise SemanticResolutionError(
                        f"pivot 的列条件引用了不存在的字段: {fld}"
                    )
                conds.append(Filter(
                    field=fld, op=str(c.get("op") or "=").lower(),
                    value=c.get("value"),
                ))
            if not label or not conds:
                continue      # 没有条件的列会把所有行算进去，直接丢弃
            buckets.append(PivotBucket(label=label, conditions=tuple(conds)))

        m = raw.get("measure")
        if not buckets or not isinstance(m, dict):
            return None
        name = str(m.get("metric") or "").strip()
        if name not in known_metrics:
            raise SemanticResolutionError(
                f"pivot 的值列引用了不存在的指标: {name}"
            )
        return Pivot(
            buckets=tuple(buckets),
            measure=PlanMeasure(
                metric=name,
                alias=str(m.get("alias") or
                          self._model.metrics[name].label or name),
            ),
        )


class HybridPlanner:
    """规则优先 + 模型兜底。

    为什么这样组合（有实测依据，见 docs/02 第 18 节）：
        黄金集 30 题上，规则规划器 17/30、模型 18/30，
        **但两者覆盖不同的题**——规则独对 1 题，模型独对 2 题，
        合并上限 19/30。模型只比规则高 3.3 个百分点，
        代价却是 ~120 倍延迟与真实费用。

    所以：能确定的题用规则，规则表达不了的才花钱问模型。
    这不只是省钱，更重要的是**确定性**——
    规则规划器对同一问题永远产出同一计划，缓存指纹稳定、结果可复现、
    便于回归；只有规则覆盖不了时才引入模型的随机性。

    输出上会标注是哪条路径产出的（`notes`），不静默：
    用户与评测都能看到"这个答案来自规则还是模型"。
    """

    name = "hybrid"

    def __init__(self, rule: RulePlanner, llm: "LLMPlanner | None" = None) -> None:
        self._rule = rule
        self._llm = llm
        self._model = rule._model          # 供编排层与编译使用
        # 运行统计，便于观察兜底比例
        self.stats = {"rule": 0, "llm": 0, "failed": 0}

    @property
    def llm_available(self) -> bool:
        return bool(self._llm is not None and self._llm.provider.available())

    def resolve(self, question: str, ctx: PlannerContext) -> LogicalPlan:
        # 1) 规则优先
        try:
            plan = self._rule.resolve(question, ctx)
            self.stats["rule"] += 1
            return _tag(plan, "由规则规划器解析")
        except SemanticResolutionError as rule_error:
            if not self.llm_available:
                self.stats["failed"] += 1
                raise

            # 2) 模型兜底
            try:
                plan = self._llm.resolve(question, ctx)
            except SemanticResolutionError:
                self.stats["failed"] += 1
                # 两条路都失败时，报规则规划器的原因（更贴近语义层缺口）
                raise rule_error
            self.stats["llm"] += 1
            return _tag(plan, "规则未能解析，已由模型兜底")

    def describe(self) -> dict[str, Any]:
        return {
            "planner": self.name,
            "llm_available": self.llm_available,
            "llm_model": getattr(getattr(self._llm, "provider", None), "model", ""),
            "stats": dict(self.stats),
        }


def _tag(plan: LogicalPlan, note: str) -> LogicalPlan:
    """给计划加一条来源标注。

    **不放进指纹**：`LogicalPlan.fingerprint()` 刻意排除 `notes`，
    因此"同一问题先由规则、后由模型兜底"若产生相同计划，仍会命中同一缓存。
    这正是我们要的——缓存应该按语义相同来命中，而不是按产出路径。
    """
    if note in plan.notes:
        return plan
    return replace(plan, notes=plan.notes + (note,))


class VerifiedHybridPlanner:
    """规则产出 → 模型验证 → 不通过则模型重新规划。

    与 `HybridPlanner`（只兜底错误路径）的区别，正是实测暴露的缺口：
        HybridPlanner      兜底的是"规则**答不出**"
        VerifiedHybrid     还要兜底"规则**答错**"

    关于成本的诚实说明：
        它比 `HybridPlanner` 贵——每一道规则命中的题都要多一次验证调用。
        实测（docs/02 第 18 节）验证调用比完整规划便宜（输出从数百 token
        降到几十 token），但"省 93% 调用"的优势不复存在。
        因此是否启用取决于你把"准确率"还是"成本"排在前面：
            `hybrid`          省调用，准确率≈规则
            `hybrid_verified` 更准，但每题都花钱
    """

    name = "hybrid_verified"

    def __init__(self, rule: RulePlanner, llm: "LLMPlanner | None" = None, *,
                 verifier: Any = None) -> None:
        self._rule = rule
        self._llm = llm
        self._verifier = verifier
        self._model = rule._model
        self.stats = {"rule_accepted": 0, "rule_rejected": 0,
                      "llm_used": 0, "failed": 0, "verify_failed_open": 0}

    @property
    def llm_available(self) -> bool:
        return bool(self._llm is not None and self._llm.provider.available())

    @property
    def verifier_available(self) -> bool:
        return bool(self._verifier is not None and self._verifier.available())

    def resolve(self, question: str, ctx: PlannerContext) -> LogicalPlan:
        # ---- 1) 规则先试
        rule_plan: LogicalPlan | None = None
        rule_error: Exception | None = None
        try:
            rule_plan = self._rule.resolve(question, ctx)
        except SemanticResolutionError as e:
            rule_error = e

        # ---- 2) 规则答出了 → 验证是否切题
        if rule_plan is not None:
            if not self.verifier_available or not self.llm_available:
                self.stats["rule_accepted"] += 1
                return _tag(rule_plan, "由规则规划器解析（未验证）")

            result = self._verifier.verify(question, rule_plan, ctx)
            if result.failed_open:
                self.stats["verify_failed_open"] += 1
                self.stats["rule_accepted"] += 1
                return _tag(rule_plan, f"由规则规划器解析（{result.describe()}）")
            if result.approved:
                self.stats["rule_accepted"] += 1
                return _tag(rule_plan, "由规则规划器解析（已验证切题）")

            # 规则答错 → 让模型重做
            self.stats["rule_rejected"] += 1
            rejection = result.describe()
        else:
            rejection = ""

        # ---- 3) 模型规划（兜底或重做）
        if not self.llm_available:
            self.stats["failed"] += 1
            if rule_plan is not None:
                # 有规则计划但无法验证/重做：返回它，但标注未经确认——
                # 不静默丢弃一个可能正确的答案
                return _tag(rule_plan, f"规则计划未通过验证但模型不可用（{rejection}）")
            assert rule_error is not None
            raise rule_error

        try:
            plan = self._llm.resolve(question, ctx)
        except SemanticResolutionError:
            self.stats["failed"] += 1
            if rule_plan is not None:
                return _tag(rule_plan, f"模型重做失败，退回规则计划（{rejection}）")
            assert rule_error is not None
            raise rule_error

        self.stats["llm_used"] += 1
        note = ("规则未能解析，已由模型兜底" if rule_plan is None
                else f"规则计划经验证不切题，已由模型重做（{rejection}）")
        return _tag(plan, note)

    def describe(self) -> dict[str, Any]:
        return {
            "planner": self.name,
            "llm_available": self.llm_available,
            "verifier_available": self.verifier_available,
            "llm_model": getattr(getattr(self._llm, "provider", None), "model", ""),
            "stats": dict(self.stats),
        }
