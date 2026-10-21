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
    #   "auto"             默认。有可用模型则用 llm，否则退化为 rule
    #   "agent"            **智能体模式**：模型在循环里自主选择工具、
    #                      看到失败后自我纠正、信息不足时向用户追问
    #   "hybrid_verified"  规则产出 → 模型验证切题性 → 不通过则模型重做
    #   "hybrid"           规则优先 + 模型只兜底"答不出"（省调用，准确率≈规则）
    #   "rule"             仅确定性规则，零模型依赖
    #   "llm"              仅模型驱动（单次规划，无循环）
    planner: str = "auto"
    llm: str | None = None                 # 注册表键；None 表示不使用模型
    clarify: bool = True                   # 允许 agent 主动追问（回合制）
    allow_freeform: bool = True            # 语义层未覆盖时是否自由生成（并标注未验证口径）

    # ---- agent 循环参数（都属于"使用者可自定义智能体细节"的一部分）----
    max_steps: int = 8                     # 单轮最多几步工具调用
    max_repeat: int = 2                    # 同一调用重复超过此数视为打转
    temperature: float = 0.0

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
                  credential_ref: str | None = None, label: str = "",
                  semantic_path: str | None = None) -> "NL2SQLConfig":
        self.sources[source_id] = SourceConfig(
            source_id=source_id, adapter="mysql", label=label or source_id,
            options=SourceOptions(host=host, port=port, user=user, database=database),
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
