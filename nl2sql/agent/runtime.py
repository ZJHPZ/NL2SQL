"""agent 运行时：循环 + 观测回灌 + 澄清 + 步数控制。

这是把"LLM 调用"升级为"智能体"的核心。分野只有一句话：

    **工具执行的结果要回到模型眼前，模型据此决定下一步。**

当前项目原有的线性流水线（`plan → compile → guard → execute`）会作为
一个内置工具 `nl2sql.solve` 存在，agent 可以选择它，也可以在它失败后
改走别的路（先探语义层、先查表结构、向用户追问）。

设计要点
--------
1. **运行时不知道有哪些工具的具体实现**，只认 `Tool` 契约与 `ToolRegistry`。
   所以"可插拔"不是修辞：换掉任何工具都不需要改这个文件。
2. **每步结果都进历史并回灌**——包括失败。失败信息是 agent 最有价值的输入。
3. **硬上限防打转**：步数上限 + 重复调用检测。没有上限的 agent 循环
   在模型卡住时会烧钱烧到天荒地老。
4. **`clarify.ask` 是回合制的**：工具返回 needs_clarification，
   运行时立即结束本轮并把它作为结果返回给用户，等下一轮带上下文重开。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..contracts.llm import ChatMessage, LLMProvider
from ..contracts.pipeline import QueryOutcome
from ..contracts.session import SessionState, Turn
from ..contracts.tool import Capability, ToolContext, ToolResult
from ..plugins import ToolRegistry

__all__ = ["AgentRuntime", "RunConfig", "Decision", "parse_decision"]


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


_DECISION_PROMPT = """你是一个数据分析智能体的决策核心。

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

## 能被合理理解的问题，直接作答，不要追问

**犹豫时选择作答**，而不是追问。具体地：
  · "哪个最好/最差"这类问题，**用默认口径直接答**（实收金额降序），
    并在回答里写明"这里以实收金额为表现标准"。用户可以再追问细化。
  · "最近"这类相对时间，若上下文里有时间锚点就按它理解，并写明用的哪段时间。
  · 只有**完全无法判断用户要什么**（如"卖了多少？"既没说时间也没说指标）
    才追问。

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


def _to_query_result(data: dict[str, Any], *, source_id: str) -> Any:
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
                final_answer="模型返回的决策格式无法解析，请重试或换个问法。",
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


