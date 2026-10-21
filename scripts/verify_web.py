"""对运行中的 Web 服务做一次端到端验收（走真实 HTTP）。

用法: python scripts/verify_web.py [base_url]
默认 http://127.0.0.1:8000
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def call(path: str, payload=None, method: str = "POST", timeout: float = 120.0):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body[:200]}


def section(title: str) -> None:
    print("\n" + "=" * 74)
    print(title)


def main() -> int:
    ok = True

    section("1) 启动自检 /api/info")
    _, info = call("/api/info", method="GET")
    print(f"  版本        : {info.get('version')}")
    print(f"  规划器模式  : {info.get('planner_mode')}")
    print(f"  模型        : {info.get('llm_model') or '（未接入）'}")
    print(f"  数据源      : {', '.join(info.get('sources') or [])}")
    print(f"  表数/知识条 : {info.get('snapshot', {}).get('table_count')} / "
          f"{info.get('knowledge', {}).get('total')}")
    safety = info.get("safety", {})
    print(f"  安全边界    : 只读={safety.get('read_only')} "
          f"上限={safety.get('max_rows')} 超时={safety.get('timeout_s')}s")

    section("2) 真实问答（走配置的规划器）")
    for q in [
        "2024年6月，数码品类的实收金额是多少？",
        "2024年上半年，各渠道的实收金额和有效订单数是多少？按实收金额降序排列。",
        "2024年上半年，各地区的退款率是多少？",
    ]:
        _, d = call("/api/query", {"question": q, "now": "2024-06-30"})
        print(f"\n  Q: {q}")
        print(f"    status={d['status']}  planner={d['planner_mode']}  "
              f"{d['elapsed_ms']}ms  cached={d['cached']}")
        if d.get("result"):
            print(f"    列: {d['result']['columns']}")
            for r in d["result"]["rows"][:4]:
                print(f"      {r}")
        if d.get("notes"):
            print(f"    notes: {d['notes']}")

    section("3) 模糊问题的处理（不应编造答非所问的 SQL）")
    # 注意：这一题的**正确行为不唯一**。
    #   * 若模型把它解析成合理的聚合（如"按月实收金额"），那是好行为；
    #   * 若解析不了，应返回 unresolved 且 sql=None。
    # 因此这里只做展示，不判失败。真正要断言的是"不该返回 SQL 的场合"，
    # 见下面第 3b 节。
    for q in ["帮我看看最近怎么样", "嗯"]:
        _, d = call("/api/query", {"question": q, "now": "2024-06-30"})
        print(f"\n  Q: {q}")
        print(f"    status={d['status']}  planner={d['planner_mode']}")
        if d["sql"]:
            print(f"    生成了 SQL（首行）: {d['sql'].splitlines()[0][:70]}")
        else:
            print(f"    sql=None（未编造查询）")
            print(f"    suggestions={d.get('suggestions')}")

    section("3b) 无法回答时必须不给 SQL")
    # 用**空问题**触发校验失败，以及一个语义层确定覆盖不到的问题
    st, d = call("/api/query", {"question": "   ", "now": "2024-06-30"})
    print(f"  空白问题 -> HTTP {st}（应为 422，被请求校验拦下）")
    if st != 422:
        ok = False
        print("  !! 空白问题未被拦下")

    _, d = call("/api/query",
                {"question": "预测下个季度的销售趋势并给出建议", "now": "2024-06-30"})
    print(f"  预测类问题 -> status={d['status']}  sql={'有' if d['sql'] else 'None'}")
    if d["status"] == "unresolved" and d["sql"] is not None:
        ok = False
        print("  !! unresolved 时不应返回 SQL")
    print(f"    message={d.get('message')}")
    print(f"    suggestions={d.get('suggestions')}")

    section("4) 手写 SQL 的安全边界（关键）")
    for sql, label, expect in [
        ("DROP TABLE fact_order", "DROP", "blocked"),
        ("DELETE FROM fact_order", "DELETE", "blocked"),
        ("UPDATE fact_order SET amount = 0", "UPDATE", "blocked"),
        ("SELECT 1; DROP TABLE fact_order", "多语句注入", "blocked"),
        ("PRAGMA user_version = 99", "写 PRAGMA", "blocked"),
        ("/* DROP TABLE x */ SELECT 1 AS ok", "注释含 DROP", None),
        ("SELECT 'DROP TABLE x' AS note", "字符串含 DROP", None),
        ("SELECT order_status, COUNT(*) AS n FROM fact_order GROUP BY order_status",
         "正常查询", None),
    ]:
        _, d = call("/api/execute", {"sql": sql})
        got = d["status"]
        mark = "OK " if (expect is None or got == expect) else "BAD"
        if expect is not None and got != expect:
            ok = False
        rows = d["result"]["row_count"] if d.get("result") else None
        print(f"  {mark} {label:12s} status={got:9s} rows={rows} "
              f"code={d.get('error_code')}")

    section("5) 强制上限与截断标记")
    _, d = call("/api/execute", {
        "sql": "SELECT a.order_id AS x, b.order_id AS y"
               " FROM fact_order a CROSS JOIN fact_order b"})
    print(f"  超出上限查询: status={d['status']} "
          f"rows={d['result']['row_count'] if d.get('result') else None} "
          f"truncated={d['result']['truncated'] if d.get('result') else None}")
    if d["status"] != "truncated":
        ok = False
        print("  !! 应被判为截断")
    _, d2 = call("/api/execute", {"sql": "SELECT * FROM fact_order"})
    print(f"  810 行查询  : status={d2['status']} "
          f"rows={d2['result']['row_count'] if d2.get('result') else None}")

    section("6) 空结果回显生效的过滤条件")
    _, d = call("/api/query",
                {"question": "2024年1月，华南地区的图书实收金额是多少？",
                 "now": "2024-06-30"})
    print(f"  status={d['status']}")
    print(f"  applied_filters={d.get('applied_filters')}")
    print(f"  suggestions={d.get('suggestions')}")

    section("7) 知识库：测试检索（命中来源）")
    # 用表结构类问题：自动生成的 schema 条目正文就是表/字段清单
    _, hits = call("/api/knowledge/test",
                   {"text": "fact_order 有哪些字段", "source_id": "shop", "top_k": 5})
    for h in hits:
        print(f"  [{h['type']:9s}] {h['title'][:18]:20s} why={h['why']}")
    print("  -- 问一个知识库里没有的内容，验证它不会乱召回 --")
    _, hits2 = call("/api/knowledge/test",
                    {"text": "新客首单转化率的口径", "source_id": "shop", "top_k": 5})
    if not hits2:
        print("  （无命中——正确：库里确实没有该口径）")
    else:
        for h in hits2:
            print(f"  [{h['type']:9s}] {h['title'][:18]:20s} why={h['why']}")

    section("8) 历史与缓存")
    _, hist = call("/api/history?limit=6", method="GET")
    for h in hist[:6]:
        print(f"  {h['status']:9s} {h['row_count']:>5} 行 {h['elapsed_ms']:>6}ms  "
              f"{h['question'][:40]}")
    _, cache = call("/api/cache", method="GET")
    print(f"\n  缓存统计: {cache}")

    section("9) 资源与 SPA 回退")
    for path, want in [("/", "html"), ("/assets/", None)]:
        try:
            with urllib.request.urlopen(BASE + path, timeout=10) as r:
                print(f"  {path:12s} -> {r.status} {r.headers.get('content-type')}")
        except urllib.error.HTTPError as e:
            print(f"  {path:12s} -> {e.code}")
    st, body = call("/api/typo/here", method="GET")
    print(f"  /api/typo/here -> {st} {str(body)[:60]}")

    print("\n" + "=" * 74)
    print("验收结果:", "全部通过" if ok else "存在失败项（见上方 !! 标记）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
