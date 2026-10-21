"""计划验证器：判断一个逻辑计划是否真的回答了用户的问题。

为什么需要它
------------
实测发现（docs/02 第 18 节）：规则规划器会**"错误地自信"**——
它对某些问题给出看似正常但答非所问的结果，状态是 ok，不抛
`SemanticResolutionError`，因此"规则优先 + 模型兜底"的兜底永远不会触发。

崩溃点在于：兜底只覆盖**错误路径**（解析失败），没覆盖**错误的正向结果**。

验证器补的就是这一环：让模型只回答一个**很小的判断问题**——
"这个计划输出的列，是用户要的吗？"——而不是让它从零规划。

设计取舍
--------
* **结构化判定而非自由回答**：要求输出
  `{"verdict": "yes|no", "reasons": [...], "should_include": [...], "should_drop": [...]}`。
  结构化输出可校验、可统计、可回归。
* **判定标准写进 Prompt**：把"过滤值不算输出列""派生指标未被点名不该额外输出"
  这些经验固化成规则，而不是指望模型自己悟。
* **不修计划**：发现不通过就让上层重新规划（`replan`）。
  让验证器顺便改计划会把两个职责混在一起，难调试也难评估。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..contracts.llm import ChatMessage, LLMProvider, PlannerContext
from ..contracts.semantic import LogicalPlan, SemanticModel
from .planners import build_semantic_digest

__all__ = ["PlanVerifier", "VerificationResult"]


_SYSTEM_PROMPT = """你是一个查询计划的审核员。
你的唯一任务：判断给定的**查询计划**是否准确回答了用户的**业务问题**。

计划里有两类内容：
  measures   = 将作为数值输出的指标列
  dimensions = 将作为分组输出的维度列

请逐条核对（任何一条不满足即判 no）：
1. **列匹配**：用户问题明文要求的每个指标/维度，计划里都要有；
   计划里不应有用户没要求的输出列。
   - 仅作为**过滤条件**出现的维度（"华东地区的销售额"里的"地区"）不是输出列；
   - 比率指标的分子分母，用户没点名就不要额外输出。
2. **对比**：问题在对比两个时间段（"本月和上月"），计划必须同时体现两段；
   只体现一段判 no。
3. **粒度**：问题要求"每个品类的前N""各渠道排名""每月累计"这类
   **组内排名 / 累计**语义时，若计划的 measures 里没有对应的
   排名或累计指标（且 dimensions 只是普通分组），判 no。
   ——这一点很重要：普通 GROUP BY 无法表达"组内 TopN"或"累计值"。

注意：不要因为"计划看起来合理"就放行。你的职责是挑出**答非所问**，
但也不要吹毛求疵——语法细节、列名写法、排序方式都不影响判定。

