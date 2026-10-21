"""依赖注入：请求级取用长生命周期的核心引擎。

**为什么引擎必须是长生命周期的**
    `NL2SQL.connect()` 会装配存储、适配器、元数据目录、知识库与管线，
    其中包含打开 SQLite、建立数据源连接、抽取/读取快照、加载语义模型。
    每个请求重建一遍会非常慢，而且反复开关连接。

    因此引擎在应用启动时创建一次，之后只读复用。

**为什么不用模块级全局**
    核心层禁止模块级可变状态（见 docs/01 的硬约束）。这里把实例挂在
    FastAPI 的 `app.state` 上，由 lifespan 管理创建与关闭——
    既满足"长生命周期"，又不违反核心的无全局状态原则。
"""
from __future__ import annotations

from typing import Any

from fastapi import Request

from nl2sql import NL2SQL

__all__ = ["get_engine", "engine_holder"]


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


def engine_holder(request: Request) -> EngineHolder:
    holder = getattr(request.app.state, "engine_holder", None)
    if holder is None or holder.engine is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail="核心引擎未就绪")
    return holder


def get_engine(request: Request) -> NL2SQL:
    return engine_holder(request).engine  # type: ignore[return-value]


def get_config(request: Request) -> Any:
    return get_engine(request).config
