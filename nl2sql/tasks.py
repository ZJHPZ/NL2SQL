"""SQL 任务（M7 数据开发工作台）：任务定义 + 版本历史 + 只读校验。

任务是什么
----------
任务 = 一条**保存下来的只读 SQL**，加上名称、描述和完整版本历史。
它把「智能体生成的 SQL」变成可审阅、可修改、可复用的资产：

    task.create   智能体把 SQL 落成任务（写的是任务库，不是业务库）
    task.update   用户或智能体修改 SQL，每次修改生成一版；AI 修改带注释
    task.run      **由用户手动触发**，只读执行，结果返回界面

边界（很重要，别越界）
----------------------
* 任务库是**项目自己的 SQLite 文件**（`<项目目录>/state/tasks.db`）。
  创建/修改任务只是写这一张表，**不等于写业务数据源**；
* SQL 必须**单条且只读**（SELECT / WITH / EXPLAIN），用
  `nl2sql.dialect.tokens.is_read_only` 统一判断；执行前还会再过一遍 guard；
* 任务**不自动调度**——「什么时候执行」由用户决定（用户明确要求）。
  这里刻意不引入 crontab/调度器，避免悄悄写业务库。

为什么版本历史放在任务库里而不是知识库
--------------------------------------
任务改坏要能一键回看"上一版长什么样"，这是**审计需求**，与语义层事实无关；
而且版本号必须和 SQL 原子地一起写，放在同一个 SQLite 事务里最简单。
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

from .dialect.tokens import is_read_only, split_statements
from .errors import TaskError
from .guard.write import WriteCheck, WriteGuard

__all__ = [
    "AI_COMMENT_PREFIX",
    "DEFAULT_TASK_FOLDERS",
    "MAX_SQL_CHARS",
    "MAX_SYNC_ROWS",
    "MAX_WORKFLOW_STEPS",
    "TASK_SYNC_MODES",
    "Task",
    "TaskRevision",
    "TaskStore",
    "default_task_store_path",
    "parse_sync_config",
    "parse_workflow",
    "stamp_ai_comment",
    "topological_order",
    "validate_folder_path",
    "validate_task_content",
    "validate_task_kind",
    "validate_task_sql",
    "validate_task_sync",
    "validate_task_workflow",
    "validate_task_write",
]

# AI 修改注释的固定前缀。用户要求格式为「ai修改，***时间」。
AI_COMMENT_PREFIX = "ai修改"
MAX_SQL_CHARS = 20000
MAX_NAME_CHARS = 80
MAX_NOTE_CHARS = 500

# 任务类型。SQL 走只读查询链路；PySpark 只保存代码，执行器尚未接入；
# 写入任务走受控写链路（单表 + 带条件 + 影响行数上限 + 预演凭据），
# 与只读问答链路完全分离；工作流任务只保存步骤 DAG，由调度器/手动触发，
# 步骤本身仍然只能是只读 SQL 任务；同步任务把「源数据源的一张表」搬到
# 项目绑定的 SQLite 写库（目标端仍是受控写防线）。
TASK_KIND_SQL = "sql"
TASK_KIND_PYSPARK = "pyspark"
TASK_KIND_WRITE = "write"
TASK_KIND_WORKFLOW = "workflow"
TASK_KIND_SYNC = "sync"
TASK_KINDS = (TASK_KIND_SQL, TASK_KIND_PYSPARK, TASK_KIND_WRITE,
              TASK_KIND_WORKFLOW, TASK_KIND_SYNC)

# 工作流（一期原生调度）：步骤上限刻意很小——本地单用户场景里，
# 超过 20 步的「工作流」其实是没人读得懂的脚本，不如拆成多个工作流。
MAX_WORKFLOW_STEPS = 20
MAX_WORKFLOW_STEP_KEY = 40

# 原生同步（一期）：源端 = 任意只读数据源的一张表；目标端 = 项目绑定的
# SQLite 写库。单次行数硬上限（项目 data_write.max_affected 只会更小）。
TASK_SYNC_MODES = ("full", "incremental")
MAX_SYNC_ROWS = 1000
_SYNC_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

# 项目首次打开任务库时自动建的四层数仓目录（ODS → DWD → DWM → ADS）。
DEFAULT_TASK_FOLDERS = ("ods", "dwd", "dwm", "ads")
MAX_FOLDER_CHARS = 200
MAX_FOLDER_SEGMENT = 32

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"
_AI_TS_FORMAT = "%Y-%m-%d %H:%M"

_AUTHOR_USER = "user"
_AUTHOR_AI = "ai"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS task (
    task_id      TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL DEFAULT '',
    source_id    TEXT NOT NULL DEFAULT '',
    folder       TEXT NOT NULL DEFAULT '',
    kind         TEXT NOT NULL DEFAULT 'sql',
    name         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    sql          TEXT NOT NULL,
    tags         TEXT NOT NULL DEFAULT '[]',
    enabled      INTEGER NOT NULL DEFAULT 1,
    revision     INTEGER NOT NULL DEFAULT 1,
    created_by   TEXT NOT NULL DEFAULT 'user',
    updated_by   TEXT NOT NULL DEFAULT 'user',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    last_run_at  TEXT NOT NULL DEFAULT '',
    last_status  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS task_revision (
    task_id      TEXT NOT NULL,
    revision     INTEGER NOT NULL,
    source_id    TEXT NOT NULL DEFAULT '',
    name         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    sql          TEXT NOT NULL,
    note         TEXT NOT NULL DEFAULT '',
    author       TEXT NOT NULL DEFAULT 'user',
    created_at   TEXT NOT NULL,
    PRIMARY KEY (task_id, revision)
);

-- 任务文件夹（树）。path 用 '/' 分段，例如 "ods"、"ods/原始层"。
CREATE TABLE IF NOT EXISTS task_folder (
    path         TEXT PRIMARY KEY,
    created_by   TEXT NOT NULL DEFAULT 'user',
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_updated ON task(updated_at);
CREATE INDEX IF NOT EXISTS idx_task_folder ON task_folder(path);

-- 受控写的一次性预演凭据：dry-run 通过后 5 分钟内可凭此提交，
-- 提交时会校验 revision / SQL / 影响行数与预演时一致。
CREATE TABLE IF NOT EXISTS write_dry_run (
    dry_run_id   TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    project_id   TEXT NOT NULL DEFAULT '',
    revision     INTEGER NOT NULL DEFAULT 0,
    source_id    TEXT NOT NULL DEFAULT '',
    kind         TEXT NOT NULL DEFAULT '',
    table_name   TEXT NOT NULL DEFAULT '',
    sql          TEXT NOT NULL,
    rows         INTEGER NOT NULL DEFAULT 0,
    before_json  TEXT NOT NULL DEFAULT '[]',
    after_json   TEXT NOT NULL DEFAULT '[]',
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL
);

-- 写入审计：预演、提交、失败都留痕；只记录元信息，不存业务数据快照。
CREATE TABLE IF NOT EXISTS write_audit (
    audit_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   TEXT NOT NULL DEFAULT '',
    task_id      TEXT NOT NULL DEFAULT '',
    task_name    TEXT NOT NULL DEFAULT '',
    revision     INTEGER NOT NULL DEFAULT 0,
    source_id    TEXT NOT NULL DEFAULT '',
    kind         TEXT NOT NULL DEFAULT '',
    table_name   TEXT NOT NULL DEFAULT '',
    sql          TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT '',
    rows         INTEGER NOT NULL DEFAULT 0,
    error        TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_write_audit ON write_audit(created_at);

-- 工作流调度（一期原生调度）：每个工作流任务一条 cron 配置。
-- next_run_at 由后台调度线程维护——本地单用户不需要分布式锁；
-- 重启后按 next_run_at 判断是否补触发（幂等：已有运行中的实例就跳过本次）。
CREATE TABLE IF NOT EXISTS workflow_schedule (
    task_id      TEXT PRIMARY KEY,
    cron         TEXT NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 0,
    next_run_at  TEXT NOT NULL DEFAULT '',
    last_run_at  TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- 运行实例：一次触发一条。trigger 记录 manual / cron / rerun。
CREATE TABLE IF NOT EXISTS workflow_run (
    run_id       TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    project_id   TEXT NOT NULL DEFAULT '',
    trigger      TEXT NOT NULL DEFAULT 'manual',
    status       TEXT NOT NULL DEFAULT 'running',
    started_at   TEXT NOT NULL,
    finished_at  TEXT NOT NULL DEFAULT '',
    error        TEXT NOT NULL DEFAULT ''
);

-- 实例里的步骤：按 step_key 与工作流定义对齐；状态 pending/running/success/
-- failed/skipped。任务名在触发时快照下来——任务后来改名，历史实例仍可读。
CREATE TABLE IF NOT EXISTS workflow_run_step (
    run_id       TEXT NOT NULL,
    step_key     TEXT NOT NULL,
    task_id      TEXT NOT NULL DEFAULT '',
    task_name    TEXT NOT NULL DEFAULT '',
    depends_on   TEXT NOT NULL DEFAULT '[]',
    status       TEXT NOT NULL DEFAULT 'pending',
    rows         INTEGER NOT NULL DEFAULT 0,
    error        TEXT NOT NULL DEFAULT '',
    started_at   TEXT NOT NULL DEFAULT '',
    finished_at  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, step_key)
);

CREATE INDEX IF NOT EXISTS idx_workflow_run
    ON workflow_run(task_id, started_at);
CREATE INDEX IF NOT EXISTS idx_workflow_run_step
    ON workflow_run_step(run_id);

-- 原生同步（一期）：增量同步的水位线（每个同步任务一行）。
-- 首次增量按全量处理（watermark 为空），之后只搬「水位之后」的行。
CREATE TABLE IF NOT EXISTS sync_state (
    task_id      TEXT PRIMARY KEY,
    watermark    TEXT NOT NULL DEFAULT '',
    rows         INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL
);

-- 同步预演凭据：预演时把「要搬多少行、水位从哪到哪」冻结下来，
-- 5 分钟内凭此提交；提交前会重读源表核对行数与水位是否仍一致。
CREATE TABLE IF NOT EXISTS sync_preview (
    preview_id       TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL,
    project_id       TEXT NOT NULL DEFAULT '',
    revision         INTEGER NOT NULL DEFAULT 0,
    mode             TEXT NOT NULL DEFAULT 'full',
    source_id        TEXT NOT NULL DEFAULT '',
    source_table     TEXT NOT NULL DEFAULT '',
    target_table     TEXT NOT NULL DEFAULT '',
    rows             INTEGER NOT NULL DEFAULT 0,
    watermark_before TEXT NOT NULL DEFAULT '',
    watermark_after  TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    expires_at       TEXT NOT NULL
);

-- 同步审计：预演、提交、失败都留痕（不存业务数据快照）。
CREATE TABLE IF NOT EXISTS sync_event (
    event_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id       TEXT NOT NULL DEFAULT '',
    task_id          TEXT NOT NULL DEFAULT '',
    task_name        TEXT NOT NULL DEFAULT '',
    revision         INTEGER NOT NULL DEFAULT 0,
    mode             TEXT NOT NULL DEFAULT '',
    source_id        TEXT NOT NULL DEFAULT '',
    source_table     TEXT NOT NULL DEFAULT '',
    target_table     TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT '',
    rows             INTEGER NOT NULL DEFAULT 0,
    error            TEXT NOT NULL DEFAULT '',
    watermark_before TEXT NOT NULL DEFAULT '',
    watermark_after  TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sync_event ON sync_event(created_at);
"""


