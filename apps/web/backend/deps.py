"""依赖注入：请求级取用长生命周期的核心引擎。

**为什么引擎必须是长生命周期的**
    `NL2SQL.connect()` 会装配存储、适配器、元数据目录、知识库与管线，
    其中包含打开 SQLite、建立数据源连接、抽取/读取快照、加载语义模型。
    每个请求重建一遍会非常慢，而且反复开关连接。

    因此引擎在应用启动时创建一次，之后只读复用。

**M7 起新增"项目"维度**：`ProjectHub` 按项目懒加载各自的引擎；
请求用 `X-Project-Id` 头指定项目，缺省 `default`（环境变量连接）。
没有 hub 时（老的测试/嵌入用法）退回单个 `EngineHolder`。

**为什么不用模块级全局**
    核心层禁止模块级可变状态（见 docs/01 的硬约束）。这里把实例挂在
    FastAPI 的 `app.state` 上，由 lifespan 管理创建与关闭——
    既满足"长生命周期"，又不违反核心的无全局状态原则。
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from nl2sql import NL2SQL

__all__ = [
    "PROJECT_HEADER",
    "EngineHolder",
    "ProjectEntryView",
    "current_project_id",
    "get_engine",
    "get_project_entry",
    "engine_holder",
]

PROJECT_HEADER = "X-Project-Id"
DEFAULT_PROJECT_ID = "default"


class EngineHolder:
    """承载启动时装配好的引擎，并记录装配过程中的警告。

    `warnings` 用于把"某数据源连接失败"这类**非致命**问题透出到界面，
    而不是让启动直接失败——个人场景下用户可能只是某个库暂时连不上。
    """

    def __init__(self) -> None:
        self.engine: NL2SQL | None = None
        self.warnings: list[str] = []
        self.started_at: str = ""

    def attach(self, engine: NL2SQL, *, started_at: str) -> None:
        self.engine = engine
        self.started_at = started_at

    def close(self) -> None:
        if self.engine is not None:
            self.engine.close()
            self.engine = None


class ProjectEntryView:
    """把 `ProjectHub` 的引擎条目摊平成路由层爱用的形状。

    字段名刻意与 `EngineHolder` 保持一致（engine/warnings），
    这样 `/api/info` 这类"引擎没起来也要回答"的接口改动最小。
    """

    __slots__ = ("project_id", "engine", "error", "warnings", "is_default")

    def __init__(self, project_id: str, engine: Any = None, error: str = "",
                 warnings: list[str] | None = None,
                 is_default: bool = False) -> None:
        self.project_id = project_id
        self.engine = engine
        self.error = error
        self.warnings = list(warnings or [])
        self.is_default = is_default


def engine_holder(request: Request) -> EngineHolder:
    holder = getattr(request.app.state, "engine_holder", None)
    if holder is None or holder.engine is None:
        raise HTTPException(status_code=503, detail="核心引擎未就绪")
    return holder


def current_project_id(request: Request) -> str:
    """请求头 `X-Project-Id`；缺省 default（环境变量连接的老行为）。"""
    raw = (request.headers.get(PROJECT_HEADER) or "").strip().lower()
    return raw or DEFAULT_PROJECT_ID


def get_project_entry(request: Request) -> ProjectEntryView:
    """取当前请求对应项目的引擎条目（**引擎可能没起来，不在这里抛 503**）。"""
    hub = getattr(request.app.state, "project_hub", None)
    if hub is None:
        holder = getattr(request.app.state, "engine_holder", None)
        if holder is None:
            return ProjectEntryView(DEFAULT_PROJECT_ID, error="核心引擎未就绪",
                                    is_default=True)
        return ProjectEntryView(
            DEFAULT_PROJECT_ID,
            engine=holder.engine,
            error="" if holder.engine is not None else "核心引擎未就绪",
            warnings=holder.warnings,
            is_default=True,
        )
    project_id = current_project_id(request)
    if project_id != DEFAULT_PROJECT_ID and not hub.store.exists(project_id):
        raise HTTPException(status_code=404,
                            detail=f"项目不存在：{project_id}")
    entry = hub.entry(project_id)
    return ProjectEntryView(
        project_id, engine=entry.engine, error=entry.error,
        warnings=entry.warnings, is_default=entry.is_default,
    )


def get_engine(request: Request) -> NL2SQL:
    entry = get_project_entry(request)
    if entry.engine is None:
        raise HTTPException(status_code=503,
                            detail=entry.error or "项目引擎未就绪")
    return entry.engine  # type: ignore[return-value]


def get_config(request: Request) -> Any:
    return get_engine(request).config
