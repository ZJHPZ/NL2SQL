"""技能契约：`SKILL.md` 的解析结果与目录。

为什么技能是一个独立契约，而不是 `KnowledgeType`
-------------------------------------------------
知识库的条目是**事实**（口径、字段说明、相似 SQL），靠 BM25 + 向量按相关度
**竞争** `top_k`，片段化地拼进 Prompt。技能是**流程**（该按什么步骤做），
按 `description` **命中一个**然后**整份**加载。

两者不能合并，理由只有一条但足够：**检索会按分数截断**。
把一份流程拆成片段喂给模型，流程就不成立了。
（给 `KnowledgeType` 加一个 `"skill"` 会让技能去和 schema 条目抢名额。）

设计约束（来自 `docs/08` 第 5 节、`docs/09` 第 4 节）
----------------------------------------------------
1. **frontmatter 只用生态公认的 `name` / `description`**，其余收进 `nl2sql:`
   命名空间——与 dbt 那边的 `meta.nl2sql` 同一套理由：用宿主格式指定的
   扩展点，而不是往顶层塞私有字段。这样同一份文件拿给别的 agent 仍然合法。
2. **`description` 只写"何时用"，不概括流程或产出**。这不是文风要求：
   实测发现描述里概括了流程，模型会照着描述做而**不去读正文**。
3. **正文说"动作"不说工具名**。因为我们的工具会消失——能力没授予的工具
   根本不注册，写死工具名的技能在那种场景下会教模型去调一个不存在的工具。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = [
    "SkillKind",
    "SkillSpec",
    "SkillCatalog",
    "FRONTMATTER_MAX_CHARS",
    "DESCRIPTION_MAX_CHARS",
    "VALID_KINDS",
]

# 生态约束：frontmatter 总长上限。超了不是"风格问题"——有些 agent 会直接
# 拒收这份技能，所以我们在加载期就拦下来。
FRONTMATTER_MAX_CHARS = 1024
# 描述上限（建议值，超了给警告而不是拒绝：它是选择依据，太长会稀释匹配）
DESCRIPTION_MAX_CHARS = 500

# 两类技能。区分它们是因为风险不同：
#   playbook      按需加载的流程说明——纯文本，只在模型主动要的时候进上下文
#   planner-hint  合并进某个 system prompt 的指令——**能改写系统指令**，
#                 风险最高，因此需要显式能力授予（P2 才做，P0 只认字段）
SkillKind = str  # "playbook" | "planner-hint"

VALID_KINDS = ("playbook", "planner-hint")


@dataclass
class SkillSpec:
    """一份 `SKILL.md` 的解析结果。

    `body` 是正文（**完整**，不截断——流程被切一半就不成立了）。
    """
    name: str
    description: str = ""
    kind: SkillKind = "playbook"
    requires: tuple[str, ...] = ()
    scope: str | None = None          # 绑定的数据源；None = 全局
    enabled: bool = True
    applies_to: tuple[str, ...] = ()  # 无模型路径用的确定性匹配模式
    body: str = ""
    origin: str = "project"           # "project" | "external" | "plugin:<name>"
    path: str = ""
    warnings: tuple[str, ...] = ()

    def matches_scope(self, source_id: str | None) -> bool:
        """技能是否适用于该数据源。`scope=None` 表示全局适用。"""
        if self.scope is None:
            return True
        return self.scope == (source_id or "")

    def catalog_line(self) -> str:
        """目录里的一行（给模型看的那一行）。"""
        return f"- {self.name}: {self.description}"


@runtime_checkable
class SkillCatalog(Protocol):
    """已加载技能的目录。**只读**：加载在装配期完成，运行期不变。

    只读是刻意的：技能在运行期被替换会让"同一问题的行为"不可复现，
    而本项目的缓存指纹与回归测试都建立在可复现上。
    """

    def names(self) -> list[str]: ...

    def get(self, name: str) -> SkillSpec | None: ...

    def all(self) -> list[SkillSpec]: ...

    def catalog_text(self, *, source_id: str | None = None) -> str:
        """渲染成给模型看的目录（只有 name + description，不含正文）。"""
        ...
