"""PAVP 会话持久化 - SQLite 存储，支持中断恢复

数据库位置: ~/.pavp/sessions.db
每完成一个阶段自动保存 SessionState。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import SessionState

DB_PATH = Path.home() / ".pavp" / "sessions.db"

_LOCK = threading.Lock()
_initialized = False


def _conn() -> sqlite3.Connection:
    global _initialized
    c = sqlite3.connect(str(DB_PATH))
    c.execute("PRAGMA journal_mode=WAL")
    c.row_factory = sqlite3.Row
    if not _initialized:
        c.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id  TEXT PRIMARY KEY,
                project_root TEXT NOT NULL DEFAULT '',
                state_json  TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_updated ON sessions(updated_at DESC)")
        c.commit()
        _initialized = True
    return c


def save(session_id: str, project_root: str, state: SessionState) -> None:
    """保存（插入或更新）会话状态到 SQLite"""
    now = datetime.now(timezone.utc).isoformat()
    state_json = json.dumps(state.model_dump(mode="json"), ensure_ascii=False)
    with _LOCK:
        c = _conn()
        c.execute(
            """INSERT INTO sessions (session_id, project_root, state_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
               state_json = excluded.state_json,
               project_root = excluded.project_root,
               updated_at = excluded.updated_at""",
            (session_id, project_root, state_json, now, now),
        )
        c.commit()


def load(session_id: str) -> Optional[SessionState]:
    """读取会话状态。不存在返回 None"""
    with _LOCK:
        c = _conn()
        row = c.execute(
            "SELECT state_json FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    if not row:
        return None
    return SessionState.model_validate(json.loads(row[0]))


def list_all() -> list[dict]:
    """列出所有会话（按更新时间倒序）"""
    c = _conn()
    rows = c.execute(
        "SELECT session_id, project_root, created_at, updated_at, state_json "
        "FROM sessions ORDER BY updated_at DESC"
    ).fetchall()
    result = []
    for r in rows:
        try:
            s = json.loads(r["state_json"])
            requirement = s.get("original_requirement", "")[:80]
            fsm = s.get("fsm_state", "?")
        except (json.JSONDecodeError, KeyError):
            requirement = "(损坏)"
            fsm = "?"
        result.append({
            "session_id": r["session_id"],
            "project_root": r["project_root"],
            "requirement": requirement,
            "fsm_state": fsm,
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        })
    return result


def load_project_root(session_id: str) -> Optional[str]:
    """仅读取会话的 project_root"""
    c = _conn()
    row = c.execute(
        "SELECT project_root FROM sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return row["project_root"] if row else None


def delete(session_id: str) -> None:
    """删除会话"""
    with _LOCK:
        c = _conn()
        c.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        c.commit()


def auto_save_factory(session_id: str, project_root: str):
    """返回一个 on_state_change 回调闭包，在每次状态变更时自动 save"""
    def _save(state: SessionState) -> None:
        save(session_id, project_root, state)
    return _save
