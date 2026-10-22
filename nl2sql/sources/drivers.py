"""JDBC 驱动管理：固定版本、从 Maven Central 下载、缓存到项目目录。

M7 的第四种数据源（JDBC 泛用）需要用户提供数据库驱动 jar。Python 侧不能
像 `pip install pymysql` 那样一条命令解决，所以这里把"驱动从哪来"固定下来：

* **固定版本**：驱动与数据库版本有兼容矩阵，"latest" 不可复现；
  每个 kind 的版本写死在 `DRIVER_REGISTRY`，升级时改一处并重跑 M7 测试。
* **固定来源**：Maven Central（`repo1.maven.org`），jar 名与坐标都可推导，
  用户网络不通时也能按 `download_url` 手动下载后放到 `dest_dir`。
* **幂等**：目标 jar 已存在且非空 → 直接复用；`force=True` 才重下。
* **可诊断**：下载失败抛 `RuntimeError`，信息里带完整 URL 与本地目标目录。

前端/后端的两个接口（见 `_tmp/m7_api_contract.md`）直接消费这里的函数：

    GET  /api/jdbc/drivers            → driver_status()
    POST /api/jdbc/drivers/download   → download_driver(kind)

注意：`download_driver` 返回**主驱动 jar** 的绝对路径。`sqlite-jdbc` 在部分
版本上需要 slf4j-api（见 `DriverSpec.extra_jars`），`extra_jar_paths()` 提供
这些附加 jar 的路径，适配器装配时一起传给 Java 桥。
"""
from __future__ import annotations

import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import ConfigError

__all__ = [
    "DriverSpec",
    "DRIVER_REGISTRY",
    "DEFAULT_DRIVER_DIR",
    "driver_spec",
    "driver_status",
    "download_driver",
    "jar_path",
    "extra_jar_paths",
    "project_root",
]

_MAVEN_BASE = "https://repo1.maven.org/maven2"

#: 项目根目录（本文件位于 `<root>/nl2sql/sources/drivers.py`）。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def project_root() -> Path:
    """项目根目录；下载默认落盘位置与相对路径展示都基于它。"""
    return _PROJECT_ROOT


def _default_dest_dir() -> Path:
    """默认驱动目录：`<project>/data/jdbc_drivers`。

    `NL2SQL_JDBC_DRIVER_DIR` 可覆盖（测试/部署时把驱动放共享目录）。
    """
    override = os.environ.get("NL2SQL_JDBC_DRIVER_DIR", "").strip()
    return Path(override) if override else _PROJECT_ROOT / "data" / "jdbc_drivers"


@dataclass(frozen=True)
class DriverSpec:
    """一种数据库的 JDBC 驱动规格（固定版本）。

    坐标形如 `org.xerial:sqlite-jdbc:3.45.3.0`；jar 路径可由坐标推导，
    因此不需要在配置里手写下载地址。
    """

    kind: str
    name: str
    group_id: str
    artifact_id: str
    version: str
    driver_class: str
    jdbc_prefix: str
    note: str = ""
    #: 附加依赖 jar（Maven 坐标，形如 "org.slf4j:slf4j-api:1.7.36"）。
    #: 大多数驱动是单 jar；有传递依赖的驱动在这里显式列出并由
    #: `extra_jar_paths()` 一并下载。SQLite JDBC 3.42+ 编译依赖 slf4j-api。
    extra_jars: tuple[str, ...] = field(default_factory=tuple)

    # ---------------------------------------------------------- 派生属性
    @property
    def maven(self) -> str:
        """Maven 坐标字符串，`GET /api/jdbc/drivers` 直接返回。"""
        return f"{self.group_id}:{self.artifact_id}:{self.version}"

    @property
    def jar_name(self) -> str:
        return f"{self.artifact_id}-{self.version}.jar"

    @property
    def download_url(self) -> str:
        group_path = self.group_id.replace(".", "/")
        return f"{_MAVEN_BASE}/{group_path}/{self.artifact_id}/{self.version}/{self.jar_name}"

    def target(self, dest_dir: Path | None = None) -> Path:
        return (dest_dir or _default_dest_dir()) / self.jar_name


def _coord_to_url(coord: str) -> tuple[str, str]:
    """Maven 坐标 → (jar 名, 下载 URL)。"""
    parts = coord.split(":")
    if len(parts) != 3:
        raise ConfigError(f"非法的 Maven 坐标（应为 group:artifact:version）: {coord!r}")
    group_id, artifact_id, version = (p.strip() for p in parts)
    jar_name = f"{artifact_id}-{version}.jar"
    url = (f"{_MAVEN_BASE}/{group_id.replace('.', '/')}/"
           f"{artifact_id}/{version}/{jar_name}")
    return jar_name, url