# --------------------------------------------------------------------- 运行时
class AgentRuntime:
    """工具循环执行器。

    依赖全部构造注入，运行时无全局状态。
    """

    def __init__(self, *, provider: LLMProvider, registry: ToolRegistry,
                 config: RunConfig | None = None,
                 capabilities: tuple[Capability, ...] = (),
                 system_prompt: str = "",
                 schema_brief: str = "") -> None:
        self._provider = provider
        self._registry = registry
        self._config = config or RunConfig()
        self._capabilities = capabilities
        self._system_prompt = system_prompt
        # 库表事实：**直接喂给模型，而不是让它去猜或额外调一次工具**。
        #
        # 起因是内部试用：问"这个库有什么数据"，模型从语义摘要推断出
        # 三张维度表，漏了 dim_product，事实表也只能含糊其辞。
        # 它说得不算错（有依据），但明显不完整——而完整的表清单我们本来就有。
        # 把事实摆在 Prompt 里，比要求模型"先去查"更省一轮调用，也更准。
        self._schema_brief = schema_brief

    @property
    def config(self) -> RunConfig:
        return self._config

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    def set_system_prompt(self, prompt: str) -> None:
        """替换决策 Prompt。

        支撑「Prompt 外置 + 热生效」：用户改完模板文件不必重启进程。
        传空字符串表示恢复内置默认。
        """
        self._system_prompt = prompt or ""

    def set_schema_brief(self, brief: str) -> None:
        """更新库表事实（元数据快照刷新后调用）。"""
        self._schema_brief = brief or ""

    def available(self) -> bool:
        return bool(self._provider is not None and self._provider.available())

    # ------------------------------------------------------------ 主循环
    def run(self, question: str, *, session: SessionState,
            resources: dict[str, Any] | None = None) -> QueryOutcome:
        """跑一轮 agent。

        返回的 `QueryOutcome` 可能是：
            status="ok"                  已给出答案
            status="need_clarification"  agent 在追问，等用户回答
            status="unresolved"          达到步数上限仍未定论
            status="error"               运行时/模型失败
        """
        t0 = time.perf_counter()
        trace: dict[str, int] = {}
        notes: list[str] = []

        if not self.available():
            return QueryOutcome(
                status="error", question=question,
                source_id=session.source_id, error_code="LLM_UNAVAILABLE",
                message="模型不可用，无法运行智能体循环",
                suggestions=["配置 NL2SQL_LLM_API_KEY，或改用 rule 规划器"],
            )

        ctx = ToolContext(
            capabilities=self._capabilities,
            session_id=session.session_id,
            source_id=session.source_id,
            now=session.now,
            question=question,
        )
        for k, v in (resources or {}).items():
            ctx.provide(k, v)

        # 先把**历史**渲染出来，再加入本轮问题。
        # 顺序很重要：回合制澄清靠它把上一轮接上——
        # 若只把本轮问题给模型，用户回答"本月"时模型根本不知道在回答什么。
        history: list[str] = []
        prior = session.render_context(limit=16)
        if prior:
            history.append("=== 之前的对话 ===")
            history.append(prior)
            history.append("=== 继续处理 ===")

        session.add(Turn(role="user", content=question,
                         created_at=_now_iso()))
        history.append(f"用户问题: {question}")
        seen: dict[str, int] = {}
        final: str = ""
        status = "unresolved"
        last_result: Any = None
        # 跟踪是否真的执行过查询。**这是防止编数据的关键机制**：
        # 光靠 Prompt 约束不够（实测模型仍会"说明口径"而不查数），
        # 必须在运行时顶回去。
        query_attempted = False
        query_succeeded = False
        _successful_query_done = False
        result_tool = ""
        # 纠错提示各只给一次。**不能无限给**：模型带着"说明"给出正确
        # 回答时若被反复拦下，会白烧步数并最终报"未得出结论"，
        # 而它其实已经拿到答案了（这是踩过的坑）。
        _used: dict[str, bool] = {}
        kind_nodata = "nodata"
        kind_deferred = "deferred"
        # 是否存在"查询类"工具。硬约束只在这种情况下生效。
        _has_query_tool = any(
            t.spec.kind == "read" for t in self._registry.all()
        )

        for step in range(1, self._config.max_steps + 1):
            s = time.perf_counter()
            decision = self._decide(history)
            trace[f"decide_{step}_ms"] = int((time.perf_counter() - s) * 1000)

            if decision.kind == "clarify":
                status = "need_clarification"
                final = decision.question or "需要你补充一点信息"
                session.pending_question = decision.question
                session.pending_options = list(decision.options)
                session.add(Turn(role="clarify", content=final))
                notes.append("智能体判断信息不足，向你追问")
                break

            if decision.kind == "final":
                # ★ 硬约束：有查询工具、却一次都没试过就想给最终回答 → 顶回去。
                # 实测模型会"说明口径 / 列可用指标"来敷衍，甚至编出带"—"的表格。
                # 这不是能力问题而是行为问题，必须在运行时强制，不能只靠 Prompt。
                #
                # 只在**存在查询类工具**时生效：否则（如纯净的探索场景或
                # 装配了自定义工具集的场景）会把正常流程也顶回去。
                if (_has_query_tool and not query_attempted
                        and not _successful_query_done
                        and not _looks_like_no_data_answer(decision.final_answer)
                        and not _used.get(kind_nodata)):
                    history.append(
                        "系统: 你还没有执行任何查询就给出了回答。"
                        "如果用户要的是数据，请先调用查询工具拿到真实数字；"
                        "如果你确实无法查询，请说明原因。"
                        "不要用占位符或凭印象编造数字。"
                    )
                    notes.append("智能体试图未查询即作答，已要求其先取数")
                    _used[kind_nodata] = True
                    continue

                # 过程叙述被误当成最终回答：模型有时把"我这就去查"写成 final。
                # 这类回答对用户毫无价值，且会掩盖"其实没查"的事实。
                if _is_narration(decision.final_answer):
                    history.append(
                        "系统: 你给出的不是完整答复，而是过程叙述。"
                        "请直接调用查询工具取得数据后，再给出包含真实数字的最终回答。"
                    )
                    notes.append("智能体以过程叙述充当回答，已要求其真正执行")
                    continue

                # ★ 更隐蔽的一种：**用答非所问的结果冒充回答，并承诺"我再查一次"**。
                # 内部试用里真实出现过：用户问"消费最高的前5个客户"，
                # 模型按地区汇总后回答"未能给出客户姓名，如需精确到客户请告知"
                # ——它自己知道结果不对，却把决定权推给用户，白烧一轮 token。
                #
                # **但这条守卫只能触发一次**（踩过的坑）：
                # 第一版不加次数限制，结果模型带着"说明"给出正确回答时也被反复拦下，
                # 白白烧完 8 步报"未得出结论"——本来已经拿到正确答案了。
                # 纠错提示可以给一次，但不能变成死循环。
                if (query_attempted and not _used.get(kind_deferred)
                        and step < self._config.max_steps
                        and _is_deferred_promise(decision.final_answer)):
                    history.append(
                        "系统: 你自己也指出上一次查询没有回答用户的问题。"
                        "既然如此，请**现在**就用正确的维度或指标重新查询，"
                        "不要停下来询问用户是否要继续——用户已经问了。"
                    )
                    notes.append("智能体承认结果不对却停手，已要求其继续查询")
                    _used[kind_deferred] = True
                    continue

                status = "ok"
                final = decision.final_answer or "（模型未给出内容）"
                session.add(Turn(role="assistant", content=final))
                break

            # ---- 调用工具
            tool_name = decision.tool
            if not tool_name:
                history.append("系统: 你的上一步没有指定工具名，请给出 tool 字段。")
                continue

            # 只要模型**尝试**调用工具就算尝试过。
            # 否则"调了不存在的工具"会导致它随后被硬约束反复顶回去、
            # 白白烧掉步数——那是把"没试"和"试了没成"混为一谈。
            query_attempted = True

            if not self._registry.has(tool_name):
                avail = "、".join(self._registry.names())
                obs = f"[失败] 工具不存在: {tool_name}。可用工具: {avail}"
                history.append(f"观察: {obs}")
                notes.append(f"模型请求了不存在的工具 {tool_name}")
                continue

            # 打转检测：同样调用重复太多次就停
            sig = f"{tool_name}:{json.dumps(decision.args, sort_keys=True, default=str)}"
            seen[sig] = seen.get(sig, 0) + 1
            if seen[sig] > self._config.max_repeat:
                status = "unresolved"
                final = ("智能体在重复同样的工具调用而无法推进，"
                         "已停止以避免继续消耗。请换个问法或补充信息。")
                notes.append(f"检测到重复调用 {tool_name} 达 {seen[sig]} 次，已中断")
                session.add(Turn(role="assistant", content=final))
                break

            s = time.perf_counter()
            result = self._invoke(tool_name, decision.args, ctx)
            trace[f"tool_{step}_ms"] = int((time.perf_counter() - s) * 1000)

            session.add(Turn(
                role="tool", tool_name=tool_name, tool_args=decision.args,
                ok=result.ok, content=result.to_observation(),
                created_at=_now_iso(),
            ))

            if result.needs_clarification:
                status = "need_clarification"
                final = result.question or "需要你补充一点信息"
                session.pending_question = result.question
                session.pending_options = list(result.options)
                session.add(Turn(role="clarify", content=final))
                break

            if result.ok and result.data is not None:
                if _carries_query_data(result.data):
                    query_succeeded = True
                    _successful_query_done = True
                    # 保留**最丰富**的那份结果，而不是最后一份。
                    # agent 多步时常常先小查一下再正式查；若一律用最后一份，
                    # 可能把中间用于探路的空结果当成最终结果给用户看。
                    if _is_richer(result.data, last_result):
                        last_result = result.data
                        result_tool = tool_name
                elif last_result is None:
                    # 没有表格数据时，也让最终结果有个兜底（如纯文本工具输出）
                    last_result = result.data

            obs = result.to_observation()
            if len(obs) > self._config.max_observation_chars:
                obs = obs[: self._config.max_observation_chars] + "…（已截断）"
            history.append(f"调用工具: {tool_name} 参数: "
                           f"{json.dumps(decision.args, ensure_ascii=False, default=str)}")
            history.append(f"观察: {obs}")
            if decision.thought:
                history.append(f"（你的推理: {decision.thought}）")
        else:
            # 循环跑满仍未 final。
            # **不能一律说"未得出结论"**：可能已经查到了结果，只是模型没
            # 收敛成最终回答。那种情况下把结果表交出去、并如实说明，
            # 比报一句"没结论"有用得多（踩过的坑：明明拿到了正确答案）。
            status = "unresolved"
            if _carries_query_data(last_result):
                final = ("已经查到数据（见下方结果表），但智能体在步数上限内"
                         "没有收敛成最终结论。结果本身是有效的，可直接使用；"
                         "若需要文字解读，可换个更具体的问法。")
                notes.append("达到步数上限，但已获得有效结果，结果表已附上")
            else:
                final = ("达到了步数上限仍未得出结论。"
                         "可以换个更具体的问法，或缩小时间范围。")
            notes.append(f"达到步数上限 {self._config.max_steps}")

        session.last_status = status
        session.updated_at = _now_iso()
        elapsed = int((time.perf_counter() - t0) * 1000)

        outcome = QueryOutcome(
            status=status,                     # type: ignore[arg-type]
            question=question,
            source_id=session.source_id,
            notes=notes,
            message=final,
            suggestions=(["请回答上面的问题，我会接着处理"] if
                         status == "need_clarification" else []),
            elapsed_ms=elapsed,
            trace=trace,
        )

        # ★ 关键：把工具拿到的**结构化结果**提升为 outcome.result。
        #
        # 为什么必须做：agent 的最终回答是**散文**（"实收金额为 99,998.5 元"），
        # 但界面需要**结果表**才能排序、过滤、导出。散文里有数字却没有结构，
        # 前端无法还原成表。
        #
        # 这里保留数据、不解析散文：解析自然语言去还原表格既不可靠也没必要——
        # 结构化数据本来就在工具返回值里，我们只是没往下传而已。
        if _carries_query_data(last_result):
            outcome.result = _to_query_result(
                last_result, source_id=session.source_id)
            outcome.sql = last_result.get("sql") or None
            if result_tool and result_tool != "nl2sql.solve":
                notes.append(f"结果表来自工具 {result_tool}")

        if status == "need_clarification":
            outcome.candidates = list(session.pending_options)
        return outcome

    # ------------------------------------------------------------ 决策
    def _decide(self, history: list[str]) -> Decision:
        # registry.all() 返回的是**工具对象**，不是 spec——要取 .spec 再转 schema
        tools_schema = [t.spec.to_json_schema() for t in self._registry.all()]
        if not tools_schema:
            return Decision(kind="final",
                            final_answer="没有可用工具，无法执行查询。")

        system = self._system_prompt or _DECISION_PROMPT
        if self._schema_brief:
            system += ("\n\n## 当前数据源的库表事实（已核对，可直接引用）\n"
                       + self._schema_brief)
        system += (
            "\n\n可用工具（JSON Schema）：\n"
            + json.dumps(tools_schema, ensure_ascii=False, indent=1)
        )
        user = "\n".join(history[-40:])

        try:
            resp = self._provider.chat(
                [ChatMessage(role="system", content=system),
                 ChatMessage(role="user", content=user)],
                temperature=self._config.temperature,
                max_tokens=self._config.max_tokens,
                json_mode=True,
            )
        except Exception as e:  # noqa: BLE001 - 模型故障不该让循环崩掉
            return Decision(
                kind="final",
                final_answer=f"模型调用失败：{e}",
                raw=str(e),
            )
        return parse_decision(resp.text)

    # ------------------------------------------------------------ 工具调用
    def _invoke(self, name: str, args: dict[str, Any],
                ctx: ToolContext) -> ToolResult:
        """调用工具，并把任何异常收敛成 ToolResult。

        **绝不向上抛**：agent 循环需要看到失败并据此决策，
        异常会打断循环、丢失上下文。
        """
        tool = self._registry.get(name)
        started = time.perf_counter()
        try:
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
        ctx.note("tool", {"name": name, "ok": result.ok,
                          "elapsed_ms": result.elapsed_ms})
        return result


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()
