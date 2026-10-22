"""JDBC 泛用数据源适配器（M7 第四引擎）。

Python 没有原生 JDBC，Oracle / SQL Server / 达梦等数据库往往只提供 JDBC
驱动。本适配器把 JDBC 能力封装成一个**常驻 Java 子进程**，用 stdin/stdout
上的二进制协议通信：

    Python JdbcAdapter  ⇄  java JdbcBridge（URLClassLoader 加载驱动 jar）
                        ⇄  JDBC 驱动 ⇄ 数据库

为什么是常驻子进程而不是"每次查询起一个 java"：JVM 启动 + 驱动加载要
几百毫秒到几秒，常驻进程让第二条查询起就是纯网络往返；进程生命周期由
适配器实例持有，`close()` 负责回收（测试里会检查没有残留 java 进程）。

契约对齐（见 `nl2sql/contracts/source.py`）：
* `test_connection()` 失败抛 `SourceConnectionError`
* `introspect()` 用 `DatabaseMetaData` 抽元数据，行数给 `None`
  （通用 JDBC 没有廉价行数接口，全表 COUNT 代价不可接受）
* `execute()` 只读、limit+1 探测 truncated、超时抛 `QueryTimeoutError`
* 进程崩溃抛 `SourceConnectionError`，错误信息带 stderr 尾部

只读在本适配器里是**双保险**：Python 侧先做词法校验（仅允许单条
SELECT / WITH，拒绝写关键字与多语句），Java 侧再 `conn.setReadOnly(true)`；
安全层的 guard 仍会在调用前拦一遍（契约第 7 行）。

Java 桥源码在 `nl2sql/sources/jdbc_bridge/JdbcBridge.java`，首次使用时用
`javac` 编译到缓存目录（默认 `<project>/data/jdbc_bridge_classes`，
`NL2SQL_JDBC_BRIDGE_DIR` 可覆盖），幂等复用。
"""
from __future__ import annotations

import json
import math
import os
import queue
import re
import shutil
import struct
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Sequence

from ..contracts.source import (
    ColumnMeta,
    QueryResult,
    SourceCapabilities,
    TableMeta,
)
from ..dialect.tokens import first_keywords, is_read_only, split_statements, tokenize
from ..errors import ConfigError, QueryBlockedError, QueryTimeoutError, SourceConnectionError
from .drivers import DRIVER_REGISTRY

__all__ = [
    "JdbcAdapter",
    "ensure_bridge_compiled",
    "default_bridge_dir",
    "bridge_source_path",
]

#: 项目根目录（本文件位于 `<root>/nl2sql/sources/jdbc.py`）。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
#: Java 桥源码位置。
_BRIDGE_SOURCE = Path(__file__).resolve().parent / "jdbc_bridge" / "JdbcBridge.java"
#: 编译后的主类名。
_BRIDGE_CLASS = "JdbcBridge"
#: 编译缓存目录名（在项目 data/ 下；env 可覆盖）。
_BRIDGE_DIR_NAME = "jdbc_bridge_classes"
#: 查询等待的额外宽限：Java 侧 setQueryTimeout 通常先触发，Python deadline
#: 只在桥完全无响应（驱动卡死）时兜底，所以比 timeout_s 略长。
_QUERY_GRACE_S = 3.0
#: stderr 只保留尾部，用于崩溃诊断，避免无限增长。
_MAX_STDERR_LINES = 40
#: 只读拒绝的统一前缀（测试按这个子串断言）。
_READ_ONLY_HINT = "JDBC 适配器只支持只读查询"

