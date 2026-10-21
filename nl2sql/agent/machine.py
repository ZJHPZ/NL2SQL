"""agent 单步状态机：一次只推进一件事。

为什么把循环体从 `runtime.py` 里搬出来
------------------------------------
M2 要加第二个执行器（LangGraph），而循环体里有 7 道护栏
（无数据作答、过程叙述、答非所问、工具不存在、打转、步数上限、澄清）。
如果两个执行器各写一遍，**差异不会出现在第一次跑通时，而是出现在半年后
某次改护栏时**——改了一边忘了另一边，而且两边都"能跑"。

所以把"一步"抽成 `AgentCore`，两种执行器只负责**决定下一步走哪个节点**：

    顺序执行器   while: decide → review → act
    LangGraph   图: decide → review → act → decide ...

它们调用的 `decide / review / act / finish` 是同一份代码，行为差异只剩
"谁在驱动"这一件事。这也是等价性测试能真正说明问题的前提：
测的不是"两段相似代码"，而是"同一段代码被两种调度方式驱动"。

一步的三段
----------
    decide   问模型：下一步做什么（一次模型调用）
    review   复核模型的答复，**必要时顶回去重来**（护栏都在这里）
    act      真正执行工具，或收尾

`review` 返回四选一，执行器据此决定往哪走：
    "retry"   护栏拦下，已把纠正提示写进历史 → 回去重新 decide
    "tool"    去执行工具
    "final"   已得出结论
    "clarify" 已在追问

状态是**普通 dict**，且所有键都能被 JSON 序列化（工具数据除外）。
这样图状态才可能被 checkpoint 持久化——虽然 M2 默认不启用 checkpoint
（见 `graph.py` 的说明）。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from ..contracts.llm import ChatMessage, LLMProvider
from ..contracts.pipeline import QueryOutcome
from ..contracts.session import SessionState, Turn
from ..contracts.tool import Capability, ToolContext, ToolResult
from ..plugins import ToolRegistry
from .context import compress_prompt_history, compress_session_context
from .resilience import (
    RetryPolicy,
    is_retryable_model_error,
    is_retryable_tool_error,
    retry_call,
)

__all__ = [
    "AgentCore", "RunConfig", "RunContext", "Decision", "parse_decision",
    "Route", "DECISION_PROMPT",
]


# --------------------------------------------------------------------- 配置
@dataclass
class RunConfig:
    """运行时参数。全部可配——这也是"使用者可自定义智能体细节"的一部分。"""

    max_steps: int = 8
    # 同一工具 + 同参数连续调用超过此数视为打转，强制停止
    max_repeat: int = 2
    # 单步工具超时（秒）；工具内部若自己控制超时，这里只是外层保护
    step_timeout_s: float = 60.0
    # 是否把工具列表交给模型自主选择。False 时用固定顺序（降级模式）
    autonomous: bool = True
    # 观察文本进入上下文的总长度上限，防止上下文爆炸
    max_observation_chars: int = 1200
    temperature: float = 0.0
    # 决策输出很短（一个 JSON），但模型有时会先写一段推理再给 JSON，
    # 900 会在长推理时把 JSON 截断。给足余量比省这点 token 划算得多。
    max_tokens: int = 1400
    require_approval_for_writes: bool = True

    # ---- 容错（借 langchain 的 retry/压缩思路，默认值保守）----
    # 模型调用：只对**瞬态**故障（超时/连接/限流/5xx）重试；认证、参数、
    # 内容安全这类重试无意义的错误直接失败。
    llm_max_retries: int = 2
    llm_retry_initial_delay: float = 0.5
    llm_retry_max_delay: float = 8.0
    # 模型输出被截断/不是合法 JSON 时，**带纠正提示**重问几次。
    # 这是决策层的修复，不是网络重试：格式问题往往一次纠正就好。
    llm_repair_attempts: int = 1
    # 工具调用：只对**只读**工具的**瞬态**异常重试（写工具可能不幂等）。
    # SQL 写错、字段不存在不重试——那是确定性失败，错误回灌给模型改。
    tool_max_retries: int = 1
    tool_retry_initial_delay: float = 0.2
    # 决策 Prompt 里历史文本的总预算（字符）。超了先压旧观察，再逐条丢弃。
    max_prompt_chars: int = 14000
    # 最近几条工具观察保留全文；更早的压成一句"做过什么"
    history_keep_observations: int = 3
    # 跨轮会话上下文（`session.render_context`）渲染时的预算
    session_context_chars: int = 4000


class Route:
    """`review` / `act` / `decide` 的返回值。用常量避免拼写漂移。"""

    # decide
    REVIEW = "review"
    EXHAUSTED = "exhausted"
    # review
    RETRY = "retry"
    TOOL = "tool"
    FINAL = "final"
    CLARIFY = "clarify"
    # act
    CONTINUE = "continue"
    STOP = "stop"


# --------------------------------------------------------------------- 决策
@dataclass
class Decision:
    """模型的一步决策。"""

    kind: str                     # "tool" | "final" | "clarify"
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    thought: str = ""
    final_answer: str = ""
    question: str = ""
    options: list[str] = field(default_factory=list)
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转成纯数据，便于放进图状态（checkpoint 需要可序列化）。"""
        return {
            "kind": self.kind, "tool": self.tool, "args": self.args,
            "thought": self.thought, "final_answer": self.final_answer,
            "question": self.question, "options": list(self.options),
            "raw": self.raw,
        }

    @staticmethod
    def from_dict(d: dict[str, Any] | None) -> "Decision":
        if not d:
            return Decision(kind="")
        return Decision(
            kind=str(d.get("kind") or ""),
            tool=str(d.get("tool") or ""),
            args=d.get("args") if isinstance(d.get("args"), dict) else {},
            thought=str(d.get("thought") or ""),
            final_answer=str(d.get("final_answer") or ""),
            question=str(d.get("question") or ""),
            options=[str(o) for o in (d.get("options") or [])],
            raw=str(d.get("raw") or ""),
        )