#: 支持的数据库类型 → 驱动规格。kind 是对外的稳定标识（前端下拉框用）。
DRIVER_REGISTRY: dict[str, DriverSpec] = {
    "sqlite": DriverSpec(
        kind="sqlite",
        name="SQLite",
        group_id="org.xerial",
        artifact_id="sqlite-jdbc",
        version="3.45.3.0",
        driver_class="org.sqlite.JDBC",
        jdbc_prefix="jdbc:sqlite:",
        note="单文件数据库，M7 测试用它做不依赖外部服务的真跑用例；"
             "3.42+ 编译依赖 slf4j-api，extra_jars 会自动补齐",
        extra_jars=("org.slf4j:slf4j-api:1.7.36",),
    ),
    "mysql": DriverSpec(
        kind="mysql",
        name="MySQL / MariaDB",
        group_id="com.mysql",
        artifact_id="mysql-connector-j",
        version="8.4.0",
        driver_class="com.mysql.cj.jdbc.Driver",
        jdbc_prefix="jdbc:mysql://",
    ),
    "postgresql": DriverSpec(
        kind="postgresql",
        name="PostgreSQL",
        group_id="org.postgresql",
        artifact_id="postgresql",
        version="42.7.3",
        driver_class="org.postgresql.Driver",
        jdbc_prefix="jdbc:postgresql://",
    ),
    "mssql": DriverSpec(
        kind="mssql",
        name="SQL Server",
        group_id="com.microsoft.sqlserver",
        artifact_id="mssql-jdbc",
        version="12.6.1.jre11",
        driver_class="com.microsoft.sqlserver.jdbc.SQLServerDriver",
        jdbc_prefix="jdbc:sqlserver://",
        note="jre11 版本兼容 JDK 17",
    ),
}

#: kind 别名：用户/旧配置可能写 postgres、sqlserver，归一到注册表 key。
_ALIASES = {
    "postgres": "postgresql",
    "pg": "postgresql",
    "sqlserver": "mssql",
    "sql-server": "mssql",
    "sql_server": "mssql",
    "mariadb": "mysql",
}


def driver_spec(kind: str) -> DriverSpec:
    """按 kind 取驱动规格；未知 kind 抛 `ConfigError`（列出支持项）。"""
    key = (kind or "").strip().lower()
    key = _ALIASES.get(key, key)
    spec = DRIVER_REGISTRY.get(key)
    if spec is None:
        raise ConfigError(
            f"不支持的 JDBC 驱动类型: {kind!r}；支持: "
            f"{', '.join(sorted(DRIVER_REGISTRY))}"
        )
    return spec


def jar_path(kind: str, dest_dir: str | Path | None = None) -> Path:
    """主驱动 jar 的预期路径（不检查是否存在）。"""
    spec = driver_spec(kind)
    dest = Path(dest_dir) if dest_dir is not None else _default_dest_dir()
    return spec.target(dest.resolve() if dest.is_absolute() else dest)


def extra_jar_paths(kind: str, dest_dir: str | Path | None = None) -> list[Path]:
    """附加依赖 jar 的预期路径（不检查是否存在）。"""
    spec = driver_spec(kind)
    dest = Path(dest_dir) if dest_dir is not None else _default_dest_dir()
    return [dest / _coord_to_url(c)[0] for c in spec.extra_jars]