#: JDBC URL 前缀 → (核心方言, 标识符引号, 分页方式, 支持 JSON 函数)。
#: 核心层按 capabilities 生成 SQL（时间粒度展开、LIMIT/ROWNUM），
#: 所以能认出方言就要认出来——否则时间粒度维度会直接报"没有方言映射"。
_DIALECT_PROFILES: dict[str, tuple[str, str, str, bool]] = {
    "sqlite": ("sqlite", '"', "limit", True),
    "mysql": ("mysql", "`", "limit", True),
    "mariadb": ("mysql", "`", "limit", True),
    "postgresql": ("postgres", '"', "limit", True),
    "postgres": ("postgres", '"', "limit", True),
    "oracle": ("oracle", '"', "rownum", False),
    # SQL Server 没有 LIMIT：声明 "none"，避免 guard/编译器拼出非法 T-SQL，
    # 行数控制由 execute(limit=...) 的 limit+1 读取兜底（见已知限制）。
    "sqlserver": ("sqlserver", '"', "none", False),
    "mssql": ("sqlserver", '"', "none", False),
    "h2": ("h2", '"', "limit", False),
    "hive": ("hive", "`", "limit", False),
    "db2": ("db2", '"', "limit", False),
    "dm": ("dm", '"', "none", False),
}
#: 认不出前缀的驱动默认按通用 SQL 处理。
_DEFAULT_PROFILE = ("jdbc", '"', "limit", False)

_JDBC_PREFIX_RE = re.compile(r"^jdbc:([A-Za-z0-9_]+):")
#: PostgreSQL dollar-quoted 字符串：内容不是 SQL，校验前先替换成空串。
_DOLLAR_STRING_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$.*?\$\1\$", re.DOTALL)
#: 锁定读（FOR SHARE / FOR NO KEY UPDATE 等）虽不写数据，但会加锁，拒绝。
_FOR_LOCKING_FOLLOWERS = {"share", "update", "no", "key"}


# ------------------------------------------------------------------ 基础工具
def bridge_source_path() -> Path:
    """Java 桥源码路径（编译/诊断用）。"""
    return _BRIDGE_SOURCE


def default_bridge_dir() -> Path:
    """默认编译缓存目录：`<project>/data/jdbc_bridge_classes`。"""
    override = os.environ.get("NL2SQL_JDBC_BRIDGE_DIR", "").strip()
    return Path(override) if override else _PROJECT_ROOT / "data" / _BRIDGE_DIR_NAME


def _find_java_tool(name: str, env_var: str) -> str | None:
    """按 `NL2SQL_JAVA(S)` → `JAVA_HOME/bin` → PATH 的顺序找 java/javac。"""
    override = os.environ.get(env_var, "").strip()
    if override and Path(override).is_file():
        return override
    java_home = os.environ.get("JAVA_HOME", "").strip()
    if java_home:
        exe = Path(java_home) / "bin" / (name + (".exe" if os.name == "nt" else ""))
        if exe.is_file():
            return str(exe)
    return shutil.which(name)


def ensure_bridge_compiled(bridge_dir: str | Path | None = None) -> Path:
    """编译 Java 桥（幂等），返回 class 文件所在目录。

    * class 文件存在且不比 `.java` 旧 → 直接复用（测试/服务重启不重复编译）
    * 否则调用 `javac` 编译到 `bridge_dir`；找不到 javac 时给出可行动错误
    """
    classes = Path(bridge_dir) if bridge_dir is not None else default_bridge_dir()
    classes = classes if classes.is_absolute() else classes.resolve()
    source = _BRIDGE_SOURCE
    if not source.is_file():
        raise SourceConnectionError(f"JDBC 桥源码不存在: {source}")
    class_file = classes / (_BRIDGE_CLASS + ".class")
    try:
        if class_file.is_file() and class_file.stat().st_mtime >= source.stat().st_mtime:
            return classes
    except OSError:
        pass  # 文件状态异常就当需要重编译

    javac = _find_java_tool("javac", "NL2SQL_JAVAC")
    if javac is None:
        raise SourceConnectionError(
            "找不到 javac，JDBC 适配器需要 JDK 17：请安装 JDK 并把它加入 PATH，"
            "或设置 JAVA_HOME / NL2SQL_JAVAC"
        )
    classes.mkdir(parents=True, exist_ok=True)
    cmd = [javac, "-encoding", "UTF-8", "-d", str(classes), str(source)]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceConnectionError(f"编译 JDBC 桥失败（javac 调用出错）: {exc}") from exc
    if proc.returncode != 0:
        tail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-2000:]
        raise SourceConnectionError(
            f"编译 JDBC 桥失败（javac 退出码 {proc.returncode}）：{tail or '无输出'}"
        )
    if not class_file.is_file():
        raise SourceConnectionError(
            f"编译 JDBC 桥失败：{class_file} 未生成（javac 输出："
            f"{((proc.stderr or proc.stdout or '').strip()[-800:])}）"
        )
    return classes


