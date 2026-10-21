"""壳层配置装配。

**壳的职责就在这里**：读环境变量、组装 `NL2SQLConfig`、注入模型提供者。
核心层刻意不读环境变量——否则"同一个配置在不同机器上行为不同"，
测试与排障都会变难。

环境变量（全部可选，缺省即降级）：
    NL2SQL_DB            SQLite 数据库路径（默认 golden_set/ecommerce.db）
    NL2SQL_STATE_DIR     状态目录（默认 data）
    NL2SQL_SOURCE_ID     数据源标识（默认 default）
    NL2SQL_CONFIG        运行配置文件路径（默认 config/nl2sql.toml）
    NL2SQL_LLM_API_KEY   模型 Key；不设则自动退化为规则规划器
    NL2SQL_LLM_BASE_URL  模型端点（默认 DeepSeek）
    NL2SQL_LLM_MODEL     模型名（默认 deepseek-chat）
    NL2SQL_PLANNER       rule / llm / hybrid / hybrid_verified / agent / auto
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
    state_dir: str = "data"
    source_id: str = "default"
    planner: str = "auto"
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
        state_dir=e.get("NL2SQL_STATE_DIR", "data"),
        source_id=e.get("NL2SQL_SOURCE_ID", "default"),
        planner=e.get("NL2SQL_PLANNER", "auto"),
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
            "NL2SQL_DB": e.get("NL2SQL_DB", ""),
            "NL2SQL_PLANNER": e.get("NL2SQL_PLANNER", ""),
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

    provider = build_provider(
        api_key=os.environ.get("NL2SQL_LLM_API_KEY") or None,
        base_url=shell.llm_base_url or None,
        model=shell.llm_model or None,
        vendor=shell.llm_vendor,
    )
    return cfg, provider
