"""配置：显式、可序列化、无隐式环境变量读取。

设计要点
--------
* **不读环境变量做隐式配置**——否则"同一个 config 行为不同"，测试与排障都变难。
  需要环境变量时由壳读取后显式填入。
* 状态目录默认 `data/`，测试传临时目录即可隔离。
* 密码不进配置（用 `credential_ref`），见 docs/02。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError

DEFAULT_STATE_DIR = "data"
DEFAULT_SOURCE_ID = "default"


@dataclass
class SourceOptions:
    """数据源连接参数。明文密码字段故意不存在。"""

    # sqlite
    path: str | None = None
    # mysql / 通用
    host: str | None = None
    port: int | None = None
    user: str | None = None
    database: str | None = None
    charset: str | None = None
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class SourceConfig:
    source_id: str
    adapter: str = "sqlite"
    label: str = ""
    options: SourceOptions = field(default_factory=SourceOptions)
    credential_ref: str | None = None
    semantic_path: str | None = None      # dbt metrics.yml

    def validate(self) -> None:
        if not self.source_id:
            raise ConfigError("source_id 不能为空")
        if self.adapter == "sqlite":
            if not self.options.path:
                raise ConfigError(f"[{self.source_id}] sqlite 适配器必须提供 options.path")
            if not Path(self.options.path).exists():
                raise ConfigError(f"[{self.source_id}] 数据库文件不存在: {self.options.path}")
        elif self.adapter == "mysql":
            if not (self.options.host and self.options.user and self.options.database):
                raise ConfigError(
                    f"[{self.source_id}] mysql 适配器需要 options.host / user / database"
                )
            if not self.credential_ref:
                raise ConfigError(
                    f"[{self.source_id}] 未提供 credential_ref；密码不得明文写在配置里"
                )
        elif self.adapter == "postgres":
            if not (self.options.host and self.options.user and self.options.database):
                raise ConfigError(
                    f"[{self.source_id}] postgres 适配器需要"
                    " options.host / user / database"
                )
            if not self.credential_ref:
                raise ConfigError(
                    f"[{self.source_id}] 未提供 credential_ref；密码不得明文写在配置里"
                )
        elif self.adapter == "jdbc":
            if not self.options.params.get("jdbc_url"):
                raise ConfigError(
                    f"[{self.source_id}] jdbc 适配器需要 options.params['jdbc_url']"
                )
            if not self.options.params.get("driver_class"):
                raise ConfigError(
                    f"[{self.source_id}] jdbc 适配器需要 options.params['driver_class']"
                )


@dataclass
class SafetyConfig:
    """安全策略。个人场景精简版，但底线不可关。"""

    read_only: bool = True                 # 强制只读（不建议关闭）
    max_rows: int = 1000                   # 强制 LIMIT
    timeout_s: float = 30.0
    max_scan_rows: int | None = None       # 超过则执行前告警（需 EXPLAIN 支持）
    forbid_multiple_statements: bool = True
    forbid_ddl_dml: bool = True


@dataclass
class CacheConfig:
    """结果缓存。跨会话意图缓存已明确去掉（见 docs/02 决策）。"""

    enabled: bool = True
    ttl_seconds: int = 900                 # 15 分钟；不做 CDC，靠 TTL + 手动刷新
    show_data_time: bool = True            # 界面明示数据时间


@dataclass
class KnowledgeConfig:
    enabled: bool = True
    top_k: int = 8
    hybrid: bool = True                    # BM25 + 向量
    auto_generate_from_comments: bool = True
    embedder: str = "hash"                 # 默认免依赖实现，可换本地/远端模型
    vector_store: str = "memory"           # "memory" | "sqlite_vec" | "lancedb"


@dataclass
class AgentConfig:
    graph: str = "default"                 # 注册表中的 GraphSpec 名
    # 规划器模式：
    #   "agent"            **默认**：智能体模式。模型在循环里自主选择工具、
    #                      看到失败后自我纠正、信息不足时向用户追问。
    #                      需要可用模型；没有模型时自动回落线性管线（rule），
    #                      不会因为没 Key 就启动失败。
    #   "auto"             旧默认。有可用模型则 hybrid（规则优先+模型兜底），
    #                      否则 rule —— **没有工具循环**，速度最快、最确定
    #   "hybrid_verified"  规则产出 → 模型验证切题性 → 不通过则模型重做
    #   "hybrid"           规则优先 + 模型只兜底"答不出"（省调用，准确率≈规则）
    #   "rule"             仅确定性规则，零模型依赖
    #   "llm"              仅模型驱动（单次规划，无循环）
    #
    # 为什么默认 agent：智能体是本项目的特色能力（自己选工具、看失败重试、
    # 信息不足时追问而不是瞎编）。代价是每次问答多 1–2 次模型调用、
    # 耗时从亚秒级变成数秒级；批量/可复现场景可以显式写 "rule" 或 "hybrid"。
    planner: str = "agent"
    llm: str | None = None                 # 注册表键；None 表示不使用模型
    clarify: bool = True                   # 允许 agent 主动追问（回合制）
    allow_freeform: bool = True            # 语义层未覆盖时是否自由生成（并标注未验证口径）

    # ---- agent 循环参数（都属于"使用者可自定义智能体细节"的一部分）----
    max_steps: int = 8                     # 单轮最多几步工具调用
    max_repeat: int = 2                    # 同一调用重复超过此数视为打转
    temperature: float = 0.0

    # ---- 执行器 ----
    # 同一套 `AgentCore` 逻辑有两种调度方式，结果必须逐字一致：
    #   "auto"       默认。装了 langgraph 就用图执行器，否则顺序执行器
    #   "loop"       顺序执行器（手写 while，零第三方依赖）
    #   "langgraph"  图执行器（节点 + 路由边，护栏本身是一个节点）
    # 为什么保留两种而不是只留图：`loop` 没有第三方依赖，且它是
    # **等价性的基准**——没有它就没有"两个执行器结果一致"这个可验证的命题。
    runtime: str = "auto"

    # ---- 能力授予 ----
    # **能力必须显式授予**：插件与内置工具都只能拿到这里列出的能力。
    # 未授予的能力对应的工具不会被注册，其说明也不会进入模型的工具清单。
    #
    # 配置文件里 `[capabilities].allow` 可以覆盖本默认值——
    # 面向个人工作者，用户是自己数据的主人，应由他决定授什么权。
    capabilities: tuple[str, ...] = (
        "semantic.read", "source.read", "knowledge.read",
        "knowledge.write", "cache.read", "cache.write",
        "session.read", "session.write", "llm.invoke", "clarify.ask",
        # M7 项目与任务：project.read 读项目信息；task.read/task.write 读写
        # **项目自己的任务库**（保存只读 SQL），不写业务数据源。
        "project.read", "task.read", "task.write",
    )

    # ---- 插件 ----
    # 目录为相对路径时按工作目录解析；空字符串表示完全不扫描插件目录。
    plugins_dir: str = "plugins"
    # **外部插件的加载白名单。空集 = 不加载任何外部插件。**
    #
    # 为什么默认空：插件是可执行代码，而"默认不运行别人的代码"是唯一
    # 站得住脚的默认值。要启用就在配置里点名（或写 ["*"] 表示全部）。
    # 这样即使 plugins/ 目录里被人放了东西，也不会在启动时被执行。
    enabled_plugins: tuple[str, ...] = ()

    # ---- 工具级启停 ----
    # 停用的工具名。停用后不注册，其说明也不进入模型的工具清单。
    # 这是**收紧**操作，与能力授予方向相反，因此默认允许。
    disabled_tools: tuple[str, ...] = ()

    # ---- 外置 Prompt ----
    # 组件名 → 模板文件路径（相对工作目录）。例如 {"decision": "prompts/decision.md"}
    prompt_files: dict[str, str] = field(default_factory=dict)

    # ---- 运行期配置文件 ----
    # `config/nl2sql.toml` 的路径。空字符串表示不使用配置文件（全用代码默认）。
    config_path: str = "config/nl2sql.toml"


@dataclass
class SkillConfig:
    """技能（`SKILL.md`）配置。

    技能是"声明式档"的文件格式：纯文本的流程说明（playbook），
    按 `description` 匹配后**整份**加载。与知识库的分工是
    **事实 vs 流程**——事实按相关度竞争进 Prompt，流程按匹配选中。

    为什么不复用知识库：检索会按分数**截断**，而一份流程被切一半就不成立了。
    """

    enabled: bool = True
    # 项目技能目录（**默认加载**）：与代码同源、同一次评审。
    # 相对路径按工作目录解析。
    dirs: tuple[str, ...] = ("skills",)
    # 外部技能根：可能是 `npx skills add` 装的、给**别的 agent** 看的技能。
    # **默认一个都不加载**——与 plugins 的"默认不运行别人的代码"同一条原则，
    # 平移成"默认不加载别人的指令"。
    external_dirs: tuple[str, ...] = (
        ".dsh/skills", ".agents/skills", "~/.dsh/skills",
    )
    # 外部技能白名单。空 = 不加载任何外部技能；`"*"` = 全部加载。
    enabled_external: tuple[str, ...] = ()
    # 单份技能正文上限。超限**拒绝加载**而不是截断。
    max_body_chars: int = 20000


@dataclass
class NL2SQLConfig:
    """核心配置。一个实例 = 一套状态目录 + 一组数据源。"""

    state_dir: str = DEFAULT_STATE_DIR
    sources: dict[str, SourceConfig] = field(default_factory=dict)
    default_source: str | None = None
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)
    skills: SkillConfig = field(default_factory=SkillConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    # 相对时间锚点。None 表示使用真实当前时间；评测时应显式传入以保证可复现
    now: str | None = None

    # ------------------------------------------------------------ 项目（M7）
    # 一个 NL2SQLConfig 仍只对应一个引擎实例；「项目」在 Web 层是一个项目
    # 目录 + 一份按项目构造的 NL2SQLConfig。核心层只关心三件事：
    #   1. 项目身份（注入 Prompt、任务归集、界面展示）
    #   2. 任务库路径（空字符串 = 不启用任务工具）
    #   3. 数据源口令（由壳在内存里注入，不写配置、不落源码树）
    project_id: str = "default"
    project_name: str = ""
    project_description: str = ""
    # 任务库（建议 `<state_dir>/tasks.db`）。空 = 不启用 project/task 工具。
    task_store_path: str = ""
    # source_id -> {"password": "..."}；空则回落到 credential_ref 的环境变量。
    credentials: dict[str, dict[str, str]] = field(default_factory=dict)
    # 语义层模式：`auto` = 有 dbt 指标文件就用、否则用内置黄金集模型（旧行为）；
    # `dbt` = 必须用指标文件（缺路径报错）；`none` = 不装语义层（接任意业务库，
    # 由 agent 直接读库表写只读 SQL；`nl2sql.solve` 会因没有指标定义而失败）。
    semantic_mode: str = "auto"

    # ------------------------------------------------------------ 便捷构造
    def add_sqlite(self, path: str, *, source_id: str = DEFAULT_SOURCE_ID,
                   label: str = "", semantic_path: str | None = None) -> "NL2SQLConfig":
        self.sources[source_id] = SourceConfig(
            source_id=source_id, adapter="sqlite", label=label or source_id,
            options=SourceOptions(path=path), semantic_path=semantic_path,
        )
        self.default_source = self.default_source or source_id
        return self

    def add_mysql(self, *, host: str, user: str, database: str,
                  source_id: str = DEFAULT_SOURCE_ID, port: int = 3306,
                  credential_ref: str | None = "env:NL2SQL_MYSQL_PASSWORD",
                  label: str = "",
                  semantic_path: str | None = None) -> "NL2SQLConfig":
        """配置一个 MySQL 数据源。

        `credential_ref` 说明**口令去哪取**，而不是口令本身：
            `"env:NAME"`  从环境变量 NAME 读（默认 `env:NL2SQL_MYSQL_PASSWORD`）
            `"env"`       等价于 `env:NL2SQL_MYSQL_PASSWORD`
            其他前缀      暂不支持，会在装配时明确报错（不静默失败）
        密码永远不进配置文件，也不进源码树。
        """
        self.sources[source_id] = SourceConfig(
            source_id=source_id, adapter="mysql", label=label or source_id,
            options=SourceOptions(host=host, port=port, user=user, database=database),
            credential_ref=credential_ref, semantic_path=semantic_path,
        )
        self.default_source = self.default_source or source_id
        return self

    def add_postgres(self, *, host: str, user: str, database: str,
                     source_id: str = DEFAULT_SOURCE_ID, port: int = 5432,
                     credential_ref: str | None = "env:NL2SQL_PG_PASSWORD",
                     label: str = "",
                     semantic_path: str | None = None) -> "NL2SQLConfig":
        """配置一个 PostgreSQL 数据源（第三引擎）。

        `credential_ref` 说明**口令去哪取**，而不是口令本身：
            `"env:NAME"`  从环境变量 NAME 读（默认 `env:NL2SQL_PG_PASSWORD`）
            `"env"`       等价于 `env:NL2SQL_PG_PASSWORD`
            其他前缀      暂不支持，会在装配时明确报错（不静默失败）
        密码永远不进配置文件，也不进源码树。

        和 MySQL 的一处差别：本地 trust 认证的库允许口令为空，
        所以这里不强制"必须取到口令"（要不要口令由服务端决定）。
        """
        self.sources[source_id] = SourceConfig(
            source_id=source_id, adapter="postgres", label=label or source_id,
            options=SourceOptions(host=host, port=port, user=user, database=database),
            credential_ref=credential_ref, semantic_path=semantic_path,
        )
        self.default_source = self.default_source or source_id
        return self

    def add_jdbc(self, *, jdbc_url: str, driver_class: str,
                 source_id: str = DEFAULT_SOURCE_ID, label: str = "",
                 jar_paths: tuple[str, ...] = (),
                 user: str | None = None,
                 credential_ref: str | None = "env:NL2SQL_JDBC_PASSWORD",
                 semantic_path: str | None = None,
                 params: dict[str, Any] | None = None) -> "NL2SQLConfig":
        """配置一个 JDBC 泛用数据源（第四引擎，M7）。

        `jdbc_url` 形如 `jdbc:mysql://127.0.0.1:3306/shop`，`driver_class`
        形如 `com.mysql.cj.jdbc.Driver`，`jar_paths` 是驱动 jar 的本地路径
        （可视化连接向导会下载到项目目录）。口令仍走 `credential_ref`，
        永远不写进配置。

        为什么要有 JDBC：Python 没有原生 JDBC，但 Oracle / SQL Server /
        达梦等库通常只有 JDBC 驱动。参数化适配器（见
        `nl2sql/sources/jdbc.py`）用 Java 桥统一接入，只读语义与其它引擎一致。
        """
        merged: dict[str, Any] = dict(params or {})
        merged.update({
            "jdbc_url": jdbc_url,
            "driver_class": driver_class,
            "jar_paths": list(jar_paths),
        })
        self.sources[source_id] = SourceConfig(
            source_id=source_id, adapter="jdbc", label=label or source_id,
            options=SourceOptions(user=user, params=merged),
            credential_ref=credential_ref, semantic_path=semantic_path,
        )
        self.default_source = self.default_source or source_id
        return self

    def resolve_source(self, source_id: str | None = None) -> SourceConfig:
        sid = source_id or self.default_source
        if sid is None:
            if len(self.sources) == 1:
                sid = next(iter(self.sources))
            else:
                raise ConfigError("未指定数据源且没有 default_source")
        if sid not in self.sources:
            avail = ", ".join(sorted(self.sources)) or "（空）"
            raise ConfigError(f"未知数据源: {sid}；已配置: {avail}")
        return self.sources[sid]

    def validate(self) -> None:
        if not self.sources:
            raise ConfigError("至少需要配置一个数据源")
        for cfg in self.sources.values():
            cfg.validate()
        Path(self.state_dir).mkdir(parents=True, exist_ok=True)

    def state_path(self, *parts: str) -> Path:
        return Path(self.state_dir).joinpath(*parts)

    # 注：SourceConfig ↔ 存储记录的转换在 `storage/sqlite_meta.py`
    # （`source_config_to_storage` / `storage_to_source_config`），
    # 因为那属于持久化细节，配置层不应依赖存储层。
