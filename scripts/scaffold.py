"""生成 nl2sql 各模块骨架。

设计：**已存在的文件默认不覆盖**，避免把后续实现冲掉。
    python scripts/scaffold.py            # 只创建缺失文件
    python scripts/scaffold.py --force    # 强制重写（会覆盖实现，谨慎）
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from _stubs import STUBS  # noqa: E402


def render(docstring: str, body: str) -> str:
    return f'"""{docstring}"""\n{body}'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="覆盖已存在文件")
    args = ap.parse_args()

    created, skipped = [], []
    for rel, (doc, body) in STUBS.items():
        path = os.path.join(ROOT, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and not args.force:
            skipped.append(rel)
            continue
        with open(path, "w", encoding="utf-8") as f:
            f.write(render(doc, body))
        created.append(rel)

    for p in created:
        print(f"  + {p}")
    if skipped:
        print(f"\n跳过已存在 {len(skipped)} 个（未覆盖）：")
        for p in skipped:
            print(f"  = {p}")
    print(f"\n新建 {len(created)}，跳过 {len(skipped)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
