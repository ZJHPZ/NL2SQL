"""会话存储（SQLite 实现）。

为什么会话必须持久化
--------------------
回合制澄清要求"下一轮能读到上一轮"：agent 追问后立即返回，
用户回答时是**另一个 HTTP 请求**。若会话只在内存里，
进程重启（开发时很常见）就丢失上下文，用户会看到"我刚回答完它又忘了"。

表结构说明
----------
`turns` 以 JSON 存储而不是拆成行：
    * 轮次整体读写，不需要按轮次查询
    * 工具调用参数是嵌套结构，拆表会让读写都变复杂
    * 个人场景下单个会话的轮次规模（几十条）完全适合 JSON
"""
from __future__ import annotations

import json
from datetime import datetime

from ..contracts.session import SessionState, Turn
from ._dbapi import SqliteKV
from .migrations import open_connection

__all__ = ["SqliteSessionStore"]


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


class SqliteSessionStore:
    """实现 `SessionStore` 契约。"""

    def __init__(self, db_path: str, *, max_turns: int = 200) -> None:
        self._conn: SqliteKV = open_connection(db_path)
        self._max_turns = max_turns

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------ 读写
    def save(self, state: SessionState) -> None:
        state.updated_at = state.updated_at or _now_iso()
        # 会话可能很长，截断保留最近的轮次，避免单行无限增长
        if len(state.turns) > self._max_turns:
            state.turns = state.turns[-self._max_turns:]
        self._conn.execute(
            "INSERT INTO agent_session (session_id, source_id, turns, "
            "pending_question, pending_options, now_anchor, last_status, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "turns=excluded.turns, pending_question=excluded.pending_question, "
            "pending_options=excluded.pending_options, "
            "now_anchor=excluded.now_anchor, last_status=excluded.last_status, "
            "updated_at=excluded.updated_at",
            (
                state.session_id,
                state.source_id,
                json.dumps([_turn_to_dict(t) for t in state.turns],
                           ensure_ascii=False),
                state.pending_question,
                json.dumps(state.pending_options, ensure_ascii=False),
                state.now,
                state.last_status,
                state.created_at or _now_iso(),
                state.updated_at,
            ),
        )
        self._conn.commit()

    def load(self, session_id: str) -> SessionState | None:
        row = self._conn.execute(
            "SELECT * FROM agent_session WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        keys = row.keys()
        return SessionState(
            session_id=row["session_id"],
            source_id=row["source_id"] or "",
            turns=[_dict_to_turn(d) for d in json.loads(row["turns"] or "[]")],
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
            pending_question=row["pending_question"] or "",
            pending_options=json.loads(row["pending_options"] or "[]"),
            now=row["now_anchor"] if "now_anchor" in keys else None,
            last_status=row["last_status"] or "",
        )

    def delete(self, session_id: str) -> None:
        self._conn.execute(
            "DELETE FROM agent_session WHERE session_id = ?", (session_id,))
        self._conn.commit()

    def list_recent(self, *, limit: int = 20) -> list[SessionState]:
        rows = self._conn.execute(
            "SELECT session_id FROM agent_session "
            "ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        out: list[SessionState] = []
        for r in rows:
            s = self.load(r["session_id"])
            if s is not None:
                out.append(s)
        return out


def _turn_to_dict(t: Turn) -> dict:
    return {
        "role": t.role, "content": t.content, "tool_name": t.tool_name,
        "tool_args": t.tool_args, "ok": t.ok, "created_at": t.created_at,
    }


def _dict_to_turn(d: dict) -> Turn:
    return Turn(
        role=d.get("role", "user"),
        content=d.get("content", ""),
        tool_name=d.get("tool_name", ""),
        tool_args=d.get("tool_args") or {},
        ok=d.get("ok"),
        created_at=d.get("created_at", ""),
    )
