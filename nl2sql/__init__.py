"""nl2sql 核心引擎。

对外唯一门面是 `NL2SQL`。三个壳（Web / SDK / 内部工具）都只通过它交互。

本包硬约束（见 docs/01-仓库结构与依赖方向.md）：
* 不依赖任何 Web 框架
* 不 import apps/*
* 无模块级可变全局状态

约束可执行化：`python scripts/check_layers.py`
"""
from __future__ import annotations

__version__ = "0.1.0"

from .api import NL2SQL, NL2SQLConfig

__all__ = ["NL2SQL", "NL2SQLConfig", "__version__"]