# ===================================================================== 校验
def validate_task_sql(sql: str) -> str:
    """校验并返回任务 SQL 原文（去首尾空白）。

    规则刻意从紧：**单条 + 只读 + 长度上限**。
    任务不是"随手记的 SQL 草稿"，它会被反复执行，所以宁可在保存时拒绝。
    """
    text = (sql or "").strip()
    if not text:
        raise TaskError("SQL 不能为空")
    if len(text) > MAX_SQL_CHARS:
        raise TaskError(
            f"SQL 太长（{len(text)} 字符，上限 {MAX_SQL_CHARS}）；任务只适合存放单条查询"
        )
    statements = [s for s in split_statements(text) if s.strip()]
    if not statements:
        raise TaskError("SQL 不能为空（只包含注释）")
    if len(statements) > 1:
        raise TaskError(
            f"任务只允许一条 SQL（检测到 {len(statements)} 条）；"
            "不要用分号拼接多条语句——任务要能被人审阅后再执行"
        )
    if not is_read_only(statements[0]):
        raise TaskError(
            "任务只允许只读查询（SELECT / WITH / EXPLAIN）；"
            "不能包含 INSERT / UPDATE / DELETE / DDL 等写操作"
        )
    return text


def validate_task_write(sql: str) -> str:
    """校验并返回「写入任务」的 SQL 原文。

    写入任务的规则比只读任务更细（见 `nl2sql.guard.write`）：
    单条、单表、INSERT/UPDATE/DELETE、UPDATE/DELETE 必须带非恒真 WHERE。
    这里只做保存时能判定的语句形状校验；影响行数上限要等执行层真的
    在事务里跑一遍才能知道，所以不在保存时判断。
    """
    text = (sql or "").strip()
    if not text:
        raise TaskError("写入 SQL 不能为空")
    if len(text) > MAX_SQL_CHARS:
        raise TaskError(
            f"SQL 太长（{len(text)} 字符，上限 {MAX_SQL_CHARS}）；任务只适合存放单条语句"
        )
    result = WriteGuard().check(text)
    if not result.allowed:
        raise TaskError(f"写入任务不合法：{result.reason}")
    return text