# ------------------------------------------------------------------ 下载
def _download_one(url: str, target: Path, *, timeout_s: float) -> Path:
    """流式下载一个 jar 到 target（先写 .part 再原子替换）。

    用标准库 urllib（不引入额外第三方依赖）：驱动下载是可选路径，
    核心层保持「默认零第三方依赖」的约束。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    request = urllib.request.Request(
        url, headers={"User-Agent": "DataAgent-M7-JDBC/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as resp:
            status = getattr(resp, "status", 200)
            if status != 200:
                raise RuntimeError(
                    f"下载 JDBC 驱动失败：HTTP {status} {url}"
                )
            ctype = resp.headers.get("content-type", "")
            if "text/html" in ctype.lower():
                raise RuntimeError(
                    f"下载 JDBC 驱动失败：响应是 HTML 而不是 jar（{url}）"
                )
            with open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    fh.write(chunk)
        size = tmp.stat().st_size
        if size < 1024:
            raise RuntimeError(
                f"下载 JDBC 驱动失败：文件过小（{size} 字节）{url}"
            )
        with open(tmp, "rb") as fh:
            magic = fh.read(2)
        if magic != b"PK":
            raise RuntimeError(
                f"下载 JDBC 驱动失败：内容不是有效 jar（魔数 {magic!r}）{url}"
            )
        os.replace(tmp, target)
        return target
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(
            f"下载 JDBC 驱动失败（{type(exc).__name__}: {exc}）；"
            f"可手动下载 {url} 放到 {target.parent}"
        ) from exc
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:  # pragma: no cover - 清理失败不影响结果
            pass


def download_driver(
    kind: str,
    dest_dir: str | Path | None = None,
    *,
    force: bool = False,
    timeout_s: float = 120.0,
    download_extras: bool = True,
) -> Path:
    """下载（或复用）一种数据库的 JDBC 驱动，返回主 jar 绝对路径。

    Args:
        kind: `sqlite` / `mysql` / `postgresql` / `mssql`（支持 postgres 等别名）。
        dest_dir: 落盘目录；默认 `<project>/data/jdbc_drivers`
            （`NL2SQL_JDBC_DRIVER_DIR` 可覆盖）。
        force: 已存在也重新下载（升级/修损坏时用）。
        timeout_s: 单次 HTTP 请求超时（秒）。
        download_extras: 是否同时下载附加依赖 jar（如 sqlite-jdbc 的 slf4j）。

    Returns:
        主驱动 jar 的绝对路径。

    Raises:
        ConfigError: 未知 kind。
        RuntimeError: 网络/写盘/内容校验失败，信息含可行动提示。
    """
    spec = driver_spec(kind)
    dest = (Path(dest_dir) if dest_dir is not None else _default_dest_dir())
    dest = dest.resolve() if not dest.is_absolute() else dest
    target = spec.target(dest)

    if target.is_file() and target.stat().st_size > 0 and not force:
        if download_extras:
            for coord in spec.extra_jars:
                jar_name, url = _coord_to_url(coord)
                extra = dest / jar_name
                if not (extra.is_file() and extra.stat().st_size > 0) or force:
                    _download_one(url, extra, timeout_s=timeout_s)
        return target

    _download_one(spec.download_url, target, timeout_s=timeout_s)
    if download_extras:
        for coord in spec.extra_jars:
            jar_name, url = _coord_to_url(coord)
            _download_one(url, dest / jar_name, timeout_s=timeout_s)
    return target


# ------------------------------------------------------------------ 状态
def _status_dict(spec: DriverSpec, dest: Path) -> dict[str, Any]:
    target = spec.target(dest)
    downloaded = target.is_file() and target.stat().st_size > 0
    extras = []
    for coord in spec.extra_jars:
        jar_name, url = _coord_to_url(coord)
        p = dest / jar_name
        extras.append({
            "maven": coord,
            "downloaded": p.is_file() and p.stat().st_size > 0,
            "path": str(p) if (p.is_file() and p.stat().st_size > 0) else "",
            "download_url": url,
        })
    try:
        rel = str(target.relative_to(_PROJECT_ROOT))
    except ValueError:
        rel = ""
    return {
        "kind": spec.kind,
        "name": spec.name,
        "maven": spec.maven,
        "version": spec.version,
        "driver_class": spec.driver_class,
        "jdbc_prefix": spec.jdbc_prefix,
        "downloaded": downloaded,
        "path": str(target) if downloaded else "",
        "path_rel": rel if downloaded else "",
        "download_url": spec.download_url,
        "extra_jars": extras,
        "note": spec.note,
    }


def driver_status(
    kind: str | None = None,
    dest_dir: str | Path | None = None,
) -> dict[str, Any] | list[dict[str, Any]]:
    """驱动状态，供 `GET /api/jdbc/drivers` 使用。

    传 `kind` 返回单个 dict；不传返回全部（按 kind 排序的 list）。
    每个 dict 的字段见 `_status_dict`：`kind`/`name`/`maven`/`downloaded`/
    `path`（绝对路径，未下载为空串）/`path_rel`/`download_url`/`driver_class`/
    `jdbc_prefix`/`extra_jars`/`version`/`note`。
    """
    dest = (Path(dest_dir) if dest_dir is not None else _default_dest_dir())
    dest = dest.resolve() if not dest.is_absolute() else dest
    if kind is not None:
        return _status_dict(driver_spec(kind), dest)
    return [_status_dict(DRIVER_REGISTRY[k], dest) for k in sorted(DRIVER_REGISTRY)]
