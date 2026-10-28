"""请求 / 响应模型。

设计原则：**响应形状由 `QueryOutcome` 决定，不在这里重新发明**。
后端的 status/notes/suggestions/candidates 等字段本身就是为界面准备的，
这一层只做 JSON 安全的转换。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


def _must_not_be_blank(v: str) -> str:
    """拒绝纯空白输入。

    只靠 `min_length=1` 不够——`"   "` 长度是 3，能通过长度校验，
    但业务上等同于空输入，会一路走到规划器才失败。
    界面虽然禁用了按钮，但**接口必须自己守住**，
    否则任何脚本调用都能绕过。
    """
    if not v or not v.strip():
        raise ValueError("不能为空或纯空白")
    return v.strip()


# --------------------------------------------------------------- 请求
class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    source_id: str | None = None
    now: str | None = Field(
        default=None,
        description="相对时间锚点（ISO 日期）。留空用配置值；显式传入可保证可复现",
    )
    use_cache: bool | None = None
    session_id: str | None = Field(
        default=None,
        description=(
            "会话标识。**agent 模式下必须带上**，否则多轮上下文（尤其澄清）会断。"
            "首次提问留空，服务端会生成并在 notes 里回传。"
        ),
    )

    _v_question = field_validator("question")(_must_not_be_blank)


class ExecuteRequest(BaseModel):
    """手写 SQL 执行。

    **必须走 guard**——这是安全边界，不是便利接口。
    若允许直接执行，界面就成了绕过只读与 LIMIT 的旁路。
    """

    sql: str = Field(..., min_length=1, max_length=20000)
    source_id: str | None = None
    plan_fingerprint: str | None = None

    _v_sql = field_validator("sql")(_must_not_be_blank)


class SnapshotRequest(BaseModel):
    source_id: str | None = None
    force: bool = False


class KnowledgeUpsertRequest(BaseModel):
    key: str | None = None
    type: Literal["schema", "business", "sql_pair", "correction"] = "business"
    title: str = ""
    body: str = ""
    source_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    note: str = ""


class KnowledgeToggleRequest(BaseModel):
    enabled: bool


class KnowledgeTestRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    source_id: str | None = None
    top_k: int = 8


class CacheRefreshRequest(BaseModel):
    source_id: str | None = None


# --------------------------------------------------------------- 响应
class ResultPayload(BaseModel):
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    elapsed_ms: int = 0
    data_time: str | None = None
    cached: bool = False
    warnings: list[str] = Field(default_factory=list)


class QueryResponse(BaseModel):
    """与 `QueryOutcome` 一一对应。形态刻意保持扁平，便于前端直接渲染。"""

    status: str
    question: str
    source_id: str
    sql: str | None = None
    result: ResultPayload | None = None

    error_code: str | None = None
    message: str | None = None
    suggestions: list[str] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)
    applied_filters: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    plan_fingerprint: str | None = None
    cached: bool = False
    elapsed_ms: int = 0
    data_time: str | None = None
    trace: dict[str, int] = Field(default_factory=dict)

    # 供界面区分成功 / 可挽救 / 失败三态，避免前端自己推断
    ok: bool = False
    planner_mode: str = ""
    # 本轮所属会话。**界面必须把它存下来并在下一轮带上**——
    # agent 的澄清是多轮的，丢了它上下文就断了。首次为空，服务端返回后需记住。
    session_id: str | None = None
    # agent 的散文回答（含真实数字）。结果表在 `result` 里，两者互补：
    # 散文给结论，表格给可排序/可导出的明细。
    answer: str | None = None


class InfoResponse(BaseModel):
    """启动自检信息。界面用它决定展示哪些能力（如规划器选择器）。"""

    version: str
    planner_mode: str
    llm_available: bool
    llm_model: str = ""
    sources: list[str] = Field(default_factory=list)
    default_source: str | None = None
    capabilities: dict[str, Any] = Field(default_factory=dict)
    safety: dict[str, Any] = Field(default_factory=dict)
    cache: dict[str, Any] = Field(default_factory=dict)
    knowledge: dict[str, Any] = Field(default_factory=dict)
    snapshot: dict[str, Any] = Field(default_factory=dict)
    # 语义层来源与可移植性（用 dbt metrics.yml 时有话要说：
    # 哪些指标在 dbt 里没有等价物、有多少能拿回 dbt 跑）
    semantic: dict[str, Any] = Field(default_factory=dict)
    # 智能体执行器：名字、要的是什么、有没有降级、图结构。
    # 用户有权知道"这句话是我自己循环跑的，还是 LangGraph 跑的"。
    runtime: dict[str, Any] = Field(default_factory=dict)
    # 引擎是否装配成功。**失败时 /api/info 仍然返回 200**——
    # 界面要能显示"为什么没起来"，而不是拿一个 503 干瞪眼。
    engine_ready: bool = True
    startup_warnings: list[str] = Field(default_factory=list)


class TableInfo(BaseModel):
    name: str
    comment: str = ""
    row_estimate: int | None = None
    column_count: int = 0
    columns: list[dict[str, Any]] = Field(default_factory=list)
    foreign_keys: list[list[str]] = Field(default_factory=list)


class KnowledgeItem(BaseModel):
    key: str
    type: str
    title: str
    body: str
    source_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True
    origin: str = "user"
    source_ref: str = ""
    version: int = 1
    created_at: str = ""
    updated_at: str = ""


class KnowledgeHit(BaseModel):
    key: str
    type: str
    title: str
    body: str
    score: float
    why: str = ""


class HistoryItem(BaseModel):
    id: str
    source_id: str
    question: str
    sql: str = ""
    status: str
    row_count: int = 0
    elapsed_ms: int = 0
    created_at: str = ""
    error_code: str | None = None
    error_message: str | None = None
    plan_fingerprint: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: str
    detail: str = ""


# --------------------------------------------------------------- 项目（M7）
class ProjectCreateRequest(BaseModel):
    """新建项目。`id` 可省略，服务端从名称生成稳定 slug。"""

    id: str | None = Field(
        default=None,
        description="项目 id（小写字母开头，可含数字/_/-）。省略则从名称生成",
    )
    name: str = Field(default="", max_length=80)
    description: str = Field(default="", max_length=500)
    semantic: Literal["none", "dbt", "auto", "golden"] = "none"
    metrics_path: str = Field(default="", max_length=500)

    _v_name = field_validator("name")(_must_not_be_blank)


class DataWriteSettings(BaseModel):
    """项目受控写设置：默认关闭；source_id 指写入目标数据源；上限按行数。"""

    enabled: bool = False
    source_id: str = Field(default="", max_length=64)
    max_affected: int = Field(default=100, ge=1, le=1000)


class ProjectUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    semantic: Literal["none", "dbt", "auto", "golden"] | None = None
    metrics_path: str | None = Field(default=None, max_length=500)
    planner: str | None = None
    runtime: str | None = None
    data_write: DataWriteSettings | None = None


class SourceRequest(BaseModel):
    """新增/修改数据源，也用于「测试连接」。

    `password` 只用于写入项目 secrets（或测试时临时使用），**永不回传**。
    """

    source_id: str = Field(default="", max_length=32)
    adapter: Literal["sqlite", "mysql", "postgres", "jdbc"]
    label: str = Field(default="", max_length=80)
    path: str | None = Field(default=None, max_length=1000)
    host: str | None = Field(default=None, max_length=200)
    port: int | None = None
    user: str | None = Field(default=None, max_length=200)
    database: str | None = Field(default=None, max_length=200)
    charset: str | None = Field(default=None, max_length=32)
    password: str | None = Field(
        default=None,
        description="口令（可选）。保存时写入项目 secrets.json；不回传",
    )
    credential_ref: str | None = Field(default=None, max_length=120)
    jdbc_url: str | None = Field(default=None, max_length=1000)
    driver_class: str | None = Field(default=None, max_length=200)
    jar_paths: list[str] = Field(default_factory=list)
    read_only: bool = True


class DriverDownloadRequest(BaseModel):
    kind: str = Field(..., min_length=1, max_length=32)


# --------------------------------------------------------------- 任务（M7）
class TaskCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    sql: str = Field(..., min_length=1, max_length=20000)
    description: str = Field(default="", max_length=1000)
    tags: list[str] = Field(default_factory=list)
    source_id: str | None = None
    folder: str = Field(default="", max_length=200, description="任务所在文件夹路径")
    kind: Literal["sql", "pyspark", "write", "workflow"] = Field(
        default="sql",
        description=("任务类型：sql 只读可运行；pyspark 暂只保存代码；"
                     "write 受控写；workflow 工作流步骤 DAG"),
    )

    _v_name = field_validator("name")(_must_not_be_blank)
    _v_sql = field_validator("sql")(_must_not_be_blank)


class TaskUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    sql: str | None = Field(default=None, max_length=20000)
    description: str | None = Field(default=None, max_length=1000)
    tags: list[str] | None = None
    enabled: bool | None = None
    source_id: str | None = None
    folder: str | None = Field(default=None, max_length=200)
    kind: Literal["sql", "pyspark", "write", "workflow"] | None = None
    note: str = Field(default="", max_length=500)


class WorkflowScheduleUpdate(BaseModel):
    """工作流 cron 配置：5 段标准 cron（分 时 日 月 周）。"""

    cron: str = Field(default="", max_length=120,
                      description="5 段 cron，例如 0 2 * * *")
    enabled: bool = False


class TaskMoveRequest(BaseModel):
    folder: str = Field(default="", max_length=200)


class TaskFolderCreateRequest(BaseModel):
    path: str = Field(..., min_length=1, max_length=200)

    _v_path = field_validator("path")(_must_not_be_blank)


class TaskFolderRenameRequest(BaseModel):
    path: str = Field(..., min_length=1, max_length=200)
    new_path: str = Field(..., min_length=1, max_length=200)

    _v_path = field_validator("path")(_must_not_be_blank)
    _v_new = field_validator("new_path")(_must_not_be_blank)


class TaskRunRequest(BaseModel):
    source_id: str | None = None


# --------------------------------------------------------------- 受控写（P1）
class WriteDryRunRequest(BaseModel):
    """预演写：不传 source_id，一律用项目 data_write 里绑定的目标源。"""

    source_id: str | None = Field(
        default=None, description="保留字段；P1 只允许项目设置里的写入源",
    )


class WriteCommitRequest(BaseModel):
    """凭预演凭据提交。must confirm=True，避免误触。"""

    dry_run_id: str = Field(..., min_length=1, max_length=64)
    confirm: bool = Field(default=False, description="必须显式确认才提交")
