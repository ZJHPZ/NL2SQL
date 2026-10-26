"""项目 / 数据源 HTTP 接口（M7 数据开发工作台）。

契约见 `_tmp/m7_api_contract.md` §1。三条设计约束：

1. **口令只进不出**：保存数据源时把 password 写进项目目录的 secrets，
   接口永远只回 `has_password`；配置层把口令放在内存 credentials 里。
2. **默认项目只读**：`default` 是环境变量引擎，数据源不能在这里编辑；
   要自定义连接请新建项目（这也让"演示环境"与"用户工作区"边界清楚）。
3. **连接失败是业务结果不是 500**：`/sources/test` 永远 200，
   失败时 `ok:false + message`，方便界面原样展示。
"""
from __future__ import annotations

import os
import random
import re
import string
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from nl2sql.sources import build_adapter

from ..deps import get_engine
from ..projects import (
    DEFAULT_PROJECT_ID,
    EngineEntry,
    ProjectHub,
    ProjectStore,
    _normalize_source,
    _source_config_from_meta,
    default_project_meta,
    normalize_data_write,
    public_source,
)
from ..schemas import (
    DriverDownloadRequest,
    ProjectCreateRequest,
    ProjectUpdateRequest,
    SourceRequest,
)
from nl2sql.errors import NL2SQLError

router = APIRouter(prefix="/api", tags=["projects"])


# ================================================================== 工具
def _hub(request: Request) -> ProjectHub:
    hub = getattr(request.app.state, "project_hub", None)
    if hub is None:
        raise HTTPException(status_code=503, detail="项目中心未就绪")
    return hub


def _store(hub: ProjectHub) -> ProjectStore:
    return hub.store


def _require_project(hub: ProjectHub, project_id: str) -> dict[str, Any]:
    if project_id == DEFAULT_PROJECT_ID:
        return default_project_meta(hub.default_config)
    meta = hub.store.get(project_id)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"项目不存在：{project_id}")
    return meta


def _entry_for(hub: ProjectHub, project_id: str) -> EngineEntry:
    entry = hub.entry(project_id)
    if entry.engine is None:
        raise HTTPException(status_code=503,
                            detail=entry.error or "项目引擎未就绪")
    return entry


def _task_count(hub: ProjectHub, project_id: str) -> int:
    if project_id == DEFAULT_PROJECT_ID:
        entry = hub.default_entry()
        store = getattr(entry.engine, "task_store", None) if entry.engine else None
        return store.count() if store is not None else 0
    path = os.path.join(hub.store.project_dir(project_id), "state", "tasks.db")
    if not os.path.isfile(path):
        return 0
    try:
        from nl2sql.tasks import TaskStore

        return TaskStore(path).count()
    except Exception:  # noqa: BLE001 - 任务库损坏不该拖垮项目列表
        return 0


def _source_out(hub: ProjectHub, project_id: str,
                raw: dict[str, Any]) -> dict[str, Any]:
    data = public_source(raw)
    sid = str(raw.get("source_id") or "")
    has_password = bool(hub.store.get_secrets(project_id).get(sid, {}).get("password"))
    out: dict[str, Any] = {
        "source_id": sid,
        "adapter": raw.get("adapter") or "",
        "label": raw.get("label") or "",
        "path": "",
        "host": "",
        "port": 0,
        "user": "",
        "database": "",
        "has_password": has_password,
        "driver_class": "",
        "jdbc_url": "",
        "jar_paths": [],
    }
    out.update(data)
    return out


def _detail(hub: ProjectHub, meta: dict[str, Any]) -> dict[str, Any]:
    project_id = str(meta.get("id") or DEFAULT_PROJECT_ID)
    try:
        entry = hub.entry(project_id)
    except Exception as exc:  # noqa: BLE001 - 详情接口不应因引擎失败而 500
        entry = EngineEntry(project_id, error=f"引擎装配失败：{exc}")
    data = {
        "id": project_id,
        "name": meta.get("name") or project_id,
        "description": meta.get("description") or "",
        "semantic": meta.get("semantic") or "none",
        "metrics_path": meta.get("metrics_path") or "",
        "sources": [_source_out(hub, project_id, s) for s in meta.get("sources") or []],
        "data_write": normalize_data_write(meta.get("data_write")),
        "created_at": meta.get("created_at") or "",
        "updated_at": meta.get("updated_at") or "",
        "is_default": project_id == DEFAULT_PROJECT_ID,
        "engine_ready": entry.engine is not None,
        "engine_error": entry.error,
        "warnings": list(entry.warnings),
        "task_count": _task_count(hub, project_id),
        "tasks_enabled": entry.engine is not None and getattr(
            entry.engine, "task_store", None) is not None,
    }
    return data


