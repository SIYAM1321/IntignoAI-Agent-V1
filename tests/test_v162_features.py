from __future__ import annotations

import sys
import time
import types

import pytest

from agent import engine
from agent.memory import Session
from core.db import DB
from tools import browser_automation as browser


def _app_dependencies():
    pytest.importorskip("flask")
    pytest.importorskip("werkzeug")
    from app import app, sessions
    return app, sessions


def test_busy_chat_accepts_followup_message_into_queue(monkeypatch):
    app, sessions = _app_dependencies()
    app.testing = True
    session = sessions.create("queue API test")
    client = app.test_client()
    try:
        with session.lock:
            session.busy = True
        response = client.post("/api/chat", json={"session_id": session.id, "message": "Do the next thing", "attachments": []})
        assert response.status_code == 200
        data = response.get_json()
        assert data["accepted"] is True and data["queued"] is True
        assert data["queue_position"] == 1
        state = client.get(f"/api/chats/{session.id}").get_json()["state"]
        assert state["queued_messages"][0]["message"] == "Do the next thing"
    finally:
        with session.lock:
            session.busy = False
            session.queued_requests.clear()
        sessions.delete(session.id)


def test_stop_clears_queued_messages_for_active_chat():
    app, sessions = _app_dependencies()
    app.testing = True
    session = sessions.create("stop clears queue test")
    client = app.test_client()
    try:
        with session.lock:
            session.busy = True
            session.queued_requests.append({"id": "q1", "message": "queued task", "attachments": []})
        response = client.post("/api/cancel", json={"session_id": session.id})
        assert response.status_code == 200
        assert response.get_json()["discarded_queued_messages"] == 1
        assert session.cancel_event.is_set()
        assert session.queued_requests == []
    finally:
        with session.lock:
            session.busy = False
            session.queued_requests.clear()
        sessions.delete(session.id)


def test_queued_turn_is_started_after_previous_turn_finishes(monkeypatch):
    session = Session()
    started = []

    def fake_start(target, message, callback, attachments=None):
        target.busy = True
        started.append((message, attachments or []))

    monkeypatch.setattr(engine, "start_request", fake_start)
    try:
        with session.lock:
            session.busy = False
            session.queued_requests.append({"id": "q1", "message": "second task", "attachments": []})
        engine._start_next_queued(session, lambda _text: None)
        assert started == [("second task", [])]
        assert session.busy is True
        assert session.queued_requests == []
    finally:
        DB.delete_chat(session.id)


def test_delete_active_busy_chat_hides_then_finishes_deletion():
    app, sessions = _app_dependencies()
    app.testing = True
    session = sessions.create("delete while busy test")
    client = app.test_client()
    try:
        with session.lock:
            session.busy = True
        response = client.delete(f"/api/chats/{session.id}")
        assert response.status_code == 200
        assert response.get_json()["deleting"] is True
        assert session.cancel_event.is_set()
        assert all(chat["id"] != session.id for chat in sessions.list_chats())
        with session.lock:
            session.busy = False
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and sessions.get(session.id) is not None:
            time.sleep(0.05)
        assert sessions.get(session.id) is None
    finally:
        # Clean up if an assertion failed before the deferred-delete worker finished.
        if sessions.get(session.id) is not None:
            with session.lock:
                session.busy = False
                session.delete_requested = False
            sessions.delete(session.id)


def test_browser_window_is_reused_and_only_closed_explicitly(monkeypatch, tmp_path):
    profiles = tmp_path / "profiles"
    monkeypatch.setattr(browser, "PROFILES_DIR", profiles)
    monkeypatch.setenv("BROWSER_USE_NATIVE_CHROME", "false")
    browser.create_profile("persistent-test")
    events = {"launch": 0, "context_close": 0, "playwright_stop": 0}

    class FakePage:
        url = "about:blank"

        def __init__(self):
            self.closed = False
            self.events = {}

        def is_closed(self):
            return self.closed

        def set_default_timeout(self, _timeout):
            pass

        def on(self, name, callback):
            self.events[name] = callback

        def wait_for_timeout(self, _ms):
            pass

        def title(self):
            return "Fake Browser"

    class FakeContext:
        def __init__(self):
            self.closed = False
            self.pages = [FakePage()]

        def is_closed(self):
            return self.closed

        def new_page(self):
            page = FakePage()
            self.pages.append(page)
            return page

        def close(self):
            self.closed = True
            events["context_close"] += 1

    class FakeChromium:
        def launch_persistent_context(self, **_kwargs):
            events["launch"] += 1
            return FakeContext()

    class FakePlaywright:
        def __init__(self):
            self.chromium = FakeChromium()

        def stop(self):
            events["playwright_stop"] += 1

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: types.SimpleNamespace(start=lambda: FakePlaywright())
    playwright_pkg = types.ModuleType("playwright")
    playwright_pkg.__path__ = []
    playwright_pkg.sync_api = sync_api
    monkeypatch.setitem(sys.modules, "playwright", playwright_pkg)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    try:
        payload = {"task": "action", "profile": "persistent-test", "headless": False, "actions": [{"action": "wait", "ms": 1}]}
        first = browser.perform_browser_task(payload)
        second = browser.perform_browser_task(payload)
        assert first["ok"] and first["browser_kept_open"] is True
        assert second["ok"] and second["browser_kept_open"] is True
        assert events["launch"] == 1
        assert events["context_close"] == 0
        assert events["playwright_stop"] == 0
        closed = browser.perform_browser_task({"task": "action", "profile": "persistent-test", "actions": [{"action": "close_browser"}]})
        assert closed["ok"] and closed["browser_closed"] is True
        assert events["context_close"] == 1
        assert events["playwright_stop"] == 1
    finally:
        browser._close_browser_session("persistent-test")
