"""执行 30 道黄金问题，产出标准结果基线。

用法: python build_baseline.py
产出:
  baseline.json       —— 标准结果（供自动判分：结果集比对）
  golden_set.jsonl    —— 问题 + 标准 SQL（供 Text2SQL 评测集加载）
  golden_set.md       —— 人读版题库与结果
"""
import io
import json
import os
import sqlite3
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from golden_questions import GOLDEN_SET

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "ecommerce.db")


def norm(v):
    """统一结果值表示，便于跨引擎/跨方言比对。"""
    if v is None:
        return None
    if isinstance(v, float):
        return f"{v:.4f}".rstrip("0").rstrip(".")
    return str(v)


def run_all():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    results, log = [], []
    for q in GOLDEN_SET:
        cur.execute(q["sql"])
        cols = [d[0] for d in cur.description]
        rows = [[norm(v) for v in r] for r in cur.fetchall()]
        results.append({
            "id": q["id"],
            "category": q["category"],
            "question": q["question"],
            "sql": q["sql"],
            "tags": q["tags"],
            "columns": cols,
            "row_count": len(rows),
            "rows": rows,
        })
        log.append(f"[{q['id']}] {len(rows):>3} 行 | {q['question'][:44]}")
    conn.close()
    return results, log


def write_jsonl(results):
    path = os.path.join(HERE, "golden_set.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps({
                "id": r["id"],
                "category": r["category"],
                "question": r["question"],
                "golden_sql": r["sql"],
                "tags": r["tags"],
            }, ensure_ascii=False) + "\n")
    return path


def write_markdown(results):
    lines = [
        "# 黄金问题集（30 题）",
        "",
        "> 由 `build_baseline.py` 自动生成，**请勿手改**——修改请改 `golden_questions.py` 后重跑。",
        "> 标准结果为 SQLite 真实执行产出，可用于自动判分（结果集比对）。",
        "",
        "**数据窗口**：2024-01-01 ~ 2024-06-30　**本月**=2024-06　**上月**=2024-05",
        "",
        "## 覆盖情况",
        "",
        "| 分组 | 题号 | 数量 |",
        "|---|---|---|",
    ]
    cats = {}
    for r in results:
        cats.setdefault(r["category"], []).append(r["id"])
    for cat, ids in cats.items():
        lines.append(f"| {cat} | {', '.join(ids)} | {len(ids)} |")
    lines.append("")

    for r in results:
        lines += [
            f"## {r['id']}　{r['question']}",
            "",
            f"- 分组：{r['category']}　标签：`{'`, `'.join(r['tags'])}`　结果行数：{r['row_count']}",
            "",
            "```sql",
            r["sql"],
            "```",
            "",
        ]
        if r["row_count"] == 0:
            lines += ["**标准结果**：空结果集", ""]
            continue
        lines.append("**标准结果**：" + "　".join(f"`{c}`" for c in r["columns"]))
        lines.append("")
        lines.append("| " + " | ".join(str(c) for c in r["columns"]) + " |")
        lines.append("|" + "---|" * len(r["columns"]))
        preview = r["rows"][:12]
        for row in preview:
            lines.append("| " + " | ".join("" if v is None else v for v in row) + " |")
        if r["row_count"] > len(preview):
            lines.append(f"| ... | 共 {r['row_count']} 行 |" if len(r["columns"]) == 2
                         else f"| （仅显示前 {len(preview)} 行，共 {r['row_count']} 行） |"
                              + " |" * (len(r["columns"]) - 1))
        lines.append("")
    return "\n".join(lines)


def main():
    results, log = run_all()
    print("\n".join(log))

    with open(os.path.join(HERE, "baseline.json"), "w", encoding="utf-8") as f:
        json.dump({
            "meta": {
                "db": "ecommerce.db",
                "seed": 20240630,
                "window": "2024-01-01~2024-06-30",
                "dialect": "sqlite",
                "total": len(results),
            },
            "cases": results,
        }, f, ensure_ascii=False, indent=2)

    p1 = write_jsonl(results)
    p2 = os.path.join(HERE, "golden_set.md")
    with open(p2, "w", encoding="utf-8") as f:
        f.write(write_markdown(results))

    empty = [r["id"] for r in results if r["row_count"] == 0]
    print(f"\nbaseline.json / {os.path.basename(p1)} / {os.path.basename(p2)} 已生成")
    print(f"结果行数为 0 的题（需检查是否合理）: {empty or '无'}")
    print("行数分布:", {r["id"]: r["row_count"] for r in results})


if __name__ == "__main__":
    main()