DECISION_PROMPT = """你是一个数据分析智能体的决策核心。

你的工作方式：**每一步只决定一件事** —— 调用一个工具，或者给出最终回答。
你会看到之前所有工具调用的结果（包括失败信息），请据此决定下一步。

## 最重要的规则

**没有执行过查询就不能回答数据类问题。**
禁止在未取得真实数字的情况下"说明口径""列出可用指标"或给出表格。
表格里出现 "—"、占位符或凭印象编的数字，是严重错误。
用户要的是数字，不是说明。

判断顺序：
1. 用户要具体数字 → 先调用查询工具拿到真实结果 → 再回答。
2. 只有当你确实缺少**无法从数据推断**的关键信息时，才追问。
   这类信息只有两个：**时间范围**和**有歧义的业务口径**。
   其余一律自己查：不知道有哪些字段就调 schema.describe，
   不确定术语就调 knowledge.search。
3. 一次追问要问全：把缺的都说清楚，不要来回问好几轮。

## 什么时候直接答、什么时候先追问

**能被合理理解的问题，直接作答**。具体地：
  · "哪个最好/最差"这类**比较/排名**问题，用默认口径直接答（实收金额降序），
    并在回答里写明"这里以实收金额为表现标准"。用户可以再追问细化。
  · "最近"这类相对时间，若上下文里有时间锚点就按它理解，并写明用的哪段时间。
  · 比较/排名类结论对时间窗口不敏感，可以先给结论并披露口径。

**但有两种情况必须先追问，不要自己猜：**
  · 问**某个指标的具体数值**（"销售额是多少""订单量多少""客单价多少"），
    却没给时间范围、上下文里也没有时间锚点 → 先追问时间范围
    （例如"看哪个时间段：本月 / 2024年上半年 / 全部历史？"）。
    时间范围不同，金额可能差几倍，这是猜错代价最高的一类信息。
  · 用户要什么**完全无法判断**（如"卖了多少？"既没说时间也没说指标）→ 追问。

**预测类问题**（"预测下个季度""下个月会怎样"）数据里没有答案：
先说明无法预测，可以给历史趋势；**不要编造预测数字**。

为什么倾向作答：追问要等用户一轮，而用户往往只是想"先看一眼"。
给出带明确假设的答案，比让用户再打一遍字更有用——
说清假设，用户自己会挑错。

## 业务指标必须走语义层，不要自己拼 SQL

**涉及业务指标（销售额、订单数、客单价、退款率、毛利率等）时，
优先调用 `nl2sql.solve`**，而不是自己写 `SUM(amount)` 这类裸 SQL。

理由：语义层里每个指标都有**明确口径**，裸 SQL 会漏掉它们。例如：
  · "实收金额" = 已支付订单 且 金额 − 优惠
  · 直接写 `SUM(amount)` 会**把退款和未支付订单也算进去**，数字偏大
  · 直接写 `COUNT(*)` 会**把取消的订单也算成订单量**

自己手写 SQL 只在这些情况下允许：
  · 语义层确实没有对应指标（此时可用 `semantic.list_metrics` 确认）
  · 需要语义层表达不了的形态（分组内 TopN、累计、跨期对比）

若你手写了 SQL，请在回答里**说明为什么不用语义层**。

## 不要反复探查

`semantic.list_metrics` 的结果**不会两次之间发生变化**。
如果你已经调用过它、看到了指标清单，就**直接用它**，
不要为了"再确认一下"重复调用——重复调用同样的工具会触发打转保护、
中断本次对话（这真实发生过：模型连查 3 次指标清单后被强制停止）。

指标清单里的 `label`（如"销量""实收金额"）就是它的业务名，
用户的说法与 label 匹配即可直接使用，不需要额外验证。

## 反例（不要这样做）

用户问"2024年上半年各渠道的实收金额是多少"，正确的下一步是
调用查询工具（如 nl2sql.solve）拿到数字，而不是回答：
"实收金额指的是已支付订单的成交金额减去优惠……请问您要看哪个时间范围？"
—— 时间范围用户已经给了。

反方向的反例：用户问"销售额是多少？"（没说时间范围），
正确的下一步是追问"看哪个时间段：本月 / 2024年上半年 / 全部历史？"，
而不是直接汇报全部历史合计——那是替用户猜了一个默认时间范围。

## 输出格式

只输出 JSON，不要解释、不要 markdown 代码块。三种形态之一：

调用工具：
{"kind":"tool","thought":"为什么调它","tool":"工具名","args":{...}}

给出最终回答（**必须先有工具返回的真实数据**）：
{"kind":"final","thought":"依据哪次工具结果","answer":"给用户的回答，包含真实数字"}

追问用户（**仅当缺时间范围或业务口径有歧义**）：
{"kind":"clarify","thought":"为什么必须问","question":"要问的问题","options":["选项1","选项2"]}

## 关于工具结果的两点说明

1. 工具返回的观察文本里，数据可能是**预览**（只展示前几行）。
   文本里会写明总行数；"预览"不代表数据缺失，回答时请基于总行数。
2. `"kind":"final"` 的 answer 必须是**给用户的完整答复**，
   不能是"我先去查询一下"这类过程叙述——那说明你这一步选错了 kind，
   应该用 `"kind":"tool"` 真正去调用查询工具。
"""