def _generate_project_id(hub: ProjectHub, name: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-_")
    slug = re.sub(r"-+", "-", slug)[:24]
    if not slug or not slug[0].isalpha():
        slug = "proj-" + "".join(random.choices(string.ascii_lowercase, k=4))
    base = slug
    counter = 2
    while hub.store.exists(slug):
        slug = f"{base[:24]}-{counter}"
        counter += 1
        if counter > 50:
            return "proj-" + "".join(random.choices(string.ascii_lowercase, k=6))
    return slug


# ================================================================== 项目
@router.get("/projects")
def list_projects(request: Request) -> dict[str, Any]:
    hub = _hub(request)
    projects = []
    for item in hub.summary():
        detail = {
            "id": item["id"],
            "name": item["name"],
            "description": item["description"],
            "semantic": item["semantic"],
            "metrics_path": "",
            "sources": item.get("sources") or [],
            "is_default": item["is_default"],
            "task_count": _task_count(hub, item["id"]),
            "tasks_enabled": item["tasks_enabled"],
        }
        projects.append(detail)
    return {"default_project_id": DEFAULT_PROJECT_ID, "projects": projects}


@router.post("/projects")
def create_project(req: ProjectCreateRequest, request: Request) -> dict[str, Any]:
    hub = _hub(request)
    name = req.name.strip()
    project_id = (req.id or "").strip().lower()
    if not project_id:
        project_id = _generate_project_id(hub, name)
    try:
        hub.store.create(project_id, name=name, description=req.description,
                         semantic=req.semantic, metrics_path=req.metrics_path)
    except NL2SQLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    meta = hub.store.get(project_id) or {}
    return _detail(hub, meta)


@router.get("/projects/{project_id}")
def get_project(project_id: str, request: Request) -> dict[str, Any]:
    hub = _hub(request)
    return _detail(hub, _require_project(hub, project_id))


@router.put("/projects/{project_id}")
def update_project(project_id: str, req: ProjectUpdateRequest,
                   request: Request) -> dict[str, Any]:
    hub = _hub(request)
    if project_id == DEFAULT_PROJECT_ID:
        raise HTTPException(
            status_code=400,
            detail="默认项目由环境变量定义，不能在这里修改；请新建项目",
        )
    _require_project(hub, project_id)
    try:
        hub.store.update(project_id, **req.model_dump(exclude_none=True))
    except NL2SQLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    hub.invalidate(project_id)
    return _detail(hub, hub.store.get(project_id) or {})


@router.delete("/projects/{project_id}")
def delete_project(project_id: str, request: Request) -> dict[str, Any]:
    hub = _hub(request)
    if project_id == DEFAULT_PROJECT_ID:
        raise HTTPException(status_code=400, detail="默认项目不能删除")
    try:
        hub.store.delete(project_id)
    except NL2SQLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    hub.invalidate(project_id)
    return {"ok": True}


# ================================================================== 数据源
@router.post("/projects/{project_id}/sources")
def upsert_source(project_id: str, req: SourceRequest,
                  request: Request) -> dict[str, Any]:
    hub = _hub(request)
    if project_id == DEFAULT_PROJECT_ID:
        raise HTTPException(
            status_code=400,
            detail="默认项目的数据源由环境变量提供，不能在这里编辑；请新建项目",
        )
    _require_project(hub, project_id)
    raw = req.model_dump(exclude_none=True)
    try:
        normalized = _normalize_source(raw)
        hub.store.upsert_source(project_id, normalized)
    except NL2SQLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if req.password:
        hub.store.set_secret(project_id, str(normalized["source_id"]),
                             req.password)
    hub.invalidate(project_id)
    meta = hub.store.get(project_id) or {}
    saved = next((s for s in meta.get("sources") or []
                  if s.get("source_id") == normalized["source_id"]), normalized)
    return _source_out(hub, project_id, saved)


@router.delete("/projects/{project_id}/sources/{source_id}")
def delete_source(project_id: str, source_id: str,
                  request: Request) -> dict[str, Any]:
    hub = _hub(request)
    if project_id == DEFAULT_PROJECT_ID:
        raise HTTPException(status_code=400, detail="默认项目的数据源不可编辑")
    try:
        hub.store.remove_source(project_id, source_id)
    except NL2SQLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    hub.invalidate(project_id)
    return {"ok": True}


@router.post("/projects/{project_id}/sources/test")
def test_source(project_id: str, req: SourceRequest,
                request: Request) -> dict[str, Any]:
    """测试连接：**永远 200**，失败给可读原因（连接失败是业务结果）。"""
    hub = _hub(request)
    _require_project(hub, project_id)
    raw = req.model_dump(exclude_none=True)
    t0 = time.perf_counter()
    try:
        normalized = _normalize_source(raw)
        config = _source_config_from_meta(normalized, project_id=project_id,
                                          semantic_path=None)
        credentials = {config.source_id: {"password": req.password}} if req.password else {}
        adapter = build_adapter(config, credentials=credentials)
    except Exception as exc:  # noqa: BLE001 - 配置问题也按"连接失败"展示
        return {"ok": False, "message": f"配置有误：{exc}", "dialect": "",
                "elapsed_ms": int((time.perf_counter() - t0) * 1000),
                "tables": []}
    try:
        adapter.test_connection()
        tables = adapter.introspect(include_comments=True)
        payload = []
        for table in tables[:100]:
            payload.append({
                "name": table.name,
                "row_estimate": table.row_estimate,
                "columns": [
                    {
                        "name": column.name,
                        "data_type": column.data_type,
                        "nullable": bool(column.nullable),
                    }
                    for column in table.columns[:200]
                ],
            })
        return {"ok": True, "message": f"连接成功（{len(tables)} 张表）",
                "dialect": config.adapter,
                "elapsed_ms": int((time.perf_counter() - t0) * 1000),
                "tables": payload}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc) or "连接失败",
                "dialect": config.adapter,
                "elapsed_ms": int((time.perf_counter() - t0) * 1000),
                "tables": []}
    finally:
        try:
            adapter.close()
        except Exception:  # noqa: BLE001
            pass