_WORKFLOW_STEP_KEY_RE = re.compile(
    r"^[A-Za-z0-9_-]{1,%d}$" % MAX_WORKFLOW_STEP_KEY)


def parse_workflow(content: str) -> list[dict[str, Any]]:
    """解析工作流正文，返回步骤列表（保持用户书写顺序）。

    正文格式刻意做成显式 DAG（不做拖拽画布）：本地单用户场景下，
    表单/文字比画布更容易审阅、diff 与版本化：

        {"steps": [
            {"key": "s1", "task_id": "task-xxxx", "depends_on": []},
            {"key": "s2", "task_id": "task-yyyy", "depends_on": ["s1"]}
        ]}

    这里只校验结构（JSON、key、依赖引用、无环），不校验 task_id 是否
    真实存在、是否为只读 SQL 任务——那需要任务库上下文，由执行层负责。
    """
    text = (content or "").strip()
    if not text:
        raise TaskError("工作流定义不能为空")
    if len(text) > MAX_SQL_CHARS:
        raise TaskError(
            f"工作流定义太长（{len(text)} 字符，上限 {MAX_SQL_CHARS}）"
        )
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise TaskError(f"工作流定义不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise TaskError('工作流定义必须是 JSON 对象，形如 {"steps": [...]}')
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list):
        raise TaskError('工作流定义缺少 steps 数组，形如 {"steps": [...]}')
    if len(raw_steps) > MAX_WORKFLOW_STEPS:
        raise TaskError(
            f"工作流步骤太多（{len(raw_steps)} 步，上限 {MAX_WORKFLOW_STEPS}）；"
            "请拆成多个工作流"
        )
    steps: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_steps, start=1):
        if not isinstance(raw, dict):
            raise TaskError(f"第 {index} 个步骤必须是 JSON 对象")
        key = str(raw.get("key") or "").strip()
        if not _WORKFLOW_STEP_KEY_RE.match(key):
            raise TaskError(
                f"第 {index} 个步骤的 key 不合法（只允许字母/数字/下划线/"
                f"短横，1–{MAX_WORKFLOW_STEP_KEY} 字符）"
            )
        if key in seen:
            raise TaskError(f"步骤 key 重复：{key}")
        seen.add(key)
        task_id = str(raw.get("task_id") or "").strip()
        if not task_id:
            raise TaskError(f"步骤 {key} 还没有选择任务")
        raw_deps = raw.get("depends_on")
        if raw_deps is None:
            raw_deps = []
        if not isinstance(raw_deps, list):
            raise TaskError(f"步骤 {key} 的 depends_on 必须是数组")
        depends: list[str] = []
        for dep in raw_deps:
            dep_key = str(dep or "").strip()
            if dep_key and dep_key not in depends:
                depends.append(dep_key)
        steps.append({"key": key, "task_id": task_id, "depends_on": depends})
    keys = {step["key"] for step in steps}
    for step in steps:
        for dep in step["depends_on"]:
            if dep == step["key"]:
                raise TaskError(f"步骤 {step['key']} 不能依赖自己")
            if dep not in keys:
                raise TaskError(
                    f"步骤 {step['key']} 依赖的步骤不存在：{dep}")
    topological_order(steps)  # 提前把环暴露出来
    return steps