def _is_narration(text: str) -> bool:
    """判断回答是否为"过程叙述"而非完整答复。

    真实故障：模型把"我先查询各渠道的核心指标数据。"写成 kind="final"。
    这种回答对用户毫无价值，还会掩盖"其实没查"的事实。
    """
    t = (text or "").strip()
    if not t or len(t) > 40:
        return False
    markers = ("我先", "我来", "我将", "让我先", "接下来", "正在查询",
               "稍等", "马上", "这就去")
    return any(t.startswith(m) for m in markers) or (
        t.endswith(("。", "，")) and any(m in t for m in markers)
    )


def _is_deferred_promise(text: str) -> bool:
    """回答是否"承认没答到 + 把决定权推回用户"。

    真实案例（内部试用）：
        用户问"消费最高的前5个客户是谁？他们分别在哪个地区？"
        模型按地区汇总后回答：
          "本次查询只按地区汇总了金额，没有给出客户姓名。
           …未能给出具体客户姓名。如需精确到客户姓名，请告知。"
    它**自己知道结果不对**，却停下来问用户要不要继续。
    用户已经问了，此时应该继续查——这是模型的行为缺陷，不是能力缺陷。
    """
    t = text or ""
    if len(t) < 20:
        return False
    # ① 明确承认没答到
    admit = ("未能给出", "没有给出", "未包含", "只按", "仅按",
             "没有按", "未按", "并未", "只汇总", "仅汇总")
    # ② 把决定权推回用户
    defer = ("如需", "请告知", "请你确认", "是否需要", "可以进一步",
             "我可进一步", "要不要")
    return any(a in t for a in admit) and any(d in t for d in defer)


def _carries_query_data(data: Any) -> bool:
    """工具结果里是否真的带了查询数据（而不是只有元信息）。

    严格校验类型：**工具结果的形状由插件决定，不能假设**。
    只检查 key 是否存在会在 `{"rows": 5}` 这种形状上翻车。
    """
    return (isinstance(data, dict) and isinstance(data.get("rows"), list)
            and data.get("columns") is not None)


def _is_richer(candidate: Any, current: Any) -> bool:
    """判断哪份结果更值得展示给用户。

    规则：行数多者优先；行数相同则列数多者优先。
    这样"先探路的小查询"不会把"正式的完整结果"顶掉。
    """
    if not _carries_query_data(candidate):
        return False
    if not _carries_query_data(current):
        return True
    c_rows = len(candidate.get("rows") or [])
    k_rows = len(current.get("rows") or [])
    if c_rows != k_rows:
        return c_rows > k_rows
    return len(candidate.get("columns") or []) > len(current.get("columns") or [])


def to_query_result(data: dict[str, Any], *, source_id: str) -> Any:
    """把工具返回的表格数据还原成 `QueryResult`，供界面渲染。

    与 `_compact` 的分工：
        `_compact` 产出"给模型看的观察"（截断、紧凑）
        这里产出"给界面用的结构"（完整、可排序）
    两者都从同一份工具结果出发，但目的不同，所以不共用实现。
    """
    from ..contracts.source import QueryResult

    rows = data.get("rows") or []
    columns = data.get("columns") or []
    if isinstance(columns, list) and columns and not isinstance(columns[0], str):
        columns = [str(c) for c in columns]
    return QueryResult(
        columns=list(columns),
        rows=[tuple(r) if isinstance(r, (list, tuple)) else (r,) for r in rows],
        row_count=int(data.get("row_count", len(rows)) or 0),
        truncated=bool(data.get("truncated")),
        source_id=source_id,
        sql=str(data.get("sql") or ""),
    )


def _looks_like_no_data_answer(text: str) -> bool:
    """回答里是否明确说明"无法查询/没有数据"。

    用于豁免硬约束：确实查不了的时候，agent 应该能如实说明，
    而不是被我们的约束逼着反复重试。
    """
    if not text:
        return False
    markers = ("无法查询", "无法访问", "没有可用", "缺少", "查不到",
               "没有权限", "工具不可用", "不能执行", "数据源不可用")
    return any(m in text for m in markers)


