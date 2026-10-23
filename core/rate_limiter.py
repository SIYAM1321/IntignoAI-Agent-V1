from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
import threading
import time
from pathlib import Path


HARD_REQUESTS_PER_MINUTE = 10


class RateLimitError(RuntimeError):
    def __init__(self, retry_after: float, *, source: str = "local", message: str | None = None):
        self.retry_after = max(0.0, float(retry_after))
        self.source = source
        super().__init__(message or f"Rate limit reached. Retry after {self.retry_after:.1f}s.")


class SlidingWindowRateLimiter:
    """SQLite-backed rolling-window limiter shared across threads and process restarts.

    This protects requests sent by this IntignoAI installation. It cannot account for
    other applications that independently use the same Google project/API key.
    """

    def __init__(self, limit: int = 10, window_seconds: float = 60.0, db_path: str | Path | None = None):
        self.limit = min(HARD_REQUESTS_PER_MINUTE, max(1, int(limit)))
        self.window_seconds = max(1.0, float(window_seconds))
        if db_path is None:
            db_path = os.getenv("INTIGNOAI_RATE_LIMIT_DB", str(Path(__file__).resolve().parent.parent / "data" / "rate_limit.sqlite3"))
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=15.0, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout=15000")
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS request_hits (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_request_hits_at ON request_hits(at)")
            conn.execute("CREATE TABLE IF NOT EXISTS limiter_state (key TEXT PRIMARY KEY, value REAL NOT NULL)")

    def configure(self, limit: int) -> int:
        """Set a value from 1 to 10; hard cap is enforced regardless of configuration."""
        self.limit = min(HARD_REQUESTS_PER_MINUTE, max(1, int(limit)))
        return self.limit

    def acquire(self) -> None:
        now = time.time()
        cutoff = now - self.window_seconds
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM request_hits WHERE at <= ?", (cutoff,))
            row = conn.execute("SELECT value FROM limiter_state WHERE key='blocked_until'").fetchone()
            blocked_until = float(row[0]) if row else 0.0
            if blocked_until > now:
                conn.execute("COMMIT")
                raise RateLimitError(blocked_until - now, source="provider", message=f"Gemini asked this app to pause for {blocked_until - now:.1f}s.")
            hits = conn.execute("SELECT at FROM request_hits ORDER BY at ASC").fetchall()
            if len(hits) >= self.limit:
                retry = max(0.0, self.window_seconds - (now - float(hits[0][0])))
                conn.execute("COMMIT")
                raise RateLimitError(retry, source="local")
            conn.execute("INSERT INTO request_hits(at) VALUES (?)", (now,))
            conn.execute("COMMIT")

    def penalize(self, retry_after: float) -> None:
        now = time.time()
        until = now + max(1.0, min(float(retry_after), 300.0))
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO limiter_state(key,value) VALUES('blocked_until',?) "
                "ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value)",
                (until,),
            )
            conn.execute("COMMIT")

    def clear_penalty(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM limiter_state WHERE key='blocked_until'")

    def snapshot(self) -> dict:
        now = time.time()
        cutoff = now - self.window_seconds
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM request_hits WHERE at <= ?", (cutoff,))
            hits = conn.execute("SELECT at FROM request_hits ORDER BY at ASC").fetchall()
            row = conn.execute("SELECT value FROM limiter_state WHERE key='blocked_until'").fetchone()
        blocked_until = float(row[0]) if row else 0.0
        used = len(hits)
        reset_after = 0.0 if not hits else max(0.0, self.window_seconds - (now - float(hits[0][0])))
        return {
            "limit": self.limit,
            "used": used,
            "remaining": max(0, self.limit - used),
            "window_seconds": self.window_seconds,
            "reset_after": round(max(reset_after, blocked_until - now, 0.0), 1),
            "blocked": blocked_until > now,
            "scope": "this IntignoAI installation; shared SQLite state",
            "hard_cap": HARD_REQUESTS_PER_MINUTE,
        }
