from __future__ import annotations

import json
import importlib.util
import mimetypes
import os
import re  # v1.13.4: was missing — the pending-confirmation approval branch uses re.match; without it every message during a confirmation wait raised NameError → HTTP 500
import socket
import shutil
import threading
import time
import uuid
from pathlib import Path
from queue import Empty
from urllib.parse import urlsplit
from werkzeug.utils import secure_filename
from werkzeug.exceptions import HTTPException

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request, stream_with_context

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

from agent import engine
from agent.knowledge import append_entry, read_knowledge
from agent.memory import SessionStore
from core.db import DB
from core.rate_limiter import HARD_REQUESTS_PER_MINUTE
from tools import browser_automation
from tools import browser_stream

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024
sessions = SessionStore()
UPLOAD_ROOT = ROOT / "data" / "uploads"
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
ALLOWED_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


@app.before_request
def protect_local_endpoints():
    """Local machine-control app: reject non-loopback hosts and cross-origin writes."""
    if request.path.startswith("/api/") or request.path == "/browser-action":
        host = (urlsplit("//" + request.host).hostname or "").lower()
        if host not in ALLOWED_LOOPBACK_HOSTS and not app.testing:
            return jsonify({"error": "IntignoAI only accepts local loopback connections."}), 403
        if request.method in {"POST", "PATCH", "PUT", "DELETE"}:
            origin = request.headers.get("Origin")
            if origin:
                origin_parts = urlsplit(origin)
                origin_host = (origin_parts.hostname or "").lower()
                expected_host = (urlsplit("//" + request.host).hostname or "").lower()
                if origin_parts.scheme not in {"http", "https"} or origin_host != expected_host:
                    return jsonify({"error": "Blocked cross-origin request. Open IntignoAI from its local URL."}), 403
            if request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
                return jsonify({"error": "Blocked cross-site request to local controls."}), 403


@app.errorhandler(413)
def upload_too_large(_error):
    return jsonify({"error": "Upload is too large. Maximum request size is 40 MB; each attachment may be up to 10 MB."}), 413


@app.errorhandler(HTTPException)
def http_exception(error: HTTPException):
    # v1.13.4: always answer with JSON so the UI shows the real reason (e.g. a 409 with its
    # message) instead of a bare "Error: HTTP 500" bubble from a non-JSON error page.
    return jsonify({"error": error.description or error.name}), error.code


@app.errorhandler(Exception)
def unhandled_exception(error: Exception):
    # v1.13.4: no endpoint may leak a bare HTML 500 page. Log the traceback server-side and
    # return the actual reason as JSON so failures stay visible and diagnosable in the UI.
    import logging

    logging.getLogger("intignoai.app").exception("Unhandled error in request %s", request.path)
    return jsonify({"error": f"Internal error: {error}"}), 500


def _session_state(session):
    with session.lock:
        pending = session.pending
        current = pending.current if pending else None
        chat = DB.get_chat(session.id) or {}
        recovery = session.recovery_state or chat.get("recovery_state")
        return {
            "session_id": session.id,
            "title": chat.get("title", "New chat"),
            "pinned": bool(chat.get("pinned")),
            "busy": session.busy,
            "live_response": session.live_response,
            "recovery_available": bool(recovery and recovery.get("phase") in {"model_request", "function_results_pending", "fallback_pending"}),
            "recovery_phase": (recovery or {}).get("phase"),
            "pending_confirmation": None if current is None else {
                "tool": current.canonical_name,
                "tool_name": current.tool_name,
                "arguments": current.arguments,
                "activity_id": current.activity_id,
            },
            "messages": list(session.messages),
            "activities": session.activity.snapshot(),
            "queued_messages": [
                {
                    "id": item.get("id"),
                    "message": item.get("message", ""),
                    "attachment_names": [str(a.get("name", "file")) for a in item.get("attachments", [])],
                    "enqueued_at": item.get("enqueued_at"),
                }
                for item in session.queued_requests
            ],
        }


def _callback(session):
    def done(final_text: str):
        with session.lock:
            if final_text and (not session.messages or session.messages[-1].get("content") != final_text):
                _now = time.time()
                session.messages.append({"role": "assistant", "content": final_text, "created_at": _now})
                session.persist_message("assistant", final_text, _now)
            session.live_response = ""
    return done


