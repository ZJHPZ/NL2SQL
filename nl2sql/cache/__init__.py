"""结果缓存：逻辑计划指纹 → 结果集。

**已明确去掉跨会话语义相似缓存**（判断错了没人发现，会静默返回错误计划）。

只保留确定性指纹缓存，且**按数据源隔离**——个人用户会同时连测试库与生产库。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ..contracts.semantic import LogicalPlan
from ..contracts.source import QueryResult
from ..contracts.storage import CachedResult, ResultCache

__all__ = ["CacheService"]


class CacheService:
    """缓存读写门面。

    设计要点：
    * 键 = `(source_id, plan.fingerprint())`，两者缺一不可
    * 写失败**不得影响正常返回**（降级为不缓存）——缓存只是优化，
      不参与正确性判断，因此它出错时正确答案仍必须给出
    """

    def __init__(self, store: ResultCache, *, enabled: bool = True,
                 ttl_seconds: int = 900) -> None:
        self._store = store
        self._enabled = enabled
        self._ttl = ttl_seconds

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------ 读
    def lookup(self, source_id: str, plan: LogicalPlan) -> CachedResult | None:
        if not self._enabled:
            return None
        try:
            return self._store.get(source_id, plan.fingerprint())
        except Exception:  # noqa: BLE001 - 缓存故障不应阻断查询
            return None

    def to_query_result(self, cached: CachedResult) -> QueryResult:
        """把缓存条目还原成 `QueryResult`，与实时执行的结果形状一致。"""
        return QueryResult(
            columns=list(cached.columns),
            rows=[tuple(r) for r in cached.rows],
            row_count=cached.row_count,
            truncated=False,
            elapsed_ms=0,
            source_id=cached.source_id,
            sql=cached.sql,
            cached=True,
            data_time=cached.data_time,
            warnings=["结果来自缓存"],
        )

    # ------------------------------------------------------------ 写
    def store_result(self, source_id: str, plan: LogicalPlan,
                     result: QueryResult, *, data_time: str | None = None) -> bool:
        """写缓存。返回是否写入成功。

        失败返回 False 而不是抛异常：缓存坏了不该让查询失败。
        """
        if not self._enabled:
            return False
        # 截断的结果不入缓存：下次可能因限额变化得到不同结果
        if result.truncated:
            return False
        try:
            self._store.put(
                CachedResult(
                    source_id=source_id,
                    plan_fingerprint=plan.fingerprint(),
                    columns=list(result.columns),
                    rows=[list(r) for r in result.rows],
                    row_count=result.row_count,
                    sql=result.sql,
                    created_at=datetime.now().replace(microsecond=0).isoformat(),
                    data_time=data_time or result.data_time,
                ),
                ttl_seconds=self._ttl,
            )
            return True
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------ 手写 SQL 缓存
    # 手写 SQL 没有逻辑计划，用 SQL 文本的规范化摘要作为指纹。
    # 只做一次空白归一化（连续空白折叠为单空格、去首尾），
    # **不做语义归一化**——手写 SQL 的正确性由用户负责，
    # 我们只保证"同一段文本"命中同一条缓存，这足够也安全。
    def lookup_sql(self, source_id: str, sql: str) -> CachedResult | None:
        if not self._enabled:
            return None
        try:
            return self._store.get(source_id, _sql_fingerprint(sql))
        except Exception:  # noqa: BLE001
            return None

    def store_sql(self, source_id: str, sql: str, result: QueryResult,
                  *, data_time: str | None = None) -> bool:
        if not self._enabled or result.truncated:
            return False
        try:
            self._store.put(
                CachedResult(
                    source_id=source_id,
                    plan_fingerprint=_sql_fingerprint(sql),
                    columns=list(result.columns),
                    rows=[list(r) for r in result.rows],
                    row_count=result.row_count,
                    sql=sql,
                    created_at=datetime.now().replace(microsecond=0).isoformat(),
                    data_time=data_time or result.data_time,
                ),
                ttl_seconds=self._ttl,
            )
            return True
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------ 维护
    def invalidate(self, source_id: str) -> int:
        """手动刷新：清空某数据源缓存。**只作用于该数据源。**"""
        try:
            return self._store.invalidate(source_id)
        except Exception:  # noqa: BLE001
            return 0

    def stats(self) -> dict[str, Any]:
        try:
            return self._store.stats()
        except Exception:  # noqa: BLE001
            return {}


def _sql_fingerprint(sql: str) -> str:
    """手写 SQL 的缓存键：空白归一化 + sha256 前 32 位。"""
    import hashlib
    import re

    normalized = re.sub(r"\s+", " ", (sql or "").strip())
    return "sql:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]
