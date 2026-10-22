"""壳层配置装配。

**壳的职责就在这里**：读环境变量、组装 `NL2SQLConfig`、注入模型提供者。
核心层刻意不读环境变量——否则"同一个配置在不同机器上行为不同"，
测试与排障都会变难。

环境变量（全部可选，缺省即降级）：
    NL2SQL_ADAPTER       数据源类型：sqlite（默认）/ mysql / postgres
    NL2SQL_DB            SQLite 数据库路径（默认 golden_set/ecommerce.db）
    NL2SQL_MYSQL_HOST    MySQL 主机（默认 127.0.0.1）
    NL2SQL_MYSQL_PORT    MySQL 端口（默认 3306）
    NL2SQL_MYSQL_USER    MySQL 账号
    NL2SQL_MYSQL_DATABASE
                         MySQL 库名
    NL2SQL_MYSQL_PASSWORD
                         MySQL 口令。**只从环境读，不进配置文件、不进页面**，
                         与 `NL2SQL_LLM_API_KEY` 同一条规矩。
    NL2SQL_PG_HOST       PostgreSQL 主机（默认 127.0.0.1）
    NL2SQL_PG_PORT       PostgreSQL 端口（默认 5432）
    NL2SQL_PG_USER       PostgreSQL 账号
    NL2SQL_PG_DATABASE   PostgreSQL 库名
    NL2SQL_PG_PASSWORD   PostgreSQL 口令。同样只从环境读；
                         trust 认证的本地库可以不设。
    NL2SQL_STATE_DIR     状态目录（默认 data）
    NL2SQL_SOURCE_ID     数据源标识（默认 default）
    NL2SQL_CONFIG        运行配置文件路径（默认 config/nl2sql.toml）
    NL2SQL_LLM_API_KEY   模型 Key；不设则自动退化为规则规划器
    NL2SQL_LLM_BASE_URL  模型端点（默认 DeepSeek）
    NL2SQL_LLM_MODEL     模型名（默认 deepseek-chat）
    NL2SQL_PLANNER       rule / llm / hybrid / hybrid_verified / agent / auto
    NL2SQL_RUNTIME       auto / loop / langgraph
                         智能体执行器。auto = 装了 langgraph 就用图执行器，
                         否则用顺序执行器。**两者结果必须逐字一致**，
                         所以换它不是"换个跑法"，而是"换个调度方式"。
    NL2SQL_PLUGINS       启用的外部插件，逗号分隔；留空 = 不加载（默认）。
                         写 `*` 表示加载 plugins/ 下全部。
    NL2SQL_METRICS       dbt metrics.yml 路径；不设则用内置参考语义模型
    NL2SQL_SKILLS        项目技能目录，逗号分隔（默认 skills）
    NL2SQL_SKILLS_ENABLED
                         要启用的**外部**技能名，逗号分隔；留空 = 一个都不加载。
                         外部技能指 .dsh/skills、.agents/skills、~/.dsh/skills
                         里的技能（可能是 npx skills add 装的、给别的 agent 看的）。
                         写 `*` 表示全部加载。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from nl2sql import NL2SQLConfig
from nl2sql.llm import build_provider

DEFAULT_DB = os.path.join("golden_set", "ecommerce.db")


@dataclass
class ShellConfig:
    """壳层解析后的运行参数，供 `/api/info` 展示与排障。"""

    db_path: str = DEFAULT_DB
    # 数据源类型：sqlite（默认）/ mysql / postgres。换引擎**只改这一个变量**，
    # 上层（规划器 / 编译器 / 守门 / 缓存）一行都不用动 ——
    # 这正是 M1 要证明的事。
    adapter: str = "sqlite"
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = ""
    mysql_database: str = ""
    pg_host: str = "127.0.0.1"
    pg_port: int = 5432
    pg_user: str = ""
    pg_database: str = ""
    state_dir: str = "data"
    source_id: str = "default"
    # 规划器模式。默认 agent（智能体是本项目特色：自主选工具/看失败重试/
    # 信息不足时追问）。没有模型时核心会自动回落线性 rule 管线，不会启动失败。
    # 想要最快最确定的路径可显式设 NL2SQL_PLANNER=rule / hybrid。
    planner: str = "agent"
    # 智能体执行器：auto / loop / langgraph
    runtime: str = "auto"
    config_path: str = "config/nl2sql.toml"
    plugins: tuple[str, ...] = ()
    # dbt metrics.yml 路径。空 = 用内置参考语义模型。
    metrics_path: str = ""
    # 项目技能目录（默认 skills）。空 = 不用技能目录。
    skills_dirs: tuple[str, ...] = ("skills",)
    # **外部**技能白名单。空 = 一个都不加载（与插件同一条原则）。
    skills_enabled: tuple[str, ...] = ()
    llm_vendor: str = "deepseek"
    llm_base_url: str = ""
    llm_model: str = ""
    llm_key_present: bool = False
    raw_env: dict[str, str] = field(default_factory=dict)


def read_env(env: dict[str, str] | None = None) -> ShellConfig:
    e = env if env is not None else os.environ
    key = e.get("NL2SQL_LLM_API_KEY", "").strip()
    return ShellConfig(
        db_path=e.get("NL2SQL_DB", DEFAULT_DB),
        adapter=(e.get("NL2SQL_ADAPTER", "sqlite") or "sqlite").strip().lower(),
        mysql_host=e.get("NL2SQL_MYSQL_HOST", "127.0.0.1"),
        mysql_port=int(e.get("NL2SQL_MYSQL_PORT", "3306") or 3306),
        mysql_user=e.get("NL2SQL_MYSQL_USER", ""),
        mysql_database=e.get("NL2SQL_MYSQL_DATABASE", ""),
        pg_host=e.get("NL2SQL_PG_HOST", "127.0.0.1"),
        pg_port=int(e.get("NL2SQL_PG_PORT", "5432") or 5432),
        pg_user=e.get("NL2SQL_PG_USER", ""),
        pg_database=e.get("NL2SQL_PG_DATABASE", ""),
        state_dir=e.get("NL2SQL_STATE_DIR", "data"),
        source_id=e.get("NL2SQL_SOURCE_ID", "default"),
        planner=e.get("NL2SQL_PLANNER", "agent"),
        runtime=(e.get("NL2SQL_RUNTIME", "auto") or "auto").strip().lower(),
        # 运行配置文件的路径**必须可注入**：否则测试之间、以及多份部署
        # 会共用同一个文件，互相覆盖对方的停用清单与能力设置。
        config_path=e.get("NL2SQL_CONFIG", "config/nl2sql.toml"),
        # 外部插件白名单：留空 = 不加载（默认）。插件是可执行代码，
        # "默认不运行别人的代码"是唯一站得住脚的默认值。
        plugins=tuple(
            p.strip() for p in e.get("NL2SQL_PLUGINS", "").split(",") if p.strip()
        ),
        # 语义层来自哪个 dbt 指标文件。指向不存在的文件时核心会报错——
        # 这是对的：静默退化成内置模型会让用户以为自己的口径生效了。
        metrics_path=e.get("NL2SQL_METRICS", "").strip(),
        # 技能：项目目录默认加载；外部技能默认一个都不加载，需要点名。
        # "默认不加载别人的指令"与"默认不运行别人的代码"是同一条原则。
        skills_dirs=tuple(
            p.strip() for p in e.get("NL2SQL_SKILLS", "skills").split(",")
            if p.strip()
        ),
        skills_enabled=tuple(
            p.strip() for p in e.get("NL2SQL_SKILLS_ENABLED", "").split(",")
            if p.strip()
        ),
        llm_vendor=e.get("NL2SQL_LLM_VENDOR", "deepseek"),
        llm_base_url=e.get("NL2SQL_LLM_BASE_URL", ""),
        llm_model=e.get("NL2SQL_LLM_MODEL", ""),
        llm_key_present=bool(key),
        raw_env={
            "NL2SQL_ADAPTER": e.get("NL2SQL_ADAPTER", ""),
            "NL2SQL_DB": e.get("NL2SQL_DB", ""),
            "NL2SQL_MYSQL_HOST": e.get("NL2SQL_MYSQL_HOST", ""),
            "NL2SQL_MYSQL_USER": e.get("NL2SQL_MYSQL_USER", ""),
            "NL2SQL_MYSQL_DATABASE": e.get("NL2SQL_MYSQL_DATABASE", ""),
            "NL2SQL_PG_HOST": e.get("NL2SQL_PG_HOST", ""),
            "NL2SQL_PG_USER": e.get("NL2SQL_PG_USER", ""),
            "NL2SQL_PG_DATABASE": e.get("NL2SQL_PG_DATABASE", ""),
            "NL2SQL_PLANNER": e.get("NL2SQL_PLANNER", ""),
            "NL2SQL_RUNTIME": e.get("NL2SQL_RUNTIME", ""),
            "NL2SQL_CONFIG": e.get("NL2SQL_CONFIG", ""),
            "NL2SQL_PLUGINS": e.get("NL2SQL_PLUGINS", ""),
            "NL2SQL_METRICS": e.get("NL2SQL_METRICS", ""),
            "NL2SQL_SKILLS": e.get("NL2SQL_SKILLS", ""),
            "NL2SQL_SKILLS_ENABLED": e.get("NL2SQL_SKILLS_ENABLED", ""),
            "NL2SQL_LLM_BASE_URL": e.get("NL2SQL_LLM_BASE_URL", ""),
            "NL2SQL_LLM_MODEL": e.get("NL2SQL_LLM_MODEL", ""),
        },
    )


def build_shell_config(shell: ShellConfig) -> tuple[NL2SQLConfig, Any]:
    """按壳层配置组装核心配置与模型提供者。

    Returns:
        (core_config, llm_provider)。provider 在没有 Key 时是 `NullLLM`，
        核心会据此自动退化为规则规划器——**"没有模型"是正常状态，不是错误**。
    """
    from nl2sql.config_file import apply_to_agent_config, load_runtime_settings

    cfg = NL2SQLConfig(state_dir=shell.state_dir)
    if shell.adapter == "postgres":
        # 口令来源规则同 MySQL：credential_ref 说明"去哪个环境变量取"。
        # trust 认证的本地库取不到口令也能连（由服务端决定要不要口令）。
        cfg.add_postgres(host=shell.pg_host, port=shell.pg_port,
                         user=shell.pg_user, database=shell.pg_database,
                         source_id=shell.source_id,
                         credential_ref="env:NL2SQL_PG_PASSWORD",
                         label=shell.source_id,
                         semantic_path=shell.metrics_path or None)
    elif shell.adapter == "mysql":
        # 口令不在 ShellConfig 里，读时现取；核心层的 credential_ref 会说
        # 明"去哪个环境变量取"，所以这里不需要把口令传第二遍。
        cfg.add_mysql(host=shell.mysql_host, port=shell.mysql_port,
                      user=shell.mysql_user, database=shell.mysql_database,
                      source_id=shell.source_id,
                      credential_ref="env:NL2SQL_MYSQL_PASSWORD",
                      label=shell.source_id,
                      semantic_path=shell.metrics_path or None)
    else:
        cfg.add_sqlite(shell.db_path, source_id=shell.source_id,
                       label=shell.source_id,
                       semantic_path=shell.metrics_path or None)
    cfg.agent.planner = shell.planner
    cfg.agent.config_path = shell.config_path
    if shell.plugins:
        cfg.agent.enabled_plugins = shell.plugins
    # 技能：项目目录 + 外部白名单
    cfg.skills.dirs = shell.skills_dirs
    cfg.skills.enabled_external = shell.skills_enabled

    # 运行配置文件里的设置合并进来（文件不存在时是空设置，一切照常）
    settings = load_runtime_settings(shell.config_path)
    cfg.agent = apply_to_agent_config(cfg.agent, settings)
    # 执行器：环境变量在配置文件之后生效（配置文件目前不管这个键）
    cfg.agent.runtime = shell.runtime

    provider = build_provider(
        api_key=os.environ.get("NL2SQL_LLM_API_KEY") or None,
        base_url=shell.llm_base_url or None,
        model=shell.llm_model or None,
        vendor=shell.llm_vendor,
    )
    return cfg, provider