def _write_env_values(updates: dict[str, str]) -> None:
    """Atomically update only selected .env keys while preserving unrelated settings."""
    env_path = ROOT / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    pending = dict(updates)
    output = []
    for line in lines:
        stripped = line.strip()
        replaced = False
        if stripped and not stripped.startswith("#") and "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in pending:
                output.append(f"{key}={pending.pop(key)}")
                replaced = True
        if not replaced:
            output.append(line)
    output.extend(f"{key}={value}" for key, value in pending.items())
    tmp = env_path.with_suffix(".env.tmp")
    tmp.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, env_path)
    for key, value in updates.items():
        os.environ[key] = value


def _resolve_attachments(session_id: str, ids) -> list[dict]:
    if not isinstance(ids, list):
        raise ValueError("attachments must be a list of uploaded attachment IDs.")
    if len(ids) > 8:
        raise ValueError("Attach up to 8 files per message.")
    folder = UPLOAD_ROOT / session_id
    items = []
    total_size = 0
    for raw_id in ids:
        attachment_id = str(raw_id or "")
        try:
            uuid.UUID(hex=attachment_id)
        except (ValueError, AttributeError):
            raise ValueError("An attachment ID is invalid. Please upload the file again.")
        matches = list(folder.glob(f"{attachment_id}--*")) if folder.exists() else []
        if not matches:
            raise ValueError("An attachment has expired or is missing. Please upload it again.")
        path = matches[0].resolve()
        if path.parent != folder.resolve() or not path.is_file():
            raise ValueError("Attachment path validation failed.")
        name = path.name.split("--", 1)[1]
        size = path.stat().st_size
        total_size += size
        if total_size > 30 * 1024 * 1024:
            raise ValueError("Combined attachments must be 30 MB or less.")
        items.append({"id": attachment_id, "name": name, "path": str(path), "size": size, "mime_type": mimetypes.guess_type(name)[0] or "application/octet-stream"})
    return items


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/session")
def create_session():
    session = sessions.create()
    return jsonify({"session_id": session.id, "state": _session_state(session)})


@app.get("/api/chats")
def list_chats():
    return jsonify({"chats": sessions.list_chats()})


@app.get("/api/chats/<chat_id>")
def load_chat(chat_id: str):
    session = sessions.get(chat_id)
    if not session:
        return jsonify({"error": "Chat not found."}), 404
    return jsonify({"session_id": session.id, "state": _session_state(session)})


@app.patch("/api/chats/<chat_id>")
def rename_chat(chat_id: str):
    data = request.get_json(silent=True) or {}
    title = str(data.get("title", "")).strip()
    if not title:
        return jsonify({"error": "Title is required."}), 400
    if not sessions.rename(chat_id, title):
        return jsonify({"error": "Chat not found."}), 404
    return jsonify({"ok": True, "chat_id": chat_id, "title": title})


@app.post("/api/chats/<chat_id>/pin")
def pin_chat(chat_id: str):
    data = request.get_json(silent=True) or {}
    pinned = bool(data.get("pinned", True))
    if not sessions.set_pinned(chat_id, pinned):
        return jsonify({"error": "Chat not found."}), 404
    return jsonify({"ok": True, "chat_id": chat_id, "pinned": pinned})


def _remove_chat_uploads(chat_id: str) -> None:
    upload_folder = (UPLOAD_ROOT / chat_id).resolve()
    try:
        upload_folder.relative_to(UPLOAD_ROOT.resolve())
        if upload_folder.is_dir():
            shutil.rmtree(upload_folder)
    except (ValueError, OSError):
        pass


def _finish_deferred_chat_delete(chat_id: str, session) -> None:
    # Let the current tool/model call observe cancellation and persist its final logs
    # before removing the chat row. A deleted chat is hidden from the UI immediately.
    while True:
        with session.lock:
            busy = session.busy
        if not busy:
            break
        time.sleep(0.2)
    sessions.delete(chat_id)
    _remove_chat_uploads(chat_id)


