"""LangGraph 管线定义。

约束：
    * `langgraph` 只在这里出现——核心其他模块不依赖它
    * state 为普通 dict，便于持久化与回放
    * 节点实现从注册表查找，便于用户替换
"""
from __future__ import annotations

from typing import Any

__all__ = ["build_langgraph"]


def build_langgraph(spec, nodes: dict[str, Any]):
    """TODO(S4): 把 GraphSpec 编译成 LangGraph 图。

    实现提示：
        from langgraph.graph import StateGraph, END
        g = StateGraph(dict)
        for node_spec in spec.nodes:
            g.add_node(node_spec.name, nodes[node_spec.name].run)
        ...
    一期不做澄清分支；clarify=False。
    """
    raise NotImplementedError("S4")