def _encode_write_utf(text: str) -> bytes:
    """实现 Java `DataOutputStream.writeUTF` 的编码（修改版 UTF-8）。

    与标准 UTF-8 的差别：`U+0000` 编码为两字节 `C0 80`，辅助平面字符按
    UTF-16 代理对逐单元编码（CESU-8 形态）。命令字段都很短，这里按 UTF-16
    码元逐个编码，保证和 Java `DataInputStream.readUTF` 完全一致。
    """
    if text is None:
        text = ""
    raw = text.encode("utf-16-be", "surrogatepass")
    buf = bytearray()
    for i in range(0, len(raw), 2):
        unit = (raw[i] << 8) | raw[i + 1]
        if unit == 0:
            buf += b"\xc0\x80"
        elif unit < 0x80:
            buf.append(unit)
        elif unit < 0x800:
            buf.append(0xC0 | (unit >> 6))
            buf.append(0x80 | (unit & 0x3F))
        else:
            buf.append(0xE0 | (unit >> 12))
            buf.append(0x80 | ((unit >> 6) & 0x3F))
            buf.append(0x80 | (unit & 0x3F))
    if len(buf) > 0xFFFF:
        raise ValueError(f"命令字段过长（{len(buf)} 字节 > 65535）")
    return struct.pack(">H", len(buf)) + bytes(buf)


def _encode_int(value: int) -> bytes:
    return struct.pack(">i", int(value))


class _RpcTimeout(Exception):
    """内部信号：桥在 deadline 内没有回响应（调用方翻译成对外异常）。"""


