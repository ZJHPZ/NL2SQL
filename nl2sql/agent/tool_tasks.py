"""项目与任务工具（M7）：让智能体看见「当前项目」，并能创建/修改 SQL 任务。

设计边界（与 M7 立项时用户拍板一致）
------------------------------------
* **任务写的是项目任务库，不是业务数据源**：task.create / task.update
  只改 `<项目>/state/tasks.db`；业务库依旧只读。
* 任务**不自动执行**：什么时候跑由用户点按钮决定（`task.run` 在 Web 层，
  不在 agent 工具里）。这里只负责"生成/修改 SQL 资产"。
* **AI 修改必须留痕**：`task.update` 改 SQL 时自动追加
  `-- ai修改，YYYY-MM-DD HH:MM` 注释，并生成新版本（版本历史可回看）。
* 工具依赖全部来自 `ToolContext`，与其它内置工具同一套纪律
  （run 不抛异常、失败信息可行动、不 import 核心私有模块）。
"""
from __future__ import annotations

from typing import Any

from ..contracts.tool import ToolContext, ToolResult, ToolSpec
from ..errors import TaskError
from ..tasks import stamp_ai_comment

__all__ = [
    "ProjectInfoTool",
    "TaskListTool",
    "TaskGetTool",
    "TaskCreateTool",
    "TaskUpdateTool",
    "build_task_tools",
    "task_tool_classes",
]


def _resource(ctx: ToolContext, name: str, capability: str
              ) -> tuple[Any, ToolResult | None]:
    """取资源；缺失/未授权时返回**可行动的失败结果**，而不是抛异常。

    工具契约要求 `run` 不抛异常（异常会打断 agent 循环）；资源没装配是
    运行时问题，但要如实告诉模型"不是你的调用错"，别让它反复重试。
    """
    try:
        value = ctx.get(name, capability)
    except (PermissionError, KeyError) as exc:
        return None, ToolResult(
            ok=False, error=f"资源不可用：{exc}",
            hint="这是运行时装配问题，不是你的调用问题",
        )
    if value is None:
        return None, ToolResult(
            ok=False, error="资源未装配",
            hint="这是运行时装配问题，不是你的调用问题",
        )
    return value, None


class ProjectInfoTool:
    spec = ToolSpec(
        name="project.info",
        description=(
            "查看当前项目的信息：项目名、描述、数据源列表、任务数量与任务清单。"
            "**当用户提到「这个项目」「当前项目」「我有哪些任务」，或你准备"
            "创建/修改任务之前，先调用它确认项目上下文**，不要凭空猜测。"
        ),
        params={"type": "object", "properties": {}, "required": []},
        requires=("project.read",),
        kind="none",
    )

    def __init__(self, deps: Any) -> None:
        self._deps = deps

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        info, err = _resource(ctx, "project_info", "project.read")
        if err is not None:
            return err
        data = info() if callable(info) else info
        return ToolResult(ok=True, data=data)


class TaskListTool:
    spec = ToolSpec(
        name="task.list",
        description=(
            "列出当前项目的 SQL 任务（任务 id、名称、版本、最近修改与执行状态）。"
            "用户问「我有哪些任务」「最近改了什么任务」时调用它。"
            "需要看某条任务的具体 SQL 时再调用 task.get。"
        ),
        params={
            "type": "object",
            "properties": {
                "limit": {"type": "integer",
                          "description": "最多返回几条，默认 20"},
            },
            "required": [],
        },
        requires=("task.read",),
        kind="none",
    )

    def __init__(self, deps: Any) -> None:
        self._deps = deps

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        store, err = _resource(ctx, "task_store", "task.read")
        if err is not None:
            return err
        try:
            limit = int(args.get("limit") or 20)
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(limit, 100))
        tasks = store.list()[:limit]
        return ToolResult(ok=True, data={
            "total": store.count(),
            "tasks": [
                {
                    "task_id": t.task_id,
                    "name": t.name,
                    "revision": t.revision,
                    "enabled": t.enabled,
                    "updated_at": t.updated_at,
                    "last_run_at": t.last_run_at,
                    "last_status": t.last_status,
                }
                for t in tasks
            ],
        })


