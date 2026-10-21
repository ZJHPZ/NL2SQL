"""元数据目录：抽取 + 缓存 + 快照。

职责：把数据源的库表结构变成可检索、可对比的快照，
并向知识库提供"库表注释 → 知识条目"的原料。

边界：
    * 不含业务语义（"这个字段是 GMV" 属于 semantics）
    * 不写知识库（只提供原料，由 knowledge 层决定怎么用）
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from ..contracts.source import SourceAdapter, SnapshotMeta, TableMeta

__all__ = ["CatalogService", "diff_snapshots", "fingerprint_tables", "render_ddl"]


def fingerprint_tables(tables: Iterable[TableMeta]) -> str:
    """结构指纹：只包含**名称与列定义**，不含行数。

    为什么不含 row_estimate：
        行数每天都在变。若算进指纹，快照会被判定为"每天都变了"，
        从而触发不必要的知识库重建提示。行数属于数据，不属于结构。
    """
    payload = [
        {
            "name": t.name,
            "schema": t.schema,
            "columns": [
                [c.name, c.data_type, bool(c.nullable), bool(c.is_primary_key)]
                for c in t.columns
            ],
            "fks": sorted(t.foreign_keys),
        }
        for t in sorted(tables, key=lambda x: (x.schema or "", x.name))
    ]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def render_ddl(
    tables: list[TableMeta],
    *,
    max_tables: int = 50,
    max_columns: int = 80,
    annotate_rows: bool = True,
    compact: bool = False,
) -> str:
    """把元数据渲染成精简 DDL 文本，供自由生成时作为上下文。

    **绝不把整库 DDL 全塞进 prompt。** 因此这里强制限额：
        max_tables  —— 最多渲染多少张表
        max_columns —— 单表最多渲染多少列（超出则省略并标注）
    """
    lines: list[str] = []
    for t in tables[:max_tables]:
        name = f"{t.schema}.{t.name}" if t.schema else t.name
        rows = ""
        if annotate_rows and t.row_estimate is not None:
            rows = f"  -- {t.row_estimate} 行"
        comment = f"  -- {t.comment}" if t.comment and not compact else ""
        lines.append(f"CREATE TABLE {name} ({rows}{comment}")

        shown = t.columns[:max_columns]
        col_parts: list[str] = []
        for c in shown:
            parts = [c.name, c.data_type or "TEXT"]
            if c.is_primary_key:
                parts.append("PRIMARY KEY")
            if not c.nullable:
                parts.append("NOT NULL")
            if c.comment and not compact:
                parts.append(f"COMMENT '{c.comment}'")
            col_parts.append("    " + " ".join(parts))
        if len(t.columns) > len(shown):
            col_parts.append(f"    -- ... 另有 {len(t.columns) - len(shown)} 列未显示")
        lines.append(",\n".join(col_parts))

        fk_parts = [
            f"    FOREIGN KEY ({col}) REFERENCES {ref_table}({ref_col or '?'})"
            for col, ref_table, ref_col in t.foreign_keys
        ]
        if fk_parts:
            lines.append(",\n".join(fk_parts))
        lines.append(");")

    if len(tables) > max_tables:
        lines.append(f"-- 另有 {len(tables) - max_tables} 张表未显示")
    return "\n".join(lines)


def diff_snapshots(
    old: list[TableMeta], new: list[TableMeta]
) -> dict[str, list[str]]:
    """快照差异，用于提示"表结构变了，知识库可能需要更新"。

    Returns:
        {"added": [...], "removed": [...], "changed": [...]}
    """
    old_map = {t.name: t for t in old}
    new_map = {t.name: t for t in new}

    added = sorted(set(new_map) - set(old_map))
    removed = sorted(set(old_map) - set(new_map))
    changed: list[str] = []
    for name in sorted(set(old_map) & set(new_map)):
        if _table_signature(old_map[name]) != _table_signature(new_map[name]):
            changed.append(name)
    return {"added": added, "removed": removed, "changed": changed}


def _table_signature(t: TableMeta) -> list[Any]:
    return [
        [c.name, c.data_type, bool(c.nullable)] for c in t.columns
    ] + [list(fk) for fk in sorted(t.foreign_keys)]


class CatalogService:
    """元数据抽取与缓存。

    用法::

        catalog = CatalogService(meta_store, adapters)
        meta = catalog.refresh("default")      # 抽取并落盘
        tables = catalog.tables("default")     # 从快照读
        ddl = catalog.render_ddl("default")    # 渲染上下文
    """

    def __init__(self, store: Any, adapters: dict[str, SourceAdapter]) -> None:
        self._store = store
        self._adapters = adapters

    # ------------------------------------------------------------ 抽取
    def refresh(self, source_id: str, *, force: bool = False,
                count_rows: bool | None = None) -> SnapshotMeta:
        """抽取元数据并落盘。

        Args:
            force: True 则无论指纹是否变化都重新写入。
            count_rows: 是否统计行数；None 用适配器默认。

        返回最新快照标识。指纹基于**结构**（不含行数），因此日常数据增长
        不会导致快照反复变更。
        """
        adapter = self._adapter(source_id)
        tables = adapter.introspect(count_rows=count_rows)
        fp = fingerprint_tables(tables)

        if not force:
            existing = self._store.get_snapshot_meta(source_id)
            if existing and existing.fingerprint == fp:
                return existing

        meta = SnapshotMeta(
            source_id=source_id,
            fingerprint=fp,
            captured_at=_now_iso(),
            table_count=len(tables),
        )
        self._store.save_snapshot(meta, tables)
        return meta

    def has_changed(self, source_id: str) -> bool:
        """当前库结构与已存快照是否不同（不落盘）。"""
        adapter = self._adapter(source_id)
        fp = fingerprint_tables(adapter.introspect(count_rows=False))
        existing = self._store.get_snapshot_meta(source_id)
        return existing is None or existing.fingerprint != fp

    # ------------------------------------------------------------ 读取
    def tables(self, source_id: str) -> list[TableMeta]:
        return self._store.get_tables(source_id)

    def snapshot_meta(self, source_id: str) -> SnapshotMeta | None:
        return self._store.get_snapshot_meta(source_id)

    def table(self, source_id: str, table_name: str) -> TableMeta | None:
        for t in self.tables(source_id):
            if t.name == table_name:
                return t
        return None

    def column_names(self, source_id: str) -> list[str]:
        """全部列名（去重）。用于"列名不存在"时给出候选。"""
        seen: dict[str, None] = {}
        for t in self.tables(source_id):
            for c in t.columns:
                seen.setdefault(c.name, None)
        return sorted(seen)

    def render_ddl(self, source_id: str, **kwargs: Any) -> str:
        return render_ddl(self.tables(source_id), **kwargs)

    def changes_since_snapshot(self, source_id: str) -> dict[str, list[str]]:
        """与已存快照的差异（要求适配器当前可连）。"""
        adapter = self._adapter(source_id)
        return diff_snapshots(self.tables(source_id),
                              adapter.introspect(count_rows=False))

    def staleness(self, source_id: str, *, max_age_hours: int = 24) -> bool:
        """快照是否过旧。过旧时界面应提示"结构可能已变化"。"""
        meta = self.snapshot_meta(source_id)
        if meta is None:
            return True
        try:
            captured = datetime.fromisoformat(meta.captured_at)
        except ValueError:
            return True
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - captured > timedelta(hours=max_age_hours)

    # ------------------------------------------------------------ 内部
    def _adapter(self, source_id: str) -> SourceAdapter:
        if source_id not in self._adapters:
            from ..errors import ConfigError

            raise ConfigError(f"数据源 {source_id} 适配器未装配")
        return self._adapters[source_id]