def topological_order(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """稳定拓扑排序：同层按用户书写顺序执行，结果可预期。"""
    by_key = {step["key"]: step for step in steps}
    indegree = {step["key"]: 0 for step in steps}
    for step in steps:
        for _dep in step["depends_on"]:
            indegree[step["key"]] += 1
    ready = [step["key"] for step in steps if indegree[step["key"]] == 0]
    ordered: list[dict[str, Any]] = []
    while ready:
        key = ready.pop(0)
        ordered.append(by_key[key])
        for step in steps:
            if key in step["depends_on"]:
                indegree[step["key"]] -= 1
                if indegree[step["key"]] == 0:
                    ready.append(step["key"])
    if len(ordered) != len(steps):
        raise TaskError("工作流步骤存在循环依赖，请调整依赖关系")
    return ordered


def validate_task_workflow(content: str) -> str:
    """校验工作流正文：结构合法、key 唯一、依赖存在、无环。"""
    parse_workflow(content)
    return (content or "").strip()


def parse_sync_config(content: str, *, draft: bool = True) -> dict[str, Any]:
    """解析同步任务正文，返回规范化配置。

    正文是 JSON（不做拖拽画布，便于审阅与版本化）：

        {
          "source_id": "default",      # 从哪个数据源读
          "table": "orders",           # 源表
          "target_table": "orders",    # 目标表（缺省同名）
          "mode": "full",              # full | incremental
          "watermark": "id"            # incremental 必填：单调递增列
        }

    `draft=True` 允许字段留空（新建任务时的占位正文）；
    预演/执行时用 `draft=False` 严格校验。
    """
    text = (content or "").strip()
    if not text:
        raise TaskError("同步任务定义不能为空")
    if len(text) > MAX_SQL_CHARS:
        raise TaskError(
            f"同步任务定义太长（{len(text)} 字符，上限 {MAX_SQL_CHARS}）"
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TaskError(f"同步任务定义不是合法 JSON：{exc.msg}（第 {exc.lineno} 行）") from exc
    if not isinstance(data, dict):
        raise TaskError("同步任务定义必须是 JSON 对象（包含 source_id / table 等字段）")

    source_id = str(data.get("source_id") or "").strip()
    table = str(data.get("table") or "").strip()
    target_table = str(data.get("target_table") or "").strip()
    mode = str(data.get("mode") or "full").strip().lower()
    watermark = str(data.get("watermark") or "").strip()

    if mode not in TASK_SYNC_MODES:
        raise TaskError(
            f"未知同步模式：{mode!r}（只支持 {' / '.join(TASK_SYNC_MODES)}）"
        )
    for label, value in (("源表", table), ("目标表", target_table),
                         ("增量列", watermark)):
        if value and not _SYNC_IDENT_RE.fullmatch(value):
            raise TaskError(
                f"{label}「{value}」不合法：只支持字母/数字/下划线，"
                "以字母或下划线开头，最长 63 字符"
            )
    if not draft:
        if not source_id:
            raise TaskError("同步任务缺少源数据源（source_id）")
        if not table:
            raise TaskError("同步任务缺少源表（table）")
        if mode == "incremental" and not watermark:
            raise TaskError(
                "增量同步必须指定递增列（watermark，例如 id 或 updated_at）"
            )
    return {
        "source_id": source_id,
        "table": table,
        "target_table": target_table or table,
        "mode": mode,
        "watermark": watermark,
    }


def validate_task_sync(content: str) -> str:
    """校验同步任务正文（允许草稿：字段可留空，预演时再严格要求）。"""
    parse_sync_config(content, draft=True)
    return (content or "").strip()


def validate_task_kind(kind: str | None) -> str:
    """校验任务类型。默认 SQL；PySpark 仅保存代码与版本，执行器未接入。"""
    text = (kind or TASK_KIND_SQL).strip().lower()
    if text not in TASK_KINDS:
        raise TaskError(
            f"未知任务类型：{kind!r}（只支持 {' / '.join(TASK_KINDS)}）"
        )
    return text


def validate_task_content(kind: str, content: str) -> str:
    """按任务类型校验代码正文。

    SQL 任务必须单条只读查询（会被真正执行）；
    PySpark 任务只是保存代码与版本，执行器未接入，因此不做只读语法限制；
    写入任务走受控写守门（单表 + 带条件），执行时还要再过影响行数上限；
    工作流任务正文是步骤 DAG 的 JSON，步骤本身在触发时再校验；
    同步任务正文是 JSON 配置，保存时允许草稿，预演/执行时严格校验。
    """
    if kind == TASK_KIND_PYSPARK:
        text = (content or "").strip()
        if not text:
            raise TaskError("PySpark 代码不能为空")
        if len(text) > MAX_SQL_CHARS:
            raise TaskError(
                f"PySpark 代码太长（{len(text)} 字符，上限 {MAX_SQL_CHARS}）"
            )
        return text
    if kind == TASK_KIND_WRITE:
        return validate_task_write(content)
    if kind == TASK_KIND_WORKFLOW:
        return validate_task_workflow(content)
    if kind == TASK_KIND_SYNC:
        return validate_task_sync(content)
    return validate_task_sql(content)


MAX_FOLDER_DEPTH = 4
_FOLDER_BAD_CHARS = set('<>:"|?*\\\\')


def validate_folder_path(path: str | None, *, allow_empty: bool = True) -> str:
    """校验并规范化文件夹路径（'/' 分段）。

    约定：空字符串表示「根目录 / 未分类」；其余按 `ods/原始层` 这样的路径存储。
    深度、段长都有上限——右键菜单建目录是给人用的，不该出现深层嵌套。
    """
    raw = (path or "").replace("\\", "/").strip()
    if not raw:
        if allow_empty:
            return ""
        raise TaskError("文件夹名称不能为空")
    segments = [seg.strip() for seg in raw.split("/") if seg.strip()]
    if not segments:
        if allow_empty:
            return ""
        raise TaskError("文件夹名称不能为空")
    if len(segments) > MAX_FOLDER_DEPTH:
        raise TaskError(f"文件夹层级太深（上限 {MAX_FOLDER_DEPTH} 层）")
    for seg in segments:
        if seg in (".", ".."):
            raise TaskError("文件夹名称不能是 . 或 ..")
        if len(seg) > MAX_FOLDER_SEGMENT:
            raise TaskError(
                f"文件夹名称太长（{len(seg)} 字，上限 {MAX_FOLDER_SEGMENT}）"
            )
        if any(ch in _FOLDER_BAD_CHARS for ch in seg):
            raise TaskError("文件夹名称不能包含 < > : \" | ? * 等字符")
        if any(ord(ch) < 32 for ch in seg):
            raise TaskError("文件夹名称不能包含控制字符")
    normalized = "/".join(segments)
    if len(normalized) > MAX_FOLDER_CHARS:
        raise TaskError(f"文件夹路径太长（上限 {MAX_FOLDER_CHARS} 字符）")
    return normalized


def _validate_name(name: str) -> str:
    text = (name or "").strip()
    if not text:
        raise TaskError("任务名称不能为空")
    if len(text) > MAX_NAME_CHARS:
        raise TaskError(f"任务名称太长（{len(text)} 字符，上限 {MAX_NAME_CHARS}）")
    return text


def stamp_ai_comment(
    sql: str,
    *,
    moment: datetime | None = None,
    summary: str = "",
) -> str:
    """在 SQL 末尾追加 AI 修改注释：「-- ai修改，2026-10-22 14:30」。

    注释追加在**末尾**而不是文件头：用户打开任务第一眼看到的是 SQL，
    版本历史里再展示注释的语义（谁改的、什么时候）。重复修改会累积多行，
    这正好是审计轨迹，不需要覆盖旧注释。
    """
    ts = (moment or datetime.now()).strftime(_AI_TS_FORMAT)
    comment = f"-- {AI_COMMENT_PREFIX}，{ts}"
    detail = (summary or "").strip()
    if detail:
        comment += f"（{detail[:60]}）"
    return f"{sql.rstrip()}\n{comment}"


def default_task_store_path(state_dir: str) -> str:
    """任务库默认位置：`<state_dir>/tasks.db`（与 app.db 分开，便于单独备份）。"""
    return str(Path(state_dir) / "tasks.db")


# ===================================================================== 数据模型
@dataclass(frozen=True)
class Task:
    """一条 SQL 任务。"""

    task_id: str
    project_id: str
    source_id: str
    name: str
    description: str
    sql: str
    revision: int
    enabled: bool = True
    tags: tuple[str, ...] = ()
    folder: str = ""
    kind: str = TASK_KIND_SQL
    created_by: str = _AUTHOR_USER
    updated_by: str = _AUTHOR_USER
    created_at: str = ""
    updated_at: str = ""
    last_run_at: str = ""
    last_status: str = ""

    def to_dict(self) -> dict[str, Any]:
        """给 Web API / agent 工具用的字典形态（只读快照，不含敏感信息）。"""
        return {
            "task_id": self.task_id,
            "project_id": self.project_id,
            "source_id": self.source_id,
            "folder": self.folder,
            "kind": self.kind,
            "name": self.name,
            "description": self.description,
            "sql": self.sql,
            "revision": self.revision,
            "enabled": self.enabled,
            "tags": list(self.tags),
            "created_by": self.created_by,
            "updated_by": self.updated_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_run_at": self.last_run_at,
            "last_status": self.last_status,
        }


@dataclass(frozen=True)
class TaskRevision:
    """任务的一个历史版本。`author == "ai"` 表示这次是智能体改的。"""

    task_id: str
    revision: int
    source_id: str = ""
    name: str = ""
    description: str = ""
    sql: str = ""
    note: str = ""
    author: str = _AUTHOR_USER
    created_at: str = ""

    @property
    def is_ai(self) -> bool:
        return self.author == _AUTHOR_AI

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "revision": self.revision,
            "source_id": self.source_id,
            "name": self.name,
            "description": self.description,
            "sql": self.sql,
            "note": self.note,
            "author": self.author,
            "is_ai": self.is_ai,
            "created_at": self.created_at,
        }


# ===================================================================== 任务库
class TaskStore:
    """任务库（独立 SQLite 文件）。

    一个项目一个库；`project_id` 仍写进每行，方便日后合并/导出。
    每个方法开一个短连接：Web 后端是多线程的，而 sqlite3 连接不跨线程共享。
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        project_id: str = "",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._path = Path(db_path)
        self._project_id = project_id
        self._clock = clock or datetime.now
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------ 基础设施
    @property
    def path(self) -> Path:
        return self._path

    @property
    def project_id(self) -> str:
        return self._project_id

    def _now(self) -> str:
        return self._clock().strftime(_TS_FORMAT)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self._path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            # 兼容早期任务库：加列而不是重建表。
            #   task 早期无 source_id，本轮新增 folder / kind；
            #   task_revision 早期无 source_id。
            for table in ("task", "task_revision"):
                columns = {
                    row["name"]
                    for row in conn.execute(f"PRAGMA table_info({table})")
                }
                if "source_id" not in columns:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN "
                        "source_id TEXT NOT NULL DEFAULT ''"
                    )
            task_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(task)")
            }
            if "folder" not in task_columns:
                conn.execute(
                    "ALTER TABLE task ADD COLUMN folder TEXT NOT NULL DEFAULT ''"
                )
            if "kind" not in task_columns:
                conn.execute(
                    "ALTER TABLE task ADD COLUMN kind TEXT NOT NULL DEFAULT 'sql'"
                )
            # 首次打开任务库时自动建四个数仓目录（ODS → DWD → DWM → ADS）。
            # 用 INSERT OR IGNORE 保证幂等：已有项目不会被重复插入或覆盖。
            now = self._now()
            for path in DEFAULT_TASK_FOLDERS:
                conn.execute(
                    "INSERT OR IGNORE INTO task_folder (path, created_by, created_at) "
                    "VALUES (?, 'system', ?)",
                    (path, now),
                )

    # ------------------------------------------------------------ 查询
    def list(self, *, limit: int | None = None) -> list[Task]:
        sql = "SELECT * FROM task ORDER BY updated_at DESC, task_id DESC"
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (max(1, int(limit)),)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_task(r) for r in rows]

    def get(self, task_id: str) -> Task | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM task WHERE task_id = ?", (task_id,)
            ).fetchone()
        return _row_to_task(row) if row else None

    def count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM task").fetchone()
        return int(row["n"]) if row else 0

    def revisions(self, task_id: str) -> list[TaskRevision]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM task_revision WHERE task_id = ? "
                "ORDER BY revision DESC",
                (task_id,),
            ).fetchall()
        return [_row_to_revision(r) for r in rows]

    def get_revision(self, task_id: str, revision: int) -> TaskRevision | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM task_revision WHERE task_id = ? AND revision = ?",
                (task_id, int(revision)),
            ).fetchone()
        return _row_to_revision(row) if row else None

    # ------------------------------------------------------------ 受控写
    def save_dry_run(
        self,
        *,
        task_id: str,
        revision: int,
        source_id: str,
        check: WriteCheck,
        rows: int,
        before: list[dict[str, Any]],
        after: list[dict[str, Any]],
        ttl_s: int = 300,
    ) -> dict[str, Any]:
        """记录一次预演结果，返回一次性凭据（提交时凭凭据 + 校验一致才生效）。"""
        moment = self._clock()
        now = moment.strftime(_TS_FORMAT)
        expires_at = (moment + timedelta(seconds=max(30, int(ttl_s)))).strftime(
            _TS_FORMAT
        )
        dry_run_id = f"wd-{uuid.uuid4().hex[:12]}"
        with self._connect() as conn:
            # 顺手清掉过期凭据，避免表越来越大。
            conn.execute("DELETE FROM write_dry_run WHERE expires_at < ?", (now,))
            conn.execute(
                "INSERT INTO write_dry_run (dry_run_id, task_id, project_id, "
                "revision, source_id, kind, table_name, sql, rows, before_json, "
                "after_json, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    dry_run_id, task_id, self._project_id, int(revision),
                    source_id, check.kind, check.table, check.sql, int(rows),
                    json.dumps(before, ensure_ascii=False, default=str),
                    json.dumps(after, ensure_ascii=False, default=str),
                    now, expires_at,
                ),
            )
        return {
            "dry_run_id": dry_run_id,
            "rows": int(rows),
            "before": before,
            "after": after,
            "expires_at": expires_at,
        }

    def get_dry_run(self, dry_run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM write_dry_run WHERE dry_run_id = ?",
                (dry_run_id,),
            ).fetchone()
            if row is None:
                return None
            # 时间戳统一是 "%Y-%m-%d %H:%M:%S"，可直接按字符串比较。
            if str(row["expires_at"]) < self._now():
                conn.execute(
                    "DELETE FROM write_dry_run WHERE dry_run_id = ?",
                    (dry_run_id,),
                )
                return None
        return {
            "dry_run_id": row["dry_run_id"],
            "task_id": row["task_id"],
            "project_id": row["project_id"],
            "revision": int(row["revision"]),
            "source_id": row["source_id"],
            "kind": row["kind"],
            "table": row["table_name"],
            "sql": row["sql"],
            "rows": int(row["rows"]),
            "before": _load_json_list(row["before_json"]),
            "after": _load_json_list(row["after_json"]),
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
        }

    def delete_dry_run(self, dry_run_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM write_dry_run WHERE dry_run_id = ?", (dry_run_id,)
            )

    def record_write_audit(
        self,
        *,
        task: Task,
        source_id: str,
        check: WriteCheck,
        status: str,
        rows: int = 0,
        error: str = "",
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO write_audit (project_id, task_id, task_name, "
                "revision, source_id, kind, table_name, sql, status, rows, error, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self._project_id, task.task_id, task.name, task.revision,
                    source_id, check.kind, check.table, check.sql, status,
                    int(rows), error[:500], self._now(),
                ),
            )
            return int(cursor.lastrowid or 0)

    def list_write_audit(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM write_audit ORDER BY audit_id DESC LIMIT ?",
                (max(1, min(200, int(limit))),),
            ).fetchall()
        return [
            {
                "audit_id": int(row["audit_id"]),
                "task_id": row["task_id"],
                "task_name": row["task_name"],
                "revision": int(row["revision"]),
                "source_id": row["source_id"],
                "kind": row["kind"],
                "table": row["table_name"],
                "sql": row["sql"],
                "status": row["status"],
                "rows": int(row["rows"]),
                "error": row["error"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    # ------------------------------------------------------------ 写入
    def create(
        self,
        name: str,
        sql: str,
        *,
        description: str = "",
        tags: tuple[str, ...] | list[str] = (),
        source_id: str = "",
        folder: str = "",
        kind: str = TASK_KIND_SQL,
        author: str = _AUTHOR_USER,
        note: str = "",
    ) -> Task:
        clean_name = _validate_name(name)
        clean_kind = validate_task_kind(kind)
        clean_sql = validate_task_content(clean_kind, sql)
        clean_folder = validate_folder_path(folder)
        if clean_folder and not self._folder_exists(clean_folder):
            raise TaskError(f"文件夹不存在：{clean_folder}（刷新后再试）")
        now = self._now()
        task_id = uuid.uuid4().hex[:12]
        clean_tags = _clean_tags(tags)
        clean_desc = (description or "").strip()[:1000]
        clean_source = (source_id or "").strip()[:64]
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO task (task_id, project_id, source_id, folder, kind, "
                "name, description, sql, tags, enabled, revision, created_by, "
                "updated_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?, ?)",
                (task_id, self._project_id, clean_source, clean_folder, clean_kind,
                 clean_name, clean_desc, clean_sql,
                 json.dumps(clean_tags, ensure_ascii=False), author, author,
                 now, now),
            )
            conn.execute(
                "INSERT INTO task_revision (task_id, revision, source_id, name, "
                "description, sql, note, author, created_at) "
                "VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, clean_source, clean_name, clean_desc, clean_sql,
                 (note or "创建任务")[:MAX_NOTE_CHARS], author, now),
            )
        created = self.get(task_id)
        assert created is not None
        return created

    def update(
        self,
        task_id: str,
        *,
        name: str | None = None,
        sql: str | None = None,
        description: str | None = None,
        tags: tuple[str, ...] | list[str] | None = None,
        enabled: bool | None = None,
        source_id: str | None = None,
        folder: str | None = None,
        kind: str | None = None,
        author: str = _AUTHOR_USER,
        note: str = "",
    ) -> Task:
        """更新任务并生成新版本。

        任意一次修改（哪怕只改描述）都会生成一个版本——
        「版本历史完整」比「版本少」重要，任务本来就是低频编辑的资产。
        """
        current = self.get(task_id)
        if current is None:
            raise TaskError(f"任务不存在: {task_id}")
        clean_name = _validate_name(name) if name is not None else current.name
        clean_kind = validate_task_kind(kind) if kind is not None else current.kind
        if sql is not None:
            clean_sql = validate_task_content(clean_kind, sql)
        elif kind is not None:
            # 只改类型也要保证当前正文对目标类型合法，不允许留下“跑不了”的任务。
            clean_sql = validate_task_content(clean_kind, current.sql)
        else:
            clean_sql = current.sql
        clean_desc = (
            (description or "").strip()[:1000]
            if description is not None else current.description
        )
        clean_tags = (
            _clean_tags(tags) if tags is not None else list(current.tags)
        )
        clean_enabled = current.enabled if enabled is None else bool(enabled)
        clean_source = (
            (source_id or "").strip()[:64]
            if source_id is not None else current.source_id
        )
        clean_folder = (
            validate_folder_path(folder) if folder is not None else current.folder
        )
        now = self._now()
        revision = current.revision + 1
        with self._connect() as conn:
            conn.execute(
                "UPDATE task SET name = ?, description = ?, sql = ?, tags = ?, "
                "enabled = ?, source_id = ?, folder = ?, kind = ?, revision = ?, "
                "updated_by = ?, updated_at = ? WHERE task_id = ?",
                (clean_name, clean_desc, clean_sql,
                 json.dumps(clean_tags, ensure_ascii=False), 1 if clean_enabled else 0,
                 clean_source, clean_folder, clean_kind, revision, author, now,
                 task_id),
            )
            conn.execute(
                "INSERT INTO task_revision (task_id, revision, source_id, name, "
                "description, sql, note, author, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, revision, clean_source, clean_name, clean_desc,
                 clean_sql, (note or "")[:MAX_NOTE_CHARS], author, now),
            )
        updated = self.get(task_id)
        assert updated is not None
        return updated

    def delete(self, task_id: str) -> bool:
        current = self.get(task_id)
        if current is None:
            return False
        with self._connect() as conn:
            run_ids = [
                row["run_id"]
                for row in conn.execute(
                    "SELECT run_id FROM workflow_run WHERE task_id = ?", (task_id,)
                ).fetchall()
            ]
            for run_id in run_ids:
                conn.execute(
                    "DELETE FROM workflow_run_step WHERE run_id = ?", (run_id,))
            conn.execute(
                "DELETE FROM workflow_run WHERE task_id = ?", (task_id,))
            conn.execute(
                "DELETE FROM workflow_schedule WHERE task_id = ?", (task_id,))
            # 同步任务：水位线、预演凭据、审计一并清理（避免孤儿数据）。
            conn.execute("DELETE FROM sync_state WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM sync_preview WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM sync_event WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM task_revision WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM task WHERE task_id = ?", (task_id,))
        return True

    def mark_run(self, task_id: str, status: str) -> None:
        """记录最近一次执行（只更新运行元数据，**不生成版本**）。"""
        with self._connect() as conn:
            conn.execute(
                "UPDATE task SET last_run_at = ?, last_status = ? WHERE task_id = ?",
                (self._now(), (status or "")[:40], task_id),
            )

    # ------------------------------------------------------------ 文件夹
    def _folder_exists(self, path: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM task_folder WHERE path = ?", (path,)
            ).fetchone()
        return row is not None

    def folders(self) -> list[dict[str, Any]]:
        """列出文件夹（每个还带直接子任务数量，便于前端画树上的计数）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT f.path, f.created_by, f.created_at, "
                "(SELECT COUNT(*) FROM task t WHERE t.folder = f.path) AS task_count "
                "FROM task_folder f ORDER BY f.path"
            ).fetchall()
        return [dict(row) for row in rows]

    def create_folder(self, path: str, *, author: str = _AUTHOR_USER) -> dict[str, Any]:
        clean = validate_folder_path(path, allow_empty=False)
        if "/" in clean:
            parent = clean.rsplit("/", 1)[0]
            if not self._folder_exists(parent):
                raise TaskError(f"父文件夹不存在：{parent}（先建上一级）")
        if self._folder_exists(clean):
            raise TaskError(f"文件夹已存在：{clean}")
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO task_folder (path, created_by, created_at) "
                "VALUES (?, ?, ?)",
                (clean, author, now),
            )
        return {"path": clean, "created_by": author, "created_at": now,
                "task_count": 0}

    def rename_folder(
        self, path: str, new_path: str, *, author: str = _AUTHOR_USER
    ) -> dict[str, Any]:
        """重命名文件夹；子文件夹与其中的任务路径一起搬家（保留层级）。"""
        clean_old = validate_folder_path(path, allow_empty=False)
        clean_new = validate_folder_path(new_path, allow_empty=False)
        if clean_old == clean_new:
            raise TaskError("新名称与原名相同")
        if not self._folder_exists(clean_old):
            raise TaskError(f"文件夹不存在：{clean_old}")
        if clean_new.startswith(clean_old + "/"):
            raise TaskError("不能把文件夹移动到它自己内部")
        if "/" in clean_new:
            parent = clean_new.rsplit("/", 1)[0]
            if not self._folder_exists(parent):
                raise TaskError(f"父文件夹不存在：{parent}（先建上一级）")
        with self._connect() as conn:
            all_paths = [
                row["path"] for row in conn.execute("SELECT path FROM task_folder")
            ]
            affected = [
                p for p in all_paths if p == clean_old or p.startswith(clean_old + "/")
            ]
            moving = {
                p: clean_new + p[len(clean_old):] for p in affected
            }
            target_set = {p for p in all_paths if p not in moving}
            clash = [p for p in moving.values() if p in target_set]
            if clash:
                raise TaskError(f"目标位置已有同名文件夹：{clash[0]}")
            now = self._now()
            # 先删掉旧行，再按新路径插回，避免主键顺序冲突。
            for old, new in sorted(moving.items(), key=lambda kv: len(kv[0]), reverse=True):
                conn.execute("DELETE FROM task_folder WHERE path = ?", (old,))
            for old, new in sorted(moving.items(), key=lambda kv: len(kv[0])):
                conn.execute(
                    "INSERT INTO task_folder (path, created_by, created_at) "
                    "VALUES (?, ?, ?)",
                    (new, author, now),
                )
            # 任务行跟着改路径。用 Python 精确比对前缀（不用 SQL LIKE，避免 _ / % 通配）。
            task_folders = [
                row["folder"]
                for row in conn.execute(
                    "SELECT DISTINCT folder FROM task WHERE folder <> ''"
                )
            ]
            for tf in task_folders:
                for old, new in moving.items():
                    if tf == old or tf.startswith(old + "/"):
                        conn.execute(
                            "UPDATE task SET folder = ? WHERE folder = ?",
                            (new + tf[len(old):], tf),
                        )
                        break
        return {"path": clean_new, "renamed_from": clean_old}

    def delete_folder(self, path: str) -> int:
        """删除空文件夹（含子目录也要求先清空）。返回删掉的目录数。"""
        clean = validate_folder_path(path, allow_empty=False)
        if not self._folder_exists(clean):
            raise TaskError(f"文件夹不存在：{clean}")
        with self._connect() as conn:
            rows = [
                row["path"] for row in conn.execute("SELECT path FROM task_folder")
            ]
            affected = [
                p for p in rows if p == clean or p.startswith(clean + "/")
            ]
            placeholders = ",".join("?" for _ in affected)
            used = conn.execute(
                f"SELECT COUNT(*) AS n FROM task WHERE folder IN ({placeholders}) "
                "AND folder <> ''",
                tuple(affected),
            ).fetchone()
            if used and int(used["n"]) > 0:
                raise TaskError(
                    f"文件夹「{clean}」里还有 {int(used['n'])} 个任务，"
                    "先把任务移走或删除，再删文件夹"
                )
            for p in sorted(affected, key=len, reverse=True):
                conn.execute("DELETE FROM task_folder WHERE path = ?", (p,))
        return len(affected)

    # ------------------------------------------------- 工作流调度（一期）
    def workflow_schedule(self, task_id: str) -> dict[str, Any] | None:
        """读取工作流的 cron 配置；没配置过返回 None（默认不调度）。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_schedule WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def save_workflow_schedule(self, task_id: str, *, cron: str,
                               enabled: bool,
                               next_run_at: str = "") -> dict[str, Any]:
        """保存 cron 配置（存在即更新，保留 last_run_at）。"""
        now = self._now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_run_at FROM workflow_schedule WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            last_run_at = str(row["last_run_at"]) if row is not None else ""
            conn.execute(
                "INSERT INTO workflow_schedule (task_id, cron, enabled, "
                "next_run_at, last_run_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET "
                "cron = excluded.cron, enabled = excluded.enabled, "
                "next_run_at = excluded.next_run_at, "
                "updated_at = excluded.updated_at",
                (task_id, cron, 1 if enabled else 0, next_run_at,
                 last_run_at, now, now),
            )
        return {
            "task_id": task_id,
            "cron": cron,
            "enabled": bool(enabled),
            "next_run_at": next_run_at,
            "last_run_at": last_run_at,
        }

    def mark_schedule_fired(self, task_id: str, *, last_run_at: str,
                            next_run_at: str) -> None:
        """调度线程触发成功后推进 last_run_at / next_run_at。"""
        with self._connect() as conn:
            conn.execute(
                "UPDATE workflow_schedule SET last_run_at = ?, "
                "next_run_at = ?, updated_at = ? WHERE task_id = ?",
                (last_run_at, next_run_at, self._now(), task_id),
            )

    def list_schedules(self) -> list[dict[str, Any]]:
        """调度线程用：本项目所有 cron 配置（含未启用的，由调用方过滤）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM workflow_schedule ORDER BY task_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def create_workflow_run(self, task: Task, *, trigger: str,
                            steps: list[dict[str, Any]]) -> dict[str, Any]:
        """创建运行实例（状态 running，步骤初始 pending）。"""
        run_id = f"wf-{uuid.uuid4().hex[:12]}"
        started_at = self._now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO workflow_run (run_id, task_id, project_id, "
                "trigger, status, started_at) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, task.task_id, task.project_id, trigger,
                 "running", started_at),
            )
            for step in steps:
                conn.execute(
                    "INSERT INTO workflow_run_step (run_id, step_key, "
                    "task_id, task_name, depends_on, status) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (run_id, step["key"], step["task_id"],
                     step.get("task_name") or "",
                     json.dumps(step.get("depends_on") or [],
                                ensure_ascii=False),
                     "pending"),
                )
        return {
            "run_id": run_id,
            "task_id": task.task_id,
            "project_id": task.project_id,
            "trigger": trigger,
            "status": "running",
            "started_at": started_at,
            "finished_at": "",
        }

    def running_workflow_run(self, task_id: str) -> dict[str, Any] | None:
        """同一工作流同时只允许一个实例（触发与调度共用这个判断）。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_run WHERE task_id = ? "
                "AND status = 'running' ORDER BY started_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def update_workflow_step(self, run_id: str, step_key: str, *,
                             status: str, rows: int = 0, error: str = "",
                             started: bool = False,
                             finished: bool = False) -> None:
        """更新实例里某一步的状态；时间戳由调用方决定是否刷新。"""
        sets = ["status = ?", "rows = ?", "error = ?"]
        params: list[Any] = [status, rows, error]
        if started:
            sets.append("started_at = ?")
            params.append(self._now())
        if finished:
            sets.append("finished_at = ?")
            params.append(self._now())
        params.extend([run_id, step_key])
        with self._connect() as conn:
            conn.execute(
                f"UPDATE workflow_run_step SET {', '.join(sets)} "
                "WHERE run_id = ? AND step_key = ?",
                params,
            )

    def finish_workflow_run(self, run_id: str, *, status: str,
                            error: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE workflow_run SET status = ?, error = ?, "
                "finished_at = ? WHERE run_id = ?",
                (status, error, self._now(), run_id),
            )

    def list_workflow_runs(self, task_id: str,
                           *, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM workflow_run WHERE task_id = ? "
                "ORDER BY started_at DESC LIMIT ?",
                (task_id, int(limit)),
            ).fetchall()
        return [self._decorate_run(dict(row)) for row in rows]

    def workflow_run(self, run_id: str) -> dict[str, Any] | None:
        """实例详情（含步骤列表，按创建顺序）。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_run WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            step_rows = conn.execute(
                "SELECT * FROM workflow_run_step WHERE run_id = ? "
                "ORDER BY rowid",
                (run_id,),
            ).fetchall()
        run = self._decorate_run(dict(row))
        steps: list[dict[str, Any]] = []
        for step_row in step_rows:
            step = dict(step_row)
            try:
                step["depends_on"] = json.loads(step.get("depends_on") or "[]")
            except ValueError:
                step["depends_on"] = []
            steps.append(step)
        run["steps"] = steps
        return run

    def _decorate_run(self, run: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as conn:
            counts = conn.execute(
                "SELECT status, COUNT(*) AS n FROM workflow_run_step "
                "WHERE run_id = ? GROUP BY status",
                (run["run_id"],),
            ).fetchall()
        summary = {str(row["status"]): int(row["n"]) for row in counts}
        run["total_steps"] = sum(summary.values())
        run["succeeded_steps"] = summary.get("success", 0)
        run["failed_steps"] = summary.get("failed", 0)
        return run

    # ------------------------------------------------------------ 原生同步
    def sync_state(self, task_id: str) -> dict[str, Any] | None:
        """增量同步的水位线（首次增量按全量处理，返回 None）。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT task_id, watermark, rows, updated_at FROM sync_state "
                "WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return dict(row) if row else None

    def save_sync_state(self, task_id: str, *, watermark: str, rows: int) -> None:
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sync_state (task_id, watermark, rows, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(task_id) DO UPDATE SET "
                "watermark = excluded.watermark, rows = excluded.rows, "
                "updated_at = excluded.updated_at",
                (task_id, (watermark or "")[:500], int(rows), now),
            )

    def save_sync_preview(
        self,
        task: Task,
        *,
        mode: str,
        source_id: str,
        source_table: str,
        target_table: str,
        rows: int,
        watermark_before: str,
        watermark_after: str,
        ttl_s: int = 300,
    ) -> dict[str, Any]:
        """保存一次预演凭据（5 分钟有效，一次性）。"""
        now = datetime.now()
        created = now.strftime(_TS_FORMAT)
        expires = (now + timedelta(seconds=ttl_s)).strftime(_TS_FORMAT)
        preview_id = "sp-" + uuid.uuid4().hex[:12]
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM sync_preview WHERE expires_at <= ?", (created,))
            conn.execute(
                "INSERT INTO sync_preview (preview_id, task_id, project_id,"
                " revision, mode, source_id, source_table, target_table, rows,"
                " watermark_before, watermark_after, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (preview_id, task.task_id, task.project_id, int(task.revision),
                 mode, source_id, source_table, target_table, int(rows),
                 (watermark_before or "")[:500], (watermark_after or "")[:500],
                 created, expires),
            )
        return {
            "preview_id": preview_id,
            "expires_at": expires,
            "rows": int(rows),
            "watermark_before": watermark_before,
            "watermark_after": watermark_after,
        }

    def get_sync_preview(self, preview_id: str) -> dict[str, Any] | None:
        now = self._now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sync_preview WHERE preview_id = ?",
                (preview_id,),
            ).fetchone()
            if row is None:
                return None
            if str(row["expires_at"]) <= now:
                conn.execute(
                    "DELETE FROM sync_preview WHERE preview_id = ?",
                    (preview_id,),
                )
                return None
        return dict(row)

    def delete_sync_preview(self, preview_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM sync_preview WHERE preview_id = ?", (preview_id,))

    def record_sync_event(
        self,
        task: Task,
        *,
        status: str,
        mode: str = "",
        source_id: str = "",
        source_table: str = "",
        target_table: str = "",
        rows: int = 0,
        error: str = "",
        watermark_before: str = "",
        watermark_after: str = "",
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO sync_event (project_id, task_id, task_name,"
                " revision, mode, source_id, source_table, target_table,"
                " status, rows, error, watermark_before, watermark_after,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task.project_id, task.task_id, task.name[:80],
                 int(task.revision), mode, source_id, source_table,
                 target_table, status, int(rows), (error or "")[:500],
                 (watermark_before or "")[:500], (watermark_after or "")[:500],
                 self._now()),
            )
            return int(cur.lastrowid)

    def list_sync_events(
        self, *, task_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sync_event"
        params: list[Any] = []
        if task_id:
            sql += " WHERE task_id = ?"
            params.append(task_id)
        sql += " ORDER BY event_id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def move(self, task_id: str, folder: str) -> Task:
        """把任务移动到另一个文件夹（元数据操作，不生成新版本）。"""
        current = self.get(task_id)
        if current is None:
            raise TaskError(f"任务不存在: {task_id}")
        clean_folder = validate_folder_path(folder)
        if clean_folder and not self._folder_exists(clean_folder):
            raise TaskError(f"文件夹不存在：{clean_folder}")
        with self._connect() as conn:
            conn.execute(
                "UPDATE task SET folder = ?, updated_at = ? WHERE task_id = ?",
                (clean_folder, self._now(), task_id),
            )
        moved = self.get(task_id)
        assert moved is not None
        return moved


# ===================================================================== 行转换
def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        task_id=row["task_id"],
        project_id=row["project_id"],
        source_id=row["source_id"],
        name=row["name"],
        description=row["description"],
        sql=row["sql"],
        revision=int(row["revision"]),
        enabled=bool(row["enabled"]),
        tags=tuple(_clean_tags(_load_tags(row["tags"]))),
        folder=row["folder"] if "folder" in row.keys() else "",
        kind=row["kind"] if "kind" in row.keys() else TASK_KIND_SQL,
        created_by=row["created_by"],
        updated_by=row["updated_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_run_at=row["last_run_at"],
        last_status=row["last_status"],
    )


def _row_to_revision(row: sqlite3.Row) -> TaskRevision:
    return TaskRevision(
        task_id=row["task_id"],
        revision=int(row["revision"]),
        source_id=row["source_id"],
        name=row["name"],
        description=row["description"],
        sql=row["sql"],
        note=row["note"],
        author=row["author"],
        created_at=row["created_at"],
    )


def _load_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


def _load_json_list(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _clean_tags(tags: tuple[str, ...] | list[str] | None) -> list[str]:
    out: list[str] = []
    for tag in tags or ():
        text = str(tag).strip()
        if text and text not in out:
            out.append(text[:24])
    return out[:8]


# 供 `from nl2sql.tasks import *` 之外显式引用（测试用）
_AI_AUTHOR = _AUTHOR_AI
_USER_AUTHOR = _AUTHOR_USER
_AI_COMMENT_RE = re.compile(rf"--\s*{AI_COMMENT_PREFIX}[，,]\s*\d{{4}}-\d{{2}}-\d{{2}}")