class TaskGetTool:
    spec = ToolSpec(
        name="task.get",
        description=(
            "读取一条 SQL 任务的完整内容（含当前 SQL、版本号、说明）以及最近的"
            "版本历史（谁改的、什么时候、备注）。"
            "**修改任务前必须先 task.get 读到当前 SQL**，不要凭记忆改。"
        ),
        params={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 id"},
            },
            "required": ["task_id"],
        },
        requires=("task.read",),
        kind="none",
    )

    def __init__(self, deps: Any) -> None:
        self._deps = deps

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        store, err = _resource(ctx, "task_store", "task.read")
        if err is not None:
            return err
        task_id = str(args.get("task_id") or "").strip()
        if not task_id:
            return ToolResult(ok=False, error="缺少参数 task_id",
                              hint="先调用 task.list 拿任务 id")
        task = store.get(task_id)
        if task is None:
            names = [t.name for t in store.list()[:8]]
            return ToolResult(
                ok=False, error=f"任务不存在: {task_id}",
                hint="先调用 task.list 查看有哪些任务",
                candidates=names,
            )
        revisions = [
            {
                "revision": r.revision,
                "author": r.author,
                "is_ai": r.is_ai,
                "note": r.note,
                "created_at": r.created_at,
            }
            for r in store.revisions(task_id)[:8]
        ]
        data = task.to_dict()
        data["revisions"] = revisions
        return ToolResult(ok=True, data=data)


class TaskCreateTool:
    spec = ToolSpec(
        name="task.create",
        description=(
            "把一条 **只读** SQL 保存成当前项目的任务。"
            "用户说「帮我创建一个查询…的任务」「把这个查询存成任务」时调用它。"
            "SQL 必须是单条 SELECT / WITH / EXPLAIN；任务只是保存 SQL 供用户"
            "之后手动执行，**不会自动运行，也不会写业务数据**。"
            "创建后把 task_id 告诉用户。"
        ),
        params={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "任务名称（必填，简短）"},
                "sql": {"type": "string", "description": "单条只读 SQL"},
                "description": {"type": "string",
                                "description": "任务说明（可选）"},
                "tags": {"type": "array", "items": {"type": "string"},
                         "description": "标签（可选）"},
            },
            "required": ["name", "sql"],
        },
        requires=("task.write",),
        kind="write",
        requires_approval=False,
    )

    def __init__(self, deps: Any) -> None:
        self._deps = deps

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        store, err = _resource(ctx, "task_store", "task.write")
        if err is not None:
            return err
        try:
            task = store.create(
                str(args.get("name") or ""),
                str(args.get("sql") or ""),
                description=str(args.get("description") or ""),
                tags=tuple(str(t) for t in (args.get("tags") or [])),
                source_id=str(getattr(ctx, "source_id", "") or ""),
                author="ai",
                note="AI 创建任务",
            )
        except TaskError as exc:
            return ToolResult(ok=False, error=str(exc),
                              hint="任务只允许单条只读 SQL；请改完再试")
        except Exception as exc:  # noqa: BLE001 - 工具不抛异常
            return ToolResult(ok=False, error=f"创建任务失败：{exc}",
                              hint="检查任务库是否可写")
        return ToolResult(ok=True, data={
            "task_id": task.task_id,
            "name": task.name,
            "revision": task.revision,
            "note": "任务已保存，不会自动执行；由用户在任务面板手动运行",
        })


