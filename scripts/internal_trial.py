"""内部试用脚本：像真实用户那样问一轮，并打印完整细节供人工核对。

与验收测试的区别：
    验收测试断言"某个布尔条件成立"；
    这个脚本只**打印事实**——问题、状态、步数、工具轨迹、SQL、结果、回答原文。
    质量判断由人来看，因为很多问题是"读起来别扭"，不是断言能覆盖的。

用法:
    python scripts/internal_trial.py            # 跑默认问题集
    python scripts/internal_trial.py "自定义问题"
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = os.environ.get("TRIAL_BASE", "http://127.0.0.1:8000")
NOW = "2024-06-30"

# 刻意覆盖不同"使用者会问的样子"：
#   精确问题 / 口语问题 / 分析型 / 缺信息 / 压根不该答的 / 表格 vs 单一数值
QUESTIONS = [
    # ---- 精确问法（语义层已覆盖）
    "2024年6月，数码品类的实收金额是多少？",
    # ---- 口语、信息不全（期待追问）
    "最近卖得怎么样？",
    # ---- 分析型：排名 + 多指标
    "哪个渠道表现最好？",
    # ---- 需要跨表
    "消费最高的前5个客户是谁？他们分别在哪个地区？",
    # ---- 口语时间词 + 依赖会话锚点
    "上个月各品类的销量对比一下",
    # ---- 开放探索型
    "这个数据库里有什么数据？",
    # ---- 不该答的：预测
    "预测下个季度的销售额",
    # ---- 边界：不存在的维度
    "各城市的销售额是多少？",
    # ---- 复杂：比率 + 分组
    "各地区的退款率怎么样？",
    # ---- 极短、几乎无信息
    "卖了多少？",
]


def call(path: str, payload=None, method: str = "POST", timeout: float = 180.0):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        return e.code, {"raw": body[:400]}
    except Exception as e:  # noqa: BLE001
        return 0, {"raw": f"{type(e).__name__}: {e}"}


def show(q: str, d: dict, elapsed: float, session: str | None) -> str | None:
    status = d.get("status")
    print("\n" + "━" * 78)
    print(f"Q  {q}")
    print("─" * 78)
    if "raw" in d:
        print(f"  !! 请求失败: {d['raw']}")
        return session
    print(f"  status={status}  planner={d.get('planner_mode')}  "
          f"{int(elapsed)}ms  行数="
          f"{(d.get('result') or {}).get('row_count')}")
    print(f"  会话={d.get('session_id')}")
    if d.get("notes"):
        print(f"  notes={d['notes']}")
    if d.get("suggestions"):
        print(f"  建议={d['suggestions']}")
    if d.get("candidates"):
        print(f"  候选/选项={d['candidates']}")
    if status == "need_clarification":
        print(f"  ❓ 追问: {d.get('message')}")
    if d.get("sql"):
        sql = d["sql"].strip().splitlines()
        print(f"  SQL({len(sql)}行):")
        for line in sql[:6]:
            print(f"      {line}")
        if len(sql) > 6:
            print(f"      … 共 {len(sql)} 行")
    if d.get("result"):
        res = d["result"]
        print(f"  结果列={res['columns']}")
        for row in res["rows"][:5]:
            print(f"      {row}")
        if res["row_count"] > 5:
            print(f"      … 共 {res['row_count']} 行")
    if d.get("answer"):
        ans = str(d["answer"]).replace("\n", "\n      ")
        print(f"  回答:\n      {ans}")
    elif d.get("message"):
        print(f"  message: {d.get('message')}")
    return d.get("session_id") or session


def main() -> int:
    qs = sys.argv[1:] or QUESTIONS
    _, info = call("/api/info", method="GET")
    print("=" * 78)
    print("内部试用")
    print("=" * 78)
    print(f"  planner={info.get('planner_mode')}  "
          f"模型={info.get('llm_model')}")
    print(f"  问题数={len(qs)}")

    # 试用期间清空结果缓存：**否则改完 Prompt/工具后重跑同一问题会命中旧结果**，
    # 让人误以为改动无效（第一次试用就差点被这个骗到）。
    call("/api/cache/refresh", {}, method="POST")
    print("  已清空结果缓存（确保每题的耗时与内容都是本次真实产出）")

    session: str | None = None
    rows: list[tuple[str, str, int, int]] = []
    for q in qs:
        t0 = time.perf_counter()
        _, d = call("/api/query", {"question": q, "now": NOW,
                                   "session_id": session})
        elapsed = time.perf_counter() - t0
        show(q, d, elapsed, session)
        # 只有澄清才沿用会话；其它问题开新会话，模拟"不同话题"
        if d.get("status") == "need_clarification":
            session = d.get("session_id") or session
        else:
            session = None
        rows.append((q, d.get("status", "?"),
                     (d.get("result") or {}).get("row_count", -1),
                     int(elapsed * 1000)))

    print("\n" + "=" * 78)
    print("汇总")
    print("=" * 78)
    for q, s, n, ms in rows:
        print(f"  {s:20s} {n:>5} 行 {ms:>6}ms  {q[:44]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
