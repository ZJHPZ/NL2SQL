"""统一异常层次。

原则：**可预期的失败不抛异常，而是落进 QueryOutcome.status**。
异常只用于"调用方用错了"或"环境坏了"这类无法继续的情况。
"""
from __future__ import annotations


class NL2SQLError(Exception):
    """全部核心异常的基类。"""


# ------------------------------------------------------------------ 配置/使用
class ConfigError(NL2SQLError):
    """配置非法（缺少数据源、路径不存在等）。"""


class NotRegisteredError(NL2SQLError):
    """请求的组件未注册（适配器 / 检索器 / DAG 节点实现）。"""


class UnsupportedCapabilityError(NL2SQLError):
    """适配器不具备所需能力（例如对不支持的源请求分页）。"""


# ------------------------------------------------------------------ 数据源
class SourceError(NL2SQLError):
    """数据源相关错误基类。"""


class SourceConnectionError(SourceError):
    """连接失败。"""


class QueryTimeoutError(SourceError):
    """查询超时。"""


class QueryBlockedError(SourceError):
    """被安全策略拦截（非只读语句 / 超限 / 越权）。"""

    def __init__(self, message: str, *, rule: str = "", suggestion: str = "") -> None:
        super().__init__(message)
        self.rule = rule
        self.suggestion = suggestion


# ------------------------------------------------------------------ 语义/知识
class SemanticError(NL2SQLError):
    """语义层错误基类。"""


class MetricNotFoundError(SemanticError):
    """指标未定义。"""


class DimensionNotFoundError(SemanticError):
    """维度未定义。"""


class KnowledgeError(NL2SQLError):
    """知识库错误基类。"""


class VersionNotFoundError(KnowledgeError):
    """请求的知识版本不存在（回滚时可能遇到）。"""


# ------------------------------------------------------------------ 编译
class CompileError(NL2SQLError):
    """逻辑计划 → 方言 SQL 编译失败。"""


# ------------------------------------------------------------------ 任务
class TaskError(NL2SQLError):
    """任务（保存下来的只读 SQL）非法，或任务不存在。

    任务库写的是**项目自己的任务表**，不是业务数据；因此这类错误
    属于"调用方用错了"，直接抛出，不需要像查询失败那样落进 QueryOutcome。
    """