# ------------------------------------------------------------------ 适配器
class JdbcAdapter:
    """实现 `SourceAdapter` 契约（只读，常驻 Java 桥）。

    Args:
        jdbc_url: JDBC URL，如 `jdbc:mysql://127.0.0.1:3306/shop`。
        driver_class: 驱动类名，如 `com.mysql.cj.jdbc.Driver`。
        jar_paths: 驱动 jar 本地路径（至少一个）。对 sqlite-jdbc 这类
            有 slf4j-api 依赖的驱动，同目录下的附加 jar 会被自动发现并加载。
        user / password: 数据库账号；口令不写进配置文件，由装配层注入。
        dialect: 可选，覆盖从 URL 前缀推断的方言（核心层据此生成 SQL）。
        read_only: 是否在 Python 侧做只读校验（默认 True；不建议关）。
        bridge_dir: 编译缓存目录；默认 `<project>/data/jdbc_bridge_classes`。
        connect_timeout_s: 连接/自检/元数据等待上限（秒）。
    """

    def __init__(
        self,
        *,
        jdbc_url: str,
        driver_class: str,
        jar_paths: Sequence[str | Path] = (),
        user: str = "",
        password: str = "",
        dialect: str | None = None,
        read_only: bool = True,
        bridge_dir: str | Path | None = None,
        connect_timeout_s: float = 15.0,
    ) -> None:
        self._jdbc_url = str(jdbc_url or "").strip()
        if not self._jdbc_url.lower().startswith("jdbc:"):
            raise ConfigError(
                f"JDBC URL 必须以 'jdbc:' 开头: {self._jdbc_url!r}"
                "（例如 jdbc:mysql://127.0.0.1:3306/shop）"
            )
        self._driver_class = str(driver_class or "").strip()
        if not self._driver_class:
            raise ConfigError("JDBC 适配器需要 driver_class（如 org.sqlite.JDBC）")

        raw_paths: list[str] = []
        if isinstance(jar_paths, (str, Path)):
            raw_paths = [str(jar_paths)]
        else:
            raw_paths = [str(p) for p in (jar_paths or [])]
        resolved: list[Path] = []
        for p in raw_paths:
            p = p.strip()
            if not p:
                continue
            path = Path(p)
            if not path.is_file():
                raise ConfigError(
                    f"JDBC 驱动 jar 不存在: {path}；"
                    "可用 /api/jdbc/drivers/download 下载（如 kind=sqlite）"
                )
            resolved.append(path.resolve())
        if not resolved:
            raise ConfigError(
                "JDBC 适配器需要至少一个驱动 jar（jar_paths）；"
                "可用 /api/jdbc/drivers/download 下载"
            )
        self._jar_paths: tuple[Path, ...] = tuple(resolved)

        self._user = str(user or "")
        self._password = str(password or "")
        self._read_only = bool(read_only)
        self._bridge_dir = Path(bridge_dir) if bridge_dir is not None else None
        self._connect_timeout_s = max(1.0, float(connect_timeout_s or 15.0))

        self._dialect = self._resolve_dialect(dialect)
        profile = _DIALECT_PROFILES.get(self._dialect, _DEFAULT_PROFILE)
        if profile[0] != self._dialect:
            # 显式传入的方言不在映射表（如 teradata）：保留用户声明，
            # 其余能力用通用默认值，并把差异写进 extras 供诊断。
            _canonical, quote, pagination, supports_json = _DEFAULT_PROFILE
            profile = (self._dialect, quote, pagination, supports_json)
        _canonical, quote, pagination, supports_json = profile
        self.capabilities = SourceCapabilities(
            dialect=_canonical,
            sql=True,
            pagination=pagination,
            supports_cte=True,
            supports_window=True,
            supports_json=supports_json,
            supports_explain=False,   # 通用 JDBC 不做 EXPLAIN（方言差异大）
            identifier_quote=quote,
        )

        # 进程状态：锁保护所有读写；_proc 为 None 表示尚未启动/已回收
        self._lock = threading.RLock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        self._stderr_tail: deque[str] = deque(maxlen=_MAX_STDERR_LINES)
        self._reader: threading.Thread | None = None
        self._err_reader: threading.Thread | None = None
        self._connected = False
        self._driver_info = ""

    # ------------------------------------------------------------ 属性
    @property
    def jdbc_url(self) -> str:
        return self._jdbc_url

    @property
    def driver_class(self) -> str:
        return self._driver_class

    @property
    def jar_paths(self) -> tuple[str, ...]:
        return tuple(str(p) for p in self._jar_paths)

    @property
    def java_pid(self) -> int | None:
        """当前桥进程 PID（未启动/已回收为 None），测试用来查残留。"""
        proc = self._proc
        return proc.pid if proc is not None and proc.poll() is None else None

    def describe(self) -> dict[str, Any]:
        """诊断信息（不触发连接）。"""
        return {
            "adapter": "jdbc",
            "dialect": self.capabilities.dialect,
            "jdbc_url": self._jdbc_url,
            "driver_class": self._driver_class,
            "jar_paths": list(self.jar_paths),
            "read_only": self._read_only,
            "connected": self._connected,
            "java_pid": self.java_pid,
            "bridge_dir": str(self._bridge_dir or default_bridge_dir()),
            "driver_info": self._driver_info,
        }

    # ------------------------------------------------------------ 方言
    def _resolve_dialect(self, dialect: str | None) -> str:
        if dialect:
            return str(dialect).strip().lower()
        match = _JDBC_PREFIX_RE.match(self._jdbc_url)
        prefix = match.group(1).lower() if match else ""
        profile = _DIALECT_PROFILES.get(prefix)
        return profile[0] if profile else _DEFAULT_PROFILE[0]

    # ------------------------------------------------------------ 进程管理
    def _start_process(self) -> None:
        classes = ensure_bridge_compiled(self._bridge_dir)
        java = _find_java_tool("java", "NL2SQL_JAVA")
        if java is None:
            raise SourceConnectionError(
                "找不到 java，JDBC 适配器需要 JRE/JDK 17：请安装并加入 PATH，"
                "或设置 JAVA_HOME / NL2SQL_JAVA"
            )
        cmd = [java, "-cp", str(classes), _BRIDGE_CLASS]
        kwargs: dict[str, Any] = dict(
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        if os.name == "nt":
            # 后端是 GUI 服务时不要弹出控制台窗口
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        try:
            proc = subprocess.Popen(cmd, **kwargs)
        except OSError as exc:
            raise SourceConnectionError(
                f"启动 JDBC 桥失败（{' '.join(cmd)}）: {exc}"
            ) from exc
        self._proc = proc
        self._queue = queue.Queue()
        self._stderr_tail.clear()
        self._reader = threading.Thread(
            target=self._pump_stdout, args=(proc, self._queue),
            name="jdbc-bridge-stdout", daemon=True,
        )
        self._reader.start()
        self._err_reader = threading.Thread(
            target=self._pump_stderr, args=(proc,),
            name="jdbc-bridge-stderr", daemon=True,
        )
        self._err_reader.start()

    @staticmethod
    def _pump_stdout(proc: subprocess.Popen[bytes], sink: queue.Queue) -> None:
        try:
            while True:
                line = proc.stdout.readline() if proc.stdout is not None else b""
                if not line:
                    break
                sink.put(line)
        except (OSError, ValueError):
            pass
        finally:
            sink.put(None)  # EOF 哨兵

    def _pump_stderr(self, proc: subprocess.Popen[bytes]) -> None:
        try:
            stream = proc.stderr
            if stream is None:
                return
            for line in stream:
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    self._stderr_tail.append(text)
        except (OSError, ValueError):
            pass

    def _kill_process(self) -> None:
        """强制回收 java 子进程（幂等，不抛异常）。"""
        proc = self._proc
        self._proc = None
        self._connected = False
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5.0)
            except Exception:  # noqa: BLE001 - 回收失败也继续清理句柄
                pass
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except (OSError, ValueError):
                    pass
        for thread in (self._reader, self._err_reader):
            if thread is not None and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=1.0)
        self._reader = None
        self._err_reader = None
        self._driver_info = ""

    def _stderr_tail_text(self) -> str:
        if not self._stderr_tail:
            return ""
        return " | ".join(list(self._stderr_tail)[-10:])[-1500:]

    def _send_recv(
        self,
        command: str,
        fields: Sequence[Any] = (),
        *,
        timeout: float,
    ) -> dict[str, Any]:
        """发一条命令并等一行 JSON 响应（调用方须持有 `_lock`）。"""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            raise SourceConnectionError("JDBC 桥进程未运行")
        payload = bytearray(_encode_write_utf(command))
        for field in fields:
            if isinstance(field, int) and not isinstance(field, bool):
                payload += _encode_int(field)
            else:
                payload += _encode_write_utf(str(field))
        try:
            assert proc.stdin is not None
            proc.stdin.write(bytes(payload))
            proc.stdin.flush()
        except (OSError, ValueError) as exc:
            rc = proc.poll()
            self._kill_process()
            raise SourceConnectionError(
                f"JDBC 桥写入失败（java 进程退出码 {rc}）：{exc}"
                f"；stderr: {self._stderr_tail_text() or '无'}"
            ) from exc

        deadline = time.monotonic() + max(0.5, timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._kill_process()
                raise _RpcTimeout()
            try:
                line = self._queue.get(timeout=remaining)
            except queue.Empty:
                self._kill_process()
                raise _RpcTimeout() from None
            if line is None:
                rc = proc.poll()
                tail = self._stderr_tail_text()
                self._kill_process()
                raise SourceConnectionError(
                    f"JDBC 桥进程意外退出（java 退出码 {rc}）："
                    f"{tail or '无 stderr 输出'}"
                )
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                response = json.loads(text)
            except json.JSONDecodeError as exc:
                self._kill_process()
                raise SourceConnectionError(
                    f"JDBC 桥返回了非 JSON 响应: {text[:500]!r}"
                ) from exc
            if not isinstance(response, dict):
                raise SourceConnectionError(f"JDBC 桥响应格式错误: {text[:500]!r}")
            return response

    def _ensure_ready(self) -> None:
        """确保 java 桥已启动且已 connect（调用方须持有 `_lock`）。"""
        if self._proc is not None and self._proc.poll() is None and self._connected:
            return
        if self._proc is not None:
            self._kill_process()
        self._start_process()
        jar_paths = self._expanded_jar_paths()
        try:
            response = self._send_recv(
                "connect",
                (self._driver_class, self._jdbc_url, self._user, self._password,
                 os.pathsep.join(jar_paths)),
                timeout=self._connect_timeout_s,
            )
        except _RpcTimeout:
            self._kill_process()
            raise SourceConnectionError(
                f"JDBC 桥在 {self._connect_timeout_s:g}s 内未响应连接请求"
                f"（{self._jdbc_url}）；可能是驱动卡在网络连接上"
            ) from None
        if not response.get("ok"):
            error = response.get("error") or "未知错误"
            self._kill_process()
            raise SourceConnectionError(
                f"无法通过 JDBC 连接 {self._jdbc_url}：{error}"
            )
        self._connected = True
        self._driver_info = str(response.get("driver") or "")

    def _expanded_jar_paths(self) -> list[str]:
        """补齐已知驱动的附加依赖 jar（如 sqlite-jdbc 需要 slf4j-api）。

        附加 jar 从已配置 jar 的同目录里找（下载目录结构一致），找不到就
        不补——真正需要时 Java 端会报 `NoClassDefFoundError`，比猜测友好。
        """
        paths = [str(p) for p in self._jar_paths]
        known = None
        for spec in DRIVER_REGISTRY.values():
            if spec.driver_class == self._driver_class:
                known = spec
                break
        if known is None or not known.extra_jars:
            return paths
        directories: list[Path] = []
        for p in self._jar_paths:
            parent = p.parent
            if parent not in directories:
                directories.append(parent)
        seen = {str(Path(p).resolve()) for p in paths}
        for coord in known.extra_jars:
            parts = coord.split(":")
            if len(parts) != 3:
                continue
            jar_name = f"{parts[1]}-{parts[2]}.jar"
            for directory in directories:
                candidate = directory / jar_name
                if candidate.is_file() and str(candidate.resolve()) not in seen:
                    paths.append(str(candidate))
                    seen.add(str(candidate.resolve()))
        return paths

    # ------------------------------------------------------------ 只读校验
    def _check_read_only(self, sql: str) -> None:
        """仅允许单条 SELECT / WITH；其余一律抛 `QueryBlockedError`。

        复用项目统一词法扫描（字符串/注释里的关键字不会被误判），
        并在其规则上收紧：JDBC 泛用适配器只放行 SELECT / WITH 开头。
        """
        if not self._read_only:
            return
        text = (sql or "").strip()
        if not text:
            raise QueryBlockedError(
                f"{_READ_ONLY_HINT}：SQL 为空",
                rule="read_only",
                suggestion="请输入一条 SELECT 或 WITH 查询。",
            )
        masked = _DOLLAR_STRING_RE.sub("''", text)
        statements = split_statements(masked)
        if len(statements) != 1:
            raise QueryBlockedError(
                f"{_READ_ONLY_HINT}：检测到 {len(statements)} 条语句（多语句一律拒绝）",
                rule="read_only",
                suggestion="一次只执行一条只读查询。",
            )
        statement = statements[0]
        first = first_keywords(statement, k=1)
        head = first[0] if first else "?"
        if head not in ("select", "with"):
            raise QueryBlockedError(
                f"{_READ_ONLY_HINT}：语句以 {head.upper()} 开头，只允许 SELECT / WITH",
                rule="read_only",
                suggestion=(
                    f"请把 {head.upper()} 改写为 SELECT/WITH；"
                    "如需写操作请使用数据库客户端。"
                ),
            )
        if not is_read_only(statement):
            raise QueryBlockedError(
                f"{_READ_ONLY_HINT}：语句包含写操作关键字（或 SELECT INTO）",
                rule="read_only",
                suggestion="移除写操作关键字后再执行。",
            )
        words = [text_.lower() for kind, text_ in tokenize(statement) if kind == "word"]
        for i, word in enumerate(words):
            if word != "for" or i + 1 >= len(words):
                continue
            follower = words[i + 1]
            if follower in _FOR_LOCKING_FOLLOWERS:
                raise QueryBlockedError(
                    f"{_READ_ONLY_HINT}：不支持锁定读（FOR {follower.upper()}）",
                    rule="read_only",
                    suggestion="去掉 FOR UPDATE / FOR SHARE 之类的锁定子句。",
                )

    # ------------------------------------------------------------ 契约实现
    def test_connection(self) -> None:
        """连接自检：失败抛 `SourceConnectionError`。"""
        with self._lock:
            self._ensure_ready()
            try:
                response = self._send_recv(
                    "test", timeout=self._connect_timeout_s + 5.0,
                )
            except _RpcTimeout:
                self._kill_process()
                raise SourceConnectionError(
                    f"JDBC 连接自检超时（{self._connect_timeout_s + 5.0:g}s）"
                ) from None
            if not response.get("ok"):
                raise SourceConnectionError(
                    f"JDBC 连接自检失败: {response.get('error') or '未知错误'}"
                )

    def introspect(
        self,
        *,
        include_comments: bool = True,
        count_rows: bool | None = None,
    ) -> list[TableMeta]:
        """抽取表/列/主键/外键/注释；行数一律 `None`。

        `count_rows` 参数与其它适配器签名保持一致（catalog 会传），但通用
        JDBC 路径**不**做全表 COUNT：Oracle/SQL Server 大表 COUNT 代价极高，
        而元数据快照只需要"有哪些表、哪些列"。
        """
        with self._lock:
            self._ensure_ready()
            try:
                response = self._send_recv(
                    "metadata", timeout=max(30.0, self._connect_timeout_s * 2),
                )
            except _RpcTimeout:
                raise SourceConnectionError(
                    "JDBC 元数据抽取超时（桥进程已回收）"
                ) from None
            if not response.get("ok"):
                raise RuntimeError(
                    f"JDBC 元数据抽取失败: {response.get('error') or '未知错误'}"
                )
        tables: list[TableMeta] = []
        for raw in response.get("tables") or []:
            table = _to_table_meta(raw, include_comments=include_comments)
            if table is not None:
                tables.append(table)
        tables.sort(key=lambda t: ((t.schema or "").lower(), t.name.lower()))
        return tables

    def execute(self, sql: str, *, limit: int, timeout_s: float) -> QueryResult:
        """执行只读 SQL；limit+1 探测截断，超时抛 `QueryTimeoutError`。"""
        self._check_read_only(sql)   # 先校验：被拒的 SQL 不启动 java 进程
        limit_int = int(limit or 0)
        if limit_int < 0:
            limit_int = 0
        limit_int = min(limit_int, 2_000_000_000)
        timeout = float(timeout_s) if timeout_s and float(timeout_s) > 0 else 30.0
        java_timeout = max(1, int(math.ceil(timeout)))
        started = time.monotonic()
        with self._lock:
            self._ensure_ready()
            try:
                response = self._send_recv(
                    "query", (sql, limit_int, java_timeout),
                    timeout=timeout + _QUERY_GRACE_S,
                )
            except _RpcTimeout:
                raise QueryTimeoutError(
                    f"JDBC 查询超过 {timeout:g}s 未返回（桥进程已回收）"
                ) from None
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if not response.get("ok"):
                error = response.get("error") or "未知错误"
                if response.get("timeout"):
                    raise QueryTimeoutError(
                        f"JDBC 查询超时（{timeout:g}s）: {error}"
                    )
                raise RuntimeError(f"JDBC 查询失败: {error}")
            columns = [str(c) for c in (response.get("columns") or [])]
            rows = [
                [_to_cell(value) for value in row]
                for row in (response.get("rows") or [])
            ]
            warnings = [str(w) for w in (response.get("warnings") or [])]
            return QueryResult(
                columns=columns,
                rows=rows,
                row_count=len(rows),
                truncated=bool(response.get("truncated")),
                elapsed_ms=elapsed_ms,
                source_id="",
                sql=sql,
                warnings=warnings,
            )

    def explain(self, sql: str) -> None:
        """通用 JDBC 不做 EXPLAIN（方言差异大），返回 None 表示不支持。"""
        return None

    def close(self) -> None:
        """关闭连接并回收 java 子进程（幂等）。"""
        with self._lock:
            proc = self._proc
            if proc is None:
                return
            try:
                if proc.poll() is None:
                    try:
                        assert proc.stdin is not None
                        proc.stdin.write(_encode_write_utf("close"))
                        proc.stdin.flush()
                    except (OSError, ValueError):
                        pass
                    try:
                        proc.wait(timeout=3.0)
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                self._kill_process()

    def __enter__(self) -> "JdbcAdapter":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - 解释器退出/异常路径
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------ 元数据转换
def _to_table_meta(raw: dict[str, Any], *, include_comments: bool) -> TableMeta | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    schema_value = raw.get("schema")
    schema = str(schema_value).strip() if schema_value else None
    comment = str(raw.get("comment") or "") if include_comments else ""

    raw_columns = raw.get("columns") or []
    primary_keys = {
        str(col.get("name"))
        for col in raw_columns
        if isinstance(col, dict) and col.get("is_primary_key")
    }
    columns: list[ColumnMeta] = []
    for index, col in enumerate(raw_columns):
        if not isinstance(col, dict):
            continue
        col_name = str(col.get("name") or "").strip()
        if not col_name:
            continue
        ordinal_value = col.get("ordinal")
        if isinstance(ordinal_value, bool) or not isinstance(ordinal_value, int):
            ordinal = index
        else:
            ordinal = ordinal_value
        columns.append(ColumnMeta(
            name=col_name,
            data_type=str(col.get("data_type") or "").upper(),
            nullable=bool(col.get("nullable", True)),
            comment=str(col.get("comment") or "") if include_comments else "",
            is_primary_key=col_name in primary_keys,
            ordinal=ordinal,
            generated=bool(col.get("generated")),
        ))
    columns.sort(key=lambda c: c.ordinal)

    foreign_keys: list[tuple[str, str, str]] = []
    for item in raw.get("foreign_keys") or []:
        if isinstance(item, (list, tuple)) and len(item) >= 3:
            foreign_keys.append((str(item[0]), str(item[1]), str(item[2])))

    return TableMeta(
        name=name,
        schema=schema,
        comment=comment,
        columns=columns,
        # 通用 JDBC 没有廉价行数接口：行数未知比"全表 COUNT 很慢"诚实
        row_estimate=None,
        foreign_keys=foreign_keys,
    )


def _to_cell(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
