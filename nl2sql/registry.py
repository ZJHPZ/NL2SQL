"""组件注册表：适配器 / 语义提供者 / 检索器 / DAG 节点实现的注册与查找。

设计要点
--------
1. **注册表实例化，不做模块级全局**：每个 `NL2SQL` 持有一个自己的注册表。
   这样多实例（测试、多租户、多数据源）互不污染。
2. **装饰器注册**：`@registry.adapter("sqlite")` 让新增数据源只写一个文件。
3. **用户自定义组件走同一入口**：自定义 Prompt / 换模型 / 自定义节点，
   都表现为注册一个具名实现，而不是改引擎代码。
"""
from __future__ import annotations

from typing import Any, Callable, Generic, TypeVar

from .errors import NotRegisteredError

T = TypeVar("T")


class _Registry(Generic[T]):
    """具名实现的注册表。"""

    def __init__(self, kind: str) -> None:
        self._kind = kind
        self._items: dict[str, T] = {}

    def register(self, name: str, obj: T) -> None:
        if name in self._items:
            raise NotRegisteredError(f"{self._kind} 已存在同名实现: {name}")
        self._items[name] = obj

    def get(self, name: str) -> T:
        if name not in self._items:
            avail = ", ".join(sorted(self._items)) or "（空）"
            raise NotRegisteredError(
                f"未注册的 {self._kind}: {name}；可用: {avail}"
            )
        return self._items[name]

    def names(self) -> list[str]:
        return sorted(self._items)

    def has(self, name: str) -> bool:
        return name in self._items


class Registry:
    """一个 NL2SQL 实例的组件集合。"""

    def __init__(self) -> None:
        self.adapters: _Registry[Callable[..., Any]] = _Registry("数据源适配器")
        self.semantic_providers: _Registry[Callable[..., Any]] = _Registry("语义提供者")
        self.retrievers: _Registry[Callable[..., Any]] = _Registry("检索器")
        self.nodes: _Registry[Callable[..., Any]] = _Registry("DAG 节点实现")
        self.vector_stores: _Registry[Callable[..., Any]] = _Registry("向量库")
        self.embedders: _Registry[Callable[..., Any]] = _Registry("向量化器")
        self.llms: _Registry[Callable[..., Any]] = _Registry("模型")

    # ---------------------------------------------------------- 装饰器形式
    def adapter(self, name: str) -> Callable[[T], T]:
        return lambda obj: (self.adapters.register(name, obj), obj)[1]

    def semantic_provider(self, name: str) -> Callable[[T], T]:
        return lambda obj: (self.semantic_providers.register(name, obj), obj)[1]

    def retriever(self, name: str) -> Callable[[T], T]:
        return lambda obj: (self.retrievers.register(name, obj), obj)[1]

    def node(self, name: str) -> Callable[[T], T]:
        return lambda obj: (self.nodes.register(name, obj), obj)[1]

    def vector_store(self, name: str) -> Callable[[T], T]:
        return lambda obj: (self.vector_stores.register(name, obj), obj)[1]

    def embedder(self, name: str) -> Callable[[T], T]:
        return lambda obj: (self.embedders.register(name, obj), obj)[1]

    def llm(self, name: str) -> Callable[[T], T]:
        return lambda obj: (self.llms.register(name, obj), obj)[1]

    def describe(self) -> dict[str, list[str]]:
        return {
            "adapters": self.adapters.names(),
            "semantic_providers": self.semantic_providers.names(),
            "retrievers": self.retrievers.names(),
            "nodes": self.nodes.names(),
            "vector_stores": self.vector_stores.names(),
            "embedders": self.embedders.names(),
            "llms": self.llms.names(),
        }