@app.delete("/api/chats/<chat_id>")
def delete_chat(chat_id: str):
    if not DB.get_chat(chat_id):
        return jsonify({"error": "Chat not found."}), 404
    session = sessions.get(chat_id) or sessions.get_cached(chat_id)
    if session:
        with session.lock:
            if session.delete_requested:
                return jsonify({"ok": True, "chat_id": chat_id, "deleting": True})
            if session.busy:
                session.delete_requested = True
                session.queued_requests.clear()
                session.cancel_event.set()
                threading.Thread(target=_finish_deferred_chat_delete, args=(chat_id, session), daemon=True).start()
                return jsonify({"ok": True, "chat_id": chat_id, "deleting": True})
    sessions.delete(chat_id)
    _remove_chat_uploads(chat_id)
    return jsonify({"ok": True, "chat_id": chat_id})


@app.post("/api/chat")
def chat():
    data = request.get_json(silent=True) or {}
    message = str(data.get("message", "")).strip()
    session_id = str(data.get("session_id", "")).strip()
    if not message and not data.get("attachments"):
        return jsonify({"error": "Enter a message or attach a file."}), 400
    session = sessions.create() if not session_id else sessions.get(session_id)
    if not session:
        return jsonify({"error": "Session not found."}), 404
    if session.delete_requested:
        return jsonify({"error": "This chat is being deleted. Please use a new chat."}), 409
    # Natural-language approval replaces the old Confirm/Cancel button bar.
    if session.pending is not None:
        reply = message.strip().lower()
        yes = bool(re.match(r"^(yes|y|yeah|yep|sure|approved|approve|proceed|go ahead|do it|confirm|okay|ok)([ !.,].*)?$", reply))
        no = bool(re.match(r"^(no|n|nope|cancel|decline|stop|don't|do not)([ !.,].*)?$", reply))
        waiver = None
        if not (yes or no):
            # v1.13.4: a "never ask for confirmation" message while an action is pending both
            # saves the standing preference permanently AND approves the pending action once.
            try:
                waiver = engine.note_user_confirmation_preference(message)
            except Exception:
                waiver = None
        if yes or no or (waiver and waiver.get("ok")):
            approved = bool(yes or (waiver and waiver.get("ok") and not no))
            with session.lock:
                session.messages.append({"role": "user", "content": message})
            session.persist_message("user", message)
            try:
                engine.confirm_pending(session, approved, _callback(session))
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 409
            response = {"session_id": session.id, "accepted": True, "approval_response": "approved" if approved else "declined"}
            if waiver and waiver.get("ok"):
                response["confirmation_preference_saved"] = {"scope": waiver.get("scope"), "duplicate": bool(waiver.get("duplicate"))}
            return jsonify(response)
        return jsonify({"error": "An action is awaiting your reply. Respond ‘yes, proceed’ to allow it, ‘no, cancel’ to decline it — or say ‘never ask for confirmation’ to allow such actions from now on."}), 409
    try:
        # v1.13.4: a waiver request outside a pending confirmation is still saved permanently.
        engine.note_user_confirmation_preference(message)
    except Exception:
        pass
    try:
        attachments = _resolve_attachments(session.id, data.get("attachments", []))
        start_message = message or "Please inspect the attached file(s) and help me with them."
        callback = _callback(session)
        with session.lock:
            if session.delete_requested:
                return jsonify({"error": "This chat is being deleted. Please use a new chat."}), 409
            if session.busy:
                if session.pending:
                    return jsonify({"error": "A confirmation is waiting for your decision. Resolve it before queueing another message."}), 409
                chosen_model = str(data.get("model") or engine.MODEL).strip()
                if chosen_model not in engine.SUPPORTED_MODELS:
                    return jsonify({"error": "Choose one of the supported models: GLM-5.3-Flash or Gemini 3.5 Flash Lite."}), 400
                queued = {
                    "id": uuid.uuid4().hex,
                    "message": start_message,
                    "attachments": attachments,
                    "model": chosen_model,
                    "reasoning_effort": str(data.get("reasoning_effort", "high")),
                    "enqueued_at": time.time(),
                }
                session.queued_requests.append(queued)
                return jsonify({
                    "session_id": session.id, "accepted": True, "queued": True,
                    "queue_position": len(session.queued_requests),
                })
            # Reserve this session before releasing its lock, preventing two rapid
            # sends from racing to start concurrent turns in the same chat.
            chosen_model = str(data.get("model") or engine.MODEL).strip()
            if chosen_model not in engine.SUPPORTED_MODELS:
                return jsonify({"error": "Choose one of the supported models: GLM-5.3-Flash or Gemini 3.5 Flash Lite."}), 400
            engine.start_request(session, start_message, callback, attachments=attachments, model=chosen_model, reasoning_effort=str(data.get("reasoning_effort", "high")))
    except Exception as exc:
        # v1.13.4: message-sending must never 500; surface the reason as a 409 JSON body.
        return jsonify({"error": str(exc)}), 409
    return jsonify({"session_id": session.id, "accepted": True, "queued": False})


