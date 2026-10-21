"""模型连通性自检。

用法（Key 通过环境变量传入，**不要写在命令行或文件里**）:

    PowerShell:
        $env:NL2SQL_LLM_API_KEY='sk-xxx'
        python scripts/check_llm.py

    bash:
        NL2SQL_LLM_API_KEY=sk-xxx python scripts/check_llm.py

可选环境变量：
    NL2SQL_LLM_BASE_URL   默认 https://api.deepseek.com/v1
    NL2SQL_LLM_MODEL      默认 deepseek-chat
"""
from __future__ import annotations

import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from nl2sql.llm import OpenAICompatProvider
from nl2sql.contracts.llm import ChatMessage

KEY = os.environ.get("NL2SQL_LLM_API_KEY", "")
BASE = os.environ.get("NL2SQL_LLM_BASE_URL", "https://api.deepseek.com/v1")
MODEL = os.environ.get("NL2SQL_LLM_MODEL", "deepseek-chat")


def main() -> int:
    print(f"key      : {'已设置(%d 字符)' % len(KEY) if KEY else '未设置'}")
    print(f"base_url : {BASE}")
    print(f"model    : {MODEL}")
    if not KEY:
        print("\n未设置 NL2SQL_LLM_API_KEY，跳过连通性测试。")
        return 1

    p = OpenAICompatProvider(api_key=KEY, base_url=BASE, model=MODEL)
    print(f"available: {p.available()}")

    # 1) 极简对话
    try:
        r = p.chat([ChatMessage(role="user", content="只回复两个字：可用")],
                   max_tokens=10)
        print(f"\n[1] 极简对话 OK  reply={r.text!r}")
        print(f"    model={r.model} tokens={r.prompt_tokens}+{r.completion_tokens}"
              f" latency={r.latency_ms}ms")
    except Exception as e:  # noqa: BLE001
        print(f"\n[1] 极简对话 FAIL  {type(e).__name__}: {str(e)[:400]}")
        return 1

    # 2) JSON 模式（规划器实际依赖的能力）
    try:
        r2 = p.chat([
            ChatMessage(role="system", content="只输出 JSON，不要解释。"),
            ChatMessage(role="user",
                        content='输出 {"ok": true, "n": 3} 这样的 JSON'),
        ], json_mode=True, max_tokens=60)
        obj = OpenAICompatProvider.parse_json(r2.text)
        print(f"[2] JSON 模式 OK    parsed={obj}")
    except Exception as e:  # noqa: BLE001
        print(f"[2] JSON 模式 FAIL  {type(e).__name__}: {str(e)[:300]}")
        return 1

    # 3) 网络往返延迟与成本估算
    print(f"[3] 往返延迟参考    {r2.latency_ms}ms（单次规划约 1 次调用）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