@router.get("/projects/{project_id}/sources/{source_id}/tables")
def source_tables(project_id: str, source_id: str,
                  request: Request) -> dict[str, Any]:
    hub = _hub(request)
    entry = _entry_for(hub, project_id)
    engine = entry.engine
    try:
        tables = engine.tables(source_id)
    except NL2SQLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not tables:
        try:
            engine.refresh_snapshot(source_id)
            tables = engine.tables(source_id)
        except Exception as exc:  # noqa: BLE001 - 刷新失败如实展示
            raise HTTPException(status_code=503,
                                detail=f"元数据抽取失败：{exc}") from exc
    payload = [{
        "name": table.name,
        "row_estimate": table.row_estimate,
        "columns": [
            {"name": c.name, "data_type": c.data_type,
             "nullable": bool(c.nullable)}
            for c in table.columns
        ],
    } for table in tables]
    return {"source_id": source_id, "tables": payload,
            "truncated": len(payload) >= 100}


@router.post("/projects/{project_id}/sources/{source_id}/snapshot")
def refresh_source_snapshot(project_id: str, source_id: str,
                            request: Request) -> dict[str, Any]:
    hub = _hub(request)
    entry = _entry_for(hub, project_id)
    try:
        meta = entry.engine.refresh_snapshot(source_id, force=True)
    except NL2SQLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"table_count": meta.table_count, "fingerprint": meta.fingerprint,
            "warnings": []}


# ================================================================== JDBC 驱动
@router.get("/jdbc/drivers")
def list_jdbc_drivers() -> dict[str, Any]:
    try:
        from nl2sql.sources import drivers as drv
    except Exception:  # noqa: BLE001 - JDBC 是可选能力
        return {"drivers": [], "note": "JDBC 驱动模块不可用（可选能力）"}
    try:
        statuses = drv.driver_status()
    except Exception as exc:  # noqa: BLE001
        return {"drivers": [], "note": f"读取驱动状态失败：{exc}"}
    return {"drivers": statuses}


@router.post("/jdbc/drivers/download")
def download_jdbc_driver(req: DriverDownloadRequest) -> dict[str, Any]:
    try:
        from nl2sql.sources import drivers as drv
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"JDBC 驱动模块不可用：{exc}"}
    try:
        path = drv.download_driver(req.kind)
    except Exception as exc:  # noqa: BLE001 - 下载失败是业务结果
        return {"ok": False, "message": str(exc) or "下载失败"}
    return {"ok": True, "path": str(path)}