@app.post("/api/upload")
def upload():
    session_id = request.form.get("session_id", "").strip()
    session = sessions.get(session_id)
    if not session:
        return jsonify({"error": "Session not found. Create or open a chat first."}), 404
    incoming = request.files.getlist("files")
    if not incoming or len(incoming) > 8:
        return jsonify({"error": "Choose between 1 and 8 files."}), 400
    saved = []
    total = 0
    folder = UPLOAD_ROOT / session.id
    folder.mkdir(parents=True, exist_ok=True)
    for item in incoming:
        name = secure_filename(item.filename or "")
        if not name:
            return jsonify({"error": "One uploaded file has an invalid filename."}), 400
        raw = item.read(10 * 1024 * 1024 + 1)
        if len(raw) > 10 * 1024 * 1024:
            return jsonify({"error": f"{name} exceeds the 10 MB per-file limit."}), 413
        total += len(raw)
        if total > 30 * 1024 * 1024:
            return jsonify({"error": "Combined files must be 30 MB or less."}), 413
        attachment_id = uuid.uuid4().hex
        path = folder / f"{attachment_id}--{name[:120]}"
        path.write_bytes(raw)
        saved.append({"id": attachment_id, "name": name, "size": len(raw), "mime_type": mimetypes.guess_type(name)[0] or "application/octet-stream"})
    return jsonify({"attachments": saved})


@app.post("/api/confirm")
def confirm():
    data = request.get_json(silent=True) or {}
    session_id = str(data.get("session_id", "")).strip()
    confirmed = bool(data.get("confirmed", False))
    if not session_id:
        return jsonify({"error": "session_id is required."}), 400
    session = sessions.get(session_id)
    if not session:
        return jsonify({"error": "Session not found."}), 404
    with session.lock:
        if session.busy:
            return jsonify({"error": "Agent is busy."}), 409
        if not session.pending:
            return jsonify({"error": "No confirmation is pending."}), 400
    try:
        engine.confirm_pending(session, confirmed, _callback(session))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"accepted": True, "confirmed": confirmed})


@app.post("/api/cancel")
def cancel():
    data = request.get_json(silent=True) or {}
    session = sessions.get(str(data.get("session_id", "")).strip())
    if not session:
        return jsonify({"error": "Session not found."}), 404
    # Stop means stop the current run and discard any not-yet-started messages in
    # this chat. Otherwise the queue would immediately start a new turn and make
    # the stop control appear ineffective.
    with session.lock:
        discarded_queued_messages = len(session.queued_requests)
        session.queued_requests.clear()
        session.cancel_event.set()
        active_response = session.active_response
    if active_response is not None:
        try:
            raw = getattr(getattr(active_response, "fp", None), "raw", None)
            sock = getattr(raw, "_sock", None)
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            active_response.close()
        except Exception:
            pass
    return jsonify({"ok": True, "message": "Cancellation requested.", "discarded_queued_messages": discarded_queued_messages})


@app.post("/api/resume")
def resume():
    data = request.get_json(silent=True) or {}
    session = sessions.get(str(data.get("session_id", "")).strip())
    if not session:
        return jsonify({"error": "Session not found."}), 404
    try:
        engine.resume_request(session, _callback(session))
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 409
    return jsonify({"ok": True, "resuming": True})


@app.delete("/api/recovery")
def discard_recovery():
    data = request.get_json(silent=True) or {}
    session = sessions.get(str(data.get("session_id", "")).strip())
    if not session:
        return jsonify({"error": "Session not found."}), 404
    with session.lock:
        if session.busy:
            return jsonify({"error": "Cannot dismiss recovery while the agent is working."}), 409
        session.recovery_state = None
    session.persist_recovery_state()
    return jsonify({"ok": True})


