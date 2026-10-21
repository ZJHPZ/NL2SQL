"""结果集比对评测器（Execution Accuracy）。

对比 baseline.json 的标准结果与被测 SQL 的执行结果。

用法:
    # 1) 自检：用标准 SQL 跑自己，应全部通过
    python evaluate.py

    # 2) 评测模型产出（JSONL，每行 {"id": "A1", "sql": "..."}）
    python evaluate.py predictions.jsonl

判分规则:
    PASS         结果集一致（行序无关、列名无关、值按数值等价比较）
    ROW_MISMATCH 可执行但结果不一致
    ERROR        执行报错（交给 sqlglot 校验层时可再细分）
    TIMEOUT      超时
    MISSING      预测文件缺少该题
"""
import io
import json
import os
import sqlite3
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "ecommerce.db")
BASELINE = os.path.join(HERE, "baseline.json")
TIMEOUT_SEC = 10.0


# --------------------------------------------------------------- 数值归一化
def is_num(s):
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


def canon_cell(v):
    if v is None:
        return ("null",)
    s = str(v).strip()
    if is_num(s):
        f = float(s)
        if f == int(f) and abs(f) < 1e15:
            return ("num", float(int(f)))
        return ("num", round(f, 4))
    return ("str", s)


def canon_rows(rows, float_tol=1e-6):
    """行序无关 + 数值容差的多重集比较。"""
    out = []
    for r in rows:
        cells = [canon_cell(v) for v in r]
        # 数值单元格分组，便于容差比较
        nums = tuple(round(c[1], 4) for c in cells if c[0] == "num")
        strs = tuple(sorted(c[1] for c in cells if c[0] != "num"))
        nulls = sum(1 for c in cells if c[0] == "null")
        out.append((nums, strs, nulls))
    return sorted(out)


def rows_equal(a, b, tol=1e-6):
    if len(a) != len(b):
        return False
    ca, cb = canon_rows(a), canon_rows(b)
    for (na, sa, xa), (nb, sb, xb) in zip(ca, cb):
        if sa != sb or xa != xb or len(na) != len(nb):
            return False
        for x, y in zip(na, nb):
            if abs(x - y) > tol:
                return False
    return True


# --------------------------------------------------------------- 执行
def run_sql(cur, sql):
    t0 = time.time()
    cur.execute(sql)
    rows = cur.fetchall()
    return rows, time.time() - t0


def load_baseline():
    with open(BASELINE, encoding="utf-8") as f:
        return json.load(f)


def evaluate(pred_path=None):
    base = load_baseline()
    cases = base["cases"]

    preds = {}
    if pred_path:
        with open(pred_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                preds[obj["id"]] = obj["sql"]

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    cur = conn.cursor()

    stats = {}
    details = []
    for c in cases:
        cid = c["id"]
        if pred_path and cid not in preds:
            stats["MISSING"] = stats.get("MISSING", 0) + 1
            details.append((cid, "MISSING", "预测文件缺少该题"))
            continue

        sql = preds[cid] if pred_path else c["sql"]
        expected = c["rows"]
        try:
            got, secs = run_sql(cur, sql)
            if secs > TIMEOUT_SEC:
                stats["TIMEOUT"] = stats.get("TIMEOUT", 0) + 1
                details.append((cid, "TIMEOUT", f"{secs:.1f}s"))
                continue
            if rows_equal(expected, got):
                stats["PASS"] = stats.get("PASS", 0) + 1
                details.append((cid, "PASS", f"{len(got)} 行 / {secs*1000:.0f}ms"))
            else:
                stats["ROW_MISMATCH"] = stats.get("ROW_MISMATCH", 0) + 1
                details.append((cid, "ROW_MISMATCH",
                                f"期望 {len(expected)} 行，实际 {len(got)} 行"))
        except Exception as e:
            stats["ERROR"] = stats.get("ERROR", 0) + 1
            details.append((cid, "ERROR", str(e)[:90]))
    conn.close()

    total = len(cases)
    passed = stats.get("PASS", 0)
    print(f"数据集: {os.path.basename(pred_path) if pred_path else 'baseline.json（自检）'}")
    print(f"总数 {total}　通过 {passed}　执行准确率 {passed/total:.1%}\n")
    for k in ["PASS", "ROW_MISMATCH", "ERROR", "TIMEOUT", "MISSING"]:
        if stats.get(k):
            print(f"  {k:14s} {stats[k]}")
    print()
    for cid, status, msg in details:
        mark = "OK  " if status == "PASS" else "FAIL"
        if status != "PASS" or pred_path is None:
            if status != "PASS":
                print(f"  {mark} [{cid}] {status}: {msg}")
    return stats


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else None
    evaluate(path)