只输出 JSON，不要解释、不要 markdown 代码块：
{"verdict":"yes","reasons":[],"should_include":[],"should_drop":[]}
verdict 只能是 "yes" 或 "no"。
不通过时 reasons 写明原因；should_include 列出缺的列；should_drop 列出多的列。
"""


@dataclass
class VerificationResult:
    """验证结论。"""

    approved: bool
    reasons: list[str] = field(default_factory=list)
    should_include: list[str] = field(default_factory=list)
    should_drop: list[str] = field(default_factory=list)
    raw: str = ""
    latency_ms: int = 0
    failed_open: bool = False   # 验证本身失败（网络/解析），按"放行"处理

    def describe(self) -> str:
        if self.approved:
            return "验证通过"
        bits = []
        if self.reasons:
            bits.append("；".join(self.reasons[:2]))
        if self.should_drop:
            bits.append("多余列: " + "、".join(self.should_drop[:3]))
        if self.should_include:
            bits.append("缺少列: " + "、".join(self.should_include[:3]))
        return "验证不通过 —— " + "；".join(bits)


class PlanVerifier:
    """用模型审核逻辑计划是否切题。"""

    name = "plan-verifier"

    def __init__(self, provider: LLMProvider, model: SemanticModel, *,
                 max_tokens: int = 300, temperature: float = 0.0) -> None:
        self._provider = provider
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._digest = build_semantic_digest(model)
        self.stats = {"checked": 0, "approved": 0, "rejected": 0, "failed_open": 0}

    @property
    def provider(self) -> LLMProvider:
        return self._provider

    def available(self) -> bool:
        return bool(self._provider is not None and self._provider.available())

    def verify(self, question: str, plan: LogicalPlan,
               ctx: PlannerContext | None = None) -> VerificationResult:
        """审核计划。**失败时放行**（fail-open）。

        fail-open 的理由：验证只是质量控制，不是安全边界。
        网络抖动就拒绝所有答案会让工具不可用；而被拒绝的问题
        本来也可能答对。安全边界（只读、LIMIT、超时）在 guard 层，
        那里是 fail-closed 的。
        """
        from .providers import LLMError, OpenAICompatProvider

        if not self.available():
            return VerificationResult(approved=True, failed_open=True,
                                      reasons=["模型不可用，跳过验证"])

        messages = [
            ChatMessage(role="system", content=_SYSTEM_PROMPT),
            ChatMessage(role="user", content=self._build_prompt(question, plan, ctx)),
        ]
        self.stats["checked"] += 1
        try:
            resp = self._provider.chat(
                messages, temperature=self._temperature,
                max_tokens=self._max_tokens, json_mode=True,
            )
        except LLMError as e:
            self.stats["failed_open"] += 1
            return VerificationResult(approved=True, failed_open=True,
                                      reasons=[f"验证调用失败: {e}"])

        try:
            payload = OpenAICompatProvider.parse_json(resp.text)
        except LLMError as e:
            self.stats["failed_open"] += 1
            return VerificationResult(approved=True, failed_open=True,
                                      reasons=[f"验证输出无法解析: {e}"],
                                      raw=resp.text[:200])

        verdict = str(payload.get("verdict", "")).strip().lower()
        approved = verdict in ("yes", "true", "通过", "是")
        result = VerificationResult(
            approved=approved,
            reasons=[str(r) for r in (payload.get("reasons") or [])][:4],
            should_include=[str(x) for x in (payload.get("should_include") or [])][:5],
            should_drop=[str(x) for x in (payload.get("should_drop") or [])][:5],
            raw=resp.text[:400],
            latency_ms=resp.latency_ms,
        )
        self.stats["approved" if approved else "rejected"] += 1
        return result

    # ------------------------------------------------------------ Prompt
    def _build_prompt(self, question: str, plan: LogicalPlan,
                      ctx: PlannerContext | None) -> str:
        measures = [
            f"{m.metric}（输出列名：{m.alias or m.metric}）" for m in plan.measures
        ] or ["（无）"]
        dims = [
            f"{d.dimension}（输出列名：{d.alias or d.dimension}）"
            for d in plan.dimensions
        ] or ["（无）"]
        filters = [
            f"{f.field} {f.op} {f.value}" for f in plan.filters
        ] or ["（无）"]
        tr = plan.time_range
        time_desc = "（无）"
        if tr and (tr.start or tr.end):
            time_desc = f"{tr.start or '*'} ~ {tr.end or '*'}"
            if tr.grain:
                time_desc += f"，粒度 {tr.grain}"
        elif tr and tr.grain:
            time_desc = f"无范围，粒度 {tr.grain}"

        parts = [
            self._digest,
            "",
            f"用户问题: {question}",
            "",
            "待审核的查询计划：",
            f"  输出指标 measures: {measures}",
            f"  输出维度 dimensions: {dims}",
            f"  过滤条件 filters: {filters}",
            f"  时间范围: {time_desc}",
        ]
        if plan.notes:
            parts.append(f"  计划备注: {list(plan.notes)}")
        return "\n".join(parts)

    def describe(self) -> dict[str, Any]:
        return {
            "verifier": self.name,
            "llm_model": getattr(self._provider, "model", ""),
            "available": self.available(),
            "stats": dict(self.stats),
        }
