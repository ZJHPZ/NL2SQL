"""FastAPI 应用装配。

启动流程（lifespan）：
    1. 读环境变量 → 壳层配置
    2. 组装核心配置 + 模型提供者
    3. 创建 `NL2SQL` 引擎并 `connect()`
    4. 抽取/刷新元数据快照（首次启动需要，之后走缓存）
    5. 把引擎挂到 `app.state`

**为什么引擎挂在 app.state 而不是模块级全局**：
    核心层禁止模块级可变状态（docs/01 硬约束）。挂在 app.state 上
    既满足长生命周期，也不违反该约束，且能被 lifespan 正确关闭。

一个刻意的设计：`/api/info` 与静态资源在引擎未就绪时**仍然可用**，
这样界面能显示"引擎未就绪"而不是白屏。
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .deps import EngineHolder
from .projects import ProjectHub, ProjectStore
from .routers import admin as admin_router
from .routers import info as info_router
from .routers import knowledge as knowledge_router
from .routers import projects as projects_router
from .routers import query as query_router
from .routers import tasks as tasks_router
from .shell_config import build_shell_config, read_env

HERE = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIST = os.path.abspath(
    os.path.join(HERE, "..", "frontend", "dist")
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    holder = EngineHolder()
    app.state.engine_holder = holder

    shell = read_env()
    cfg, provider = build_shell_config(shell)

    from nl2sql import NL2SQL

    try:
        engine = NL2SQL(cfg, llm_provider=provider)
        engine.connect()
        holder.attach(engine, started_at=datetime.now().isoformat(timespec="seconds"))

        # 首次启动需要元数据快照；抽取失败不致命（可能是数据源暂时连不上）
        for sid in cfg.sources:
            try:
                engine.refresh_snapshot(sid)
            except Exception as e:  # noqa: BLE001
                holder.warnings.append(f"[{sid}] 元数据抽取失败: {e}")
    except Exception as e:  # noqa: BLE001
        # 启动失败也要让服务起来，否则界面只能白屏
        holder.warnings.append(f"引擎装配失败: {e}")

    # ---------------------------------------------------------- 项目中心
    # M7：多项目。默认项目 = 上面这个环境变量引擎（旧行为完全不变）；
    # 其它项目按 `projects/<id>/project.toml` 懒加载各自引擎。
    try:
        projects_root = os.environ.get("NL2SQL_PROJECTS_DIR") or os.path.join(
            os.getcwd(), "projects")
        hub = ProjectHub(
            ProjectStore(projects_root),
            provider=provider,
            default_engine=holder.engine,
            default_cfg=cfg,
            default_warnings=holder.warnings,
        )
        app.state.project_hub = hub
        # 工作流 cron 调度线程：只有项目中心可用时才启动。线程内部只扫
        # 已启用的 workflow_schedule，失败只记日志，不阻断服务。
        from .workflow_scheduler import WorkflowScheduler

        scheduler = WorkflowScheduler(hub)
        scheduler.start()
        app.state.workflow_scheduler = scheduler
    except Exception as e:  # noqa: BLE001 - 项目中心坏掉不阻断演示环境
        holder.warnings.append(f"项目中心不可用: {e}")

    yield

    scheduler = getattr(app.state, "workflow_scheduler", None)
    if scheduler is not None:
        scheduler.stop()
    hub = getattr(app.state, "project_hub", None)
    if hub is not None:
        hub.close()
    holder.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="talk-to-sql 本地工作台",
        version="0.1.0",
        description="自然语言查询数仓：SQL + 结果表（面向开发者个人）",
        lifespan=lifespan,
    )

    # 开发时前端跑在 5173，需要跨域；生产由本服务托管静态文件，同源
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(info_router.router)
    app.include_router(query_router.router)
    app.include_router(knowledge_router.router)
    app.include_router(admin_router.router)
    app.include_router(projects_router.router)
    app.include_router(tasks_router.router)

    # ---------------------------------------------------------- 异常处理
    # 核心层的可预期错误（配置错、连接失败）应转成结构化 HTTP 错误，
    # 否则前端拿到的是 500 + 内部堆栈——既难看又暴露实现细节。
    from nl2sql.errors import (  # noqa: PLC0415
        ConfigError,
        NL2SQLError,
        NotRegisteredError,
        SourceConnectionError,
    )

    @app.exception_handler(ConfigError)
    async def _config_error(_request, exc: ConfigError):  # type: ignore[no-untyped-def]
        return JSONResponse(status_code=400,
                            content={"error": "config_error", "detail": str(exc)})

    @app.exception_handler(SourceConnectionError)
    async def _conn_error(_request, exc: SourceConnectionError):  # type: ignore[no-untyped-def]
        return JSONResponse(
            status_code=503,
            content={"error": "source_unavailable", "detail": str(exc)},
        )

    @app.exception_handler(NotRegisteredError)
    async def _reg_error(_request, exc: NotRegisteredError):  # type: ignore[no-untyped-def]
        return JSONResponse(
            status_code=400,
            content={"error": "not_registered", "detail": str(exc)},
        )

    @app.exception_handler(NL2SQLError)
    async def _core_error(_request, exc: NL2SQLError):  # type: ignore[no-untyped-def]
        return JSONResponse(
            status_code=400,
            content={"error": type(exc).__name__, "detail": str(exc)},
        )

    @app.get("/api/health", include_in_schema=False)
    def _health() -> dict:
        return {"ok": True}

    # ---------------------------------------------------------- 静态资源
    # 生产模式：FastAPI 直接托管 Vite 构建产物，只跑一个进程。
    if os.path.isdir(FRONTEND_DIST):
        assets = os.path.join(FRONTEND_DIST, "assets")
        if os.path.isdir(assets):
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/", include_in_schema=False)
        def _index() -> FileResponse:
            return FileResponse(os.path.join(FRONTEND_DIST, "index.html"))

        @app.get("/{full_path:path}", include_in_schema=False)
        def _spa_fallback(full_path: str):
            """SPA 回退：未匹配的路径交给前端路由。

            **但以 /api 开头的必须 404**，否则前端会把 API 拼写错误
            当成页面请求，返回一坨 HTML，排障时非常迷惑。
            """
            if full_path.startswith("api/"):
                return JSONResponse(
                    status_code=404,
                    content={"error": "not_found", "detail": f"/{full_path}"},
                )
            candidate = os.path.join(FRONTEND_DIST, full_path)
            if os.path.isfile(candidate):
                return FileResponse(candidate)
            return FileResponse(os.path.join(FRONTEND_DIST, "index.html"))
    else:
        @app.get("/", include_in_schema=False)
        def _no_frontend() -> dict:
            return {
                "message": "前端尚未构建",
                "hint": "cd apps/web/frontend && npm install && npm run build；"
                        "开发模式请用 npm run dev（5173）",
                "api_docs": "/docs",
            }

    return app


app = create_app()