class TaskUpdateTool:
    spec = ToolSpec(
        name="task.update",
        description=(
            "修改一条已有任务的 SQL（或名称/说明），生成新版本。"
            "用户说「把任务 X 里的某段逻辑改成…」「这个任务再加一个筛选条件」时，"
            "**先用 task.get 读到当前 SQL**，改好后调用本工具；"
            "SQL 修改会自动追加 `-- ai修改，时间` 注释并记入版本历史。"
            "只有只读 SQL 才能保存；任务不会自动执行。"
        ),
        params={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 id（必填）"},
                "sql": {"type": "string",
                        "description": "修改后的完整单条只读 SQL（可选）"},
                "name": {"type": "string", "description": "新任务名（可选）"},
                "description": {"type": "string", "description": "新说明（可选）"},
                "note": {"type": "string",
                         "description": "这次修改的备注（可选，写清改了什么）"},
            },
            "required": ["task_id"],
        },
        requires=("task.write",),
        kind="write",
        requires_approval=False,
    )

    def __init__(self, deps: Any) -> None:
        self._deps = deps

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        store, err = _resource(ctx, "task_store", "task.write")
        if err is not None:
            return err
        task_id = str(args.get("task_id") or "").strip()
        if not task_id:
            return ToolResult(ok=False, error="缺少参数 task_id",
                              hint="先调用 task.list 拿任务 id")
        current = store.get(task_id)
        if current is None:
            names = [t.name for t in store.list()[:8]]
            return ToolResult(ok=False, error=f"任务不存在: {task_id}",
                              hint="先调用 task.list 查看有哪些任务",
                              candidates=names)
        raw_sql = args.get("sql")
        kwargs: dict[str, Any] = {}
        note = str(args.get("note") or "").strip()
        if raw_sql is not None and str(raw_sql).strip():
            # AI 修改留痕：注释追加在 SQL 末尾（版本历史里也能看到 note）。
            kwargs["sql"] = stamp_ai_comment(str(raw_sql), summary=note)
            kwargs["note"] = note or "AI 修改 SQL"
            kwargs["author"] = "ai"
        if args.get("name") is not None:
            kwargs["name"] = str(args.get("name") or "")
        if args.get("description") is not None:
            kwargs["description"] = str(args.get("description") or "")
        if not kwargs:
            return ToolResult(
                ok=False, error="没有可更新的内容",
                hint="至少提供 sql / name / description 之一",
            )
        kwargs.setdefault("author", "ai")
        kwargs.setdefault("note", note or "AI 修改任务")
        if not current.source_id and getattr(ctx, "source_id", ""):
            kwargs.setdefault("source_id", str(ctx.source_id))
        try:
            task = store.update(task_id, **kwargs)
        except TaskError as exc:
            return ToolResult(ok=False, error=str(exc),
                              hint="任务只允许单条只读 SQL；请改完再试")
        except Exception as exc:  # noqa: BLE001 - 工具不抛异常
            return ToolResult(ok=False, error=f"更新任务失败：{exc}",
                              hint="检查任务库是否可写")
        return ToolResult(ok=True, data={
            "task_id": task.task_id,
            "name": task.name,
            "revision": task.revision,
            "sql": task.sql,
            "note": "新版本已保存；任务不会自动执行，由用户手动运行",
        })


def task_tool_classes() -> tuple[type, ...]:
    """任务/项目工具类集合（不实例化即可枚举规格，供管理界面用）。"""
    return (
        ProjectInfoTool,
        TaskListTool,
        TaskGetTool,
        TaskCreateTool,
        TaskUpdateTool,
    )


def build_task_tools(deps: Any) -> list[Any]:
    """按装配情况返回可用的任务/项目工具实例。

    * 没有 `task_store` → 任务工具一个都不注册（与 skill.load 的条件注册同一原则：
      留一个只会回"未装配"的工具，等于让模型白花一步）；
    * 没有 `project_info` → 不注册 project.info。
    """
    tools: list[Any] = []
    if getattr(deps, "project_info", None) is not None:
        tools.append(ProjectInfoTool(deps))
    if getattr(deps, "task_store", None) is not None:
        tools.extend([
            TaskListTool(deps),
            TaskGetTool(deps),
            TaskCreateTool(deps),
            TaskUpdateTool(deps),
        ])
    return tools