@app.get("/api/activity/stream")
def activity_stream():
    session_id = request.args.get("session_id", "").strip()
    session = sessions.get(session_id)
    if not session:
        return jsonify({"error": "Session not found."}), 404

    @stream_with_context
    def generate():
        last = None
        last_keepalive = time.monotonic()
        while True:
            payload = jsonify(_session_state(session)).get_data(as_text=True)
            if payload != last:
                yield f"event: state\ndata: {payload}\n\n"
                last = payload
                last_keepalive = time.monotonic()
            elif time.monotonic() - last_keepalive >= 10:
                yield ": keepalive\n\n"
                last_keepalive = time.monotonic()
            time.sleep(0.15)

    return Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive",
    })


@app.get("/api/status")
def status():
    return jsonify({
        "name": "IntignoAI Agent", "version": engine.VERSION, "model": engine.MODEL,
        "workspace": str(engine.get_workspace()), "os_name": engine._pretty_os_name(), "shell": engine.default_shell(),
        "terminal_processes": engine.TERMINAL_MANAGER.list(include_finished=False),
        "rate_limit": engine.rate_limiter.snapshot(), "api_key_configured": bool(os.getenv("GOOGLE_API_KEY") or engine.API_KEY),
        "login_required": False, "host": os.getenv("HOST", "127.0.0.1"), "port": int(os.getenv("PORT", "2000")),
        "browser_automation": {"available": importlib.util.find_spec("playwright") is not None, "profiles": browser_automation.list_profiles()["count"], "headless_default": browser_automation.DEFAULT_HEADLESS},
    })



@app.get("/api/browser-profiles")
def browser_profiles_list():
    """List persistent profiles without exposing cookies or browsing history."""
    return jsonify(browser_automation.list_profiles())


@app.post("/api/browser-profiles")
def browser_profiles_create():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    if not name:
        return jsonify({"error": "Profile name is required."}), 400
    try:
        return jsonify(browser_automation.create_profile(name)), 201
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/browser-profiles/<profile>/extensions")
def browser_profile_add_extension(profile: str):
    data = request.get_json(silent=True) or {}
    extension_path = str(data.get("extension_path", "")).strip()
    if not extension_path:
        return jsonify({"error": "extension_path is required. Use an unpacked extension folder containing manifest.json."}), 400
    try:
        return jsonify(browser_automation.add_profile_extension(profile, extension_path))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/browser/open")
def browser_open_for_user():
    """Open the persistent browser for MANUAL use (agent disconnected). Works for Google sign-in."""
    data = request.get_json(silent=True) or {}
    payload = {"task": "manual_signin"}
    url = str(data.get("url", "")).strip()
    if url:
        payload["url"] = url
    result = browser_automation.perform_browser_task(payload)
    status_code = int(result.pop("status_code", 200 if result.get("ok") else 500))
    return jsonify(result), status_code


@app.post("/api/browser/release")
def browser_release_to_user():
    """Disconnect the agent from the browser; the window stays open under the user's control."""
    result = browser_automation.perform_browser_task({"task": "release"})
    status_code = int(result.pop("status_code", 200 if result.get("ok") else 500))
    return jsonify(result), status_code


# ---- v1.9: live embedded-browser streaming (CDP screencast over SSE) -----------------------

@app.get("/api/browser/stream/status")
def browser_stream_status():
    """Current state of the embedded-browser mirror."""
    return jsonify(browser_stream.MANAGER.status())


@app.post("/api/browser/stream/start")
def browser_stream_start():
    """Start mirroring the persistent agent browser (launches it if needed, via the automation module)."""
    data = request.get_json(silent=True) or {}
    url_hint = str(data.get("url", data.get("tab", "")) or "").strip()
    try:
        result = browser_stream.MANAGER.start(url_hint=url_hint or None)
    except browser_stream.BrowserStreamError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Could not start the browser stream: {exc}"}), 500
    return jsonify(result), (200 if result.get("ok") else 409)


