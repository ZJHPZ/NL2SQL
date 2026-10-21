"""dbt `metrics.yml` 加载器：把 dbt 的指标定义读成 `SemanticModel`。

为什么是这个模块
----------------
项目有一条早期决策：**语义层兼容 dbt `metrics.yml`，不自创指标 DSL**。
理由是分工——"什么是指标、口径是什么"该由已经在做这件事的生态定义，
我们再发明一套只会让用户付出双重迁移成本。这个模块就是那条决策的落地。

它做什么
--------
输入：一个或多个 YAML 文件（dbt 项目里就是 `models/**/*.yml`）
输出：`SemanticModel`（指标 + 维度 + join + 同义词），可直接喂给规划器与编译器

**不做的事**：不连 dbt、不读 `manifest.json`、不跑 `dbt parse`。
只解析 YAML 文本。因此它零副作用、可在任意环境跑、也便于测试。

支持哪两种规范
--------------
dbt 自己有两套指标规范，本模块都读。

1. **legacy（独立 metrics 文件）** —— `calculation_method` + `expression`

   ```yaml
   metrics:
     - name: net_revenue
       label: 实收金额
       model: ref('fact_order')
       calculation_method: sum
       expression: amount - discount_amount
       timestamp: order_date
       time_grains: [day, month]
       filters:
         - field: order_status
           operator: '='
           value: "'已支付'"
   ```

   自包含，不需要 `semantic_models`。这是最省事的入口。

2. **latest（v1.12 / v2，模型内联）** —— `type` + `agg` / `expr` / `numerator`

   ```yaml
   models:
     - name: fact_order
       semantic_model:
         enabled: true
         name: orders
         entities: [...]
         dimensions: [...]
       metrics:
         - name: net_revenue
           type: simple
           agg: sum
           expr: amount - discount_amount
   ```

`semantic_models:` / `models[].semantic_model` 提供三样东西：表的身份
（`base_table`）、**实体**（外键 → JOIN）、**维度**。

`type` 支持 `simple` / `derived` / `ratio`；`cumulative` / `conversion`
**明确拒绝并说明原因**（近似一个累计指标会给出错误数字，比报错糟得多）。

join 从哪来
-----------
不是我们发明的语法。dbt 用 `entities` 表达外键：

    orders    : entities: [ {name: order,    type: primary, expr: order_id},
                            {name: customer, type: foreign, expr: customer_id} ]
    customers : entities: [ {name: customer, type: primary, expr: customer_id} ]

`foreign` 按名字配对到持有同名 `primary` 的模型，两者的 `expr` 组成等值条件，
就得到 `LEFT JOIN dim_customer AS c ON f.customer_id = c.customer_id`。
**这条推导链就是 dbt 语义层自己的连接解析方式**，我们照做即可。

兼容的边界（必须说清楚，否则"兼容"是空话）
-------------------------------------------
dbt 的规范里**确实没有**一些东西，我们变不出来：

* 窗口指标（占比、排名）—— dbt 把比例交给 BI 层或 `ratio`/`cumulative`，
  没有"在聚合结果上再开窗"的表达
* 指标**同义词**、输出**小数位数**
* 需要 CTE 的复杂维度
* 派生表作为 FROM（dbt 的做法是另建一个 dbt model 把它物化掉）

对这些，我们用 dbt **自己的扩展点** `meta:`（dbt 明确允许放任意键并原样忽略），
命名空间 `meta.nl2sql.*`。**这不是另造 DSL**——它是 dbt 指定的"放你自己东西"
的地方，dbt 会安静地忽略它。

代价是这些字段**在 dbt 里不生效**。所以加载器会算一份**可移植性报告**，
把扩展分成两类：

* **标注类**（`synonyms` / `decimals` / `ratio`）：只影响怎么匹配问句和
  怎么格式化数字，**SQL 口径不变**，拿回 dbt 结果数字仍然对
* **结构类**（`window` / `select` / `table` / `logical_*`）：**dbt 里没有等价物**，
  拿回 dbt 就跑不出同样的数

报告里这两类是分开计数的。诚实地说"这份文件有多少能拿回 dbt 跑"，
比声称 100% 兼容有价值。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..contracts.semantic import (
    DimensionDefinition,
    JoinSpec,
    MetricDefinition,
    SemanticModel,
)
from ..errors import ConfigError

# `meta:` 下的命名空间。改它等于改文件格式，所以做成常量。
EXTENSION_KEY = "nl2sql"

# 只影响匹配与格式化、不影响 SQL 口径的扩展键
_ANNOTATION_KEYS = frozenset({"synonyms", "decimals", "ratio"})

# dbt legacy 的 calculation_method → SQL 聚合函数
_LEGACY_AGG: dict[str, str] = {
    "sum": "SUM",
    "count": "COUNT",
    "count_distinct": "COUNT_DISTINCT",     # 特殊：COUNT(DISTINCT x)
    "average": "AVG",
    "min": "MIN",
    "max": "MAX",
}

# 有实现但**不保证跨库**的方法：接受，但发出警告
_PORTABLE_WARN: dict[str, str] = {
    "median": "MEDIAN 在多数库不可用（SQLite 就没有）",
    "percentile": "PERCENTILE_CONT 是窗口函数，各库写法差异大",
}

# 明确不支持的 dbt 指标类型。拒绝而不是静默近似。
_UNSUPPORTED_TYPES: dict[str, str] = {
    "cumulative": (
        "累计指标需要 time spine（时间骨架表）才能确定每个时间点的取值，"
        "当前语义层没有这个概念。改用普通聚合指标 + 时间范围过滤"
    ),
    "conversion": (
        "转化指标需要实体级的事件配对（base→conversion 时间窗），属于行为分析"
        "而非聚合查询。建议在数仓里预先算好一张转化表，再当普通指标接入"
    ),
}

# 表达式标识符重写时要跳过的 SQL 关键字/函数名。
# 不做这层过滤会把 `SUM(amount)` 里的 SUM 当成维度名去找。
_SQL_RESERVED = frozenset("""
select from where group by having order limit offset union all as on and or not
case when then else end null is in like between distinct count sum avg min max
cast integer int real text date datetime substr coalesce nullif round abs
over partition rows range unbounded preceding following current row
""".split())

_IDENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
# dbt MetricFlow 的 jinja 引用：{{ Dimension('order__status') }}
_JINJA_REF = re.compile(
    r"\{\{\s*(Dimension|Metric|Entity|TimeDimension)\s*\(\s*"
    r"(['\"])([^'\"]+)\2\s*(?:,\s*(['\"])([^'\"]*)\4\s*)?\)\s*\}\}"
)
class DbtMetricsError(ConfigError):
    """dbt 指标文件无法加载。

    消息必须**可行动**：说清哪个指标、哪个字段、可用值是什么。
    用户拿到的应该是"改这里"，而不是"加载失败"。

    继承 `ConfigError` 而不是裸 Exception：指标文件写错**就是**配置错，
    这样 Web 壳层已有的异常处理器会把它转成 400 `config_error`，
    而不是漏成 500 + 内部堆栈。
    """


# 各层级认识的键。**不认识的键发警告而不是报错**：
# dbt 会持续加字段，硬拒绝会让新版本的文件直接加载不了；
# 但完全不说也不行——像是 YAML 流式写法里漏引号导致键被拆开
# （`expr: substr(a, 1, 7)` 会被 YAML 当成 `expr: substr(a` + 一个新键），
# 那种静默半解析最难查。
_KNOWN_KEYS: dict[str, frozenset[str]] = {
    "dimension": frozenset({
        "name", "type", "expr", "description", "label", "meta", "config",
        "type_params", "is_partition", "granularity", "dimension",
    }),
    "entity": frozenset({
        "name", "type", "expr", "description", "label", "meta", "config",
        "role",
    }),
    "semantic_model": frozenset({
        "name", "model", "relation", "description", "label", "meta", "config",
        "entities", "dimensions", "measures", "columns", "defaults",
        "primary_entity", "agg_time_dimension", "enabled", "__columns__",
        "__file__",
    }),
    "metric": frozenset({
        "name", "label", "description", "type", "meta", "config", "filter",
        "filters", "dimensions", "model", "sql", "timestamp", "time_grains",
        "window", "calculation_method", "expression", "version",
        "type_params", "measure", "agg", "aggregate", "expr",
        "numerator", "denominator", "input_measures", "input_metrics",
        "metrics", "window", "offset_window", "grain_to_date",
    }),
}


# --------------------------------------------------------------------- 报告
@dataclass
class LoadReport:
    """加载报告。存在的理由是**把"兼容"从声明变成可核对的事实**。"""

    files: list[str] = field(default_factory=list)
    specs: list[str] = field(default_factory=list)      # legacy / latest
    metric_count: int = 0
    dimension_count: int = 0
    join_count: int = 0
    entity_joins: int = 0
    native_metrics: list[str] = field(default_factory=list)
    annotated_metrics: list[str] = field(default_factory=list)
    structural_metrics: list[str] = field(default_factory=list)
    extension_keys: list[str] = field(default_factory=list)
    rejected_metrics: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def portable_count(self) -> int:
        """口径可移植的指标数（原生 + 仅用标注）。"""
        return len(self.native_metrics) + len(self.annotated_metrics)

    @property
    def portability(self) -> float:
        """**口径**可移植比例。1.0 = 拿回 dbt 也能跑出同样的数。"""
        total = self.portable_count + len(self.structural_metrics)
        if not total:
            return 1.0
        return self.portable_count / total

    def describe(self) -> dict[str, Any]:
        return {
            "files": list(self.files),
            "specs": list(self.specs),
            "metrics": self.metric_count,
            "dimensions": self.dimension_count,
            "joins": self.join_count,
            "entity_joins": self.entity_joins,
            "portability": round(self.portability, 4),
            "native_metrics": list(self.native_metrics),
            "annotated_metrics": list(self.annotated_metrics),
            "structural_metrics": list(self.structural_metrics),
            "extension_keys": list(self.extension_keys),
            "rejected_metrics": list(self.rejected_metrics),
            "warnings": list(self.warnings),
        }

    def lines(self) -> list[str]:
        """给人看的报告行。CLI 与文档共用，避免两处文案漂移。"""
        out = [
            f"文件: {', '.join(self.files) or '（无）'}",
            f"规范: {', '.join(self.specs) or '（无）'}",
            f"指标 {self.metric_count}　维度 {self.dimension_count}　"
            f"join {self.join_count}（实体推导 {self.entity_joins}）",
            f"口径可移植性 {self.portability:.0%}"
            f"　原生 {len(self.native_metrics)}"
            f" / 仅标注 {len(self.annotated_metrics)}"
            f" / 用结构扩展 {len(self.structural_metrics)}",
        ]
        if self.structural_metrics:
            out.append(
                "  dbt 里没有等价物（结构扩展）: "
                + ", ".join(self.structural_metrics)
            )
        if self.extension_keys:
            out.append(f"  扩展键: {', '.join(self.extension_keys)}")
        if self.rejected_metrics:
            out.append(f"  被拒绝: {', '.join(self.rejected_metrics)}")
        out.extend(f"  ⚠ {w}" for w in self.warnings)
        return out


# --------------------------------------------------------------------- 小工具
def _read_yaml(path: str) -> Any:
    """读一个 YAML 文件。

    **刻意不让 YAML 的宽容吞掉结构错误**：文件写错时静默返回半份模型，
    表现是"某些指标莫名其妙不见了"——比直接报错难查得多。
    """
    try:
        import yaml
    except ImportError as e:  # pragma: no cover - 环境缺依赖
        raise DbtMetricsError(
            "读取 dbt metrics.yml 需要 PyYAML：pip install pyyaml"
        ) from e

    if not os.path.isfile(path):
        raise DbtMetricsError(f"指标文件不存在: {path}")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise DbtMetricsError(f"YAML 解析失败 {path}: {e}") from e


def _as_list(v: Any) -> list[Any]:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _ref_name(v: Any) -> str:
    """`ref('fact_order')` / `source('shop','orders')` / `fact_order` → 表名。"""
    if not isinstance(v, str):
        return ""
    s = v.strip()
    m = re.match(r"^\s*(ref|source)\s*\(\s*(.+?)\s*\)\s*$", s)
    if m:
        args = re.findall(r"['\"]([^'\"]+)['\"]", m.group(2))
        return args[-1] if args else ""
    return s


def _default_alias(table: str) -> str:
    """表名 → 短别名。取各段首字母（`fact_order` → `fo`）。"""
    words = [w for w in re.split(r"[^0-9A-Za-z]+", table or "") if w]
    if not words:
        return "t"
    if len(words) == 1:
        return words[0][0].lower()
    return "".join(w[0].lower() for w in words)


def _ext(*metas: Any) -> dict[str, Any]:
    """取出 `meta.nl2sql` 扩展块。meta 不是字典时返回空。"""
    for meta in metas:
        if isinstance(meta, dict):
            block = meta.get(EXTENSION_KEY)
            if isinstance(block, dict):
                return block
    return {}


def _quote_value(v: Any) -> str:
    """把 dbt 过滤值变成 SQL 字面量。

    dbt 的约定是**值里自带引号**（`value: "'已支付'"`），所以已经是合法
    SQL 字面量时原样使用，只对裸字符串补引号。数字与布尔不加引号。
    """
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(v)
    if v is None:
        return "NULL"
    s = str(v).strip()
    if not s:
        return "''"
    if (len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"") \
            or s.upper() in ("NULL", "TRUE", "FALSE"):
        return s
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        return s
    if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*\s*\(.*\)", s, re.S):
        return s
    return "'" + s.replace("'", "''") + "'"


def _translate_jinja(expr: str, report: LoadReport, *, where: str) -> str:
    """把 dbt MetricFlow 的 jinja 引用翻成内部语法。

        {{ Dimension('order__status') }}  → {{ dimension('order_status') }}
        {{ Metric('net_revenue') }}       → {{ metric('net_revenue') }}
        {{ TimeDimension('order__order_date', 'month') }}
                                          → {{ dimension('order_date') }}

    `TimeDimension` 的粒度参数会被丢掉并**发警告**：我们的粒度由
    `TimeRange.grain` 管，表达式里带粒度无处安放。丢掉粒度会改变输出行数
    （12 行 vs 1 行），不能静默。
    """
    def repl(m: re.Match[str]) -> str:
        kind = m.group(1)
        name = m.group(3)
        grain = m.group(5)
        # dbt 用 `实体__维度` 双下划线分隔，取后一段
        local = name.split("__")[-1]
        if kind == "Metric":
            return "{{ metric('%s') }}" % local
        if kind == "TimeDimension":
            if grain:
                report.warnings.append(
                    f"{where}: TimeDimension 的粒度 '{grain}' 被丢弃"
                    f"（粒度由 TimeRange 控制，请用 time_grains 或问句表达）"
                )
            return "{{ dimension('%s') }}" % local
        if kind == "Entity":
            report.warnings.append(
                f"{where}: 表达式里直接引用了实体 '{name}'，已按维度名处理；"
                f"实体不是列时请改写成维度"
            )
            return "{{ dimension('%s') }}" % local
        return "{{ dimension('%s') }}" % local

    return _JINJA_REF.sub(repl, expr)


def _rewrite_bare_identifiers(
    sql: str, *, dims: Iterable[str], metrics: Iterable[str],
) -> str:
    """把表达式里**裸写的列名/指标名**换成内部引用。

    为什么需要：dbt legacy 的 `expression` 是纯 SQL（`amount - discount_amount`），
    那样写就丢掉了"这是语义层的哪个维度"这层信息——编译器要靠它决定要不要
    引入 JOIN。裸列名出现在被 JOIN 的表上时，作者根本不知道该写什么别名。

    规则保守，宁可不换：
    * 只换**恰好命中**维度名/指标名的标识符
    * 跳过 SQL 关键字与函数名、跳过 `表.列` 里的限定名、跳过字符串字面量
    """
    dim_set = set(dims)
    metric_set = set(metrics)

    holes: list[str] = []

    def hide(m: re.Match[str]) -> str:
        holes.append(m.group(0))
        return f"\x00{len(holes) - 1}\x00"

    masked = _STRING_LITERAL.sub(hide, sql)

    out: list[str] = []
    i, n = 0, len(masked)
    while i < n:
        ch = masked[i]
        if ch == "\x00":
            j = masked.index("\x00", i + 1)
            out.append(holes[int(masked[i + 1:j])])
            i = j + 1
            continue
        m = _IDENT.match(masked, i)
        if not m:
            out.append(ch)
            i += 1
            continue
        word = m.group(0)
        start, end = m.span()
        prev = masked[start - 1] if start else ""
        nxt = masked[end] if end < n else ""
        # 限定名（a.b）与函数调用（f(x)）：不动
        if prev == "." or nxt == "(" or word.lower() in _SQL_RESERVED:
            out.append(word)
        elif word in dim_set:
            out.append("{{ dimension('%s') }}" % word)
        elif word in metric_set:
            out.append("{{ metric('%s') }}" % word)
        else:
            out.append(word)
        i = end
    return "".join(out)


def _legacy_filter_sql(f: Any, *, where: str) -> str:
    """dbt legacy 过滤项 → SQL 条件。

    dbt 的形状是 `{field, operator, value}`，operator 兼容 SQL 比较符与
    `is` / `is not` / `in` / `not in`。
    """
    if not isinstance(f, dict):
        raise DbtMetricsError(
            f"{where}: filters 每一项必须是 {{field, operator, value}}，"
            f"得到 {type(f).__name__}"
        )
    fld = str(f.get("field") or "").strip()
    op = str(f.get("operator") or "=").strip()
    value = f.get("value")
    if not fld:
        raise DbtMetricsError(f"{where}: filters 缺 field")

    op_l = op.lower()
    if op_l in ("is", "is not"):
        rhs = "NULL" if value is None else _quote_value(value)
        return f"{fld} IS {'NOT ' if op_l == 'is not' else ''}{rhs}"
    if op_l in ("in", "not in"):
        vals = value if isinstance(value, (list, tuple)) else [value]
        items = ", ".join(_quote_value(v) for v in vals)
        return f"{fld} {'NOT IN' if op_l == 'not in' else 'IN'} ({items})"
    if op_l in ("=", "!=", "<>", ">", ">=", "<", "<=", "like"):
        return f"{fld} {op_l} {_quote_value(value)}"
    raise DbtMetricsError(
        f"{where}: 不支持的 operator '{op}'；"
        f"可用: = != <> > >= < <= like in not in is is not"
    )


def _wrap_filters(expr: str, conds: list[str], *, agg: str,
                  expr_empty: bool) -> str:
    """把过滤条件揉进聚合表达式。

    这是 dbt "指标自带过滤"的落地方式：不是在 WHERE 里加条件（那会连分组里
    其它行一起过滤掉），而是**条件聚合**。写错这一层会静默给出错误数字，
    所以分支要覆盖全：

        SUM(x)            + 过滤 → SUM(CASE WHEN c THEN x END)
        COUNT(*)          + 过滤 → SUM(CASE WHEN c THEN 1 ELSE 0 END)
        COUNT(DISTINCT x) + 过滤 → COUNT(DISTINCT CASE WHEN c THEN x END)
    """
    if not conds:
        if agg == "COUNT_DISTINCT":
            return f"COUNT(DISTINCT {expr})"
        if agg == "COUNT" and expr_empty:
            return "COUNT(*)"
        return f"{agg}({expr})"

    cond = " AND ".join(conds)
    if agg == "COUNT" and expr_empty:
        return f"SUM(CASE WHEN {cond} THEN 1 ELSE 0 END)"
    if agg == "COUNT_DISTINCT":
        return f"COUNT(DISTINCT CASE WHEN {cond} THEN {expr} END)"
    return f"{agg}(CASE WHEN {cond} THEN {expr} END)"


def _qualify_columns(expr: str, columns: Iterable[str], alias: str) -> str:
    """给表达式里的**裸列名**补上表别名。

    dbt 的维度 `expr` 是裸列名或裸列名组成的表达式（`substr(order_date,1,7)`），
    而我们的编译器按 `别名.列` 拼接——多表 JOIN 时 `order_date` 可能歧义。

    靠什么知道哪些标识符是列？**靠 dbt 模型 YAML 里的 `columns:`**。
    这正是它存在的意义：不连库也能知道表结构。所以：

    * 命中 `columns` 的裸标识符 → 加 `别名.` 前缀
    * 已经限定的（`a.b`）、函数调用（`f(x)`）、SQL 关键字 → 不动
    * 没声明 `columns` 时不知道是不是列 → **宁可不动**（猜错会生成查不动的
      SQL，比不补更糟），并发一条提示让作者把 `columns:` 补上
    """
    col_set = set(columns)
    if not col_set:
        return expr.strip()

    holes: list[str] = []

    def hide(m: re.Match[str]) -> str:
        holes.append(m.group(0))
        return f"\x00{len(holes) - 1}\x00"

    masked = _STRING_LITERAL.sub(hide, expr)
    out: list[str] = []
    i, n = 0, len(masked)
    while i < n:
        ch = masked[i]
        if ch == "\x00":
            j = masked.index("\x00", i + 1)
            out.append(holes[int(masked[i + 1:j])])
            i = j + 1
            continue
        m = _IDENT.match(masked, i)
        if not m:
            out.append(ch)
            i += 1
            continue
        word = m.group(0)
        start, end = m.span()
        prev = masked[start - 1] if start else ""
        nxt = masked[end] if end < n else ""
        if prev == "." or nxt == "(" or word.lower() in _SQL_RESERVED:
            out.append(word)
        elif word in col_set:
            out.append(f"{alias}.{word}")
        else:
            out.append(word)
        i = end
    return "".join(out)


def _normalize_sql_ws(expr: str) -> str:
    """折叠表达式里的空白（多行 YAML 写成一段，读起来清楚，编译时归一）。

    为什么需要：`expression` 常写成多行（人们会为了可读性换行），YAML 块标量
    还会把缩进更多的行按换行保留。这些换行进入 SQL 文本后会**参与缓存指纹**，
    让"同一口径、只是排版不同"的两份文件产生两个缓存条目。

    **字符串字面量里的空白不能动**（`'已 支付'` 与 `'已支付'` 是两个值）。
    """
    out: list[str] = []
    i, n = 0, len(expr)
    while i < n:
        m = _STRING_LITERAL.match(expr, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        ch = expr[i]
        if ch.isspace():
            j = i
            while j < n and expr[j].isspace():
                j += 1
            prev = out[-1][-1] if out else ""
            nxt = expr[j] if j < n else ""
            # 只去掉括号旁边的空格（`f( x )` → `f(x)`）。
            # **逗号后的空格要保留**——`f(a, b)` 是通行写法，
            # 去掉会让人以为归一化把 SQL 弄坏了。
            if prev and nxt and prev != "(" and nxt not in "),":
                out.append(" ")
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out).strip()


def _starts_with_agg(expr: str, calc: str) -> bool:
    """表达式是否已经以该 calculation_method 对应的聚合函数开头。

    只看**最外层**：`SUM(x)` / `SUM (x)` / `sum(x)` 都算；
    `x + SUM(y)` 不算（它确实需要被包一层）。
    """
    fn = _LEGACY_AGG.get(calc)
    if not fn:
        return False
    want = "COUNT" if fn == "COUNT_DISTINCT" else fn
    return bool(re.match(r"^\s*" + want + r"\s*\(", expr, re.I))


def _metric_ref(v: Any, *, where: str) -> str:
    """`numerator: net_revenue` 或 `numerator: {name: net_revenue}` → 指标名。"""
    if isinstance(v, str) and v.strip():
        return v.strip()
    if isinstance(v, dict) and v.get("name"):
        return str(v["name"])
    raise DbtMetricsError(f"{where} 必须是指标名或 {{name: 指标名}}")


def _strip_alias(expr: str, alias: str) -> str:
    """`f.order_id` → `order_id`（已带别名前缀时去掉，避免 `f.f.order_id`）。"""
    e = expr.strip()
    if alias and e.startswith(alias + "."):
        return e[len(alias) + 1:]
    return e


def _owner(meta: Any) -> str:
    return str(meta.get("owner") or "") if isinstance(meta, dict) else ""


# --------------------------------------------------------------------- 结构
class _ModelTable:
    """一个语义模型（≈ 一张表）的原始声明。"""

    __slots__ = ("name", "table", "meta", "dimensions", "entity_defs",
                 "measures", "columns", "file", "raw")

    def __init__(self, sm: dict[str, Any]) -> None:
        self.raw = sm
        self.file = str(sm.get("__file__") or "")
        self.name = str(sm.get("name") or "").strip()
        self.table = _ref_name(sm.get("model") or sm.get("relation")
                               or self.name) or self.name
        self.meta = sm.get("meta") or {}
        if isinstance(sm.get("config"), dict) and sm["config"].get("meta"):
            self.meta = {**self.meta, **sm["config"]["meta"]}
        self.columns = [c for c in _as_list(sm.get("__columns__") or
                                            sm.get("columns"))
                        if isinstance(c, dict)]
        self.dimensions = [
            d for d in _as_list(sm.get("dimensions")) if isinstance(d, dict)
        ]
        self.entity_defs = [
            e for e in _as_list(sm.get("entities")) if isinstance(e, dict)
        ]
        self.measures = [
            x for x in _as_list(sm.get("measures")) if isinstance(x, dict)
        ]

    @property
    def column_names(self) -> set[str]:
        """这张表的列名集合（用于给裸列名补别名）。

        来源：`columns:` 的列名 + 维度声明的名字。维度名就是列名是 dbt 的
        常态（`- name: order_status` 既是列也是维度），所以两者都收。
        """
        out = {str(c.get("name")) for c in self.columns if c.get("name")}
        for d in self.dimensions:
            n = str(d.get("name") or "")
            # 维度名可能是语义别名（如 `month`），只有当 expr 恰好是它自己时
            # 才说明它也是列名
            if n and str(d.get("expr") or n) == n:
                out.add(n)
        return out

    @property
    def alias(self) -> str:
        return str(_ext(self.meta).get("alias") or _default_alias(self.table))

    def primary_entities(self) -> list[tuple[str, str]]:
        out = []
        for e in self.entity_defs:
            if str(e.get("type") or "").lower() == "primary":
                n = str(e.get("name") or "")
                out.append((n, str(e.get("expr") or n)))
        return out

    def foreign_entities(self) -> list[dict[str, Any]]:
        return [e for e in self.entity_defs
                if str(e.get("type") or "").lower() == "foreign"]


class _Tables:
    """按出现顺序保存语义模型，保证输出确定性。"""

    def __init__(self) -> None:
        self._items: list[_ModelTable] = []
        self._by_name: dict[str, _ModelTable] = {}

    def add(self, sm: dict[str, Any]) -> _ModelTable | None:
        t = _ModelTable(sm)
        if not t.name or not t.table:
            return None
        if t.name in self._by_name:
            return self._by_name[t.name]
        self._by_name[t.name] = t
        self._items.append(t)
        return t

    def in_order(self) -> list[_ModelTable]:
        return list(self._items)

    def by_name(self, name: str) -> _ModelTable | None:
        return self._by_name.get(name)


@dataclass
class _Sites:
    """从若干 YAML 文档里收集到的声明。"""

    semantic_models: list[dict[str, Any]] = field(default_factory=list)
    metrics: list[dict[str, Any]] = field(default_factory=list)
    specs: list[str] = field(default_factory=list)


def _col_to_dim(c: dict[str, Any]) -> dict[str, Any]:
    """`columns[]` 上的 `dimension:` 配置 → 语义模型维度声明。"""
    cfg = c.get("dimension") if isinstance(c.get("dimension"), dict) else {}
    out: dict[str, Any] = {
        "name": c.get("name"),
        "expr": cfg.get("expr") or c.get("name"),
        "description": c.get("description") or "",
    }
    if cfg.get("type"):
        out["type"] = cfg["type"]
    if c.get("meta"):
        out["meta"] = c["meta"]
    if cfg.get("meta"):
        out["meta"] = {**(out.get("meta") or {}), **cfg["meta"]}
    return out


def _col_to_entity(c: dict[str, Any]) -> dict[str, Any]:
    cfg = c.get("entity") if isinstance(c.get("entity"), dict) else {}
    out: dict[str, Any] = {
        "name": cfg.get("name") or c.get("name"),
        "type": cfg.get("type") or "foreign",
        "expr": cfg.get("expr") or c.get("name"),
    }
    if c.get("meta"):
        out["meta"] = c["meta"]
    return out


def _collect_sites(docs: list[tuple[str, dict[str, Any]]]) -> _Sites:
    """把多份 YAML 里散落的声明收集到一起（与 dbt 的加载方式一致）。

    一个文件里可能出现：
        metrics:         指标列表（legacy 或 latest）
        semantic_models: MetricFlow 语义模型
        models:          dbt 模型；里面可能有 semantic_model 与 metrics
    """
    sites = _Sites()
    for path, doc in docs:
        for sm in _as_list(doc.get("semantic_models")):
            if isinstance(sm, dict):
                sites.semantic_models.append({**sm, "__file__": path})

        for mo in _as_list(doc.get("models")):
            if not isinstance(mo, dict):
                continue
            inner = mo.get("semantic_model")
            if isinstance(inner, dict) and inner.get("enabled", True):
                merged: dict[str, Any] = {**inner}
                merged.setdefault("name", mo.get("name"))
                merged.setdefault("model", mo.get("name"))
                cols = [c for c in _as_list(mo.get("columns"))
                        if isinstance(c, dict)]
                merged["__columns__"] = cols
                merged["dimensions"] = [
                    *_as_list(merged.get("dimensions")),
                    *[_col_to_dim(c) for c in cols if c.get("dimension")],
                ]
                merged["entities"] = [
                    *_as_list(merged.get("entities")),
                    *[_col_to_entity(c) for c in cols if c.get("entity")],
                ]
                merged["__file__"] = path
                sites.semantic_models.append(merged)
            for m in _as_list(mo.get("metrics")):
                if isinstance(m, dict):
                    sites.metrics.append(m)
                    sites.specs.append(_spec_of(m))

        for m in _as_list(doc.get("metrics")):
            if isinstance(m, dict):
                sites.metrics.append(m)
                sites.specs.append(_spec_of(m))
    return sites


def _spec_of(m: dict[str, Any]) -> str:
    return "latest" if (m.get("type") and not m.get("calculation_method")) \
        else "legacy"


# --------------------------------------------------------------------- 加载器
class DbtMetricsLoader:
    """读 dbt 指标 YAML，产出 `SemanticModel`。

    与 `PluginLoader` 同构：结果模型返回，过程数据放 `self.report`。
    """

    def __init__(self, *, source_id: str, base_table: str | None = None,
                 base_alias: str = "f", version: str | None = None) -> None:
        self.source_id = source_id
        self.base_table = base_table
        self.base_alias = base_alias
        self.version = version
        self.report = LoadReport()
        # 计数器：只用来把"每条指标都会触发"的提示汇总成一条
        self._time_grains_seen = 0

    # ------------------------------------------------------------ 入口
    def load(self, paths: str | os.PathLike[str]
             | Iterable[str | os.PathLike[str]]) -> SemanticModel:
        if isinstance(paths, (str, os.PathLike)):
            paths = [paths]
        files = [os.fspath(p) for p in paths]
        if not files:
            raise DbtMetricsError("未提供任何 YAML 文件")

        self.report = LoadReport()
        self._time_grains_seen = 0
        docs: list[tuple[str, dict[str, Any]]] = []
        for p in files:
            doc = _read_yaml(p)
            self.report.files.append(p)
            if not isinstance(doc, dict):
                raise DbtMetricsError(
                    f"{p}: 顶层必须是映射（dbt YAML 的形状），"
                    f"得到 {type(doc).__name__}"
                )
            docs.append((p, doc))

        return self._build(_collect_sites(docs))

    # ------------------------------------------------------------ 构建
    def _build(self, sites: _Sites) -> SemanticModel:
        rep = self.report
        rep.specs = sorted(set(sites.specs))

        # ---- 1) 语义模型 ----
        tables = _Tables()
        for sm in sites.semantic_models:
            t = tables.add(sm)
            if t is None:
                rep.warnings.append(
                    f"{sm.get('__file__', '?')}: 语义模型缺 name/model，已跳过"
                )
        base = self._pick_base(tables)
        base_alias = str(_ext(base.meta).get("alias") or self.base_alias or "f")

        # ---- 2) 维度 ----
        dims: dict[str, DimensionDefinition] = {}
        for t in tables.in_order():
            is_base = t is base
            alias = base_alias if is_base else t.alias
            self._check_keys(t.raw, "semantic_model", f"semantic_model[{t.name}]")
            if t.dimensions and not t.column_names:
                rep.warnings.append(
                    f"{t.name}: 没有声明 `columns:`，维度表达式里的裸列名不会被"
                    f"补表别名。单表查询没问题，多表 JOIN 时可能报「列名歧义」"
                    f"——补上 models[].columns 即可"
                )
            for d in t.dimensions:
                dd = self._dimension(d, table=t, alias=alias, is_base=is_base)
                if dd.name in dims:
                    rep.warnings.append(
                        f"维度 {dd.name} 重复定义（{t.name} 的覆盖前一个）"
                    )
                dims[dd.name] = dd

        # ---- 3) 实体 → JOIN ----
        joins, table_joins = self._derive_joins(tables, base, base_alias)

        # ---- 4) 指标 ----
        # 先把全集喂给重写器，否则前向引用（写在后面的指标被前面引用）
        # 换不成 `{{ metric(...) }}`，派生指标就会静默失效。
        all_metric_names = {
            str(m.get("name")) for m in sites.metrics if m.get("name")
        }
        metrics: dict[str, MetricDefinition] = {}
        for m in sites.metrics:
            md = self._metric(m, dim_names=set(dims),
                              metric_names=all_metric_names,
                              base_columns=base.column_names,
                              base_alias=base_alias)
            if md is not None:
                metrics[md.name] = md

        # ---- 5) 装配 ----
        ext = _ext(base.meta)
        model = SemanticModel(
            source_id=self.source_id,
            version=str(self.version or ext.get("version") or "dbt-v1"),
            base_table=base.table,
            base_alias=base_alias,
            metrics=metrics,
            dimensions=dims,
            joins=joins,
            table_joins=table_joins,
            entities=self._entity_map(tables),
            synonyms=self._synonyms(sites),
        )
        if ext.get("logical_source"):
            model.logical_source = str(ext["logical_source"]).strip()
            model.logical_tables = tuple(
                str(x) for x in _as_list(ext.get("logical_tables"))
            )
            model.logical_join_key = (
                str(ext["logical_join_key"]) if ext.get("logical_join_key")
                else None
            )
        if ext.get("internal_dimensions"):
            model.entities["__internal_dimensions__"] = ",".join(
                sorted(str(x) for x in _as_list(ext["internal_dimensions"]))
            )

        rep.dimension_count = len(dims)
        rep.join_count = len(joins)
        rep.metric_count = len(metrics)
        if self._time_grains_seen:
            rep.warnings.append(
                f"{self._time_grains_seen} 个指标声明了 timestamp/time_grains，"
                f"不进入语义模型——时间粒度由 TimeRange 决定（问句里表达即可），"
                f"这条不影响口径"
            )
        rep.extension_keys = sorted(set(rep.extension_keys))
        return model

    # ------------------------------------------------------------ 选主表
    def _pick_base(self, tables: _Tables) -> _ModelTable:
        """选事实表（FROM 的主表）。

        规则：显式 `base_table` > 第一个带 primary 实体的模型 > 第一个模型。
        **不去猜"行数最多的表"**——加载器不连库，猜不出来也不该猜。
        """
        items = tables.in_order()
        if self.base_table:
            for t in items:
                if self.base_table in (t.table, t.name):
                    return t
            self.report.warnings.append(
                f"base_table='{self.base_table}' 在文件里找不到对应语义模型，"
                f"已回退到第一个带 primary 实体的模型"
            )
        for t in items:
            if t.primary_entities():
                return t
        if not items:
            raise DbtMetricsError(
                "指标文件里没有可用的语义模型（semantic_models / "
                "models[].semantic_model）。若只想写指标，用 legacy 规范并给每个"
                "指标写 `model: ref('表名')`，同时至少给一个模型加 "
                "`semantic_model:` 声明主键实体"
            )
        return items[0]

    # ------------------------------------------------------------ 维度
    def _dimension(self, d: dict[str, Any], *, table: _ModelTable,
                   alias: str, is_base: bool) -> DimensionDefinition:
        where = f"{table.name}.dimension"
        name = str(d.get("name") or "").strip()
        if not name:
            raise DbtMetricsError(f"{where}: 维度缺 name")
        self._check_keys(d, "dimension", f"{where}[{name}]")

        ext = _ext(d.get("meta"), (d.get("config") or {}).get("meta")
                   if isinstance(d.get("config"), dict) else None)
        self._note_ext(ext)

        raw = d.get("expr")
        if raw is None:
            raw = name
        expr = _translate_jinja(str(raw).strip(), self.report,
                                where=f"{where}({name})")

        # 维度归属哪张表：默认本模型；扩展可显式覆盖（用于"维度在 FROM 的
        # 派生表里，但要标成某张逻辑表"这类情况）
        owner = ext.get("table")
        if owner is None:
            owner_table = None if is_base else table.table
        else:
            owner_table = str(owner) or None

        select = ext.get("select")
        if select:
            expr_final = ""
        else:
            expr_final = _qualify_columns(expr, table.column_names, alias)
            expr_final = _normalize_sql_ws(expr_final)

        return DimensionDefinition(
            name=name,
            expr=expr_final,
            table=owner_table,
            select=str(select).strip() if select else None,
            alias=str(ext["select_alias"]) if ext.get("select_alias") else None,
            description=str(d.get("description") or ""),
            synonyms=[str(x) for x in _as_list(ext.get("synonyms"))],
        )

    # ------------------------------------------------------------ JOIN
    def _derive_joins(self, tables: _Tables, base: _ModelTable,
                      base_alias: str) -> tuple[dict[str, JoinSpec],
                                                dict[str, str]]:
        """实体配对 → JOIN。

        扫描**所有**模型声明的 `foreign` 实体：持有同名 `primary` 实体的模型
        就是连接目标。条件写成 `from_alias.fk = join_alias.pk`。

        只在**直接挂在 FROM 表上**时可用：我们的 `JoinSpec.condition` 是相对
        FROM 别名写的（不是 SQL AST），表达不了"经由中间表"的多跳链。
        所以非 FROM 表上的外键会被记录成警告而不是硬猜——猜错就是错误结果。
        """
        rep = self.report
        primaries: dict[str, tuple[_ModelTable, str]] = {}
        for t in tables.in_order():
            for name, expr in t.primary_entities():
                primaries.setdefault(name, (t, expr))
        # FOREIGN 实体所在表的别名（FROM 表用 base_alias）
        alias_of: dict[str, str] = {t.name: (base_alias if t is base else t.alias)
                                    for t in tables.in_order()}

        joins: dict[str, JoinSpec] = {}
        table_joins: dict[str, str] = {}
        for holder in tables.in_order():
            for e in holder.foreign_entities():
                ename = str(e.get("name") or "")
                self._check_keys(e, "entity", f"{holder.name}.entity[{ename}]")
                pair = primaries.get(ename)
                if pair is None:
                    rep.warnings.append(
                        f"{holder.name}: 外键实体 '{ename}' 找不到对应的 primary "
                        f"实体（需要另一个语义模型用 type: primary 声明它），"
                        f"该 join 未建立"
                    )
                    continue
                target, target_expr = pair
                if target is holder:
                    continue
                if holder is not base:
                    rep.warnings.append(
                        f"{holder.name} → {target.name}（实体 '{ename}'）是多跳"
                        f"外键，当前 JoinSpec 只能相对 FROM 表写条件，未自动建立；"
                        f"请在 {holder.name} 的 `meta.{EXTENSION_KEY}.joins` 里显式声明"
                    )
                    continue

                jalias = str(_ext(e.get("meta")).get("alias")
                             or _ext(target.meta).get("alias")
                             or target.alias)
                lk = _strip_alias(str(e.get("expr") or ename), base_alias)
                rk = _strip_alias(target_expr, jalias)
                kind = str(e.get("kind") or "left").lower()
                if kind not in ("left", "inner"):
                    kind = "left"
                joins[target.name] = JoinSpec(
                    name=target.name, table=f"{target.table} {jalias}",
                    condition=f"{base_alias}.{lk} = {jalias}.{rk}", kind=kind,
                )
                table_joins[target.table] = target.name
                rep.entity_joins += 1

        # 手工 join（结构扩展）：dbt 的实体表达不了的情形
        for name, spec in (_ext(base.meta).get("joins") or {}).items():
            if not isinstance(spec, dict):
                continue
            self._note_ext({"joins": True})
            tbl = str(spec.get("table") or "")
            cond = str(spec.get("condition") or "")
            if not tbl or not cond:
                rep.warnings.append(
                    f"meta.{EXTENSION_KEY}.joins.{name} 缺 table 或 condition，已忽略"
                )
                continue
            joins[name] = JoinSpec(
                name=name, table=tbl, condition=cond,
                kind=str(spec.get("kind") or "left").lower(),
            )
            tname = tbl.split()[0] if tbl.split() else ""
            if tname:
                table_joins[tname] = name
        return joins, table_joins

    def _entity_map(self, tables: _Tables) -> dict[str, str]:
        out: dict[str, str] = {}
        for t in tables.in_order():
            for e in t.entity_defs:
                name = str(e.get("name") or "")
                if name and name not in out:
                    out[name] = f"{t.table}.{str(e.get('expr') or name)}"
        return out

    # ------------------------------------------------------------ 同义词
    def _synonyms(self, sites: _Sites) -> dict[str, str]:
        """汇总 `口语 → 语义名`。

        语义模型里没有"同义词"这个概念，所以它整体属于标注类扩展。
        汇总成一张表是为了让规划器一次查表，而不是遍历所有定义。

        **只发一条汇总警告**：同义词会写在几十个指标/维度上，逐条警告会把
        报告刷成噪声，而"警告太多"等于"没有警告"。
        """
        out: dict[str, str] = {}
        hits = 0

        def eat(meta: Any, target: str) -> None:
            nonlocal hits
            block = _ext(meta)
            if block:
                self._note_ext(block)
            for s in _as_list(block.get("synonyms")):
                if s and target:
                    out.setdefault(str(s), target)
                    hits += 1

        for sm in sites.semantic_models:
            for d in _as_list(sm.get("dimensions")):
                if isinstance(d, dict):
                    eat(d.get("meta"), str(d.get("name") or ""))
            for c in _as_list(sm.get("__columns__")):
                if isinstance(c, dict):
                    eat(c.get("meta"), str(c.get("name") or ""))
        for m in sites.metrics:
            eat(m.get("meta"), str(m.get("name") or ""))

        if hits:
            self.report.warnings.append(
                f"共 {hits} 条同义词写在 meta.{EXTENSION_KEY}.synonyms 里"
                f"——dbt 会忽略这个键（**不影响口径**，只影响中文问句的匹配率）"
            )
        return {k: v for k, v in out.items() if k and v}

    # ------------------------------------------------------------ 指标
    def _metric(self, m: dict[str, Any], *, dim_names: set[str],
                metric_names: set[str], base_columns: set[str] = frozenset(),
                base_alias: str = "f") -> MetricDefinition | None:
        name = str(m.get("name") or "").strip()
        if not name:
            raise DbtMetricsError("metrics: 有一项缺 name")
        where = f"metrics[{name}]"
        self._check_keys(m, "metric", where)

        meta: dict[str, Any] = {}
        if isinstance(m.get("meta"), dict):
            meta.update(m["meta"])
        if isinstance(m.get("config"), dict) and isinstance(
                m["config"].get("meta"), dict):
            meta.update(m["config"]["meta"])
        ext = _ext(meta)

        try:
            expr = self._metric_expr(m, where=where)
        except DbtMetricsError as e:
            self.report.rejected_metrics.append(name)
            self.report.warnings.append(f"{where}: 已跳过 —— {e}")
            return None

        # 两步、顺序不能反：
        #   1) 裸列名/裸指标名 → 内部引用（dbt legacy 的 expression 是纯 SQL）
        #   2) 剩下的裸列名补表别名（filters 里的列往往不是维度，换不成引用）
        # 反过来的话，`f.amount` 已经带前缀，第 1 步就不会再把它换成维度引用，
        # 编译器也就不知道要引入 JOIN。
        expr = _rewrite_bare_identifiers(
            expr, dims=dim_names, metrics=metric_names)
        expr = _translate_jinja(expr, self.report, where=f"{where}.expression")
        expr = _qualify_columns(expr, base_columns, base_alias)
        expr = _normalize_sql_ws(expr)
        if not expr.strip():
            self.report.rejected_metrics.append(name)
            self.report.warnings.append(f"{where}: 表达式为空，已跳过")
            return None

        table = ext.get("table")
        md = MetricDefinition(
            name=name,
            label=str(m.get("label") or name),
            description=str(m.get("description") or ""),
            expr=expr,
            table=str(table) if table else None,
            dimensions=[str(x) for x in _as_list(m.get("dimensions"))],
            filters=[str(x) for x in _as_list(m.get("filters"))],
            version=str(m.get("version") or "1"),
            owner=_owner(meta),
            effective_date=(str(meta["effective_date"])
                            if meta.get("effective_date") else None),
            synonyms=[str(x) for x in _as_list(ext.get("synonyms"))],
            window=bool(ext.get("window")),
            decimals=(int(ext["decimals"]) if ext.get("decimals") is not None
                      else None),
            ratio=bool(ext.get("ratio")),
        )
        self._classify(name, ext)
        if m.get("timestamp") or m.get("time_grains"):
            # 只报一次：这条警告会在几乎每个指标上触发，逐条报会把报告刷成噪声，
            # 而"警告太多"等于"没有警告"。
            self._time_grains_seen += 1
        return md

    def _classify(self, name: str, ext: dict[str, Any]) -> None:
        """把指标分进 原生 / 仅标注 / 结构扩展 三档。"""
        if not ext:
            self.report.native_metrics.append(name)
            return
        self.report.extension_keys.extend(ext.keys())
        if set(ext) <= _ANNOTATION_KEYS:
            self.report.annotated_metrics.append(name)
        else:
            self.report.structural_metrics.append(name)

    def _note_ext(self, ext: Any) -> None:
        if isinstance(ext, dict) and ext:
            self.report.extension_keys.extend(ext.keys())

    def _check_keys(self, obj: dict[str, Any], kind: str,
                    where: str) -> None:
        known = _KNOWN_KEYS.get(kind)
        if not known:
            return
        # 键可能是非字符串（YAML 把 `1:` 解析成整数），排序前统一成字符串
        unknown = sorted({str(k) for k in obj if k not in known})
        if unknown:
            self.report.warnings.append(
                f"{where}: 不认识的键 {unknown} 被忽略"
                f"——如果是写错了字段名请改；若是 YAML 流式写法缺引号导致键被"
                f"逗号拆开，给值加引号即可"
            )

    def _metric_expr(self, m: dict[str, Any], *, where: str) -> str:
        """按规范算出 SQL 聚合表达式。"""
        calc = m.get("calculation_method")
        mtype = m.get("type")

        if mtype and not calc:
            return self._latest_expr(m, where=where)
        if not calc:
            raise DbtMetricsError(
                "缺 calculation_method（legacy 规范）或 type（latest 规范）；"
                "legacy 可用: "
                + ", ".join(sorted({*_LEGACY_AGG, "derived", *_PORTABLE_WARN}))
                + "；latest 可用: simple, derived, ratio"
            )

        calc = str(calc).strip().lower()
        raw = m.get("expression")
        expr = "" if raw is None else str(raw).strip()

        # ---- 派生指标：表达式本身就是其它指标的组合 ----
        if calc == "derived":
            if not expr:
                raise DbtMetricsError("derived 指标必须提供 expression")
            return expr

        # ---- 作者已经把聚合写进 expression 了 ----
        # 这不是刁难：`expression: SUM(x)` 配 `calculation_method: sum` 在真实
        # 文件里常见，再套一层就会得到 `SUM(SUM(x))`——SQL 直接报错或语义错。
        # 检测到就**原样采用**并警告，而不是硬包。
        if expr and _starts_with_agg(expr, calc):
            self.report.warnings.append(
                f"{where}: expression 已自带聚合函数，calculation_method "
                f"'{calc}' 不再重复包裹（否则会变成 {calc.upper()}"
                f"({calc.upper()}(...))）"
            )
            return expr

        # ---- 跨库可移植性存疑的聚合：接受但警告 ----
        if calc in _PORTABLE_WARN:
            self.report.warnings.append(
                f"{where}: calculation_method '{calc}' —— {_PORTABLE_WARN[calc]}"
            )
            self.report.extension_keys.append(f"calculation_method:{calc}")
            return f"{calc.upper()}({expr or '1'})"

        if calc not in _LEGACY_AGG:
            raise DbtMetricsError(
                f"不支持的 calculation_method '{calc}'；可用: "
                + ", ".join(sorted({*_LEGACY_AGG, "derived", *_PORTABLE_WARN}))
            )

        agg = _LEGACY_AGG[calc]
        if calc == "count" and not expr:
            expr = ""          # COUNT(*) —— 数行，不需要列
        elif not expr:
            raise DbtMetricsError(
                f"calculation_method '{calc}' 必须提供 expression"
            )

        conds: list[str] = []
        # `filter:` 是 latest 规范的字段，`filters:` 是 legacy 的。
        # **两种都读**：真实文件里两套写法混用很常见，只认一种就会把过滤条件
        # 静默丢掉——那是错误数字，不是报错。
        flt = m.get("filter")
        if flt:
            conds.append(str(flt).strip())
        conds.extend(
            _legacy_filter_sql(f, where=f"{where}.filters")
            for f in _as_list(m.get("filters"))
        )
        return _wrap_filters(expr, conds, agg=agg, expr_empty=(expr == ""))

    def _latest_expr(self, m: dict[str, Any], *, where: str) -> str:
        """latest 规范（v1.12/v2）的 `type` 分支。"""
        mtype = str(m.get("type") or "").strip().lower()

        if mtype in _UNSUPPORTED_TYPES:
            raise DbtMetricsError(
                f"type '{mtype}' 暂不支持：{_UNSUPPORTED_TYPES[mtype]}"
            )
        if mtype == "simple":
            return self._simple_expr(m, where=where)
        if mtype == "derived":
            expr = str(m.get("expr") or "").strip()
            if not expr:
                raise DbtMetricsError("type: derived 需要 expr")
            # dbt 的 expr 里直接写指标名（`net_revenue - cost_total`）；
            # 换引用的工作在 _rewrite_bare_identifiers 里统一做。
            return expr
        if mtype == "ratio":
            num = _metric_ref(m.get("numerator"), where=f"{where}.numerator")
            den = _metric_ref(m.get("denominator"), where=f"{where}.denominator")
            # NULLIF 保护：分母 0 时返回 NULL 而不是报错/Infinity。
            # 这是口径层面的选择（0 行 → 无意义，不是 0），写进表达式里。
            return ("{{ metric('%s') }} * 1.0"
                    " / NULLIF({{ metric('%s') }}, 0)" % (num, den))
        raise DbtMetricsError(
            f"不支持的 type '{mtype}'；可用: simple, derived, ratio"
            f"（不支持: {', '.join(sorted(_UNSUPPORTED_TYPES))}）"
        )

    def _simple_expr(self, m: dict[str, Any], *, where: str) -> str:
        """`type: simple` → 聚合表达式。"""
        agg = m.get("agg") or m.get("aggregate")
        measure = None
        # 两种写法都见过：顶层 `measure:` 与 `type_params.measure:`
        for holder in (m, m.get("type_params") or {}):
            if isinstance(holder, dict) and holder.get("measure"):
                measure = str(holder["measure"])
                break
        if agg is None and measure is None:
            raise DbtMetricsError(
                "type: simple 需要 agg（sum/count/count_distinct/average）"
                "或 measure（引用语义模型的 measures）"
            )

        raw = m.get("expr")
        if raw is None and measure:
            raw = measure
        expr = "" if raw is None else str(raw).strip()

        agg_map = {
            "sum": "SUM", "count": "COUNT", "count_distinct": "COUNT_DISTINCT",
            "average": "AVG", "avg": "AVG", "min": "MIN", "max": "MAX",
            "median": "MEDIAN", "percentile": "PERCENTILE",
        }
        agg_l = str(agg or "sum").strip().lower()
        if agg_l not in agg_map:
            raise DbtMetricsError(
                f"不支持的 agg '{agg}'；可用: {', '.join(sorted(agg_map))}"
            )
        if agg_l in _PORTABLE_WARN:
            self.report.warnings.append(
                f"{where}: agg '{agg_l}' —— {_PORTABLE_WARN[agg_l]}"
            )
        if agg_l != "count" and not expr:
            raise DbtMetricsError(f"agg '{agg}' 需要 expr 或 measure")

        conds: list[str] = []
        flt = m.get("filter")
        if flt:
            conds.append(str(flt).strip())
        for f in _as_list(m.get("filters")):
            conds.append(_legacy_filter_sql(f, where=f"{where}.filters"))
        return _wrap_filters(expr, conds, agg=agg_map[agg_l],
                             expr_empty=(expr == ""))


# --------------------------------------------------------------------- 门面
def load_semantic_model(
    paths: str | os.PathLike[str] | Iterable[str | os.PathLike[str]], *,
    source_id: str, base_table: str | None = None, base_alias: str = "f",
    version: str | None = None,
) -> SemanticModel:
    """便捷门面：加载并丢弃报告。需要报告时直接用 `DbtMetricsLoader`。"""
    return DbtMetricsLoader(
        source_id=source_id, base_table=base_table,
        base_alias=base_alias, version=version,
    ).load(paths)


def load_with_report(
    paths: str | os.PathLike[str] | Iterable[str | os.PathLike[str]], *,
    source_id: str, base_table: str | None = None, base_alias: str = "f",
    version: str | None = None,
) -> tuple[SemanticModel, LoadReport]:
    """加载并返回 `(模型, 报告)`。"""
    loader = DbtMetricsLoader(
        source_id=source_id, base_table=base_table,
        base_alias=base_alias, version=version,
    )
    model = loader.load(paths)
    return model, loader.report


# --------------------------------------------------------------------- CLI
def _main(argv: Sequence[str]) -> int:
    """`python -m nl2sql.semantics.dbt_loader <file.yml> [...]`

    存在的意义：写 metrics.yml 的人需要一个**立刻能验证**的手段。
    没有它就只能启动整个服务才知道文件写没写对。
    """
    import argparse
    import sys

    # Windows 控制台默认 GBK，中文报告与符号会直接抛 UnicodeEncodeError。
    # 这是真实踩到的坑：一个只用来"看一眼文件写对没有"的 CLI 不该因为
    # 终端代码页而崩掉。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):  # pragma: no cover
            pass

    ap = argparse.ArgumentParser(
        prog="python -m nl2sql.semantics.dbt_loader",
        description="加载 dbt metrics.yml 并打印可移植性报告",
    )
    ap.add_argument("paths", nargs="+", help="YAML 文件（可多个）")
    ap.add_argument("--source-id", default="shop")
    ap.add_argument("--base-table", default=None,
                    help="指定事实表；不指定则取第一个带 primary 实体的模型")
    ap.add_argument("--base-alias", default="f")
    ap.add_argument("--show-sql", action="store_true",
                    help="顺便打印每个指标展开后的 SQL 表达式")
    args = ap.parse_args(list(argv))

    loader = DbtMetricsLoader(
        source_id=args.source_id, base_table=args.base_table,
        base_alias=args.base_alias,
    )
    try:
        model = loader.load(args.paths)
    except DbtMetricsError as e:
        print(f"加载失败: {e}")
        return 2

    for line in loader.report.lines():
        print(line)
    print()
    print(f"FROM {model.from_clause()[0]} AS {model.base_alias}")
    print(f"指标 {len(model.metrics)}: {', '.join(model.metric_names())}")
    print(f"维度 {len(model.dimensions)}: {', '.join(model.dimension_names())}")
    for j in model.joins.values():
        print(f"JOIN {j.kind.upper()} {j.table} ON {j.condition}")
    if args.show_sql:
        from ..dialect import expand_metric_expr

        print()
        for n in model.metric_names():
            try:
                print(f"  {n} = {expand_metric_expr(n, model)}")
            except Exception as e:  # noqa: BLE001
                print(f"  {n} = <展开失败: {e}>")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    raise SystemExit(_main(sys.argv[1:]))
