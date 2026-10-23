from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from core.activity import ActivityLog
from core.db import DB


@dataclass
class PendingToolCall:
    tool_name: str
    canonical_name: str
    arguments: dict[str, Any]
    call_id: str
    activity_id: str


@dataclass
class PendingConfirmation:
    interaction_id: str
    calls: list[PendingToolCall]
    results: dict[str, Any]
    awaiting_index: int = 0
    all_calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def current(self) -> PendingToolCall | None:
        # v1.13.4: return None instead of raising IndexError when the pending list is
        # exhausted or stale, so request paths can handle it with a friendly message.
        if 0 <= self.awaiting_index < len(self.calls):
            return self.calls[self.awaiting_index]
        return None


@dataclass
class Session:
    """An in-memory, live runtime handle for one chat.

    The chat's durable identity (title, pin state, messages, activity log) lives in
    SQLite (see core.db). `Session.id` is always the same as the chat's row id, so a
    session can be safely dropped from memory and rehydrated later without losing
    history. If constructed directly (bypassing SessionStore, e.g. in tests or
    scripts) a fresh chat row is created automatically so persistence calls have
    something to attach to.
    """

    id: str = field(default_factory=lambda: DB.create_chat()["id"])
    last_interaction_id: str | None = None
    active_model: str = ""
    active_thinking: bool = True
    active_reasoning_effort: str = "high"
    _last_model: str = ""
    _nim_histories: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    pending: PendingConfirmation | None = None
    activity: ActivityLog = field(default_factory=ActivityLog)
    messages: list[dict[str, str]] = field(default_factory=list)
    live_response: str = ""
    busy: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)
    _activity_seq: int = 0
    cancel_event: threading.Event = field(default_factory=threading.Event)
    recovery_state: dict[str, Any] | None = None
    _last_checkpoint_at: float = 0.0
    _last_activity_persist_at: float = 0.0
    active_response: Any = None
    queued_requests: list[dict[str, Any]] = field(default_factory=list)
    delete_requested: bool = False
    # v1.12: start time of the text segment currently streaming, and how many
    # intermediate segments were already flushed into the chat during this turn.
    _segment_started_at: float = 0.0
    _turn_flushed: int = 0

    def persist_message(self, role: str, content: str, created_at: float | None = None) -> None:
        DB.add_message(self.id, role, content, created_at)
        DB.touch_chat(self.id, self.last_interaction_id)

    def persist_interaction_id(self) -> None:
        DB.touch_chat(self.id, self.last_interaction_id)

    def persist_recovery_state(self) -> None:
        DB.save_recovery_state(self.id, self.recovery_state)

    def persist_activity_snapshot(self, *, force: bool = True) -> None:
        now = __import__("time").monotonic()
        if not force and now - self._last_activity_persist_at < 1.0:
            return
        for activity in self.activity.items:
            self._activity_seq += 1
            DB.upsert_activity(self.id, activity.to_dict(), self._activity_seq)
        self._last_activity_persist_at = now


class SessionStore:
    """Keeps live Session objects in memory while chat identity/history lives in SQLite.

    Any chat_id known to the database can be `get`/`require`d even if it was never
    loaded into memory before (e.g. after a server restart), in which case its
    messages and activity log are rehydrated from disk on first access.
    """

    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    # -- chat lifecycle -----------------------------------------------------
    def create(self, title: str = "New chat") -> Session:
        chat = DB.create_chat(title=title)
        session = Session(id=chat["id"])
        with self._lock:
            self._sessions[session.id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        session_id = str(session_id or "").strip()
        if not session_id:
            return None
        with self._lock:
            session = self._sessions.get(session_id)
        if session is not None:
            return None if session.delete_requested else session
        chat = DB.get_chat(session_id)
        if not chat:
            return None
        session = Session(id=chat["id"], last_interaction_id=chat.get("last_interaction_id"))
        session.recovery_state = chat.get("recovery_state")
        if isinstance(session.recovery_state, dict):
            session.live_response = str(session.recovery_state.get("live_response") or "")
        session.messages = DB.list_messages(session_id)
        for item in DB.list_activities(session_id):
            session.activity.hydrate(item)
        session._activity_seq = len(session.activity.items)
        with self._lock:
            self._sessions[session_id] = session
        return session

    def get_cached(self, session_id: str) -> Session | None:
        """Return an already-loaded session even while its deletion is pending."""
        with self._lock:
            return self._sessions.get(str(session_id or "").strip())

    def require(self, session_id: str) -> Session:
        session = self.get(session_id)
        if not session:
            raise KeyError(f"Unknown session: {session_id}")
        return session

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)
        DB.delete_chat(session_id)

    # -- chat metadata --------------------------------------------------------
    def list_chats(self) -> list[dict[str, Any]]:
        chats = DB.list_chats()
        with self._lock:
            hidden = {sid for sid, session in self._sessions.items() if session.delete_requested}
        return [chat for chat in chats if chat.get("id") not in hidden]

    def rename(self, session_id: str, title: str) -> bool:
        return DB.rename_chat(session_id, title)

    def set_pinned(self, session_id: str, pinned: bool) -> bool:
        return DB.set_pinned(session_id, pinned)
