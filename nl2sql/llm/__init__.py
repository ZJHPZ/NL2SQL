"""模型层：可替换的 LLM 提供者与规划器。

对外暴露：
    `build_provider(...)`    按配置构造模型提供者（不读环境变量）
    `RulePlanner`            确定性规则规划器（零模型依赖）
    `LLMPlanner`             模型驱动规划器（OpenAI 兼容协议）

选型只在装配层发生，链路其余部分不感知用的是哪个。
"""
from __future__ import annotations

from .planners import (
    HybridPlanner,
    LLMPlanner,
    RulePlanner,
    VerifiedHybridPlanner,
)
from .verifier import PlanVerifier, VerificationResult
from .providers import (
    DEFAULT_BASE_URLS,
    DEFAULT_MODELS,
    LLMError,
    MockLLM,
    NullLLM,
    OpenAICompatProvider,
)

__all__ = [
    "RulePlanner",
    "LLMPlanner",
    "HybridPlanner",
    "VerifiedHybridPlanner",
    "PlanVerifier",
    "VerificationResult",
    "OpenAICompatProvider",
    "MockLLM",
    "NullLLM",
    "LLMError",
    "DEFAULT_BASE_URLS",
    "DEFAULT_MODELS",
    "build_provider",
]


def build_provider(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    vendor: str = "deepseek",
    timeout_s: float = 60.0,
) -> object:
    """构造模型提供者。

    刻意要求**显式传入** api_key：本项目的核心层不读环境变量，
    避免"同一个配置在不同机器上行为不同"。壳层负责读环境变量后传入。

    Returns:
        配置齐全时返回 `OpenAICompatProvider`，否则返回 `NullLLM`
        （"没有模型"是正常状态，用 NullLLM 表达而不是抛异常）。
    """
    provider = OpenAICompatProvider(
        api_key=api_key, base_url=base_url,
        model=model or DEFAULT_MODELS.get(vendor, ""),
        vendor=vendor, timeout_s=timeout_s,
    )
    return provider if provider.available() else NullLLM()
