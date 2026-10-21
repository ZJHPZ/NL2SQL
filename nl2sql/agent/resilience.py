"""容错：重试策略与"可重试"分类。

为什么单独一个模块
------------------
重试逻辑写两遍（模型调用一处、工具调用一处）很容易漂移：一处退避一处不退、
一处重试认证错误一处不重试。所以抽成纯函数，由 `machine.AgentCore` 调用，
**两个执行器（loop / langgraph）共用同一实现**——这是"两执行器结果一致"
成立的前提之一。

借鉴来源与取舍
--------------
`langchain-master/libs/langchain_v1/langchain/agents/middleware/_retry.py`
与 `tool_retry.py` / `model_retry.py` 的做法：

    * 可重试异常**分类**，而不是"全部重试"或"全部不重试"
    * 指数退避 + 上限 + ±25% 抖动（避免一批重试同时打回服务端）
    * 次数用尽后把错误**回灌**给模型/用户，而不是静默吞掉

没有照搬的是它们的 middleware 管线：本项目是一个显式状态机（`Route`），
硬护栏写死在 `review` 里，套一层可组合中间件只会多一层抽象。留的是
**策略本身**，不是框架。

默认值刻意保守：宁可少重试，也不要"重试掩盖真问题"或重复副作用。
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any, Callable

__all__ = [
    "RetryPolicy",
    "retry_call",
    "is_retryable_model_error",
    "is_retryable_tool_error",
]


@dataclass(frozen=True)
class RetryPolicy:
    """重试策略。

    `jitter` 打开时延时乘 `1 ± 25%` 的随机抖动；测试里关掉以保证可复现。
    """

    # 首次调用之外**最多**再试几次（与 langchain 的 max_retries 同义）
    max_retries: int = 2
    # 第一次重试前的等待（秒）
    initial_delay: float = 0.5
    # 每次重试的等待倍数：initial * backoff ** n
    backoff_factor: float = 2.0
    # 单次等待上限（秒）
    max_delay: float = 8.0
    jitter: bool = True

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError(f"max_retries 不能为负: {self.max_retries}")
        if self.initial_delay < 0 or self.max_delay < 0:
            raise ValueError("initial_delay / max_delay 不能为负")
        if self.backoff_factor < 0:
            raise ValueError("backoff_factor 不能为负")

    def delay_for(self, retry_number: int) -> float:
        """第 `retry_number` 次重试（0 起）之前应等待的秒数。"""
        delay = self.initial_delay * (self.backoff_factor ** max(0, retry_number))
        delay = min(delay, self.max_delay)
        if self.jitter and delay > 0:
            delay = max(0.0, delay * (1.0 + random.uniform(-0.25, 0.25)))
        return delay


def retry_call(
    fn: Callable[[], Any],
    *,
    policy: RetryPolicy,
    classify: Callable[[BaseException], bool],
    on_retry: Callable[[BaseException, int, float], None] | None = None,
) -> tuple[Any, int]:
    """调用 `fn()`，只在 `classify(exc)` 为真时按策略重试。

    返回 `(结果, 实际重试次数)`。次数用尽或不可重试时**原样抛出**最后一个
    异常——由调用方决定怎么降级，这里不替它做决定。

    `on_retry` 在每次真正重试前回调（异常、第几次重试、将等待秒数），
    用于留痕（事件 / trace / notes）。回调自己抛异常不影响重试。
    """
    retries = 0
    while True:
        try:
            return fn(), retries
        except BaseException as exc:  # noqa: BLE001 - 分类交给调用方
            if retries >= policy.max_retries or not classify(exc):
                raise
            delay = policy.delay_for(retries)
            retries += 1
            if on_retry is not None:
                try:
                    on_retry(exc, retries, delay)
                except Exception:  # noqa: BLE001 - 留痕是旁路
                    pass
            if delay > 0:
                time.sleep(delay)


# ------------------------------------------------------------------ 模型错误
# 明确**不该重试**的：认证 / 参数 / 权限 / 模型不存在 / 内容安全。
# 这些重试一百次也是同样的结果，只会让用户多等。
_MODEL_PERMANENT_HINTS = (
    "invalid api key", "incorrect api key", "unauthorized", "authentication",
    "permission", "not installed", "未安装", "model not found",
    "invalid_request", "bad request", "unsupported", "content filter",
    "content policy", "safety", "context_length_exceeded", "context length",
    " 401", " 400", " 403", " 404",
)
# 值得重试的：网络抖动 / 限流 / 服务端 5xx / 连接被重置。
_MODEL_TRANSIENT_HINTS = (
    "timeout", "timed out", "connection", "connect", "reset", "broken pipe",
    "temporarily", "overloaded", "rate limit", "too many requests",
    " 429", " 500", " 502", " 503", " 504", "server error", "network",
    "unreachable", "eof",
)
_TRANSIENT_EXC_TYPES = {
    "APIConnectionError", "APITimeoutError", "InternalServerError",
    "RateLimitError", "ServiceUnavailableError", "ConnectError",
    "ReadTimeout", "ConnectTimeout", "RemoteProtocolError",
}


def is_retryable_model_error(exc: BaseException) -> bool:
    """这次模型调用失败，重试有没有意义。"""
    message = f"{type(exc).__name__} {exc}".lower()
    if any(h in message for h in _MODEL_PERMANENT_HINTS):
        return False
    if type(exc).__name__ in _TRANSIENT_EXC_TYPES:
        return True
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    return any(h in message for h in _MODEL_TRANSIENT_HINTS)


# ------------------------------------------------------------------ 工具错误
# 只重试**瞬态**错误。SQL 写错、字段不存在这类是"确定性失败"，
# 重试同一调用只会白等——它们应该作为观察回灌给模型，让它改 SQL。
_TOOL_PERMANENT_HINTS = (
    "syntax error", "no such column", "no such table", "unknown column",
    "doesn't exist", "does not exist", "unknown table", "invalid",
    "permission", "not found", "unsupported", "ambiguous",
)
_TOOL_TRANSIENT_HINTS = (
    "database is locked", "database table is locked", "database is busy",
    "disk i/o error", "temporarily unavailable", "deadlock",
    "lock wait timeout", "server has gone away", "lost connection",
    "connection reset", "broken pipe", "timed out", "timeout",
    "too many connections", "try again",
)


def is_retryable_tool_error(exc: BaseException) -> bool:
    """这次工具失败是不是瞬态的（重试可能成功）。

    注意：判断依据是异常类型与消息，**不看返回值**。很多工具自己把错误
    收敛成 `ToolResult(ok=False)`，那种情况没有"重试"可言（同一个调用、
    同一份数据，结果还会一样），错误会作为观察回灌给模型自行改法。
    """
    message = f"{type(exc).__name__} {exc}".lower()
    if any(h in message for h in _TOOL_PERMANENT_HINTS):
        return False
    if isinstance(exc, (TimeoutError, ConnectionError, BrokenPipeError)):
        return True
    return any(h in message for h in _TOOL_TRANSIENT_HINTS)