@app.post("/api/browser/stream/stop")
def browser_stream_stop():
    """Stop capturing screencast frames; existing viewers receive an ended event."""
    try:
        result = browser_stream.MANAGER.stop()
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Could not stop the browser stream: {exc}"}), 500
    return jsonify(result)


@app.post("/api/browser/stream/resync")
def browser_stream_resync():
    """Point the mirror at the tab the agent is currently using (or a url/tab substring)."""
    data = request.get_json(silent=True) or {}
    url_hint = str(data.get("url", data.get("tab", "")) or "").strip()
    try:
        result = browser_stream.MANAGER.resync(url_hint=url_hint or None)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Could not resync the browser stream: {exc}"}), 500
    return jsonify(result), (200 if result.get("ok") else 409)


@app.post("/api/browser/input")
def browser_stream_input():
    """Forward one manual interaction (click/dblclick/wheel/keys/navigate/new_tab) to the mirrored page.

    Coordinates are viewport CSS pixels of the streamed page; commands are executed on the
    capture thread's own CDP session so they never cross Playwright threads.
    """
    data = request.get_json(silent=True) or {}
    try:
        result = browser_stream.MANAGER.dispatch_input(data)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Could not forward the input: {exc}"}), 500
    return jsonify(result), (200 if result.get("ok") else 400)


@app.get("/api/browser/stream/feed")
def browser_stream_feed():
    """SSE mirror: emits status events and JPEG screencast frames of the agent browser.

    Frame payloads are pre-serialized JSON ({seq, image, metadata, ts}); each viewer gets
    the newest frame first, then every published frame (drop-oldest per viewer). A status
    event every 2 s doubles as the keepalive. Viewers receive ended when capture stops.
    """
    viewer = browser_stream.MANAGER.subscribe()

    @stream_with_context
    def generate():
        try:
            next_status = 0.0
            while True:
                now = time.monotonic()
                if now >= next_status:
                    next_status = now + 2.0
                    status_payload = jsonify(browser_stream.MANAGER.status()).get_data(as_text=True)
                    yield f"event: status\ndata: {status_payload}\n\n"
                remaining = max(0.05, next_status - time.monotonic())
                try:
                    item = viewer.get(timeout=remaining)
                except Empty:
                    continue
                if item is None:
                    ended_payload = jsonify(browser_stream.MANAGER.status()).get_data(as_text=True)
                    yield f"event: ended\ndata: {ended_payload}\n\n"
                    return
                yield f"event: frame\ndata: {item}\n\n"
        finally:
            browser_stream.MANAGER.unsubscribe(viewer)

    return Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive",
    })


