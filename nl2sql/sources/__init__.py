"""数据源适配层：能力声明 + 元数据抽取 + 执行。

**新增数据源 = 加一个文件 + 在 `_BUILTIN` 里加一行**，不改核心任何代码。

MySQL 是这条路的第一个真实用例：`nl2sql/sources/mysql.py` 一个文件
加上下面 `_BUILTIN` 的一行，就把"只支持 SQLite"变成了"两个引擎"。
这条声明以前没有证据支撑（只跑过 SQLite），现在有了。

PostgreSQL 是第二个用例：`nl2sql/sources/postgres.py` 同样一个文件 +
`_BUILTIN` 一行 + `config.add_postgres`，就成了第三个引擎。
**核心层（语义/编译/守门/管线）没有改一行** —— 这不是目标本身，
而是"capability 驱动降级"这条设计原则的直接结果。

JDBC 是第四个用例：`nl2sql/sources/jdbc.py` 一个文件 + `_BUILTIN` 一行，
把 Oracle / SQL Server / 达梦等"只有 JDBC 驱动"的数据库接进来
（Java 桥子进程加载驱动 jar，协议见 `jdbc.py` 模块 docstring）。
"""
from __future__ import annotations

import os
from typing import Callable

from ..config import SourceConfig
from ..contracts.source import SourceAdapter
from ..errors import ConfigError, NotRegisteredError
from ..registry import Registry
from .sqlite import SqliteAdapter

__all__ = ["build_adapter", "resolve_secret", "SqliteAdapter"]


_DEFAULT_SECRET_ENV = "NL2SQL_MYSQL_PASSWORD"


def resolve_secret(credential_ref: str | None,
                   credentials: dict[str, str]) -> str | None:
    """按 `credential_ref` 取口令。

    配置文件里说"去哪取"，而不是"是什么"：
        `"env:NAME"` → 环境变量 NAME
        `"env"`      → 默认环境变量
        `None`       → 也退回默认环境变量（给脚本用，不必写多余配置）

    认不出的前缀**报错而不是静默退回**：若把 `keyring:xxx` 悄悄当成
    环境变量去找，用户会拿到"口令为空"而不是"这种来源还不支持"。
    """
    if credentials.get("password"):
        return credentials["password"]

    ref = (credential_ref or "").strip()
    if not ref:
        return os.environ.get(_DEFAULT_SECRET_ENV)
    if ref == "env":
        return os.environ.get(_DEFAULT_SECRET_ENV)
    if ref.startswith("env:"):
        name = ref[4:].strip()
        if not name:
            raise ConfigError("credential_ref 写成 env: 但没有给变量名")
        return os.environ.get(name)
    raise ConfigError(
        f"不支持的 credential_ref: {ref!r}；目前只支持 'env:NAME' / 'env'"
    )


def _build_mysql(source_cfg: SourceConfig, *,
                 credentials: dict[str, str]) -> SourceAdapter:
    """从配置 + 凭据装配 MySQL 适配器。口令来源见 `resolve_secret`。"""
    from .mysql import MysqlAdapter

    opts = source_cfg.options
    password = resolve_secret(source_cfg.credential_ref, credentials)
    if not password:
        raise ConfigError(
            f"[{source_cfg.source_id}] mysql 未取到口令（credential_ref="
            f"{source_cfg.credential_ref!r}）：请设置对应环境变量，"
            "或在装配时注入 credentials={'password': ...}"
        )
    if not (opts.host and opts.user and opts.database):
        raise ConfigError(
            f"[{source_cfg.source_id}] mysql 需要 options.host / user / database"
        )
    return MysqlAdapter(
        host=opts.host,
        port=int(opts.port or 3306),
        user=opts.user,
        database=opts.database,
        password=password,
        charset=opts.charset or "utf8mb4",
        read_only=bool(opts.params.get("read_only", True)),
    )


def _build_postgres(source_cfg: SourceConfig, *,
                    credentials: dict[str, str]) -> SourceAdapter:
    """从配置 + 凭据装配 PostgreSQL 适配器。

    口令来源见 `resolve_secret`。POSTGRES 的 `credential_ref` 默认指向
    `NL2SQL_PG_PASSWORD`；本地 trust 认证时允许口令为空（不像 MySQL
    那样直接报错），因为要不要口令是服务端决定的，适配器不替它把关。
    """
    from .postgres import PostgresAdapter

    opts = source_cfg.options
    ref = source_cfg.credential_ref or "env:NL2SQL_PG_PASSWORD"
    password = resolve_secret(ref, credentials) or ""
    if not (opts.host and opts.user and opts.database):
        raise ConfigError(
            f"[{source_cfg.source_id}] postgres 需要 options.host / user / database"
        )
    return PostgresAdapter(
        host=opts.host,
        port=int(opts.port or 5432),
        user=opts.user,
        database=opts.database,
        password=password,
        schema=str(opts.params.get("schema") or "public"),
        read_only=bool(opts.params.get("read_only", True)),
    )


