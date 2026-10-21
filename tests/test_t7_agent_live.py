"""T7 验收：真实模型下的 agent 行为。

前面 T1–T6 用脚本化模型验证了**机制**（循环、回灌、澄清、会话）。
这一份验证**真模型在循环里会不会真的用工具**——机制对但模型不用，
等于没升级。

三个要观察的行为：
    1. 正常问题：能在步数内给出正确答案（不能因为多步而变差）
    2. 缺信息问题：是否会主动追问，而不是猜一个时间范围
    3. 是否留下工具调用轨迹（可追溯"它为什么这么答"）

运行:
    $env:NL2SQL_LLM_API_KEY='sk-xxx'
    python tests/test_t7_agent_live.py
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN_DB = os.path.join(ROOT, "golden_set", "ecommerce.db")
BASELINE = os.path.join(ROOT, "golden_set", "baseline.json")
NOW = "2024-06-30"

from nl2sql import NL2SQL, NL2SQLConfig  # noqa: E402
from nl2sql.llm import OpenAICompatProvider  # noqa: E402
from nl2sql.semantics.golden_model import build_model  # noqa: E402

_failures: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        _failures.append(f"{label} {detail}")


def _canon(v):
    if v is None:
        return ("null",)
    s = str(v).strip()
    try:
        f = float(s)
    except (TypeError, ValueError):
        return ("str", s)
    return ("num", float(int(f)) if f == int(f) and abs(f) < 1e15 else round(f, 4))


def rows_match(expected, got, tol: float = 1e-6) -> bool:
    def sig(rows):
        out = []
        for r in rows:
            cells = [_canon(v) for v in r]
            out.append((
                tuple(sorted(c[1] for c in cells if c[0] == "num")),
                tuple(sorted(c[1] for c in cells if c[0] == "str")),
            ))
        return sorted(out)

    a, b = sig(expected), sig(got)
    if len(a) != len(b):
        return False
    for (na, sa), (nb, sb) in zip(a, b):
        if sa != sb or len(na) != len(nb):
            return False
        if any(abs(x - y) > tol for x, y in zip(na, nb)):
            return False
    return True


def _norm_num(s: str) -> str:
    """归一化数字文本：去掉千分位与空白，便于在散文答案里核对数值。

    agent 的最终回答是给人看的散文（可能写 "99,998.5"），
    与基线里的 "99998.5" 形式不同但语义相同。
    """
    return s.replace(",", "").replace("，", "").replace(" ", "")


def answer_contains(answer: str, expected_rows: list[list[str]]) -> tuple[bool, list[str]]:
    """检查答案文本里是否出现了基线中的全部数值。

    这是"agent 真的查到了数据"的证据——编造的答案不会命中这些具体数字。
    """
    text = _norm_num(answer or "")
    missing: list[str] = []
    for row in expected_rows:
        for cell in row:
            n = _norm_num(str(cell))
            # 只核对较长的数字，避免 "5" 这种出现在任何文本里造成假阳性
            if len(n) >= 4 and n not in text:
                missing.append(str(cell))
    return (not missing), missing


def main() -> int:
    key = os.environ.get("NL2SQL_LLM_API_KEY", "")
    if not key:
        print("未设置 NL2SQL_LLM_API_KEY，无法运行真实模型测试。")
        return 1

    base = os.environ.get("NL2SQL_TEST_TMP") or os.path.join(ROOT, "data", "_test_t7")
    work = os.path.join(base, "run")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    db = os.path.join(work, "shop.db")
    shutil.copy2(GOLDEN_DB, db)

    with open(BASELINE, encoding="utf-8") as f:
        cases = {c["id"]: c for c in json.load(f)["cases"]}

    provider = OpenAICompatProvider(
        api_key=key,
        base_url=os.environ.get("NL2SQL_LLM_BASE_URL",
                                "https://api.deepseek.com/v1"),
        model=os.environ.get("NL2SQL_LLM_MODEL", "deepseek-chat"),
    )

    cfg = NL2SQLConfig(state_dir=os.path.join(work, "state"))
    cfg.add_sqlite(db, source_id="shop")
    cfg.agent.planner = "agent"
    cfg.agent.max_steps = 6

    print("=" * 74)
    print("agent 模式端到端（真实模型）")
    print("=" * 74)

    with NL2SQL(cfg, semantic_model=build_model(order_level=True),
                llm_provider=provider) as engine:
        engine.refresh_snapshot()
        print(f"planner_mode : {engine.planner_mode}")
        print(f"工具数       : {len(engine.tools.names())}")

        # ---------------------------------------------------------- 1) 正常问题
        print("\n[1] 清晰问题：必须真的查到数据（不能只是说明口径）")
        for qid in ["A1", "B1", "C2"]:
            c = cases[qid]
            sid = engine.new_session_id()
            t0 = time.perf_counter()
            out = engine.ask(c["question"], now=NOW, session_id=sid)
            elapsed = int((time.perf_counter() - t0) * 1000)
            session = engine.storage.sessions.load(sid)
            tool_turns = [t for t in (session.turns if session else [])
                          if t.role == "tool"]
            ok_vals, missing = answer_contains(out.message or "", c["rows"])
            check(out.status == "ok", f"{qid} 正常结束",
                  f"{out.status} {(out.message or '')[:60]}")
            check(len(tool_turns) >= 1, f"{qid} 执行过工具", str(len(tool_turns)))
            check(ok_vals, f"{qid} ★ 答案含基线中的真实数值",
                  f"缺失 {missing[:4]}")
            # ★ 结构化结果：界面要靠它渲染可排序、可导出的结果表。
            # 只有散文是不够的——散文里有数字但没有结构。
            check(out.result is not None, f"{qid} ★ 返回结构化结果表",
                  "result=None")
            if out.result is not None:
                # 列名刻意**不做精确比对**：那是模型生成的别名，
                # 措辞不同（"销售渠道" vs "渠道"）不影响正确性——
                # 同样的规则也写在 golden_set/README.md 的判分说明里。
                # 这里只要求列数对得上，值由 rows_match 校验。
                check(len(out.result.columns) == len(c["columns"]),
                      f"{qid} 结果表列数一致",
                      f"{out.result.columns} vs {c['columns']}")
                check(rows_match(c["rows"], out.result.rows),
                      f"{qid} ★ 结果表内容与基线一致",
                      f"{out.result.row_count} 行")
                check(bool(out.sql), f"{qid} 结果表附带 SQL")
            print(f"        {elapsed}ms  steps={_count_steps(out.trace)}  "
                  f"tools={[t.tool_name for t in tool_turns]}")
            if out.result is not None:
                print(f"        表: {out.result.columns} × {out.result.row_count} 行")
            print(f"        答: {(out.message or '')[:100]}")

        # ---------------------------------------------------------- 2) 澄清
        print("\n[2] 缺信息问题：应主动追问而不是猜时间范围")
        sid = engine.new_session_id()
        out = engine.ask("销售额是多少？", now=NOW, session_id=sid)
        print(f"\n        Q: 销售额是多少？")
        print(f"        status={out.status}")
        print(f"        message={(out.message or '')[:100]}")
        if out.candidates:
            print(f"        选项={out.candidates[:4]}")
        check(out.status == "need_clarification",
              "★ 时间范围缺失时主动追问（而不是猜一个）", out.status)
        if out.status == "need_clarification":
            out2 = engine.ask("本月", now=None, session_id=sid)
            print(f"        → 回答「本月」后: status={out2.status}")
            print(f"          答: {(out2.message or '')[:110]}")
            check(out2.status in ("ok", "empty"),
                  "第二回合接上上下文并完成", out2.status)
            # 「本月」锚点保留在会话里，第二回合应查到 6 月数据
            ok_vals, missing = answer_contains(
                out2.message or "", cases["C4"]["rows"][0:1])
            print(f"          含 6 月数值: {ok_vals} {missing[:2]}")
            session = engine.storage.sessions.load(sid)
            check(session is not None and session.now == NOW,
                  "★ 会话保持了时间锚点（第二回合没漂移到真实今天）",
                  str(session.now if session else None))

        # ---------------------------------------------------------- 3) 轨迹
        print("\n[3] 工具调用轨迹（可追溯：它为什么这么答）")
        sid = engine.new_session_id()
        engine.ask("2024年6月，数码品类的实收金额是多少？", now=NOW, session_id=sid)
        session = engine.storage.sessions.load(sid)
        tool_turns = [t for t in (session.turns if session else [])
                      if t.role == "tool"]
        print(f"        会话轮次数: {len(session.turns) if session else 0}")
        for t in tool_turns:
            args = json.dumps(t.tool_args, ensure_ascii=False)[:58]
            print(f"          - {t.tool_name}({args}) ok={t.ok}")
        check(session is not None, "会话已持久化")
        check(len(tool_turns) >= 1, "★ 轨迹记录了工具调用（可追溯依据）",
              str(len(tool_turns)))

        # ---------------------------------------------------------- 4) 探索行为
        print("\n[4] 开放问题：是否会先探索再答")
        sid = engine.new_session_id()
        out = engine.ask("这个库里有哪几张表？分别是什么用途？", now=NOW,
                         session_id=sid)
        session = engine.storage.sessions.load(sid)
        used = [t.tool_name for t in (session.turns if session else [])
                if t.role == "tool"]
        print(f"        status={out.status}  调用工具={used}")
        print(f"        答: {(out.message or '')[:150]}")
        check(len(used) >= 1, "★ 主动调用工具探索（而非凭空回答）", str(used))

    shutil.rmtree(work, ignore_errors=True)
    print("\n" + "=" * 74)
    if _failures:
        print(f"T7 验收未通过：{len(_failures)} 项失败")
        for x in _failures[:20]:
            print("  -", x)
        return 1
    print("T7 验收全部通过")
    return 0


def _count_steps(trace: dict) -> int:
    return sum(1 for k in trace if k.startswith("decide_"))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
