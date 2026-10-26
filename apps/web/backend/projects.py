"""多项目工作区：项目定义（文件系统） + 按项目装配引擎。

M7「数据开发工作台」的地基：

- **一个项目 = 一份数据源连接配置 + 语义层设置 + 自己的 SQL 任务库 +
  自己的 state 目录**。项目之间互不共享会话、缓存、知识库与任务。
- 项目定义落在 ``projects/<project_id>/project.toml``，**口令不进 toml**：
  单独放 ``secrets.json``，由后端读进 ``NL2SQLConfig.credentials``（内存），
  核心层与配置层都不需要知道口令存放在哪。
- **默认项目 ``default`` 不是文件**：它就是环境变量装配出来的那个引擎
  （保持旧行为，旧测试与 ``/api/query`` 调用完全不变）。只有显式打开
  ``NL2SQL_TASKS_ENABLED=1`` 时才给默认项目挂任务库。

为什么项目定义不放进 ``app.db``：项目是"工作区"，用户可能想备份 / 迁移 /
纳入 git；TOML 文本可读、可手改，和 `config/nl2sql.toml` 的风格一致。
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nl2sql.api import NL2SQL
from nl2sql.config import NL2SQLConfig, SourceConfig, SourceOptions
from nl2sql.errors import ConfigError, NL2SQLError

DEFAULT_PROJECT_ID = "default"
DEFAULT_PROJECT_NAME = "默认项目"

_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_ADAPTERS = ("sqlite", "mysql", "postgres", "jdbc")
_TRUTHY = {"1", "true", "yes", "on"}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _truthy(value: str | None) -> bool:
    return bool(value) and value.strip().lower() in _TRUTHY


def _clean(value: Any, *, limit: int = 200) -> str:
    return str(value or "").strip()[:limit]


def _toml_quote(value: str) -> str:
    """单行 TOML 字符串。项目字段都是短文本，转义换行/引号即可。"""
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("\n", "\\n").replace("\r", "\\r")
    return f'"{text}"'


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, str):
        return _toml_quote(value)
    return str(value)


class ProjectStore:
    """项目定义的读写（文件系统，项目目录即工作区）。"""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    # ----------------------------------------------------------- 路径
    def project_dir(self, project_id: str) -> str:
        return os.path.join(self.root, project_id)

    def _toml_path(self, project_id: str) -> str:
        return os.path.join(self.project_dir(project_id), "project.toml")

    def _secrets_path(self, project_id: str) -> str:
        return os.path.join(self.project_dir(project_id), "secrets.json")

    def exists(self, project_id: str) -> bool:
        return os.path.isfile(self._toml_path(project_id))

    # ----------------------------------------------------------- 读
    def list_ids(self) -> list[str]:
        ids: list[str] = []
        try:
            entries = sorted(os.listdir(self.root))
        except OSError:
            return ids
        for name in entries:
            if _ID_RE.match(name) and os.path.isfile(self._toml_path(name)):
                ids.append(name)
        return ids

    def get(self, project_id: str) -> dict[str, Any] | None:
        project_id = _clean(project_id, limit=64)
        if not project_id or not self.exists(project_id):
            return None
        try:
            with open(self._toml_path(project_id), "rb") as handle:
                data = _load_toml(handle)
        except (OSError, ValueError) as exc:
            raise ConfigError(f"项目 {project_id} 的 project.toml 读取失败：{exc}") from exc
        meta = dict(data)
        meta["id"] = str(meta.get("id") or project_id)
        meta.setdefault("name", meta["id"])
        meta.setdefault("description", "")
        meta.setdefault("semantic", "none")
        meta.setdefault("metrics_path", "")
        meta.setdefault("planner", "agent")
        meta.setdefault("runtime", "auto")
        meta.setdefault("created_at", "")
        meta.setdefault("updated_at", "")
        sources = meta.get("sources")
        meta["sources"] = [dict(item) for item in sources] if isinstance(sources, list) else []
        meta["data_write"] = normalize_data_write(meta.get("data_write"))
        return meta

    def list_projects(self) -> list[dict[str, Any]]:
        projects: list[dict[str, Any]] = []
        for project_id in self.list_ids():
            meta = self.get(project_id)
            if meta is not None:
                projects.append(meta)
        return projects

    # ----------------------------------------------------------- 写
    def create(self, project_id: str, *, name: str = "", description: str = "",
               semantic: str = "none", metrics_path: str = "") -> dict[str, Any]:
        project_id = _clean(project_id, limit=64).lower()
        if not _ID_RE.match(project_id):
            raise ConfigError(
                "项目 id 不合法：只允许小写字母开头，后接小写字母/数字/_/-，"
                "总长 1–32。例如 shop、project-a"
            )
        if self.exists(project_id):
            raise ConfigError(f"项目已存在：{project_id}")
        semantic = _clean(semantic, limit=16) or "none"
        if semantic not in ("none", "dbt", "auto", "golden"):
            raise ConfigError("semantic 只能是 none / dbt / auto")
        os.makedirs(self.project_dir(project_id), exist_ok=True)
        meta = {
            "id": project_id,
            "name": _clean(name, limit=80) or project_id,
            "description": _clean(description, limit=500),
            "semantic": semantic,
            "metrics_path": _clean(metrics_path, limit=500),
            "planner": "agent",
            "runtime": "auto",
            "created_at": _now(),
            "updated_at": _now(),
            "sources": [],
        }
        self._write(project_id, meta)
        return meta

    def update(self, project_id: str, **fields: Any) -> dict[str, Any]:
        meta = self.get(project_id)
        if meta is None:
            raise ConfigError(f"项目不存在：{project_id}")
        allowed = {"name", "description", "semantic", "metrics_path",
                   "planner", "runtime", "data_write"}
        for key, value in fields.items():
            if key not in allowed or value is None:
                continue
            if key == "semantic":
                value = _clean(value, limit=16) or "none"
                if value not in ("none", "dbt", "auto", "golden"):
                    raise ConfigError("semantic 只能是 none / dbt / auto")
            elif key == "data_write":
                value = normalize_data_write(value)
            elif key == "planner":
                value = _clean(value, limit=16) or "agent"
                if value not in ("agent", "auto", "hybrid", "hybrid_verified", "llm", "rule"):
                    raise ConfigError("planner 取值不合法")
            elif key == "runtime":
                value = _clean(value, limit=16) or "auto"
                if value not in ("auto", "loop", "langgraph"):
                    raise ConfigError("runtime 取值不合法")
            else:
                value = _clean(value, limit=500)
            meta[key] = value
        meta["updated_at"] = _now()
        self._write(project_id, meta)
        return meta

    def delete(self, project_id: str) -> None:
        project_id = _clean(project_id, limit=64)
        if project_id == DEFAULT_PROJECT_ID:
            raise ConfigError("默认项目不能删除")
        if not self.exists(project_id):
            raise ConfigError(f"项目不存在：{project_id}")
        shutil.rmtree(self.project_dir(project_id), ignore_errors=True)

    # ----------------------------------------------------------- 数据源
    def upsert_source(self, project_id: str, raw: dict[str, Any]) -> dict[str, Any]:
        meta = self.get(project_id)
        if meta is None:
            raise ConfigError(f"项目不存在：{project_id}")
        source = _normalize_source(raw)
        existing = {str(s.get("source_id")): s for s in meta["sources"]}
        existing[source["source_id"]] = source
        # 保持文件里数据源的顺序稳定：已有顺序 + 新增放末尾
        order = [str(s.get("source_id")) for s in meta["sources"]]
        if source["source_id"] not in order:
            order.append(source["source_id"])
        meta["sources"] = [existing[sid] for sid in order if sid in existing]
        meta["updated_at"] = _now()
        self._write(project_id, meta)
        return meta

    def remove_source(self, project_id: str, source_id: str) -> dict[str, Any]:
        meta = self.get(project_id)
        if meta is None:
            raise ConfigError(f"项目不存在：{project_id}")
        source_id = _clean(source_id, limit=64)
        meta["sources"] = [s for s in meta["sources"]
                           if str(s.get("source_id")) != source_id]
        meta["updated_at"] = _now()
        self._write(project_id, meta)
        self._write_secrets(project_id, {
            sid: value for sid, value in self.get_secrets(project_id).items()
            if sid != source_id
        })
        return meta

    # ----------------------------------------------------------- 口令
    def get_secrets(self, project_id: str) -> dict[str, dict[str, str]]:
        path = self._secrets_path(project_id)
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        raw = data.get("sources") if isinstance(data, dict) else None
        secrets: dict[str, dict[str, str]] = {}
        if isinstance(raw, dict):
            for sid, value in raw.items():
                if isinstance(value, dict):
                    secrets[str(sid)] = {
                        "password": str(value.get("password") or "")
                    }
        return secrets

    def set_secret(self, project_id: str, source_id: str, password: str) -> None:
        secrets = self.get_secrets(project_id)
        source_id = _clean(source_id, limit=64)
        if password:
            secrets[source_id] = {"password": str(password)}
        else:
            secrets.pop(source_id, None)
        self._write_secrets(project_id, secrets)

    def _write_secrets(self, project_id: str,
                       secrets: dict[str, dict[str, str]]) -> None:
        path = self._secrets_path(project_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {"sources": secrets}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        try:  # 尽力收紧权限；Windows 上 chmod 语义有限，仅作防御
            os.chmod(path, 0o600)
        except OSError:
            pass

    # ----------------------------------------------------------- 内部
    def _write(self, project_id: str, meta: dict[str, Any]) -> None:
        lines: list[str] = [
            "# 由 talk-to-sql 数据开发工作台生成；口令不写在这里。",
            f'id = {_toml_quote(str(meta.get("id") or project_id))}',
            f'name = {_toml_quote(str(meta.get("name") or ""))}',
            f'description = {_toml_quote(str(meta.get("description") or ""))}',
            f'semantic = {_toml_quote(str(meta.get("semantic") or "none"))}',
            f'metrics_path = {_toml_quote(str(meta.get("metrics_path") or ""))}',
            f'planner = {_toml_quote(str(meta.get("planner") or "agent"))}',
            f'runtime = {_toml_quote(str(meta.get("runtime") or "auto"))}',
            f'created_at = {_toml_quote(str(meta.get("created_at") or ""))}',
            f'updated_at = {_toml_quote(str(meta.get("updated_at") or ""))}',
            "",
        ]
        for source in meta.get("sources") or []:
            lines.append("[[sources]]")
            for key, value in source.items():
                if value in ("", None, [], ()):
                    continue
                lines.append(f"{key} = {_toml_value(value)}")
            lines.append("")
        data_write = normalize_data_write(meta.get("data_write"))
        if (data_write["enabled"] or data_write["source_id"]
                or data_write["max_affected"] != DATA_WRITE_MAX_DEFAULT):
            lines.append("[data_write]")
            lines.append(
                f"enabled = {'true' if data_write['enabled'] else 'false'}"
            )
            if data_write["source_id"]:
                lines.append(
                    f'source_id = {_toml_quote(data_write["source_id"])}'
                )
            lines.append(f"max_affected = {int(data_write['max_affected'])}")
            lines.append("")
        path = self._toml_path(project_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines))
        os.replace(tmp, path)


def _load_toml(handle: Any) -> dict[str, Any]:
    try:
        import tomllib
    except ImportError as exc:  # pragma: no cover - Python 3.11+ 才有
        raise ConfigError("读取 project.toml 需要 Python 3.11+（tomllib）") from exc
    return tomllib.load(handle)


DATA_WRITE_MAX_DEFAULT = 100
DATA_WRITE_MAX_CAP = 1000


def normalize_data_write(raw: Any) -> dict[str, Any]:
    """收敛项目写入设置：默认关闭、绑定哪一个数据源、单次影响行数上限。

    默认必须是关的——受控写的前提是用户明确打开，而不是升级后自动拥有。
    """
    data = raw if isinstance(raw, dict) else {}
    enabled = bool(data.get("enabled", False))
    source_id = _clean(data.get("source_id"), limit=64).lower()
    if source_id and not re.match(r"^[a-z][a-z0-9_-]{0,31}$", source_id):
        raise ConfigError("写入数据源 id 不合法：小写字母开头，后接小写字母/数字/_/-")
    try:
        max_affected = int(data.get("max_affected") or DATA_WRITE_MAX_DEFAULT)
    except (TypeError, ValueError) as exc:
        raise ConfigError("data_write.max_affected 必须是整数") from exc
    max_affected = max(1, min(DATA_WRITE_MAX_CAP, max_affected))
    return {
        "enabled": enabled,
        "source_id": source_id,
        "max_affected": max_affected,
    }


def _normalize_source(raw: dict[str, Any]) -> dict[str, Any]:
    """把前端/接口传来的数据源描述收敛成 project.toml 里的一节。"""
    if not isinstance(raw, dict):
        raise ConfigError("数据源描述必须是对象")
    adapter = _clean(raw.get("adapter"), limit=16).lower()
    if adapter not in _ADAPTERS:
        raise ConfigError(f"未知数据源类型：{adapter or '（空）'}；"
                          f"支持 {' / '.join(_ADAPTERS)}")
    source_id = (_clean(raw.get("source_id"), limit=32) or adapter).lower()
    if not re.match(r"^[a-z][a-z0-9_-]{0,31}$", source_id):
        raise ConfigError("数据源 id 不合法：小写字母开头，后接小写字母/数字/_/-")
    source: dict[str, Any] = {
        "source_id": source_id,
        "adapter": adapter,
        "label": _clean(raw.get("label"), limit=80),
    }
    if adapter == "sqlite":
        path = _clean(raw.get("path"), limit=1000)
        if not path:
            raise ConfigError("SQLite 数据源缺少 path")
        source["path"] = os.path.abspath(os.path.expanduser(path))
    elif adapter in ("mysql", "postgres"):
        for key in ("host", "user", "database"):
            value = _clean(raw.get(key), limit=200)
            if not value:
                raise ConfigError(f"{adapter} 数据源缺少 {key}")
            source[key] = value
        port = raw.get("port")
        source["port"] = int(port) if str(port or "").strip() else (
            3306 if adapter == "mysql" else 5432)
        ref = _clean(raw.get("credential_ref"), limit=120)
        source["credential_ref"] = ref or f"env:NL2SQL_{adapter.upper()}_PASSWORD"
        charset = _clean(raw.get("charset"), limit=32)
        if charset:
            source["charset"] = charset
    else:  # jdbc
        jdbc_url = _clean(raw.get("jdbc_url"), limit=1000)
        driver_class = _clean(raw.get("driver_class"), limit=200)
        if not jdbc_url or not driver_class:
            raise ConfigError("JDBC 数据源必须提供 jdbc_url 与 driver_class")
        source["jdbc_url"] = jdbc_url
        source["driver_class"] = driver_class
        user = _clean(raw.get("user"), limit=200)
        if user:
            source["user"] = user
        jar_paths = raw.get("jar_paths") or []
        if isinstance(jar_paths, str):
            jar_paths = [part for part in jar_paths.split(os.pathsep) if part.strip()]
        source["jar_paths"] = [os.path.abspath(os.path.expanduser(str(p)))
                               for p in jar_paths if str(p).strip()]
        ref = _clean(raw.get("credential_ref"), limit=120)
        source["credential_ref"] = ref or "env:NL2SQL_JDBC_PASSWORD"
    # 用核心的 SourceConfig 校验一遍：配置层是唯一事实来源，
    # Web 层不要另写一套"什么算合法"。
    try:
        _source_config_from_meta(source, project_id="", semantic_path=None).validate()
    except NL2SQLError:
        raise
    return source


def _source_config_from_meta(raw: dict[str, Any], *, project_id: str,
                             semantic_path: str | None) -> SourceConfig:
    adapter = str(raw.get("adapter") or "sqlite")
    options = SourceOptions()
    if adapter == "sqlite":
        options.path = str(raw.get("path") or "")
    elif adapter in ("mysql", "postgres"):
        defaults = {"mysql": 3306, "postgres": 5432}
        options.host = str(raw.get("host") or "")
        options.port = int(raw.get("port") or defaults[adapter])
        options.user = str(raw.get("user") or "")
        options.database = str(raw.get("database") or "")
        options.charset = str(raw.get("charset") or "")
    elif adapter == "jdbc":
        options.user = str(raw.get("user") or "")
        options.params = {
            "jdbc_url": str(raw.get("jdbc_url") or ""),
            "driver_class": str(raw.get("driver_class") or ""),
            "jar_paths": list(raw.get("jar_paths") or []),
        }
    return SourceConfig(
        source_id=str(raw.get("source_id") or adapter),
        adapter=adapter,
        label=str(raw.get("label") or ""),
        options=options,
        credential_ref=str(raw.get("credential_ref") or "") or None,
        semantic_path=semantic_path,
    )


def build_project_config(meta: dict[str, Any], *,
                         semantic_path: str | None = None
                         ) -> tuple[NL2SQLConfig, list[str]]:
    """把项目定义变成核心的 ``NL2SQLConfig``。

    返回 ``(config, warnings)``：语义层缺失、没有数据源这类"能启动但要提示"
    的情况走 warnings，不抛异常；真正的配置错误（字段非法）让 ``validate()`` 抛。
    """
    project_id = str(meta.get("id") or DEFAULT_PROJECT_ID)
    project_dir = os.path.abspath(str(meta.get("_dir") or project_id))
    warnings: list[str] = []
    cfg = NL2SQLConfig(state_dir=os.path.join(project_dir, "state"))
    cfg.project_id = project_id
    cfg.project_name = str(meta.get("name") or "")
    cfg.project_description = str(meta.get("description") or "")
    cfg.task_store_path = os.path.join(project_dir, "state", "tasks.db")
    semantic = str(meta.get("semantic") or "none")
    if semantic == "golden":
        semantic = "auto"  # auto + 无指标文件 = 内置黄金集模型（演示用）
    cfg.semantic_mode = semantic
    cfg.agent.planner = str(meta.get("planner") or "agent")
    cfg.agent.runtime = str(meta.get("runtime") or "auto")

    metrics_path = str(meta.get("metrics_path") or "")
    if metrics_path:
        if not os.path.isabs(metrics_path):
            metrics_path = os.path.join(project_dir, metrics_path)
        metrics_path = os.path.abspath(metrics_path)
        if not os.path.isfile(metrics_path):
            warnings.append(f"语义指标文件不存在：{metrics_path}")
    if semantic == "dbt" and not metrics_path:
        raise ConfigError("项目 semantic=dbt 但没有配置指标文件（metrics_path）")

    project_skills = os.path.join(project_dir, "skills")
    skill_dirs: list[str] = []
    if os.path.isdir(project_skills):
        skill_dirs.append(project_skills)
    skill_dirs.append(str(meta.get("_skills_dir") or "skills"))
    cfg.skills.dirs = tuple(skill_dirs)

    sources = meta.get("sources") or []
    if not sources:
        warnings.append("项目还没有配置数据源；请先在「项目」页添加一个数据源。")
    source_semantic = metrics_path or None
    for raw in sources:
        source = _source_config_from_meta(dict(raw), project_id=project_id,
                                          semantic_path=source_semantic)
        cfg.sources[source.source_id] = source
    if cfg.sources:
        cfg.default_source = next(iter(cfg.sources))
    cfg.validate()
    return cfg, warnings


# ------------------------------------------------------------------ 引擎
@dataclass
class EngineEntry:
    """一个项目的引擎及其状态。``engine`` 为空表示装配失败。"""

    project_id: str
    engine: Any = None
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    is_default: bool = False


class ProjectHub:
    """按项目懒加载引擎；默认项目用环境变量引擎（旧行为不变）。"""

    def __init__(self, store: ProjectStore, *, provider: Any = None,
                 default_engine: Any = None, default_cfg: NL2SQLConfig | None = None,
                 default_warnings: list[str] | None = None,
                 tasks_enabled: bool | None = None) -> None:
        self.store = store
        self._provider = provider
        self._default_engine = default_engine
        self._default_cfg = default_cfg
        self._default_warnings = list(default_warnings or [])
        self._entries: dict[str, EngineEntry] = {}
        self._lock = threading.RLock()
        if tasks_enabled is None:
            tasks_enabled = _truthy(os.environ.get("NL2SQL_TASKS_ENABLED"))
        self.tasks_enabled = bool(tasks_enabled)

    @property
    def default_config(self) -> NL2SQLConfig | None:
        """环境变量配置的只读视图（路由用它描述默认项目）。"""
        return self._default_cfg

    # ----------------------------------------------------------- 默认项目
    def default_entry(self) -> EngineEntry:
        if not self.tasks_enabled or self._default_cfg is None:
            return EngineEntry(DEFAULT_PROJECT_ID, engine=self._default_engine,
                               error="" if self._default_engine is not None else "核心引擎未就绪",
                               warnings=list(self._default_warnings),
                               is_default=True)
        with self._lock:
            entry = self._entries.get(DEFAULT_PROJECT_ID)
            if entry is not None:
                return entry
            cfg = copy.copy(self._default_cfg)
            cfg.project_id = DEFAULT_PROJECT_ID
            cfg.project_name = DEFAULT_PROJECT_NAME
            cfg.project_description = "环境变量连接（NL2SQL_DB / NL2SQL_MYSQL_* 等）"
            state_dir = cfg.state_dir
            cfg.task_store_path = os.path.join(state_dir, "tasks.db")
            cfg.skills = copy.copy(cfg.skills)
            cfg.agent = copy.copy(cfg.agent)
            cfg.credentials = copy.copy(getattr(cfg, "credentials", {}) or {})
            try:
                engine = NL2SQL(cfg, llm_provider=self._provider)
                engine.connect()
            except Exception as exc:  # noqa: BLE001 - 失败要能显示在界面上
                entry = EngineEntry(DEFAULT_PROJECT_ID, error=f"引擎装配失败：{exc}",
                                    warnings=list(self._default_warnings),
                                    is_default=True)
            else:
                entry = EngineEntry(DEFAULT_PROJECT_ID, engine=engine,
                                    warnings=list(self._default_warnings),
                                    is_default=True)
            self._entries[DEFAULT_PROJECT_ID] = entry
            return entry

    # ----------------------------------------------------------- 项目引擎
    def entry(self, project_id: str | None = None) -> EngineEntry:
        pid = _clean(project_id, limit=64) or DEFAULT_PROJECT_ID
        if pid == DEFAULT_PROJECT_ID:
            return self.default_entry()
        with self._lock:
            cached = self._entries.get(pid)
            if cached is not None:
                return cached
            entry = self._build_entry(pid)
            self._entries[pid] = entry
            return entry

    def _build_entry(self, project_id: str) -> EngineEntry:
        try:
            meta = self.store.get(project_id)
        except NL2SQLError as exc:
            return EngineEntry(project_id, error=str(exc))
        if meta is None:
            return EngineEntry(project_id, error=f"项目不存在：{project_id}")
        meta = dict(meta)
        meta["_dir"] = self.store.project_dir(project_id)
        meta["_skills_dir"] = os.path.join(os.getcwd(), "skills")
        try:
            cfg, warnings = build_project_config(meta)
            cfg.credentials = self.store.get_secrets(project_id)
            engine = NL2SQL(cfg, llm_provider=self._provider)
            engine.connect()
        except (ConfigError, NL2SQLError) as exc:
            return EngineEntry(project_id, error=f"项目配置有误：{exc}")
        except Exception as exc:  # noqa: BLE001
            return EngineEntry(project_id, error=f"项目引擎装配失败：{exc}")
        for source_id in list(cfg.sources):
            try:
                engine.refresh_snapshot(source_id, force=True)
            except Exception as exc:  # noqa: BLE001 - 连接失败不阻断项目
                warnings.append(f"[{source_id}] 元数据抽取失败：{exc}")
        return EngineEntry(project_id, engine=engine, warnings=warnings)

    # ----------------------------------------------------------- 变更
    def invalidate(self, project_id: str | None = None) -> None:
        pid = _clean(project_id, limit=64) or DEFAULT_PROJECT_ID
        with self._lock:
            entry = self._entries.pop(pid, None)
        if entry is not None and entry.engine is not None and not entry.is_default:
            try:
                entry.engine.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响下次装配
                pass

    def close(self) -> None:
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            if entry.engine is None or entry.is_default:
                continue
            try:
                entry.engine.close()
            except Exception:  # noqa: BLE001
                pass

    # ----------------------------------------------------------- 概览
    def summary(self) -> list[dict[str, Any]]:
        default = self.default_entry()
        default_meta = default_project_meta(self._default_cfg)
        items: list[dict[str, Any]] = [{
            "id": DEFAULT_PROJECT_ID,
            "name": DEFAULT_PROJECT_NAME,
            "description": "环境变量连接的默认项目",
            "semantic": "auto",
            "source_count": len(default_meta["sources"]),
            "sources": [s["source_id"] for s in default_meta["sources"]],
            "is_default": True,
            "tasks_enabled": self.tasks_enabled,
            "warnings": list(default.warnings),
        }]
        for meta in self.store.list_projects():
            items.append({
                "id": meta["id"],
                "name": meta.get("name") or meta["id"],
                "description": meta.get("description") or "",
                "semantic": meta.get("semantic") or "none",
                "source_count": len(meta.get("sources") or []),
                "sources": [s.get("source_id") for s in meta.get("sources") or []],
                "is_default": False,
                "tasks_enabled": True,
                "warnings": [],
            })
        return items


def default_project_meta(cfg: NL2SQLConfig | None) -> dict[str, Any]:
    """默认项目（环境变量引擎）的只读视图，形状与 ProjectStore 的 meta 对齐。"""
    sources: list[dict[str, Any]] = []
    if cfg is not None:
        for source_id, source in cfg.sources.items():
            raw: dict[str, Any] = {
                "source_id": source_id,
                "adapter": source.adapter,
                "label": source.label,
                "credential_ref": source.credential_ref or "",
            }
            options = source.options
            if source.adapter == "sqlite":
                raw["path"] = options.path
            elif source.adapter in ("mysql", "postgres"):
                raw.update({"host": options.host, "port": options.port,
                            "user": options.user, "database": options.database})
            elif source.adapter == "jdbc":
                raw.update({
                    "jdbc_url": (options.params or {}).get("jdbc_url", ""),
                    "driver_class": (options.params or {}).get("driver_class", ""),
                    "user": options.user or "",
                })
            sources.append(raw)
    return {
        "id": DEFAULT_PROJECT_ID,
        "name": DEFAULT_PROJECT_NAME,
        "description": "环境变量连接（NL2SQL_DB / NL2SQL_MYSQL_* 等）",
        "semantic": "auto",
        "metrics_path": "",
        "planner": str(getattr(getattr(cfg, "agent", None), "planner", "agent") or "agent"),
        "runtime": str(getattr(getattr(cfg, "agent", None), "runtime", "auto") or "auto"),
        "created_at": "",
        "updated_at": "",
        "sources": sources,
        "is_default": True,
    }


def _default_source_count(cfg: NL2SQLConfig | None) -> int:
    return len(cfg.sources) if cfg is not None else 0


def project_public(meta: dict[str, Any], *, engine_entry: EngineEntry | None = None,
                   tasks_enabled: bool = True) -> dict[str, Any]:
    """项目详情（给界面）：**不含口令**，只暴露引擎状态与数据源公开字段。"""
    data = {
        "id": meta.get("id"),
        "name": meta.get("name") or meta.get("id"),
        "description": meta.get("description") or "",
        "semantic": meta.get("semantic") or "none",
        "metrics_path": meta.get("metrics_path") or "",
        "planner": meta.get("planner") or "agent",
        "runtime": meta.get("runtime") or "auto",
        "created_at": meta.get("created_at") or "",
        "updated_at": meta.get("updated_at") or "",
        "sources": [public_source(s) for s in meta.get("sources") or []],
        "data_write": normalize_data_write(meta.get("data_write")),
        "tasks_enabled": bool(tasks_enabled),
        "is_default": bool(meta.get("id") == DEFAULT_PROJECT_ID),
    }
    if engine_entry is not None:
        data["engine_ready"] = engine_entry.engine is not None
        data["engine_error"] = engine_entry.error
        data["warnings"] = list(engine_entry.warnings)
    return data


def public_source(raw: dict[str, Any]) -> dict[str, Any]:
    """数据源公开字段：连接参数可以给界面看，口令不行。"""
    adapter = str(raw.get("adapter") or "")
    data = {
        "source_id": raw.get("source_id"),
        "adapter": adapter,
        "label": raw.get("label") or "",
    }
    if adapter == "sqlite":
        data["path"] = raw.get("path") or ""
    elif adapter in ("mysql", "postgres"):
        for key in ("host", "port", "user", "database", "charset", "credential_ref"):
            if raw.get(key) not in (None, ""):
                data[key] = raw.get(key)
    elif adapter == "jdbc":
        for key in ("jdbc_url", "driver_class", "user", "jar_paths", "credential_ref"):
            if raw.get(key) not in (None, "", []):
                data[key] = raw.get(key)
    return data
