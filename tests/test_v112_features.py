"""v1.12: interleaved response/log segments, embedded-only browser, google_login."""
import time

import pytest

from agent import engine
from agent.memory import Session
from tools import browser_automation as ba


def test_flush_segment_saves_text_before_tool_logs():
    session = Session()
    engine._append_stream(session, "Let me look at the project.")
    started = session._segment_started_at
    assert started > 0
    time.sleep(0.01)
    activity = session.activity.add(activity_type="tool", tool="list_files", description="List", status="running")
    engine._flush_segment(session)
    assert session.live_response == ""
    assert session.messages[-1]["content"] == "Let me look at the project."
    # The saved segment sorts BEFORE the tool activity that followed it.
    assert session.messages[-1]["created_at"] <= activity.started_at
    assert session._turn_flushed == 1


def test_flush_segment_ignores_empty_text():
    session = Session()
    engine._append_stream(session, "   \n")
    engine._flush_segment(session)
    assert session.messages == []
    assert session._turn_flushed == 0


def test_segments_persist_with_timestamps():
    from core.db import DB
    session = Session()
    engine._append_stream(session, "Step one text")
    engine._flush_segment(session)
    stored = DB.list_messages(session.id)
    assert stored[-1]["content"] == "Step one text"
    assert stored[-1]["created_at"] == pytest.approx(session.messages[-1]["created_at"])


def test_auto_handoff_never_opens_physical_browser_in_embedded_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("BROWSER_EMBEDDED_UI", "true")

    def boom(*a, **k):
        raise AssertionError("a physical browser must not be opened")

    monkeypatch.setattr(ba, "_manual_browser_open", boom)

    class Page:
        url = "https://accounts.google.com/v3/signin"

    out = ba._auto_handoff("default", Page(), None, tmp_path, [])
    assert out and out["human_action_required"] and out["embedded_only"]
    rejected = ba._auto_handoff("default", Page(), {"kind": "provider_security_block"}, tmp_path, [])
    assert rejected["embedded_only"] and "No physical browser" in rejected["message"]


def test_google_login_requires_credentials():
    class Page:
        url = "about:blank"

    with pytest.raises(ba.BrowserAutomationError):
        ba._google_login(Page(), {"email": "a@b.com"})
    with pytest.raises(ba.BrowserAutomationError):
        ba._google_login(Page(), {"password": "x"})


def test_google_login_is_exposed_to_the_model():
    spec = engine.TOOL_MAP["browser_action"]
    enum = spec.parameters["properties"]["actions"]["items"]["properties"]["action"]["enum"]
    assert "google_login" in enum
    props = spec.parameters["properties"]["actions"]["items"]["properties"]
    assert "password" in props and "email" in props


def test_password_is_redacted_in_activity_arguments():
    safe = engine._safe_activity_arguments("browser_action", {"actions": [{"action": "google_login", "email": "a@b.com", "password": "hunter2"}]})
    assert "hunter2" not in str(safe)


def test_embedded_headless_override_is_ignored(monkeypatch):
    monkeypatch.setenv("BROWSER_EMBEDDED_UI", "true")
    assert ba._embedded_ui_enabled() is True
    monkeypatch.setenv("BROWSER_EMBEDDED_UI", "false")
    assert ba._embedded_ui_enabled() is False


def test_full_turn_interleaves_text_and_tool_logs(monkeypatch):
    """Fake Gemini: text -> tool -> text -> tool -> final text. The saved timeline must alternate."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    calls = {"n": 0}

    def sse(events):
        out = []
        for name, data in events:
            payload = data if name == "done" else json.dumps(data)
            out.append(f"event: {name}\ndata: {payload}\n\n")
        return "".join(out).encode()

    def tool_turn(iid, text, call_id):
        return [
            ("interaction.created", {"interaction": {"id": iid}}),
            ("step.start", {"index": 0, "step": {"type": "model_output"}}),
            ("step.delta", {"index": 0, "delta": {"type": "text", "text": text}}),
            ("step.start", {"index": 1, "step": {"type": "function_call", "id": call_id, "name": "default_api:get_system_info", "arguments": {}}}),
            ("step.delta", {"index": 1, "delta": {"type": "arguments_delta", "arguments": "{}"}}),
            ("interaction.completed", {"interaction": {"id": iid, "status": "requires_action"}}),
            ("done", "[DONE]"),
        ]

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            calls["n"] += 1
            n = calls["n"]
            if n == 1:
                events = tool_turn("i1", "First I will inspect the system.", "c1")
            elif n == 2:
                events = tool_turn("i2", "Now I will inspect it again.", "c2")
            else:
                events = [
                    ("interaction.created", {"interaction": {"id": "i3"}}),
                    ("step.start", {"index": 0, "step": {"type": "model_output"}}),
                    ("step.delta", {"index": 0, "delta": {"type": "text", "text": "Final answer."}}),
                    ("interaction.completed", {"interaction": {"id": "i3", "status": "completed"}}),
                    ("done", "[DONE]"),
                ]
            body = sse(events)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(engine, "API_URL", f"http://127.0.0.1:{server.server_port}/interactions")
    monkeypatch.setattr(engine, "API_KEY", "test-key", raising=False)
    session = Session()
    done = []
    engine.start_request(session, "inspect twice", lambda t: done.append(t), model="gemini-3.5-flash-lite")
    for _ in range(300):
        if not session.busy:
            break
        time.sleep(0.03)
    server.shutdown()

    assistant = [m for m in session.messages if m["role"] == "assistant"]
    assert [m["content"] for m in assistant] == ["First I will inspect the system.", "Now I will inspect it again.", "Final answer."], session.messages
    tools = sorted((a for a in session.activity.items if a.type == "tool"), key=lambda a: a.started_at)
    assert len(tools) == 2
    # Chronology: text1 < tool1 < text2 < tool2 < final text.
    t1, t2, t3 = (m["created_at"] for m in assistant)
    assert t1 <= tools[0].started_at <= t2 <= tools[1].started_at <= t3
    assert done and done[-1] == "Final answer."