@app.post("/api/browser-action")
@app.post("/browser-action")
def browser_action():
    """Run one structured Playwright automation, scrape, inspect, or smoke-test task."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Send a JSON object describing the browser task."}), 400
    result = browser_automation.perform_browser_task(data)
    status_code = int(result.pop("status_code", 200 if result.get("ok") else 500))
    return jsonify(result), status_code


@app.get("/api/terminals")
def terminals():
    include_finished = request.args.get("include_finished", "true").lower() != "false"
    return jsonify({"terminals": engine.TERMINAL_MANAGER.list(include_finished=include_finished)})


@app.get("/api/workspace")
def workspace_get():
    return jsonify({"workspace": str(engine.get_workspace())})


@app.post("/api/workspace")
def workspace_set():
    data = request.get_json(silent=True) or {}
    value = str(data.get("workspace", "")).strip()
    if not value:
        return jsonify({"error": "Workspace path is required."}), 400
    try:
        workspace = engine.set_workspace(value)
        _write_env_values({"WORKSPACE": value})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"workspace": workspace})


@app.get("/api/settings")
def settings_get():
    key = str(os.getenv("GOOGLE_API_KEY") or engine.API_KEY or "").strip()
    nvidia_key = str(os.getenv("NVIDIA_API_KEY") or engine.NVIDIA_API_KEY or "").strip()
    return jsonify({
        "workspace": str(engine.get_workspace()), "model": engine.MODEL,
        "api_key_configured": bool(key), "api_key_suffix": key[-4:] if key else "",
        "nvidia_api_key_configured": bool(nvidia_key), "nvidia_api_key_suffix": nvidia_key[-4:] if nvidia_key else "",
        "requests_per_minute": engine.rate_limiter.limit, "hard_cap": HARD_REQUESTS_PER_MINUTE,
        "retries": engine.API_MAX_RETRIES,
    })


@app.post("/api/settings")
def settings_save():
    data = request.get_json(silent=True) or {}
    updates: dict[str, str] = {}
    key = str(data.get("api_key", "")).strip()
    nvidia_key = str(data.get("nvidia_api_key", "")).strip()
    model = str(data.get("model", engine.MODEL)).strip()
    workspace = str(data.get("workspace", engine.get_workspace())).strip()
    try:
        limit = max(1, min(HARD_REQUESTS_PER_MINUTE, int(data.get("requests_per_minute", engine.rate_limiter.limit))))
        retries = max(0, min(2, int(data.get("retries", engine.API_MAX_RETRIES))))
    except (TypeError, ValueError):
        return jsonify({"error": "Request cap and retry count must be valid integers."}), 400
    if model not in engine.SUPPORTED_MODELS:
        return jsonify({"error": "Choose GLM-5.3-Flash or Gemini 3.5 Flash Lite as the default model."}), 400
    if not workspace:
        return jsonify({"error": "Workspace path is required."}), 400
    if key:
        if len(key) < 12 or any(c.isspace() for c in key):
            return jsonify({"error": "Google API key format looks invalid. Paste the key without spaces."}), 400
        updates["GOOGLE_API_KEY"] = key
    if nvidia_key:
        if len(nvidia_key) < 12 or any(c.isspace() for c in nvidia_key):
            return jsonify({"error": "NVIDIA API key format looks invalid. Paste the key without spaces."}), 400
        updates["NVIDIA_API_KEY"] = nvidia_key
    updates.update({"INTIGNOAI_DEFAULT_MODEL": model, "GEMINI_MAX_REQUESTS_PER_MINUTE": str(limit), "MAX_REQUESTS_PER_MINUTE": str(limit), "GEMINI_MAX_RETRIES": str(retries), "WORKSPACE": workspace})
    try:
        configured_workspace = engine.set_workspace(workspace)
        _write_env_values(updates)
        engine.API_KEY = str(os.getenv("GOOGLE_API_KEY", "")).strip()
        engine.NVIDIA_API_KEY = str(os.getenv("NVIDIA_API_KEY", "")).strip()
        engine.MODEL = model
        engine.API_MAX_RETRIES = retries
        engine.rate_limiter.configure(limit)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({
        "ok": True, "workspace": configured_workspace, "model": model,
        "api_key_configured": bool(os.getenv("GOOGLE_API_KEY") or engine.API_KEY),
        "nvidia_api_key_configured": bool(os.getenv("NVIDIA_API_KEY") or engine.NVIDIA_API_KEY),
        "requests_per_minute": engine.rate_limiter.limit, "hard_cap": HARD_REQUESTS_PER_MINUTE,
        "retries": engine.API_MAX_RETRIES,
    })


@app.route("/api/knowledge", methods=["GET", "POST"])
def knowledge():
    if request.method == "GET":
        return jsonify(read_knowledge(request.args.get("kind", "both"), max_chars=80000))
    data = request.get_json(silent=True) or {}
    kind = str(data.get("kind", "memory")).strip().lower()
    if kind not in {"memory", "skill"}:
        return jsonify({"error": "kind must be 'memory' or 'skill'."}), 400
    try:
        result = append_entry(kind, str(data.get("content", "")), title=str(data.get("title", "")))
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.getenv("PORT", "2000"))
    url_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{url_host}:{port}"

    # Do not spawn a physical browser window automatically. The embedded Agent UI
    # mirrors the headless persistent automation browser inside its Browser panel.
    print("=" * 70)
    print(f"IntignoAI Agent v{engine.VERSION}")
    print(f"Model     : {engine.MODEL}")
    print(f"Workspace : {engine.get_workspace()}")
    print(f"Open      : {url}")
    print("API key   : configured" if (os.getenv("GOOGLE_API_KEY") or engine.API_KEY or os.getenv("NVIDIA_API_KEY") or engine.NVIDIA_API_KEY) else "API key   : not configured (use Settings)")
    print("=" * 70)
    app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)