def _salvage_answer(text: str) -> str:
    """从"几乎合法"的 JSON 里把 answer 字段抠出来。

    覆盖两类真实故障：
      * 输出被 max_tokens 截断，JSON 不闭合
      * 模型在 JSON 前后多写了一句解释

    这不是过度防御——它决定用户看到的是答案还是一坨原始 JSON。
    """
    import re

    # 先处理带转义的正常情况
    m = re.search(r'"answer"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if m:
        try:
            return json.loads(f'"{m.group(1)}"')
        except json.JSONDecodeError:
            return m.group(1).replace("\\n", "\n").replace('\\"', '"')
    # 截断在字符串中间：从 answer 后面一直取到结尾。
    # 这里不能用 r'...$' 形式，尾部反斜杠会转义收尾引号。
    m = re.search('"answer"[ \t]*:[ \t]*"(.*)', text, flags=re.S)
    if m:
        tail = m.group(1).rstrip().rstrip("}").rstrip().rstrip('"')
        if tail:
            return tail.replace("\\n", "\n").replace('\\"', '"')
    return ""


_FORMAT_FAIL_ANSWER = "模型返回的决策格式无法解析，请重试或换个问法。"


def _repair_hint(resp: Any, decision: Decision) -> str | None:
    """判断这次模型输出是否需要"带纠正提示重问"，返回提示文本。

    两类**决策层**失败，都不是网络问题，重试（原样再问一遍）没用，
    必须把"错在哪、要怎么改"写进下一轮的 user 消息：

    1. `finish_reason == "length"`：输出被 `max_tokens` 截断。真模型在
       长推理后会撞上这个上限，截断的 JSON 解析出来往往是残缺的 final。
    2. 空回复，或看起来是 JSON 但解析失败（`parse_decision` 的兜底文案）。

    纯文本回答**不修**：`parse_decision` 刻意把它当 final 容忍下来
    （用户至少能看到模型说了什么），强行纠正可能把一次能用的回答弄没了。
    """
    text = (resp.text or "").strip()
    if getattr(resp, "finish_reason", "") == "length":
        return ("你的上一条回复因为长度上限被截断了（finish_reason=length）。"
                "请只输出一个简短的 JSON 决策对象，不要写大段推理或表格。")
    if not text or decision.final_answer == _FORMAT_FAIL_ANSWER:
        snippet = text[:300].replace("\n", " ")
        return ("你的上一条回复不是合法的 JSON 决策。请只输出一个 JSON 对象，"
                "形如 {\"kind\":\"tool\",\"tool\":\"...\",\"args\":{...}} 或 "
                "{\"kind\":\"final\",\"answer\":\"...\"} 或 "
                "{\"kind\":\"clarify\",\"question\":\"...\",\"options\":[...]}，"
                "不要包含任何额外文字。上一条回复开头："
                f"{snippet!r}")
    return None


def parse_decision(text: str) -> Decision:
    """解析模型输出。宽容处理常见的格式偏差。

    真实模型经常在 JSON 外裹一层说明或代码块，所以不能直接 json.loads。
    解析失败时返回一个 `kind="final"`，把原文当回答——**不要让一次格式
    问题导致整个 agent 崩掉**，用户至少能看到模型说了什么。
    """
    raw = (text or "").strip()
    if not raw:
        return Decision(kind="final", final_answer="（模型返回为空）", raw=raw)

    body = raw
    if body.startswith("```"):
        lines = body.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        body = "\n".join(lines).strip()

    payload: dict[str, Any] | None = None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        start, end = body.find("{"), body.rfind("}")
        if 0 <= start < end:
            try:
                payload = json.loads(body[start:end + 1])
            except json.JSONDecodeError:
                payload = None

    if not isinstance(payload, dict):
        # 不是合法 JSON：**先尝试抢救 answer 字段**，再退化为纯文本。
        # 真实模型经常因为截断/多写一句而让 JSON 解析失败，
        # 直接把整段 JSON 原文当回答给用户是很糟的体验
        # （用户会看到 {"kind":"final","thought":"..."} 这种东西）。
        salvaged = _salvage_answer(body)
        if salvaged:
            return Decision(kind="final", final_answer=salvaged, raw=raw)
        if body.lstrip().startswith(("{", "[")):
            return Decision(
                kind="final",
                final_answer=_FORMAT_FAIL_ANSWER,
                raw=raw,
            )
        return Decision(kind="final", final_answer=raw, raw=raw)

    kind = str(payload.get("kind") or payload.get("type") or "").strip().lower()
    thought = str(payload.get("thought") or payload.get("reason") or "")

    if kind in ("final", "answer", "done"):
        return Decision(
            kind="final", thought=thought, raw=raw,
            final_answer=str(payload.get("answer") or payload.get("final") or ""),
        )
    if kind in ("clarify", "ask", "question"):
        opts = payload.get("options") or []
        return Decision(
            kind="clarify", thought=thought, raw=raw,
            question=str(payload.get("question") or payload.get("ask") or ""),
            options=[str(o) for o in opts] if isinstance(opts, list) else [],
        )
    if kind == "tool" or "tool" in payload:
        args = payload.get("args") or payload.get("arguments") or {}
        return Decision(
            kind="tool", thought=thought, raw=raw,
            tool=str(payload.get("tool") or payload.get("name") or ""),
            args=args if isinstance(args, dict) else {},
        )
    return Decision(kind="final", thought=thought, raw=raw, final_answer=raw)


# --------------------------------------------------------------------- 单轮
@dataclass
class RunContext:
    """一次运行里**不入图状态**的东西。

    为什么不放进 state：会话（SessionState）与工具上下文（ToolContext）
    是应用层对象，塞进图状态会让 checkpoint 无法序列化，
    也会把"这一轮推理"和"这段对话的历史"搅在一起。
    图状态只留纯数据，RunContext 随调用一起传入。
    """

    question: str
    session: SessionState
    tool_ctx: ToolContext
    has_query_tool: bool
    # 这一轮的事件接收器（SSE 用）。**按轮存放，不放在 AgentCore 上**：
    # 一个引擎只造一个 AgentCore，但可能同时有两路请求在跑；
    # 把接收器挂在 self 上会让两路事件串到一起，且后到的那路会覆盖前者。
    on_event: Callable[[dict[str, Any]], None] | None = None


class AgentCore:
    """单步状态机。**没有循环**——循环由执行器负责。"""

    def __init__(self, *, provider: LLMProvider, registry: ToolRegistry,
                 config: RunConfig | None = None,
                 capabilities: tuple[Capability, ...] = (),
                 system_prompt: str = "",
                 schema_brief: str = "",
                 on_event: Callable[[dict[str, Any]], None] | None = None,
                 on_tool_call: Callable[[dict[str, Any]], None] | None = None
                 ) -> None:
        self._provider = provider
        self._registry = registry
        self._config = config or RunConfig()
        self._capabilities = capabilities
        self._system_prompt = system_prompt
        # 观测回调：每次工具调用后上报一条记录（M5 的"工具调用统计"）。
        # **与事件回调分开**：事件是给界面的进度流（可以没有订阅者），
        # 观测是给统计的持久化（不依赖是否有人看）。合并会让"没开 SSE
        # 就没有统计"这种坑出现。
        self._on_tool_call = on_tool_call
        # 库表事实：**直接喂给模型，而不是让它去猜或额外调一次工具**。
        #
        # 起因是内部试用：问"这个库有什么数据"，模型从语义摘要推断出
        # 三张维度表，漏了 dim_product，事实表也只能含糊其辞。
        # 它说得不算错（有依据），但明显不完整——而完整的表清单我们本来就有。
        # 把事实摆在 Prompt 里，比要求模型"先去查"更省一轮调用，也更准。
        self._schema_brief = schema_brief
        self._on_event = on_event

    # ------------------------------------------------------------ 只读属性
    @property
    def config(self) -> RunConfig:
        return self._config

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    @property
    def schema_brief(self) -> str:
        return self._schema_brief

    def available(self) -> bool:
        return bool(self._provider is not None and self._provider.available())

    def set_system_prompt(self, prompt: str) -> None:
        """替换决策 Prompt。

        支撑「Prompt 外置 + 热生效」：用户改完模板文件不必重启进程。
        传空字符串表示恢复内置默认。
        """
        self._system_prompt = prompt or ""

    def set_schema_brief(self, brief: str) -> None:
        """更新库表事实（元数据快照刷新后调用）。"""
        self._schema_brief = brief or ""

    def set_on_event(self, cb: Callable[[dict[str, Any]], None] | None) -> None:
        self._on_event = cb

    # 参数名用 `event` 而不是 `kind`：载荷自己也有一个 `kind`
    # （决策类型），同名会让调用点直接 TypeError。
    def emit(self, event: str, *, rctx: "RunContext | None" = None,
             **payload: Any) -> None:
        """发出一个进度事件（供 SSE 使用）。

        优先发给**本轮**的接收器（`rctx.on_event`），没有才回落到
        构造时设的进程级接收器。

        **永远不能因为观察者出错而影响主流程**：事件只是旁路，
        订阅方断线不该让一次查询失败。
        """
        cb = (rctx.on_event if rctx is not None else None) or self._on_event
        if cb is None:
            return
        try:
            cb({"event": event, **payload})
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ 开局
    def begin(self, question: str, *, session: SessionState,
              resources: dict[str, Any] | None = None,
              on_event: Callable[[dict[str, Any]], None] | None = None
              ) -> tuple[dict[str, Any], RunContext]:
        """准备这一轮的初始状态与上下文。"""
        ctx = ToolContext(
            capabilities=self._capabilities,
            session_id=session.session_id,
            source_id=session.source_id,
            now=session.now,
            question=question,
        )
        for k, v in (resources or {}).items():
            ctx.provide(k, v)

        history: list[str] = []
        # 先把**历史**渲染出来，再加入本轮问题。
        # 顺序很重要：回合制澄清靠它把上一轮接上——
        # 若只把本轮问题给模型，用户回答"本月"时模型根本不知道在回答什么。
        #
        # 渲染时做**预算压缩**：会话里持久化的工具输出是完整的（界面要能
        # 追溯"为什么给这个答案"），但喂给模型的只需要最近几条，更早的
        # 按预算截断。持久化与 Prompt 是两种用途，不能共用一个尺寸。
        prior = compress_session_context(
            session.render_context(limit=16),
            max_chars=self._config.session_context_chars,
        )
        if prior:
            history.append("=== 之前的对话 ===")
            history.append(prior)
            history.append("=== 继续处理 ===")

        session.add(Turn(role="user", content=question, created_at=_now_iso()))
        history.append(f"用户问题: {question}")

        state: dict[str, Any] = {
            "question": question,
            "source_id": session.source_id,
            "step": 0,
            "history": history,
            "notes": [],
            "trace": {},
            "seen": {},
            "used": {},
            "decision": None,
            # 跟踪是否真的执行过查询。**这是防止编数据的关键机制**：
            # 光靠 Prompt 约束不够（实测模型仍会"说明口径"而不查数），
            # 必须在运行时顶回去。
            "query_attempted": False,
            "query_succeeded": False,
            "successful_query_done": False,
            "last_result": None,
            "result_tool": "",
            "status": "unresolved",
            "final": "",
            # 是否存在"查询类"工具。硬约束只在这种情况下生效。
            "done": False,
        }
        rctx = RunContext(
            question=question, session=session, tool_ctx=ctx,
            has_query_tool=any(t.spec.kind == "read" for t in self._registry.all()),
            on_event=on_event,
        )
        return state, rctx

    # ------------------------------------------------------------ 一步：决策
    def decide(self, state: dict[str, Any], rctx: RunContext) -> str:
        """问模型下一步。返回 `Route.REVIEW` 或 `Route.EXHAUSTED`。"""
        step = int(state.get("step") or 0) + 1
        state["step"] = step
        if step > self._config.max_steps:
            return Route.EXHAUSTED

        s = time.perf_counter()
        decision = self._ask_model(
            state["history"], rctx=rctx, step=step,
            trace=state["trace"], notes=state["notes"],
        )
        elapsed = int((time.perf_counter() - s) * 1000)
        state["trace"][f"decide_{step}_ms"] = elapsed
        state["decision"] = decision.to_dict()
        self.emit("decision", rctx=rctx, step=step, kind=decision.kind,
                  tool=decision.tool, thought=decision.thought,
                  elapsed_ms=elapsed)
        return Route.REVIEW

    # ------------------------------------------------------------ 一步：复核
    def review(self, state: dict[str, Any], rctx: RunContext) -> str:
        """复核模型这一步的答复，必要时顶回去。

        返回 `Route.RETRY` / `Route.TOOL` / `Route.FINAL` / `Route.CLARIFY`。
        护栏全部在这里——**模型的行为缺陷要用机制顶回去，不能只写进 Prompt**。
        """
        decision = Decision.from_dict(state.get("decision"))
        step = int(state.get("step") or 0)
        history: list[str] = state["history"]
        notes: list[str] = state["notes"]
        used: dict[str, bool] = state["used"]

        if decision.kind == "clarify":
            question = decision.question or "需要你补充一点信息"
            state["status"] = "need_clarification"
            state["final"] = question
            state["done"] = True
            rctx.session.pending_question = decision.question
            rctx.session.pending_options = list(decision.options)
            rctx.session.add(Turn(role="clarify", content=question))
            notes.append("智能体判断信息不足，向你追问")
            self.emit("clarify", rctx=rctx, step=step, question=question)
            return Route.CLARIFY

        if decision.kind != "final":
            if not decision.tool:
                history.append("系统: 你的上一步没有指定工具名，请给出 tool 字段。")
                return Route.RETRY
            return Route.TOOL

        # ---- 以下都是 kind == "final" 的复核

        # ★ 硬约束：有查询工具、却一次都没试过就想给最终回答 → 顶回去。
        # 实测模型会"说明口径 / 列可用指标"来敷衍，甚至编出带"—"的表格。
        # 这不是能力问题而是行为问题，必须在运行时强制，不能只靠 Prompt。
        #
        # 只在**存在查询类工具**时生效：否则（如纯净的探索场景或
        # 装配了自定义工具集的场景）会把正常流程也顶回去。
        if (rctx.has_query_tool and not state["query_attempted"]
                and not state["successful_query_done"]
                and not _looks_like_no_data_answer(decision.final_answer)
                and not used.get("nodata")):
            history.append(
                "系统: 你还没有执行任何查询就给出了回答。"
                "如果用户要的是数据，请先调用查询工具拿到真实数字；"
                "如果你确实无法查询，请说明原因。"
                "不要用占位符或凭印象编造数字。"
            )
            notes.append("智能体试图未查询即作答，已要求其先取数")
            used["nodata"] = True
            self.emit("guard", rctx=rctx, step=step, rule="nodata")
            return Route.RETRY

        # 过程叙述被误当成最终回答：模型有时把"我这就去查"写成 final。
        # 这类回答对用户毫无价值，且会掩盖"其实没查"的事实。
        if _is_narration(decision.final_answer):
            history.append(
                "系统: 你给出的不是完整答复，而是过程叙述。"
                "请直接调用查询工具取得数据后，再给出包含真实数字的最终回答。"
            )
            notes.append("智能体以过程叙述充当回答，已要求其真正执行")
            self.emit("guard", rctx=rctx, step=step, rule="narration")
            return Route.RETRY

        # ★ 更隐蔽的一种：**用答非所问的结果冒充回答，并承诺"我再查一次"**。
        # 内部试用里真实出现过：用户问"消费最高的前5个客户"，
        # 模型按地区汇总后回答"未能给出客户姓名，如需精确到客户请告知"
        # ——它自己知道结果不对，却把决定权推给用户，白烧一轮 token。
        #
        # **但这条守卫只能触发一次**（踩过的坑）：
        # 第一版不加次数限制，结果模型带着"说明"给出正确回答时也被反复拦下，
        # 白白烧完 8 步报"未得出结论"——本来已经拿到正确答案了。
        # 纠错提示可以给一次，但不能变成死循环。
        if (state["query_attempted"] and not used.get("deferred")
                and step < self._config.max_steps
                and _is_deferred_promise(decision.final_answer)):
            history.append(
                "系统: 你自己也指出上一次查询没有回答用户的问题。"
                "既然如此，请**现在**就用正确的维度或指标重新查询，"
                "不要停下来询问用户是否要继续——用户已经问了。"
            )
            notes.append("智能体承认结果不对却停手，已要求其继续查询")
            used["deferred"] = True
            self.emit("guard", rctx=rctx, step=step, rule="deferred")
            return Route.RETRY

        state["status"] = "ok"
        state["final"] = decision.final_answer or "（模型未给出内容）"
        state["done"] = True
        rctx.session.add(Turn(role="assistant", content=state["final"]))
        return Route.FINAL

    # ------------------------------------------------------------ 一步：行动
    def act(self, state: dict[str, Any], rctx: RunContext) -> str:
        """真正调用工具。返回 `Route.CONTINUE` 或 `Route.STOP`。"""
        decision = Decision.from_dict(state.get("decision"))
        step = int(state.get("step") or 0)
        history: list[str] = state["history"]
        notes: list[str] = state["notes"]
        seen: dict[str, int] = state["seen"]
        tool_name = decision.tool

        # 只要模型**尝试**调用工具就算尝试过。
        # 否则"调了不存在的工具"会导致它随后被硬约束反复顶回去、
        # 白白烧掉步数——那是把"没试"和"试了没成"混为一谈。
        state["query_attempted"] = True

        if not self._registry.has(tool_name):
            avail = "、".join(self._registry.names())
            obs = f"[失败] 工具不存在: {tool_name}。可用工具: {avail}"
            history.append(f"观察: {obs}")
            notes.append(f"模型请求了不存在的工具 {tool_name}")
            self.emit("tool_missing", rctx=rctx, step=step, tool=tool_name)
            return Route.CONTINUE

        # 打转检测：同样调用重复太多次就停
        import json as _json

        sig = f"{tool_name}:{_json.dumps(decision.args, sort_keys=True, default=str)}"
        seen[sig] = seen.get(sig, 0) + 1
        if seen[sig] > self._config.max_repeat:
            state["status"] = "unresolved"
            state["final"] = ("智能体在重复同样的工具调用而无法推进，"
                              "已停止以避免继续消耗。请换个问法或补充信息。")
            state["done"] = True
            notes.append(f"检测到重复调用 {tool_name} 达 {seen[sig]} 次，已中断")
            rctx.session.add(Turn(role="assistant", content=state["final"]))
            self.emit("guard", rctx=rctx, step=step, rule="repeat", tool=tool_name)
            return Route.STOP

        s = time.perf_counter()
        result = self.invoke(tool_name, decision.args, rctx.tool_ctx)
        elapsed = int((time.perf_counter() - s) * 1000)
        state["trace"][f"tool_{step}_ms"] = elapsed

        rctx.session.add(Turn(
            role="tool", tool_name=tool_name, tool_args=decision.args,
            ok=result.ok, content=result.to_observation(),
            created_at=_now_iso(),
        ))

        if result.needs_clarification:
            question = result.question or "需要你补充一点信息"
            state["status"] = "need_clarification"
            state["final"] = question
            state["done"] = True
            rctx.session.pending_question = result.question
            rctx.session.pending_options = list(result.options)
            rctx.session.add(Turn(role="clarify", content=question))
            self.emit("clarify", rctx=rctx, step=step, question=question)
            return Route.STOP

        if result.ok and result.data is not None:
            if _carries_query_data(result.data):
                state["query_succeeded"] = True
                state["successful_query_done"] = True
                # 保留**最丰富**的那份结果，而不是最后一份。
                # agent 多步时常常先小查一下再正式查；若一律用最后一份，
                # 可能把中间用于探路的空结果当成最终结果给用户看。
                if _is_richer(result.data, state["last_result"]):
                    state["last_result"] = result.data
                    state["result_tool"] = tool_name
            elif state["last_result"] is None:
                # 没有表格数据时，也让最终结果有个兜底（如纯文本工具输出）
                state["last_result"] = result.data

        obs = result.to_observation()
        if len(obs) > self._config.max_observation_chars:
            obs = obs[: self._config.max_observation_chars] + "…（已截断）"
        history.append(f"调用工具: {tool_name} 参数: "
                       f"{_json.dumps(decision.args, ensure_ascii=False, default=str)}")
        history.append(f"观察: {obs}")
        if decision.thought:
            history.append(f"（你的推理: {decision.thought}）")
        self.emit("tool", rctx=rctx, step=step, tool=tool_name, ok=result.ok,
                  elapsed_ms=elapsed, observation=obs[:400])
        return Route.CONTINUE

    # ------------------------------------------------------------ 收尾
    def finish(self, state: dict[str, Any], rctx: RunContext) -> None:
        """步数跑满仍未收敛时的收尾（只在没有结论时补）。"""
        if state.get("done"):
            return
        state["status"] = "unresolved"
        notes: list[str] = state["notes"]
        # **不能一律说"未得出结论"**：可能已经查到了结果，只是模型没
        # 收敛成最终回答。那种情况下把结果表交出去、并如实说明，
        # 比报一句"没结论"有用得多（踩过的坑：明明拿到了正确答案）。
        if _carries_query_data(state.get("last_result")):
            state["final"] = ("已经查到数据（见下方结果表），但智能体在步数上限内"
                              "没有收敛成最终结论。结果本身是有效的，可直接使用；"
                              "若需要文字解读，可换个更具体的问法。")
            notes.append("达到步数上限，但已获得有效结果，结果表已附上")
        else:
            state["final"] = ("达到了步数上限仍未得出结论。"
                              "可以换个更具体的问法，或缩小时间范围。")
        notes.append(f"达到步数上限 {self._config.max_steps}")
        state["done"] = True

    def to_outcome(self, state: dict[str, Any], rctx: RunContext, *,
                   elapsed_ms: int) -> QueryOutcome:
        """把最终状态转成对外结果。"""
        status = str(state.get("status") or "unresolved")
        session = rctx.session
        session.last_status = status
        session.updated_at = _now_iso()
        notes: list[str] = state["notes"]

        outcome = QueryOutcome(
            status=status,                      # type: ignore[arg-type]
            question=rctx.question,
            source_id=session.source_id,
            notes=notes,
            message=str(state.get("final") or ""),
            suggestions=(["请回答上面的问题，我会接着处理"] if
                         status == "need_clarification" else []),
            elapsed_ms=elapsed_ms,
            trace=dict(state["trace"]),
        )

        # ★ 关键：把工具拿到的**结构化结果**提升为 outcome.result。
        #
        # 为什么必须做：agent 的最终回答是**散文**（"实收金额为 99,998.5 元"），
        # 但界面需要**结果表**才能排序、过滤、导出。散文里有数字却没有结构，
        # 前端无法还原成表。
        #
        # 这里保留数据、不解析散文：解析自然语言去还原表格既不可靠也没必要——
        # 结构化数据本来就在工具返回值里，我们只是没往下传而已。
        last_result = state.get("last_result")
        if _carries_query_data(last_result):
            outcome.result = to_query_result(
                last_result, source_id=session.source_id)
            outcome.sql = last_result.get("sql") or None
            result_tool = str(state.get("result_tool") or "")
            if result_tool and result_tool != "nl2sql.solve":
                notes.append(f"结果表来自工具 {result_tool}")

        if status == "need_clarification":
            outcome.candidates = list(session.pending_options)
        self.emit("outcome", rctx=rctx, status=status,
                  elapsed_ms=elapsed_ms, notes=list(notes))
        return outcome

    # ------------------------------------------------------------ 模型
    def _ask_model(self, history: list[str], *, rctx: "RunContext | None" = None,
                   step: int = 0, trace: dict[str, Any] | None = None,
                   notes: list[str] | None = None) -> Decision:
        # registry.all() 返回的是**工具对象**，不是 spec——要取 .spec 再转 schema
        tools_schema = [t.spec.to_json_schema() for t in self._registry.all()]
        if not tools_schema:
            return Decision(kind="final",
                            final_answer="没有可用工具，无法执行查询。")

        system = self._system_prompt or DECISION_PROMPT
        if self._schema_brief:
            system += ("\n\n## 当前数据源的库表事实（已核对，可直接引用）\n"
                       + self._schema_brief)
        system += (
            "\n\n可用工具（JSON Schema）：\n"
            + json.dumps(tools_schema, ensure_ascii=False, indent=1)
        )
        # 历史按预算压缩（旧观察压成一句、超预算再丢最老的条目）。
        # 只压**喂给模型**的文本，不改 state["history"] 本身——
        # 后者是这一轮的推理轨迹，最终还要用于 notes/追溯。
        compressed = compress_prompt_history(
            history,
            max_chars=self._config.max_prompt_chars,
            keep_observations=self._config.history_keep_observations,
        )
        user = "\n".join(compressed)

        def _note(msg: str) -> None:
            if notes is not None:
                notes.append(msg)

        messages = [ChatMessage(role="system", content=system),
                    ChatMessage(role="user", content=user)]

        # 为什么不用 openai SDK 自带的 retries：
        #   * 它是黑盒——重试了没有、为什么，trace/界面里看不见；
        #   * 它只处理网络/限流，不管"输出被截断""JSON 坏掉"这类
        #     **决策层**失败，而那才是真模型最常见的失败方式。
        # 所以模型调用统一走本项目自己的策略，SDK 的 retries 保持在 2 作为
        # 最底层兜底（两层不会互相放大：内层先重试，外层看到的只是最终错误）。
        policy = RetryPolicy(
            max_retries=self._config.llm_max_retries,
            initial_delay=self._config.llm_retry_initial_delay,
            max_delay=self._config.llm_retry_max_delay,
        )

        def _on_retry(exc: BaseException, attempt: int, delay: float) -> None:
            if trace is not None:
                trace["llm_retries"] = int(trace.get("llm_retries") or 0) + 1
            _note(f"模型调用失败（{type(exc).__name__}: {exc}），"
                  f"第 {attempt} 次重试")
            self.emit("model_retry", rctx=rctx, step=step, attempt=attempt,
                      error=f"{type(exc).__name__}: {exc}"[:200],
                      delay_ms=int(delay * 1000))

        repairs = 0
        while True:
            try:
                resp, _retries = retry_call(
                    lambda: self._provider.chat(
                        messages,
                        temperature=self._config.temperature,
                        max_tokens=self._config.max_tokens,
                        json_mode=True,
                    ),
                    policy=policy,
                    classify=is_retryable_model_error,
                    on_retry=_on_retry,
                )
            except Exception as e:  # noqa: BLE001 - 模型故障不该让循环崩掉
                _note(f"模型调用失败：{e}")
                return Decision(
                    kind="final",
                    final_answer=f"模型调用失败：{e}",
                    raw=str(e),
                )

            decision = parse_decision(resp.text)
            hint = _repair_hint(resp, decision)
            if hint is None or repairs >= self._config.llm_repair_attempts:
                return decision
            repairs += 1
            if trace is not None:
                trace["llm_repairs"] = int(trace.get("llm_repairs") or 0) + 1
            _note("模型输出无法直接使用（"
                  f"{'被截断' if 'length' == getattr(resp, 'finish_reason', '') else '格式不合'}），"
                  "已带纠正提示重问")
            self.emit("model_repair", rctx=rctx, step=step,
                      reason=str(getattr(resp, "finish_reason", "") or "format"))
            messages = messages + [
                ChatMessage(role="assistant", content=(resp.text or "（空回复）")),
                ChatMessage(role="user", content=hint),
            ]


    # ------------------------------------------------------------ 工具调用
    def invoke(self, name: str, args: dict[str, Any],
               ctx: ToolContext) -> ToolResult:
        """调用工具，并把任何异常收敛成 ToolResult。

        **绝不向上抛**：agent 循环需要看到失败并据此决策，
        异常会打断循环、丢失上下文。

        容错只在**只读工具**上做（写工具可能不幂等，重试一次就多写一次），
        且只对瞬态异常（锁等待/连接断开）。SQL 写错这类确定性失败不重试：
        错误会原样回灌给模型，让它改 SQL——那比"偷偷重试同一个坏 SQL"有用。
        """
        tool = self._registry.get(name)
        started = time.perf_counter()
        retries = 0
        try:
            if tool.spec.kind == "read" and self._config.tool_max_retries > 0:
                policy = RetryPolicy(
                    max_retries=self._config.tool_max_retries,
                    initial_delay=self._config.tool_retry_initial_delay,
                    max_delay=max(1.0, self._config.tool_retry_initial_delay * 4),
                )
                result, retries = retry_call(
                    lambda: tool.run(args, ctx),
                    policy=policy,
                    classify=is_retryable_tool_error,
                )
            else:
                result = tool.run(args, ctx)
        except PermissionError as e:
            result = ToolResult(
                ok=False, error=f"权限不足: {e}",
                hint="该工具需要的能力未被授予；检查插件声明与配置",
            )
        except Exception as e:  # noqa: BLE001
            result = ToolResult(
                ok=False, error=f"{type(e).__name__}: {e}",
                hint="工具内部异常；可尝试换一种调用方式或换用其它工具",
            )
        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
        note: dict[str, Any] = {"name": name, "ok": result.ok,
                                "elapsed_ms": result.elapsed_ms}
        if retries:
            # 重试过就要留痕：否则用户看到"这次查询慢"，不知道慢在重试上
            note["retries"] = retries
        ctx.note("tool", note)
        self._report_tool_call(ctx, name, result)
        return result

    def _report_tool_call(self, ctx: ToolContext, name: str,
                          result: ToolResult) -> None:
        """上报一条工具调用观测记录。**观测失败绝不影响查询**。"""
        if self._on_tool_call is None:
            return
        try:
            self._on_tool_call({
                "tool": name,
                "ok": bool(result.ok),
                "elapsed_ms": int(result.elapsed_ms),
                "error": (result.error or "")[:300] or None,
                "session_id": ctx.session_id,
                "source_id": ctx.source_id,
            })
        except Exception:  # noqa: BLE001 - 统计是旁路，不能拖垮主流程
            pass


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()
