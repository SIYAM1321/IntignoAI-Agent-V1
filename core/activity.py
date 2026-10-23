from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Activity:
    id: str
    type: str
    tool: str | None
    status: str
    description: str
    started_at: float
    duration: float | None = None
    input: Any = field(default_factory=dict)
    output: Any = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "tool": self.tool,
            "status": self.status,
            "description": self.description,
            "started_at": self.started_at,
            "duration": self.duration,
            "input": self.input,
            "output": self.output,
            "error": self.error,
        }


class ActivityLog:
    """Live, observable log of everything the agent does for one chat.

    Every step the agent takes (understanding the request, calling the model,
    running a tool, waiting on a terminal process) is recorded here as an
    `Activity`, with clear status transitions (running -> success/error/pending)
    so the UI can show exactly what is happening and what happened, in order.
    """

    def __init__(self, max_items: int = 1000):
        self.items: list[Activity] = []
        self.max_items = max_items
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)

    def add(self, *, activity_type: str, tool: str | None, description: str,
            input_data: Any = None, status: str = "running") -> Activity:
        activity = Activity(
            id=f"activity_{uuid.uuid4().hex[:10]}",
            type=activity_type,
            tool=tool,
            status=status,
            description=description,
            started_at=time.time(),
            input={} if input_data is None else input_data,
        )
        with self._condition:
            self.items.append(activity)
            self.items = self.items[-self.max_items:]
            self._condition.notify_all()
        return activity

    def update(self, activity: Activity, *, status: str | None = None,
               description: str | None = None, output: Any = None,
               error: str | None = None, set_output: bool = False) -> Activity:
        if status is not None:
            activity.status = status
        if description is not None:
            activity.description = description
        if set_output:
            activity.output = {} if output is None else output
        if error is not None:
            activity.error = error
        with self._condition:
            self._condition.notify_all()
        return activity

    def finish(self, activity: Activity, *, status: str, output: Any = None,
               error: str | None = None) -> Activity:
        activity.status = status
        activity.duration = round(max(0.0, time.time() - activity.started_at), 3)
        activity.output = {} if output is None else output
        activity.error = error
        with self._condition:
            self._condition.notify_all()
        return activity

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [item.to_dict() for item in self.items]

    def hydrate(self, row: dict[str, Any]) -> Activity:
        """Rebuild an Activity from a persisted SQLite row (used when a chat is reloaded)."""
        activity = Activity(
            id=row["id"],
            type=row.get("type") or "agent",
            tool=row.get("tool"),
            status=row.get("status") or "success",
            description=row.get("description") or "",
            started_at=float(row.get("started_at") or time.time()),
            duration=row.get("duration"),
            input=row.get("input") or {},
            output=row.get("output") or {},
            error=row.get("error"),
        )
        with self._condition:
            self.items.append(activity)
            self.items = self.items[-self.max_items:]
        return activity
