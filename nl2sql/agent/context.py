"""上下文预算：把"给模型看的历史"压到可承受的大小。

为什么要单独处理
----------------
决策 Prompt 里塞的是**纯文本历史**（用户问题、工具调用、观察、推理）。
工具观察在进入历史时已经被 `max_observation_chars` 截了一次，但：

    * 一次运行有 8 步，每步都追加观察 + 推理，历史线性增长；
    * 跨轮（回合制澄清）时，`session.render_context()` 会把**已持久化的
      完整**工具输出再渲染一遍——上一轮的观察没截，本轮又叠加。

结果是"第 8 步的 Prompt 可能比第 1 步大一个数量级"，而其中真正决定下一步
的通常只是**最近几条**观察。旧观察的作用是"证明做过什么"，压成一句就够。

借鉴来源与取舍
--------------
`langchain-master` 的 `context_editing.py`（ClearToolUsesEdit：把旧工具结果
清成占位消息、保留最近 N 次）与 `summarization.py`（按 messages/tokens/fraction
阈值触发摘要，保留最近若干条）。

**没有引入摘要模型**：摘要是又一次模型调用，会引入新的非确定性，也会让
"两个执行器结果一致"更难验证。本项目的历史是结构化的，直接按预算裁剪旧观察
更便宜、更可复现，而且不改变最近几步的语义。

所有函数都是纯函数、无随机性：同样的输入永远得到同样的输出。
这样 loop 与 langgraph 两个执行器（以及录制—重放测试）看到的 Prompt 一致。
"""
from __future__ import annotations

__all__ = ["compress_prompt_history", "compress_session_context"]

# 观察在历史里的前缀（见 `machine.act`：`history.append(f"观察: {obs}")`）。
_OBS_PREFIX = "观察: "
# 会话分隔行不参与裁剪：它们是"历史/本轮"的结构标记，删了模型会读混。
_HEADER_PREFIX = "==="
# 压缩后仍超预算时，被舍弃内容的占位符
_OMIT_MARK = "…（较早的历史内容已省略）"


def _clip(text: str, cap: int) -> str:
    """截到 `cap` 字符，并**明确告诉模型这里被压缩了**。

    若只截断不给标记，模型会把"半句话"当成完整观察，据此推理——
    与 `ToolResult._compact` 里"前 3 行被误认为数据缺失"是同一类坑。
    """
    if cap <= 0 or len(text) <= cap:
        return text
    return text[:cap] + "…（已压缩）"


def compress_prompt_history(
    entries: list[str],
    *,
    max_chars: int = 14000,
    keep_observations: int = 3,
    old_observation_cap: int = 240,
    hard_cap: int = 80,
) -> list[str]:
    """按预算裁剪决策 Prompt 的历史文本列表。

    三档、由轻到重：

    1. 只保留最近 `keep_observations` 条观察的全文，更早的观察压到
       `old_observation_cap`（它们是"做过什么"的证据，不是当前决策依据）；
    2. 仍超预算：从最老的条目开始压到 `hard_cap`；
    3. 仍超预算：从最老的条目开始整条丢弃。

    第 2、3 档永远保住最后 4 条与 `===` 分隔行：最新观察/问题不能丢，
    结构标记丢了模型会分不清哪段是"之前的对话"。
    """
    out = list(entries)

    # ---- 1) 旧观察压缩
    obs_positions = [i for i, e in enumerate(out)
                     if isinstance(e, str) and e.startswith(_OBS_PREFIX)]
    keep = max(0, keep_observations)
    for i in obs_positions[:max(0, len(obs_positions) - keep)]:
        out[i] = _clip(out[i], old_observation_cap)

    total = _total_len(out)

    # ---- 2) 逐条压到最小长度（从最老的开始）
    if total > max_chars:
        tail_start = max(0, len(out) - 4)
        for i in range(tail_start):
            if total <= max_chars:
                break
            if out[i].startswith(_HEADER_PREFIX):
                continue
            before = len(out[i])
            out[i] = _clip(out[i], hard_cap)
            total -= before - len(out[i])

    # ---- 3) 整条丢弃（从最老的开始，不丢尾部 4 条与分隔行）
    if total > max_chars:
        tail_keep = min(4, len(out))
        i = 0
        while total > max_chars and i < len(out) - tail_keep:
            if out[i].startswith(_HEADER_PREFIX):
                i += 1
                continue
            total -= len(out[i])
            del out[i]
        if total > max_chars and out:
            out.insert(0, _OMIT_MARK)

    return out


def compress_session_context(
    text: str,
    *,
    max_chars: int = 4000,
    line_cap: int = 600,
) -> str:
    """压缩**跨轮**会话上下文（上一轮的工具输出可能很长）。

    先按行长截断（保留行首结论），再按总预算保留**最近的**行——最近一轮
    才决定"用户刚才在说什么"，更早的只当背景。
    """
    if not text:
        return ""
    lines = text.splitlines()
    lines = [_clip(line, line_cap) for line in lines]

    total = sum(len(line) for line in lines)
    if total <= max_chars or len(lines) <= 1:
        return "\n".join(lines)

    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        if used + len(line) > max_chars and kept:
            break
        kept.append(line)
        used += len(line) + 1
    kept.reverse()
    return "\n".join([_OMIT_MARK] + kept)


def _total_len(entries: list[str]) -> int:
    return sum(len(e) for e in entries)
