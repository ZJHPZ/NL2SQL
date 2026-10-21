"""知识库契约：条目、版本、检索。

设计要点（个人场景）
------------------
1. **免审**：没有 draft/pending/active 审批流，条目默认可用。
2. **但保留机制**：`enabled` 开关 + 全量版本留痕 + 测试检索。
   "免审 ≠ 无版本"——出问题时必须能定位并回滚到某条知识。
3. **测试检索是一等能力**：没有它，知识库的错误是静默的。
4. **四类召回统一接口**：schema / 业务知识 / 相似SQL / 纠错。
   后期换向量库、加图检索、加精排都不动上层。
5. **自动生成**：首次连接数据源时从库表注释生成初版条目，
   避免用户面对空白页——这是降低上手门槛的关键。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

KnowledgeType = Literal["schema", "business", "sql_pair", "correction"]

# 知识类型 → 用途
#   schema     : 表/列说明、同义词、值域枚举      → 帮模型选对字段
#   business   : 口径、业务规则、特殊日期定义      → 帮模型用对语义
#   sql_pair   : 历史 (问题, SQL) 对               → few-shot 示例
#   correction : 用户反馈的纠错                    → 高优先级召回


@dataclass
class KnowledgeEntry:
    """一条知识。`key` + `version` 唯一标识一个修订。"""

    key: str                          # 稳定标识，跨版本不变
    type: KnowledgeType
    title: str = ""
    body: str = ""                    # 正文（进入向量检索的内容）
    source_id: str | None = None      # 绑定的数据源；None 表示全局
    tags: list[str] = field(default_factory=list)
    enabled: bool = True              # 停用开关（替代审批流）
    origin: str = "user"              # "user" | "auto:comments" | "import"
    source_ref: str = ""              # 来源文档/表名，便于溯源
    created_at: str = ""
    updated_at: str = ""
    version: int = 1
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Revision:
    """历史修订，用于 diff 与回滚。"""

    key: str
    version: int
    body: str
    title: str = ""
    changed_at: str = ""
    changed_by: str = ""
    note: str = ""


@dataclass
class RetrievalHit:
    """一条召回结果。`why` 用于测试检索页展示命中原因。"""

    entry: KnowledgeEntry
    score: float
    why: str = ""                     # "bm25:0.82" / "vector:0.77" / "rerank:0.91"
    matched_terms: list[str] = field(default_factory=list)


@dataclass
class RetrievalQuery:
    """检索请求。"""

    text: str
    source_id: str | None = None
    types: tuple[KnowledgeType, ...] = ()
    top_k: int = 8
    hybrid: bool = True               # BM25 + 向量 + 重排


class KnowledgeStore(Protocol):
    """知识持久化契约（实现见 storage/）。"""

    def upsert(self, entry: KnowledgeEntry, *, note: str = "") -> KnowledgeEntry: ...
    def get(self, key: str) -> KnowledgeEntry | None: ...
    def list(
        self, *, source_id: str | None = None,
        types: tuple[KnowledgeType, ...] = (), include_disabled: bool = False,
    ) -> list[KnowledgeEntry]: ...
    def history(self, key: str) -> list[Revision]: ...
    def rollback(self, key: str, version: int) -> KnowledgeEntry: ...
    def set_enabled(self, key: str, enabled: bool) -> None: ...
    def delete(self, key: str) -> None: ...


@runtime_checkable
class Retriever(Protocol):
    """召回契约。四类知识共用同一接口，便于后期替换实现。"""

    name: str

    def index(self, entries: list[KnowledgeEntry]) -> None: ...

    def retrieve(self, query: RetrievalQuery) -> list[RetrievalHit]: ...


@runtime_checkable
class KnowledgeBase(Protocol):
    """知识库门面：管理 + 检索 + 自动生成。"""

    def upsert(self, entry: KnowledgeEntry, *, note: str = "") -> KnowledgeEntry: ...
    def retrieve(self, query: RetrievalQuery) -> list[RetrievalHit]: ...
    def test_retrieve(self, text: str, *, source_id: str | None = None,
                      top_k: int = 8) -> list[RetrievalHit]:
        """测试检索：返回命中条目与命中原因，供 UI 展示。"""
        ...

    def auto_generate_from_comments(self, source_id: str) -> list[KnowledgeEntry]:
        """从数据源库表注释生成初版条目（幂等，不覆盖用户已改内容）。"""
        ...
