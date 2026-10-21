"""文本切分：为 FTS5 准备 CJK 检索单元。

背景（这是本项目必须踩过的一个坑）
--------------------------------
SQLite FTS5 内置的 `unicode61` / `porter` 分词器**不切分中文**：
一整段连续中文（"GMV指成交金额含优惠但不剔除退款实收金额需减去"）会被当作
**单个 token**。因此：

    MATCH '"金额"'   → 0 命中

无论文档里有多少次"金额"。这会让中文知识库的 BM25 检索完全失效，
而且失效是**静默的**——不报错，只是永远召回不到。

解决方案
--------
入库时把 CJK 连续段预先切成**二元组**并用空格连接，交给 FTS5 索引：

    "实收金额" → "实收 收金 金额"

查询侧用同样的二元组做 OR 匹配，"金额" 就能作为独立 token 命中。

代价：产生 "收金" 这类跨词边界的噪声二元组。这在检索场景可接受
（噪声只影响排序权重，不改变命中与否）。

注意：**整数型 token 不会被 FTS5 索引**（`40` 查不到），
这是已知取舍——数字通常不是知识条目的检索词。
"""
from __future__ import annotations

import re

_CJK = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")
_MIN_ASCII_LEN = 2


def split_tokens(text: str) -> list[str]:
    """把文本切成检索 token。

    * CJK 连续段 → 重叠二元组（长度 < 2 的段丢弃）
    * ASCII 字母/数字段 → 整词，长度 >= 2 才保留
    """
    out: list[str] = []

    for run in _CJK.findall(text):
        if len(run) >= 2:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))

    for tok in _ASCII_TOKEN.findall(text):
        if len(tok) >= _MIN_ASCII_LEN:
            out.append(tok)

    return out


def index_text(*parts: str) -> str:
    """生成供 FTS5 索引的**预切分文本**（空格分隔的 token）。"""
    tokens: list[str] = []
    for p in parts:
        if p:
            tokens.extend(split_tokens(p))
    return " ".join(tokens)


def match_expr(text: str, *, max_tokens: int = 40) -> str:
    """生成 FTS5 MATCH 表达式。

    每个 token 加双引号并转义内部引号——必须转义，
    否则 `-` `*` `"` 会被当成 FTS 语法，轻则查不到，重则抛错。
    """
    tokens = split_tokens(text)[:max_tokens]
    if not tokens:
        return ""
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)
