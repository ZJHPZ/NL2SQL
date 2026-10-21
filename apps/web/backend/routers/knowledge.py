"""知识库路由：条目管理 + **测试检索**。

测试检索是这里的核心能力，不是附属功能：
没有它，用户上传的知识是否生效只能靠猜——检索可能永远命中不到，
或命中到错误条目，而界面上看不出来。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from nl2sql.contracts.knowledge import KnowledgeEntry
from nl2sql.knowledge.naming import stable_key

from ..deps import get_engine
from ..schemas import (
    KnowledgeHit,
    KnowledgeItem,
    KnowledgeTestRequest,
    KnowledgeToggleRequest,
    KnowledgeUpsertRequest,
)
from ..serializers import knowledge_hit, knowledge_item

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


@router.get("", response_model=list[KnowledgeItem])
def list_entries(source_id: str | None = None, type: str | None = None,
                 include_disabled: bool = False,
                 engine: Any = Depends(get_engine)) -> list[KnowledgeItem]:
    types = (type,) if type else ()
    entries = engine.knowledge_list(
        source_id=source_id, types=types, include_disabled=include_disabled)
    return [knowledge_item(e) for e in entries]


@router.post("", response_model=KnowledgeItem)
def upsert_entry(req: KnowledgeUpsertRequest,
                 engine: Any = Depends(get_engine)) -> KnowledgeItem:
    """新增或更新条目。免审，但留版本——出问题时可回滚。"""
    key = req.key or stable_key(req.type, req.title or req.body[:16])
    entry = KnowledgeEntry(
        key=key, type=req.type, title=req.title, body=req.body,
        source_id=req.source_id, tags=list(req.tags or []),
    )
    saved = engine.knowledge_upsert(entry, note=req.note or "界面写入")
    return knowledge_item(saved)


@router.post("/{key:path}/toggle", response_model=KnowledgeItem)
def toggle_entry(key: str, req: KnowledgeToggleRequest,
                 engine: Any = Depends(get_engine)) -> KnowledgeItem:
    """停用/启用。怀疑某条污染结果时可立即下线，不必删除。"""
    engine.knowledge_set_enabled(key, req.enabled)
    entry = engine._knowledge.get(key)  # noqa: SLF001
    if entry is None:
        raise HTTPException(status_code=404, detail=f"条目不存在: {key}")
    return knowledge_item(entry)


@router.get("/{key:path}/history", response_model=list[dict])
def entry_history(key: str, engine: Any = Depends(get_engine)) -> list[dict]:
    return [
        {
            "key": r.key, "version": r.version, "title": r.title,
            "body": r.body, "changed_at": r.changed_at,
            "changed_by": r.changed_by, "note": r.note,
        }
        for r in engine.knowledge_history(key)
    ]


@router.post("/{key:path}/rollback", response_model=KnowledgeItem)
def rollback_entry(key: str, version: int,
                   engine: Any = Depends(get_engine)) -> KnowledgeItem:
    """回滚到历史版本。**生成新版本号，不回退**（否则历史会分叉）。"""
    try:
        return knowledge_item(engine.knowledge_rollback(key, version))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.delete("/{key:path}")
def delete_entry(key: str, engine: Any = Depends(get_engine)) -> dict[str, str]:
    """软删：停用并移出检索，历史保留。"""
    engine.knowledge_delete(key)
    return {"deleted": key}


@router.post("/test", response_model=list[KnowledgeHit])
def test_retrieve(req: KnowledgeTestRequest,
                  engine: Any = Depends(get_engine)) -> list[KnowledgeHit]:
    """**测试检索**：输入一个问题，看实际命中了哪几条、为什么命中。

    `why` 字段形如 `bm25#1+vector#3`，说明来自哪一路召回、排第几。
    """
    hits = engine.knowledge_test_retrieve(
        req.text, source_id=req.source_id, top_k=req.top_k)
    return [knowledge_hit(h) for h in hits]


@router.get("/few-shot")
def few_shot(question: str, source_id: str | None = None,
             top_k: int = 3, engine: Any = Depends(get_engine)) -> list[dict]:
    """查看某问题会召回哪些 few-shot 示例（用于排查规划质量）。"""
    pairs = engine.knowledge_few_shot(
        question, source_id=source_id, top_k=top_k)
    return [{"question": q, "sql": s} for q, s in pairs]


@router.post("/reindex")
def reindex(engine: Any = Depends(get_engine)) -> dict[str, int]:
    """全量重建向量索引。换 embedding 模型后必须调用。"""
    return {"indexed": engine.knowledge_reindex()}
