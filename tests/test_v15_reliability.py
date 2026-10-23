from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from core.rate_limiter import HARD_REQUESTS_PER_MINUTE, RateLimitError, SlidingWindowRateLimiter
from agent import knowledge, engine
from agent.memory import Session
from agent.engine import RecoverableTurnError
from tools.web_fetch import _PageText, _validate_public_url


def test_rate_limiter_hard_cap_and_state_survive_new_instance(tmp_path):
    db = tmp_path / "limiter.sqlite3"
    first = SlidingWindowRateLimiter(limit=99, window_seconds=60, db_path=db)
    assert first.limit == HARD_REQUESTS_PER_MINUTE == 10
    for _ in range(10):
        first.acquire()
    with pytest.raises(RateLimitError):
        first.acquire()
    second = SlidingWindowRateLimiter(limit=10, window_seconds=60, db_path=db)
    assert second.snapshot()["used"] == 10
    assert second.snapshot()["remaining"] == 0
    with pytest.raises(RateLimitError):
        second.acquire()


def test_memory_and_skills_are_append_only_deduplicated_and_secret_filtered(tmp_path, monkeypatch):
    memory_path = tmp_path / "MEMORY.MD"
    skills_path = tmp_path / "SKILLS.MD"
    monkeypatch.setattr(knowledge, "MEMORY_PATH", memory_path)
    monkeypatch.setattr(knowledge, "SKILLS_PATH", skills_path)

    one = knowledge.append_entry("memory", "Prefers step-by-step Bangla explanations", title="Learning style")
    two = knowledge.append_entry("memory", "Prefers step-by-step Bangla explanations", title="Learning style")
    knowledge.append_entry("skill", "Always run tests after edits.", title="Testing workflow")

    assert one["duplicate"] is False
    assert two["duplicate"] is True
    assert memory_path.read_text().count("Prefers step-by-step") == 1
    assert "Testing workflow" in skills_path.read_text()
    with pytest.raises(ValueError, match="secret"):
        knowledge.append_entry("memory", "GOOGLE_API_KEY=AIza123456789012345678901234567890")


def test_unknown_source_file_attachment_is_read_as_text(tmp_path):
    source = tmp_path / "example.rarelang"
    source.write_text("module Example\nfn main() { print('works') }\n", encoding="utf-8")
    request_input, display = engine._prepare_user_input("Review this", [{"name": source.name, "path": str(source)}])
    assert "module Example" in request_input
    assert "example.rarelang" in display


def test_image_attachment_is_encoded_for_model(tmp_path):
    image = tmp_path / "pixel.png"
    # Minimal PNG signature is sufficient to test packaging (the file is not decoded here).
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    request_input, _ = engine._prepare_user_input("Describe it", [{"name": image.name, "path": str(image)}])
    assert isinstance(request_input, list)
    block = next(item for item in request_input if item.get("type") == "image")
    assert block["mime_type"] == "image/png"
    assert base64.b64decode(block["data"]).startswith(b"\x89PNG")


def test_private_url_fetch_is_blocked_without_network():
    with pytest.raises(PermissionError):
        _validate_public_url("http://127.0.0.1:2000/api/status")
    with pytest.raises(PermissionError):
        _validate_public_url("http://localhost/private")


def test_html_parser_omits_navigation_and_script_content():
    parser = _PageText()
    parser.feed("<html><head><title>Page title</title><script>secret()</script></head><body><nav>Skip nav</nav><main><h1>Helpful text</h1><p>Body here</p></main></body></html>")
    content = "".join(parser.parts)
    assert "Helpful text" in content
    assert "Body here" in content
    assert "Skip nav" not in content
    assert "secret()" not in content


def test_stream_parser_does_not_silently_drop_call_without_id(monkeypatch):
    import io

    events = [
        ("event: step.start", {"index": 0, "step": {"type": "function_call", "name": r"default_api\:web_search", "arguments": {}}}),
        ("event: interaction.completed", {"interaction": {"id": "interaction-incomplete", "status": "requires_action"}}),
    ]
    raw = "".join(f"{name}\ndata: {json.dumps(data)}\n\n" for name, data in events) + "data: [DONE]\n\n"
    monkeypatch.setattr(engine, "_open_request", lambda _body: io.BytesIO(raw.encode("utf-8")))
    with pytest.raises(RecoverableTurnError, match="call_id_present=False"):
        engine.stream_interaction({}, lambda _chunk: None)


def test_handoff_protocol_failure_uses_saved_results_without_reexecuting_tool(monkeypatch):
    session = Session()
    session.messages = [{"role": "user", "content": "Find the answer"}]
    calls = [{"name": r"default_api\:web_search", "call_id": "call-1", "arguments": {"query": "x"}, "result": {"ok": True, "result": "already collected"}}]
    monkeypatch.setattr(engine, "_send_function_results", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("Please ensure that function response turn comes immediately after a function call turn. Got function response with name 'web_search'.")))
    captured = {}

    def fake_send_to_model(_session, body, description, on_text):
        captured["body"] = body
        captured["description"] = description
        return engine.StreamRun("fresh-interaction", "completed", [], "Recovered answer")

    monkeypatch.setattr(engine, "_send_to_model", fake_send_to_model)
    result = engine._send_function_results_resilient(session, "old-interaction", calls, lambda _chunk: None)
    assert result.output_text == "Recovered answer"
    assert result.interaction_id == "fresh-interaction"
    assert "previous_interaction_id" not in captured["body"]
    assert "already collected" in captured["body"]["input"]
    assert session.recovery_state["phase"] == "fallback_pending"


def test_settings_and_local_origin_guard(monkeypatch, tmp_path):
    pytest.importorskip("flask")
    import app as app_module

    monkeypatch.setattr(app_module, "UPLOAD_ROOT", tmp_path / "uploads")
    app_module.app.testing = True
    client = app_module.app.test_client()

    denied = client.post("/api/session", json={}, headers={"Origin": "http://evil.example"})
    assert denied.status_code == 403

    settings = client.get("/api/settings")
    assert settings.status_code == 200
    data = settings.get_json()
    assert data["hard_cap"] == 10
    assert data["requests_per_minute"] <= 10
    assert "api_key" not in data


def test_upload_endpoint_and_attachment_resolution(monkeypatch, tmp_path):
    pytest.importorskip("flask")
    import app as app_module

    monkeypatch.setattr(app_module, "UPLOAD_ROOT", tmp_path / "uploads")
    app_module.app.testing = True
    client = app_module.app.test_client()
    created = client.post("/api/session", json={}).get_json()
    session_id = created["session_id"]
    uploaded = client.post("/api/upload", data={
        "session_id": session_id,
        "files": (io.BytesIO(b"print('hello')"), "hello.py"),
    }, content_type="multipart/form-data")
    assert uploaded.status_code == 200, uploaded.get_json()
    item = uploaded.get_json()["attachments"][0]
    resolved = app_module._resolve_attachments(session_id, [item["id"]])
    assert Path(resolved[0]["path"]).read_text() == "print('hello')"
    app_module.sessions.delete(session_id)
