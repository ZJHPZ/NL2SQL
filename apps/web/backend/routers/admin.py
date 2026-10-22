"""插件与工具管理接口（界面「看得见 / 管得了 / 配得了」的后端）。

设计立场
--------
面向个人工作者，**用户是自己数据的主人**，所以能力也允许在界面里配置——
替他做决定是越权。

但配套要求是「把真相摆出来」：
    * 每项能力标明 **risk** 与 **unlocks**（解锁什么）
    * 透出 guard 的只读状态与"当前没有任何工具实现写操作"的事实
这样用户是在**知情**下授权，而不是被界面诱导。
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from nl2sql.config_file import (
    LOOP_FIELDS,
    load_runtime_settings,
    save_runtime_settings,
)
from nl2sql.contracts.tool import CAPABILITIES

from ..deps import get_engine

router = APIRouter(prefix="/api/admin", tags=["admin"])

# 能力风险与说明。**放在后端而不是前端硬编码**：
# 新增能力时只改这里，界面自动跟上，不会出现"前端不知道有这项能力"。
_CAPABILITY_META: dict[str, dict[str, str]] = {
    "source.read": {"risk": "low", "unlocks": "执行只读 SELECT、读取库表元数据"},
    "source.write": {"risk": "high", "unlocks": "执行 INSERT/UPDATE/DELETE/DDL（当前无工具实现）"},
    "semantic.read": {"risk": "low", "unlocks": "读取指标、维度、口径定义"},
    "semantic.write": {"risk": "medium", "unlocks": "新增或覆盖语义层指标定义"},
    "knowledge.read": {"risk": "low", "unlocks": "检索知识库条目与历史示例"},
    "knowledge.write": {"risk": "medium", "unlocks": "写入或修改知识库条目"},
    "cache.read": {"risk": "low", "unlocks": "读取结果缓存"},
    "cache.write": {"risk": "low", "unlocks": "写入结果缓存"},
    "session.read": {"risk": "low", "unlocks": "读取当前会话上下文"},
    "session.write": {"risk": "low", "unlocks": "写入会话上下文"},
    "llm.invoke": {"risk": "low", "unlocks": "调用大模型（会产生费用）"},
    "clarify.ask": {"risk": "low", "unlocks": "向用户提问并等待回答"},
    "ui.render": {"risk": "low", "unlocks": "向界面输出额外渲染内容"},
}

# 停用后会让 agent 明显退化的工具——界面要提醒
_DEGRADE_WARNING: dict[str, str] = {
    "nl2sql.solve": (
        "nl2sql.solve 是「一步到位」的主力工具。停用它后智能体只能靠"
        "探索类工具自行拼装查询，成功率与速度都可能明显下降。"
    ),
    "clarify.ask": (
        "停用后智能体无法向你追问，遇到信息不足时只能猜或直接放弃。"
    ),
    "schema.describe": (
        "停用后智能体无法核对字段与取值，SQL 报错时的自我纠正能力会大幅下降。"
    ),
}


class TogglePluginRequest(BaseModel):
    name: str = Field(..., min_length=1)
    enabled: bool


class ToggleToolRequest(BaseModel):
    name: str = Field(..., min_length=1)
    enabled: bool


class CapabilityRequest(BaseModel):
    grant: list[str] = Field(default_factory=list)
    revoke: list[str] = Field(default_factory=list)


class LoopRequest(BaseModel):
    max_steps: int | None = None
    max_repeat: int | None = None
    temperature: float | None = None


class ConfigTextRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=100000)


# --------------------------------------------------------------------- 辅助
def _cfg_path(engine: Any) -> str:
    return engine.config.agent.config_path


def _reload_settings(engine: Any) -> Any:
    """从配置文件重新读出设置并合并进运行配置。

    热生效的实现方式：改配置 → 重新合并 → 重建运行时。
    **不重启进程**，因为循环参数、Prompt、模型绑定都不涉及模块导入。
    （插件代码变更仍需重启，界面上会明确标注。）
    """
    from nl2sql.config_file import apply_to_agent_config

    settings = load_runtime_settings(_cfg_path(engine))
    engine.config.agent = apply_to_agent_config(engine.config.agent, settings)
    return settings


def _capability_payload(granted: set[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name in sorted(CAPABILITIES):
        meta = _CAPABILITY_META.get(name, {"risk": "unknown", "unlocks": ""})
        out.append({
            "name": name,
            "description": CAPABILITIES[name],
            "risk": meta["risk"],
            "unlocks": meta["unlocks"],
            "granted": name in granted,
        })
    return out


# --------------------------------------------------------------------- 读
@router.get("/tools")
def list_tools(engine: Any = Depends(get_engine)) -> dict[str, Any]:
    """插件与工具总览：界面「看得见」的全部数据。"""
    engine.connect()
    granted = set(engine.config.agent.capabilities)
    disabled = set(engine.config.agent.disabled_tools)

    # 内置工具 + 插件工具的**全集**：靠类的静态定义枚举，
    # 因为被停用/未授权的工具不在注册表里，但仍要显示给用户去启用。
    all_specs = engine.all_tool_specs()
    registered = set(engine.tools.names()) if engine.tools else set()

    tools: list[dict[str, Any]] = []
    for spec in all_specs:
        origin = "core"
        if engine.tools is not None and engine.tools.has(spec.name):
            origin = engine.tools.origin_of(spec.name)
        missing = [r for r in spec.requires if r not in granted]
        enabled = spec.name not in disabled
        tools.append({
            "name": spec.name,
            "description": spec.description,
            "kind": spec.kind,
            "requires": list(spec.requires),
            "requires_approval": spec.requires_approval,
            "origin": origin,
            "enabled": enabled,
            "registered": spec.name in registered,
            "risk": ("high" if spec.kind == "write" else
                     "medium" if spec.requires_approval else "low"),
            "missing_capabilities": missing,
            "reason": (
                "已被停用" if not enabled else
                f"缺少能力: {', '.join(missing)}" if missing else ""
            ),
        })

    plugins: list[dict[str, Any]] = []
    for p in getattr(engine, "plugin_manifests", []) or []:
        contributed = [t["name"] for t in tools
                       if t["origin"] == f"plugin:{p.get('name')}"]
        plugins.append({**p, "tools": contributed, "enabled": True})

    # 磁盘上**存在但未启用**的插件也要列出。
    # 否则用户在界面里看不到它们，"启用某个插件"就无从下手。
    # 注意：这里只读 plugin.toml，**不导入代码**——列清单不该有副作用。
    enabled_names = {"*", *engine.config.agent.enabled_plugins}
    allow_all = "*" in engine.config.agent.enabled_plugins
    available: list[dict[str, Any]] = []
    try:
        from nl2sql.plugins.loader import discover_manifests

        manifests, _errors = discover_manifests(engine.config.agent.plugins_dir)
        loaded_names = {p["name"] for p in plugins}
        for m in manifests:
            if m.name in loaded_names:
                continue
            # 区分「没在白名单里」与「在白名单里但加载失败」。
            # 否则用户会看到一个启用按钮，点了也没变化——那是最糟的界面状态。
            whitelisted = allow_all or m.name in enabled_names
            if whitelisted:
                reason = ("已在加载范围内，但本次加载失败——请看下方「插件加载问题」，"
                          "修好 plugin.py 后重启进程")
            else:
                reason = "不在 plugins.enabled 白名单中，未加载"
            available.append({
                "name": m.name,
                "version": m.version,
                "description": m.description,
                "author": m.author,
                "path": str(m.path) if m.path else "",
                "requires": list(m.requires),
                "tools": list(m.tool_names),
                "enabled": whitelisted,
                "reason": reason,
            })
    except Exception as e:  # noqa: BLE001
        available = [{"name": "(扫描失败)", "error": str(e)}]

    from nl2sql import __version__

    # 技能（第四块）。与插件/工具/能力并列，因为用户判断
    # "我改的口径到底生效了没有"时必须看到同一份清单。
    skills_payload: list[dict[str, Any]] = []
    for s in engine.skills():
        skills_payload.append({
            "name": s.name,
            "description": s.description,
            "kind": s.kind,
            "requires": list(s.requires),
            "scope": s.scope,
            "origin": s.origin,
            "path": s.path,
            "applies_to": list(s.applies_to),
            "body_chars": len(s.body),
        })
    skill_report = (
        engine.skill_report.describe() if engine.skill_report else {}
    )

    return {
        "tools": tools,
        "plugins": plugins,
        "plugins_available": available,
        "plugins_enabled": sorted(enabled_names - {"*"}),
        "plugins_allow_all": allow_all,
        "plugin_errors": list(getattr(engine, "plugin_errors", []) or []),
        "skills": skills_payload,
        "skill_report": skill_report,
        "capabilities": _capability_payload(granted),
        "disabled_tools": sorted(disabled),
        "safety": {
            # 第二道防线：即使授予了写能力，guard 仍会拦
            "read_only": bool(engine.config.safety.read_only),
            "max_rows": engine.config.safety.max_rows,
            "timeout_s": engine.config.safety.timeout_s,
            # 诚实告知：目前没有任何工具实现**写业务数据源**，所以授予
            # source.write 并不会真的解锁写库。M7 的任务工具虽然是 kind="write"，
            # 但只写项目自己的任务库（task.*），不碰业务库；这里按 source.write
            # 能力声明来判定"真正能写业务库的工具"，避免把任务工具误报成写库能力。
            "write_tools_implemented": any(
                t["kind"] == "write" and "source.write" in (t.get("requires") or [])
                for t in tools
            ),
            "note": ("授予 source.write 不会立即解锁写操作："
                     "当前没有实现写业务数据源的工具，且 guard 的只读守门独立于此。"
                     "真正的写操作防线在 guard（read_only）。"
                     "任务工具（task.create/task.update）只写项目任务库，不写业务数据。"),
        },
        "loop": {
            k: getattr(engine.config.agent, k) for k in LOOP_FIELDS
        },
        "planner_mode": engine.planner_mode,
        "version": __version__,
    }


@router.get("/stats")
def admin_stats(days: int = 30,
                engine: Any = Depends(get_engine)) -> dict[str, Any]:
    """使用统计（M5）：问答次数 / 状态分布 / 失败率 / 延迟分位 / 工具调用。

    `days` 是回看窗口（默认 30 天）。口径：**need_clarification 单列，
    不算失败**；`by_status` 九态零填充，所以界面不用猜"没出现的状态是 0
    还是没统计"。
    """
    from datetime import datetime, timedelta

    engine.connect()
    since = (datetime.now() - timedelta(days=max(1, days))).replace(
        microsecond=0).isoformat()
    return engine.stats(since=since)


@router.get("/config")
def read_config(engine: Any = Depends(get_engine)) -> dict[str, Any]:
    """配置文件原文。界面允许直接编辑——个人工具的用户就是开发者。"""
    path = _cfg_path(engine)
    if not path:
        return {"path": "", "exists": False, "text": ""}
    if not os.path.isfile(path):
        # 文件不存在时给出模板，而不是空白：用户需要知道能配什么
        from nl2sql.config_file import render_config_toml

        return {
            "path": path, "exists": False,
            "text": render_config_toml({
                "loop": {k: getattr(engine.config.agent, k) for k in LOOP_FIELDS},
                "tools": {"disabled": list(engine.config.agent.disabled_tools)},
                "plugins": {"enabled": list(engine.config.agent.enabled_plugins)},
                "capabilities": list(engine.config.agent.capabilities),
            }),
        }
    with open(path, encoding="utf-8") as f:
        return {"path": path, "exists": True, "text": f.read()}


# --------------------------------------------------------------------- 写
@router.post("/plugins/toggle")
def toggle_plugin(req: TogglePluginRequest,
                  engine: Any = Depends(get_engine)) -> dict[str, Any]:
    """启用/停用某个外部插件。

    **启用插件 = 允许它贡献的工具注册**。因为插件是可执行代码，
    默认白名单为空（不加载任何插件）；这里的操作是把它加进白名单，
    写回配置文件后需要重新装配工具集。
    """
    engine.connect()
    from nl2sql.plugins.loader import discover_manifests

    manifests, _errs = discover_manifests(engine.config.agent.plugins_dir)
    known = {m.name for m in manifests}
    if req.name not in known:
        raise HTTPException(
            status_code=400,
            detail=f"插件不存在: {req.name}；可用: {', '.join(sorted(known)) or '（无）'}",
        )

    enabled = set(engine.config.agent.enabled_plugins) - {"*"}
    allow_all = "*" in engine.config.agent.enabled_plugins
    warning = ""
    if req.enabled:
        if allow_all:
            warning = "当前配置为加载全部插件（*），该插件已在加载范围内。"
        else:
            enabled.add(req.name)
    else:
        if allow_all:
            # 在"全部加载"模式下无法单独关掉一个——必须改成显式白名单，
            # 否则用户会以为关掉了其实没关。这一点必须说清楚。
            raise HTTPException(
                status_code=400,
                detail=("当前配置为加载全部插件（plugins.enabled = [\"*\"]），"
                        "无法单独停用某个插件。请先改成显式白名单。"),
            )
        enabled.discard(req.name)

    engine.config.agent.enabled_plugins = tuple(sorted(enabled))
    _persist(engine)
    engine.rebuild_toolset()
    return {
        "ok": True,
        "name": req.name,
        "enabled": req.enabled,
        "plugins_enabled": sorted(enabled),
        "warning": warning,
        "note": ("已生效。插件的**代码变更**（改 plugin.py）仍需重启进程——"
                 "Python 卸载已导入模块不可靠。"),
    }


@router.post("/tools/toggle")
def toggle_tool(req: ToggleToolRequest,
                engine: Any = Depends(get_engine)) -> dict[str, Any]:
    """启用/停用某个工具。**这是收紧操作**，方向与能力授予相反。"""
    engine.connect()
    all_names = {s.name for s in engine.all_tool_specs()}
    if req.name not in all_names:
        raise HTTPException(status_code=400, detail=f"工具不存在: {req.name}")

    disabled = set(engine.config.agent.disabled_tools)
    if req.enabled:
        disabled.discard(req.name)
    else:
        disabled.add(req.name)
    engine.config.agent.disabled_tools = tuple(sorted(disabled))

    _persist(engine)

    warning = ""
    if not req.enabled and req.name in _DEGRADE_WARNING:
        warning = _DEGRADE_WARNING[req.name]

    return {
        "ok": True,
        "name": req.name,
        "enabled": req.enabled,
        "disabled": sorted(disabled),
        "warning": warning,
        "restart_required": False,
        "note": "已生效；插件代码本身的增删仍需重启进程。",
    }


@router.post("/capabilities")
def update_capabilities(req: CapabilityRequest,
                        engine: Any = Depends(get_engine)) -> dict[str, Any]:
    """授予 / 收窄能力。

    个人工具：用户自己决定授什么权。但我们要把代价说清楚——
    返回 `notice` 说明"授予后解锁什么、还有哪些防线"。
    """
    engine.connect()
    for name in req.grant + req.revoke:
        if name not in CAPABILITIES:
            raise HTTPException(
                status_code=400,
                detail=f"未知能力: {name}；可用: {', '.join(sorted(CAPABILITIES))}",
            )

    caps = set(engine.config.agent.capabilities)
    caps.update(req.grant)
    caps.difference_update(req.revoke)
    engine.config.agent.capabilities = tuple(sorted(caps))
    _persist(engine)

    notices: list[str] = []
    for name in req.grant:
        meta = _CAPABILITY_META.get(name, {})
        if meta.get("risk") == "high":
            notices.append(
                f"已授予高危能力 {name}：{meta.get('unlocks', '')}。"
                f"注意 guard 的只读守门（read_only="
                f"{engine.config.safety.read_only}）是独立防线，"
                f"修改它才会真正放开写操作。"
            )
    if req.revoke:
        # 收回能力会让依赖它的工具不再注册——这是用户可能没预料到的后果，
        # 必须明说而不是静默生效。
        registered = set(engine.tools.names()) if engine.tools else set()
        affected = [
            s.name for s in engine.all_tool_specs()
            if any(r in req.revoke for r in s.requires) and s.name in registered
        ]
        if affected:
            notices.append(
                f"收回能力后这些工具将不再注册（需重载生效）: {affected}"
            )

    return {
        "ok": True,
        "capabilities": sorted(caps),
        "notice": "\n".join(notices),
        "reload_required": True,
        "reload_reason": "能力变更影响工具注册，需要重新装配工具集",
    }


@router.post("/loop")
def update_loop(req: LoopRequest,
                engine: Any = Depends(get_engine)) -> dict[str, Any]:
    """改 agent 循环参数。**热生效**，无需重启。"""
    engine.connect()
    # 逐项按 config_file 里的同一套边界校验。
    # 不复用 Pydantic 的数值约束是为了**保持单一真相**：
    # 边界只定义在 LOOP_FIELDS 一处，配置文件与界面走同一套规则。
    for key, value in req.model_dump(exclude_none=True).items():
        if key not in LOOP_FIELDS:
            raise HTTPException(status_code=400, detail=f"不支持的参数: {key}")
        typ, lo, hi = LOOP_FIELDS[key]
        try:
            cast = typ(value)
        except (TypeError, ValueError) as e:
            raise HTTPException(
                status_code=400,
                detail=f"{key} 需要 {typ.__name__}，得到 {value!r}") from e
        if not (lo <= cast <= hi):
            raise HTTPException(
                status_code=400, detail=f"{key} 超出范围 [{lo}, {hi}]，得到 {cast}")
        setattr(engine.config.agent, key, cast)
    try:
        _persist(engine)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e)) from e
    engine.apply_runtime_settings()
    return {
        "ok": True,
        "loop": {k: getattr(engine.config.agent, k) for k in LOOP_FIELDS},
        "restart_required": False,
    }


@router.post("/config")
def write_config(req: ConfigTextRequest,
                 engine: Any = Depends(get_engine)) -> dict[str, Any]:
    """整份写回配置原文。

    **先校验再落盘**：非法内容直接拒绝，且不破坏原文件——
    否则用户手抖一下就把能用的配置弄没了。
    """
    path = _cfg_path(engine)
    if not path:
        raise HTTPException(status_code=400, detail="未配置 config_path")

    import tempfile

    from nl2sql.config_file import load_runtime_settings as _load

    # 先写到临时文件校验，通过后才覆盖正式文件
    tmp_dir = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(tmp_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(suffix=".toml", dir=tmp_dir)
    os.close(fd)
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(req.text)
        from nl2sql.errors import ConfigError

        try:
            settings = _load(tmp_path)
        except ConfigError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    with open(path, "w", encoding="utf-8") as f:
        f.write(req.text)
    engine.apply_runtime_settings()
    return {
        "ok": True,
        "path": path,
        "applied": settings.describe(),
        "restart_required": False,
        "note": "循环参数与 Prompt 已热生效；插件代码变更需重启进程。",
    }


@router.post("/reload")
def reload_engine(engine: Any = Depends(get_engine)) -> dict[str, Any]:
    """重新读配置并重新装配工具集。

    用于：改完能力或停用清单后让它生效。
    **不重新导入插件模块**——Python 卸载已导入模块有诡异问题，
    个人工具不值得冒这个险；新增插件目录请重启进程。
    """
    engine.connect()
    try:
        settings = _reload_settings(engine)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e)) from e
    engine.rebuild_toolset()
    return {
        "ok": True,
        "applied": settings.describe(),
        "tools": engine.tools.names() if engine.tools else [],
        "plugin_errors": list(getattr(engine, "plugin_errors", []) or []),
        "note": "工具集已重新装配；插件代码的增删仍需重启进程。",
    }


def _persist(engine: Any) -> None:
    """把当前运行配置写回文件。

    只写我们管理的字段，**保留 capabilities 的显式设置**——
    未显式设置过就写当前值，避免"打开界面一次就把默认值固化了"。
    """
    from nl2sql.config_file import load_runtime_settings

    path = _cfg_path(engine)
    if not path:
        return
    existing = load_runtime_settings(path)
    payload = {
        "loop": {k: getattr(engine.config.agent, k) for k in LOOP_FIELDS},
        "tools": {"disabled": list(engine.config.agent.disabled_tools)},
        # 插件白名单**总是写入**：空列表是一个有意义的设置
        # （= 不加载任何外部插件），不能因为"空"就省略，
        # 否则用户停用最后一个插件后配置里会留下旧值。
        "plugins": {"enabled": list(engine.config.agent.enabled_plugins)},
        "capabilities": list(engine.config.agent.capabilities),
        "prompt": dict(engine.config.agent.prompt_files),
    }
    save_runtime_settings(path, payload)
