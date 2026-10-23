from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

DB_PATH = Path(os.getenv("INTIGNOAI_DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "intignoai.db")))

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'New chat',
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_interaction_id TEXT,
    recovery_json TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS activities (
    id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    type TEXT,
    tool TEXT,
    status TEXT,
    description TEXT,
    started_at REAL,
    duration REAL,
    input TEXT,
    output TEXT,
    error TEXT,
    seq INTEGER,
    FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);
CREATE INDEX IF NOT EXISTS idx_activities_chat ON activities(chat_id, seq);
"""


class Database:
    """Thread-safe SQLite access layer for chat history, messages and activity logs.

    A single connection is reused with `check_same_thread=False` and every access is
    serialized behind a lock, since the agent runs its own worker threads and the
    Flask dev server is multi-threaded.
    """

    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        with self._lock:
            self._conn.executescript(SCHEMA)
            # Lightweight in-place migration for existing v1.4 databases.
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(chats)").fetchall()}
            if "recovery_json" not in columns:
                self._conn.execute("ALTER TABLE chats ADD COLUMN recovery_json TEXT")
            self._conn.commit()

    # ---- chats -----------------------------------------------------------
    def create_chat(self, title: str = "New chat", chat_id: str | None = None) -> dict[str, Any]:
        chat_id = chat_id or uuid.uuid4().hex
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO chats (id, title, pinned, created_at, updated_at) VALUES (?, ?, 0, ?, ?)",
                (chat_id, title, now, now),
            )
            self._conn.commit()
        return self.get_chat(chat_id)

    def get_chat(self, chat_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item["recovery_state"] = json.loads(item.pop("recovery_json", None) or "null")
        except Exception:
            item["recovery_state"] = None
        return item

    def save_recovery_state(self, chat_id: str, state: dict[str, Any] | None) -> None:
        payload = json.dumps(state, ensure_ascii=False, default=str) if state else None
        with self._lock:
            self._conn.execute(
                "UPDATE chats SET recovery_json = ?, updated_at = ? WHERE id = ?",
                (payload, time.time(), chat_id),
            )
            self._conn.commit()

    def list_chats(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM chats ORDER BY pinned DESC, updated_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def rename_chat(self, chat_id: str, title: str) -> bool:
        title = (title or "").strip() or "Untitled chat"
        with self._lock:
            cur = self._conn.execute(
                "UPDATE chats SET title = ?, updated_at = ? WHERE id = ?", (title, time.time(), chat_id)
            )
            self._conn.commit()
        return cur.rowcount > 0

    def set_pinned(self, chat_id: str, pinned: bool) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE chats SET pinned = ?, updated_at = ? WHERE id = ?",
                (1 if pinned else 0, time.time(), chat_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def touch_chat(self, chat_id: str, last_interaction_id: str | None = None) -> None:
        with self._lock:
            if last_interaction_id is not None:
                self._conn.execute(
                    "UPDATE chats SET updated_at = ?, last_interaction_id = ? WHERE id = ?",
                    (time.time(), last_interaction_id, chat_id),
                )
            else:
                self._conn.execute(
                    "UPDATE chats SET updated_at = ? WHERE id = ?", (time.time(), chat_id)
                )
            self._conn.commit()

    def delete_chat(self, chat_id: str) -> bool:
        with self._lock:
            self._conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
            self._conn.execute("DELETE FROM activities WHERE chat_id = ?", (chat_id,))
            cur = self._conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
            self._conn.commit()
        return cur.rowcount > 0

    # ---- messages ----------------------------------------------------------
    def add_message(self, chat_id: str, role: str, content: str, created_at: float | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, role, content, float(created_at) if created_at else time.time()),
            )
            self._conn.commit()

    def list_messages(self, chat_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content, created_at FROM messages WHERE chat_id = ? ORDER BY id ASC",
                (chat_id,),
            ).fetchall()
        return [{"role": row["role"], "content": row["content"], "created_at": row["created_at"]} for row in rows]

    # ---- activities --------------------------------------------------------
    def upsert_activity(self, chat_id: str, activity: dict[str, Any], seq: int) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO activities (id, chat_id, type, tool, status, description, started_at, duration, input, output, error, seq)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    description=excluded.description,
                    duration=excluded.duration,
                    input=excluded.input,
                    output=excluded.output,
                    error=excluded.error
                """,
                (
                    activity["id"], chat_id, activity.get("type"), activity.get("tool"),
                    activity.get("status"), activity.get("description"), activity.get("started_at"),
                    activity.get("duration"),
                    json.dumps(activity.get("input"), ensure_ascii=False, default=str),
                    json.dumps(activity.get("output"), ensure_ascii=False, default=str),
                    activity.get("error"), seq,
                ),
            )
            self._conn.commit()

    def list_activities(self, chat_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM activities WHERE chat_id = ? ORDER BY seq ASC", (chat_id,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for key in ("input", "output"):
                try:
                    item[key] = json.loads(item[key]) if item[key] else {}
                except Exception:
                    item[key] = {}
            result.append(item)
        return result


DB = Database()
