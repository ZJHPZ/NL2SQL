"""结果缓存（SQLite 实现）。

键 = `(source_id, plan_fingerprint)`。**source_id 必须参与键**——
个人用户会同时连测试库与生产库，串缓存是灾难。

失效策略：TTL + 手动刷新（`invalidate`）。**不做 CDC**（太复杂）。

值序列化约定：`rows` 以 JSON 存储，所有值统一归一化为字符串或 null。
理由见 `normalize_value`。这与 `golden_set/evaluate.py` 的判分归一化保持一致，
保证"缓存命中"与"重新执行"得到可比较的结果。
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Sequence

from ..contracts.storage import CachedResult
from ._dbapi import SqliteKV
from .migrations import open_connection


def normalize_value(v: Any) -> Any:
    """把执行结果的值归一化为 JSON 可存的标量。

    为什么要归一化：
        不同驱动返回的类型不同（float / Decimal / int / datetime），
        若不统一，同一份数据在"缓存命中"与"重新执行"两条路径上
        可能得到不同表示，导致结果比对与界面展示不一致。

    规则：
        整数 → 整数字符串（"40"）
        浮点 → 最多 4 位小数并去掉尾零（"216339.57"）
        日期时间 → ISO8601
        Decimal → 按浮点规则
        None → None
        其他 → str()
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, (float, Decimal)):
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return str(f)
        if f == int(f) and abs(f) < 1e15:
            return str(int(f))
        return f"{f:.4f}".rstrip("0").rstrip(".")
    if isinstance(v, (datetime, date, time)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return str(v)


def normalize_rows(rows: Sequence[Sequence[Any]]) -> list[list[Any]]:
    return [[normalize_value(v) for v in row] for row in rows]


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _plus_seconds(iso_ts: str, seconds: int) -> str:
    try:
        base = datetime.fromisoformat(iso_ts)
    except ValueError:
        base = datetime.now()
    return (base + timedelta(seconds=seconds)).replace(microsecond=0).isoformat()


class SqliteResultCache:
    """实现 `ResultCache` 契约。"""

    def __init__(self, db_path: str) -> None:
        self._conn: SqliteKV = open_connection(db_path)

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------ 读写
    def get(self, source_id: str, plan_fingerprint: str) -> CachedResult | None:
        row = self._conn.execute(
            "SELECT * FROM result_cache WHERE source_id = ? AND plan_fingerprint = ?",
            (source_id, plan_fingerprint),
        ).fetchone()
        if row is None:
            return None

        expires_at = row["expires_at"]
        if expires_at and expires_at <= _now_iso():
            # 过期即删，避免陈旧结果继续被读到
            self._conn.execute(
                "DELETE FROM result_cache WHERE source_id = ? AND plan_fingerprint = ?",
                (source_id, plan_fingerprint),
            )
            self._conn.commit()
            return None

        return CachedResult(
            source_id=row["source_id"],
            plan_fingerprint=row["plan_fingerprint"],
            columns=json.loads(row["columns"] or "[]"),
            rows=json.loads(row["rows"] or "[]"),
            row_count=row["row_count"] or 0,
            sql=row["sql"] or "",
            created_at=row["created_at"] or "",
            expires_at=expires_at,
            data_time=row["data_time"],
        )

    def put(self, item: CachedResult, *, ttl_seconds: int | None = None) -> None:
        created_at = item.created_at or _now_iso()
        expires_at = item.expires_at
        if expires_at is None and ttl_seconds is not None:
            expires_at = _plus_seconds(created_at, ttl_seconds)
        self._conn.execute(
            "INSERT INTO result_cache (source_id, plan_fingerprint, columns, rows,"
            " row_count, sql, created_at, expires_at, data_time)"
            " VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(source_id, plan_fingerprint) DO UPDATE SET"
            " columns=excluded.columns, rows=excluded.rows,"
            " row_count=excluded.row_count, sql=excluded.sql,"
            " created_at=excluded.created_at, expires_at=excluded.expires_at,"
            " data_time=excluded.data_time",
            (item.source_id, item.plan_fingerprint,
             json.dumps(item.columns, ensure_ascii=False),
             json.dumps(normalize_rows(item.rows), ensure_ascii=False),
             item.row_count, item.sql, created_at, expires_at, item.data_time),
        )
        self._conn.commit()

    def invalidate(self, source_id: str) -> int:
        """手动刷新：清空某数据源缓存，返回清除条数。"""
        cur = self._conn.execute(
            "DELETE FROM result_cache WHERE source_id = ?", (source_id,)
        )
        self._conn.commit()
        return cur.rowcount or 0

    def purge_expired(self) -> int:
        cur = self._conn.execute(
            "DELETE FROM result_cache WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (_now_iso(),),
        )
        self._conn.commit()
        return cur.rowcount or 0

    def stats(self) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(row_count), 0) AS rows_total"
            " FROM result_cache"
        ).fetchone()
        per_source = self._conn.execute(
            "SELECT source_id, COUNT(*) AS n FROM result_cache GROUP BY source_id"
        ).fetchall()
        return {
            "entries": row["n"] if row else 0,
            "rows_total": row["rows_total"] if row else 0,
            "by_source": {r["source_id"]: r["n"] for r in per_source},
        }