def _build_jdbc(source_cfg: SourceConfig, *,
                credentials: dict[str, str]) -> SourceAdapter:
    """从配置 + 凭据装配 JDBC 泛用适配器。

    配置约定（见 `config.add_jdbc`）::

        options.params["jdbc_url"]     必填，如 jdbc:mysql://127.0.0.1:3306/shop
        options.params["driver_class"] 必填，如 com.mysql.cj.jdbc.Driver
        options.params["jar_paths"]    必填，驱动 jar 绝对路径列表
                                       （可用 /api/jdbc/drivers/download 下载）

    可选参数：`dialect`（覆盖从 URL 推断的方言）、`read_only`（默认 True）、
    `bridge_dir`（Java 桥编译缓存目录）、`connect_timeout_s`。
    口令：默认 `NL2SQL_JDBC_PASSWORD` 环境变量；JDBC 允许无口令（SQLite/
    trust 认证），取不到时给空串而不是报错——要不要口令由数据库决定。
    """
    from .jdbc import JdbcAdapter

    params = source_cfg.options.params or {}
    jdbc_url = str(params.get("jdbc_url") or "").strip()
    driver_class = str(params.get("driver_class") or "").strip()
    if not jdbc_url or not driver_class:
        raise ConfigError(
            f"[{source_cfg.source_id}] jdbc 适配器需要 options.params['jdbc_url']"
            " 和 options.params['driver_class']"
        )

    raw_jar_paths = params.get("jar_paths") or []
    if isinstance(raw_jar_paths, (str, os.PathLike)):
        raw_jar_paths = [os.fspath(raw_jar_paths)]
    jar_paths = [str(p).strip() for p in raw_jar_paths if str(p).strip()]
    if not jar_paths:
        raise ConfigError(
            f"[{source_cfg.source_id}] jdbc 适配器需要 options.params['jar_paths']"
            "（驱动 jar 列表）：可调用 /api/jdbc/drivers/download 下载，如 kind=sqlite"
        )

    password = resolve_secret(source_cfg.credential_ref, credentials) or ""
    return JdbcAdapter(
        jdbc_url=jdbc_url,
        driver_class=driver_class,
        jar_paths=jar_paths,
        user=source_cfg.options.user or str(params.get("user") or ""),
        password=password,
        dialect=params.get("dialect"),
        read_only=bool(params.get("read_only", True)),
        bridge_dir=params.get("bridge_dir"),
        connect_timeout_s=float(params.get("connect_timeout_s") or 15.0),
    )


#: 内置适配器工厂。第三方数据源不必登记在这里——
#: 传 `registry` 覆盖即可（registry 优先）。
_BUILTIN: dict[str, Callable[..., SourceAdapter]] = {
    "mysql": _build_mysql,
    "postgres": _build_postgres,
    "jdbc": _build_jdbc,
}


def build_adapter(
    source_cfg: SourceConfig,
    *,
    registry: Registry | None = None,
    credentials: dict[str, str] | None = None,
) -> SourceAdapter:
    """按配置装配适配器。

    Args:
        source_cfg: 数据源配置。
        registry: 组件注册表；优先于内置工厂，便于外部替换同名适配器。
        credentials: 已解析的凭据（例如从 OS 凭据存储取出）。
            **密码不经过配置文件**，只在这里注入。

    Returns:
        实现 `SourceAdapter` 契约的适配器实例。
    """
    adapter_name = source_cfg.adapter
    creds = credentials or {}

    if adapter_name == "sqlite":
        path = source_cfg.options.path
        if not path:
            raise ConfigError(f"[{source_cfg.source_id}] sqlite 缺少 options.path")
        return SqliteAdapter(path)

    factory = None
    # 用 has() 而不是 get()：get() 在未注册时抛异常，
    # 而这里"注册表里没有"是正常情况（走内置工厂），
    # 真正该报错的是两边都没有。
    if registry is not None and registry.adapters.has(adapter_name):
        factory = registry.adapters.get(adapter_name)
    if factory is None:
        factory = _BUILTIN.get(adapter_name)
    if factory is None:
        known = ", ".join(sorted(set(_BUILTIN) | {"sqlite"}))
        raise NotRegisteredError(
            f"未知适配器 {adapter_name}；内置仅 {known}，"
            "其他数据源需要在 registry 里注册工厂"
        )
    return factory(source_cfg, credentials=creds)
