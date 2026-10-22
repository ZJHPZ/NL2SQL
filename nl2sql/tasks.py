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
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from .dialect.tokens import is_read_only, split_statements
from .errors import TaskError

__all__ = [
    "AI_COMMENT_PREFIX",
    "MAX_SQL_CHARS",
    "Task",
    "TaskRevision",
    "TaskStore",
    "default_task_store_path",
    "stamp_ai_comment",
    "validate_task_sql",
]

# AI 修改注释的固定前缀。用户要求格式为「ai修改，***时间」。
AI_COMMENT_PREFIX = "ai修改"
MAX_SQL_CHARS = 20000
MAX_NAME_CHARS = 80
MAX_NOTE_CHARS = 500

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"
_AI_TS_FORMAT = "%Y-%m-%d %H:%M"

_AUTHOR_USER = "user"
_AUTHOR_AI = "ai"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS task (
    task_id      TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL DEFAULT '',
    source_id    TEXT NOT NULL DEFAULT '',
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

CREATE INDEX IF NOT EXISTS idx_task_updated ON task(updated_at);
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
            # 兼容早期任务库（无 source_id 列）：加列而不是重建表。
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

    # ------------------------------------------------------------ 写入
    def create(
        self,
        name: str,
        sql: str,
        *,
        description: str = "",
        tags: tuple[str, ...] | list[str] = (),
        source_id: str = "",
        author: str = _AUTHOR_USER,
        note: str = "",
    ) -> Task:
        clean_name = _validate_name(name)
        clean_sql = validate_task_sql(sql)
        now = self._now()
        task_id = uuid.uuid4().hex[:12]
        clean_tags = _clean_tags(tags)
        clean_desc = (description or "").strip()[:1000]
        clean_source = (source_id or "").strip()[:64]
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO task (task_id, project_id, source_id, name, "
                "description, sql, tags, enabled, revision, created_by, "
                "updated_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?, ?)",
                (task_id, self._project_id, clean_source, clean_name,
                 clean_desc, clean_sql,
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
        clean_sql = validate_task_sql(sql) if sql is not None else current.sql
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
        now = self._now()
        revision = current.revision + 1
        with self._connect() as conn:
            conn.execute(
                "UPDATE task SET name = ?, description = ?, sql = ?, tags = ?, "
                "enabled = ?, source_id = ?, revision = ?, updated_by = ?, "
                "updated_at = ? WHERE task_id = ?",
                (clean_name, clean_desc, clean_sql,
                 json.dumps(clean_tags, ensure_ascii=False), 1 if clean_enabled else 0,
                 clean_source, revision, author, now, task_id),
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
