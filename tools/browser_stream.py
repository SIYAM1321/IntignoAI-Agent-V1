"""Live embedded-browser streaming (CDP screencast) for IntignoAI Agent v1.9.

Mirrors the persistent agent browser (tools/browser_automation.py) into the web UI:
this module attaches its own independent, read-only CDP connection to the SAME browser
the agent already runs, captures JPEG frames via the CDP Page.startScreencast command,
and feeds them to the SSE consumers exposed by app.py (GET /api/browser/stream/feed).

Design constraints (kept deliberately narrow):
- Read-only mirror: no input injection. The person interacts in the real browser window
  or asks the agent; this module never drives the page.
- browser_automation.py stays the single owner of browser lifecycle: when the persistent
  browser is not running (or has no local automation endpoint), this module starts it
  through browser_automation.perform_browser_task() so profile handling, window mode and
  endpoint discovery remain in one place. Nothing in that module is modified.
- Playwright's sync API is thread-affine, so all capture work runs on one dedicated
  thread ("IntignoAI-BrowserStream") that owns its own sync_playwright() connection.
  Chromium supports several concurrent CDP clients, so this second attach runs beside
  the agent's existing connection without disturbing it.
- Frames are published to per-viewer bounded queues with drop-oldest semantics, so a
  slow viewer always converges to the newest frame instead of buffering lag.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from collections import deque
from logging.handlers import RotatingFileHandler
from typing import Any, Optional

from tools import browser_automation

# ---- Tuning knobs ---------------------------------------------------------------------------
# Mirror pacing: CDP pushes a frame on every repaint (can be 60 fps). We ACK every frame
# but publish at most MAX_FPS per viewer; a viewer that lags silently converges to the
# newest frame because each queue holds only the latest two entries.
MAX_FPS = max(1.0, float(os.getenv("BROWSER_STREAM_MAX_FPS", "12")))
JPEG_QUALITY = max(10, min(95, int(os.getenv("BROWSER_STREAM_QUALITY", "60"))))
MAX_WIDTH = max(320, min(2560, int(os.getenv("BROWSER_STREAM_MAX_WIDTH", "1280"))))
MAX_HEIGHT = max(240, min(2560, int(os.getenv("BROWSER_STREAM_MAX_HEIGHT", "800"))))
PUMP_INTERVAL_MS = 120  # capture-thread tick; also how long CDP events are pumped per cycle

ARTIFACTS_DIR = browser_automation.ARTIFACTS_DIR
LOG_PATH = ARTIFACTS_DIR / "browser_stream.log"

logger = logging.getLogger("intignoai.browser_stream")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("[BrowserStream] %(levelname)s %(message)s"))
    logger.addHandler(console)


class BrowserStreamError(RuntimeError):
    """Expected, reportable browser streaming error."""


class BrowserStreamManager:
    """Captures and distributes Page.startScreencast frames for one agent browser."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop_requested = threading.Event()
        self._generation = 0
        self._state = "idle"  # idle | starting | capturing | stopped | error
        self._error: Optional[str] = None
        self._url_hint: Optional[str] = None
        self._resync_requested = False
        self._resync_hint: Optional[str] = None
        self._subscribers: list[queue.Queue] = []
        self._latest: Optional[str] = None  # pre-serialized SSE frame payload
        self._seq = 0
        self._tab_info: Optional[dict[str, str]] = None
        self._fps: deque[float] = deque(maxlen=128)
        self._last_publish = 0.0
        self._pending_publish = False
        self._min_frame_interval = 1.0 / MAX_FPS
        # v1.9: manual interaction forwarded from the embedded Browser panel. Commands are
        # drained by the capture thread and executed on ITS CDP session (the same thread
        # that owns the connection), so injection never crosses Playwright threads.
        self._input_queue: "queue.Queue" = queue.Queue()

    # ---- Public API (called from Flask request threads) ------------------------------------

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            recent = sum(1 for stamp in self._fps if now - stamp <= 4.0)
            return {
                "ok": True,
                "state": self._state,
                "capturing": self._state == "capturing",
                "fps": round(recent / 4.0, 1),
                "viewers": len(self._subscribers),
                "tab": dict(self._tab_info) if self._tab_info else None,
                "error": self._error,
            }

    def start(self, url_hint: Optional[str] = None) -> dict[str, Any]:
        """Ensure the agent browser is reachable, then start capturing. Idempotent."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                status = self.status()
                status.update({"ok": True, "already_running": True})
                return status
        readiness = self._ensure_browser_endpoint()
        if not readiness.get("ok"):
            return {**readiness, "state": self._state, "capturing": False}
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                status = self.status()
                status.update({"ok": True, "already_running": True})
                return status
            self._generation += 1
            self._stop_requested = threading.Event()
            self._state = "starting"
            self._error = None
            self._url_hint = url_hint
            self._latest = None
            self._seq = 0
            self._tab_info = None
            self._fps.clear()
            self._last_publish = 0.0
            self._pending_publish = False
            stop_event = self._stop_requested
            self._thread = threading.Thread(
                target=self._run, args=(url_hint, stop_event), name="IntignoAI-BrowserStream", daemon=True
            )
            self._thread.start()
        logger.info("Browser stream start requested (hint=%r)", url_hint)
        status = self.status()
        status.update({"ok": True})
        return status

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._stop_requested.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=4.0)
        logger.info("Browser stream stopped by request")
        return self.status()

    def resync(self, url_hint: Optional[str] = None) -> dict[str, Any]:
        """Follow the tab the agent is currently using (or the given URL substring)."""
        with self._lock:
            capturing = self._state == "capturing"
            self._resync_hint = url_hint
            self._resync_requested = True
        status = self.status()
        status["ok"] = capturing
        if not capturing:
            status["error"] = "The stream is not running."
        return status

    def subscribe(self) -> queue.Queue:
        """Register an SSE consumer; returns a queue of serialized frame payloads."""
        viewer: queue.Queue = queue.Queue(maxsize=2)
        with self._lock:
            self._subscribers.append(viewer)
            latest = self._latest
            state = self._state
        if latest:
            self._offer(viewer, latest)
        if state in ("stopped", "error"):
            self._offer(viewer, None)
        return viewer

    def unsubscribe(self, viewer: queue.Queue) -> None:
        with self._lock:
            try:
                self._subscribers.remove(viewer)
            except ValueError:
                pass

    def latest_frame(self) -> Optional[str]:
        with self._lock:
            return self._latest

    def dispatch_input(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Queue one manual-interaction command (click/wheel/keys/navigate/new_tab/switch)."""
        kind = str((payload or {}).get("type", "")).strip().lower()
        allowed = {"click", "dblclick", "move", "wheel", "keydown", "keyup", "press", "type", "navigate", "new_tab", "switch_tab"}
        if kind not in allowed:
            return {"ok": False, "error": f"Unsupported input type '{kind}'. Allowed: {sorted(allowed)}"}
        if kind in {"click", "dblclick", "move", "wheel"}:
            try:
                float(payload.get("x"))
                float(payload.get("y"))
            except (TypeError, ValueError):
                return {"ok": False, "error": "Pointer input needs numeric x and y (viewport CSS pixels)."}
        if kind == "navigate":
            url = str(payload.get("url", "")).strip()
            if not url.lower().startswith(("http://", "https://")):
                return {"ok": False, "error": "navigate needs an http(s) URL."}
        self._input_queue.put(dict(payload))
        return {"ok": True, "queued": True, "type": kind}

    # ---- Browser readiness ------------------------------------------------------------------

    def _live_devtools_port(self) -> Optional[int]:
        try:
            profile_path = browser_automation._profile_path("default")
            return browser_automation._read_devtools_port(profile_path)
        except Exception as exc:
            logger.debug("Devtools endpoint discovery failed: %s", exc)
            return None

    def _ensure_browser_endpoint(self) -> dict[str, Any]:
        if self._live_devtools_port() is not None:
            return {"ok": True}
        # The browser is not reachable through its automation endpoint. Route the launch
        # through the automation module so window mode/profile handling stays consistent.
        try:
            if browser_automation._browser_commands.unfinished_tasks:
                return {"ok": False, "error": "The agent is currently running a browser task; streaming can start once it finishes."}
        except Exception:
            pass
        boot = browser_automation.perform_browser_task({"task": "action", "actions": [], "timeout_ms": 20000})
        if not boot.get("ok"):
            failure = {
                "ok": False,
                "error": boot.get("error") or "The agent browser could not be started for streaming.",
            }
            for key in ("human_action_required", "human_action", "message"):
                if boot.get(key):
                    failure[key] = boot[key]
            return failure
        if self._live_devtools_port() is None:
            return {
                "ok": False,
                "error": "The browser is running without a local automation endpoint (headless or manual-only mode), so it cannot be mirrored. Close it and press Start stream again; it will reopen as an agent-attached window.",
            }
        return {"ok": True}

    # ---- Capture thread ---------------------------------------------------------------------

    def _run(self, url_hint: Optional[str], stop_event: threading.Event) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            self._finish_with_error(f"Playwright is not installed, so the browser cannot be mirrored: {exc}")
            return
        with sync_playwright() as playwright:
            attempts = 0
            outcome = "stopped"
            while not stop_event.is_set():
                outcome = self._capture(playwright, url_hint, stop_event)
                if outcome in ("stopped", "error"):
                    break
                # "browser_gone": give a short grace period in case the browser is restarting.
                attempts += 1
                if attempts > 3:
                    self._finish_with_error(
                        "The agent browser is no longer reachable, so the stream ended. Press Start stream to relaunch and resume."
                    )
                    return
                logger.info("Browser not reachable; retrying attach (%d/3)", attempts)
                stop_event.wait(1.5)
        # Finalize on every exit path that is not already an error: viewers must always
        # receive an end signal instead of waiting on a silent connection.
        with self._lock:
            if self._state in ("capturing", "starting"):
                self._state = "stopped"
        self._notify_ended()

    def _capture(self, playwright, url_hint: Optional[str], stop_event: threading.Event) -> str:
        """Own one CDP connection until it dies or the stream is stopped.

        Returns "stopped", "error", or "browser_gone" (connection/browser lost).
        """
        port = self._live_devtools_port()
        if port is None:
            return "browser_gone"
        try:
            browser = self._connect_cdp(playwright, port)
        except Exception as exc:
            self._finish_with_error(f"Could not attach to the agent browser over CDP: {exc}")
            return "error"
        logger.info("Attached to agent browser over CDP port %d", port)
        context = None
        cdp = None
        page = None
        try:
            contexts = browser.contexts
            context = contexts[0] if contexts else browser.new_context()
            page = self._pick_page(context, url_hint)
            if page is None:
                try:
                    page = context.new_page()
                except Exception:
                    page = None
            if page is None:
                self._finish_with_error("No browser tab is open to mirror.")
                return "error"
            current_url: Optional[str] = None
            while not stop_event.is_set():
                if cdp is None:
                    cdp = self._attach_screencast(context, page)
                    if cdp is None:
                        return "browser_gone"
                    self._set_state("capturing")
                # Pump CDP events (screencast frames arrive via the handler) for one tick.
                try:
                    page.wait_for_timeout(PUMP_INTERVAL_MS)
                except Exception:
                    page = self._pick_page(context, None)
                    if page is None:
                        return "browser_gone"
                    cdp = self._detach_screencast(cdp)
                    continue
                resync_hint = None
                with self._lock:
                    if self._resync_requested:
                        self._resync_requested = False
                        resync_hint = self._resync_hint
                closed = True
                try:
                    closed = page.is_closed()
                except Exception:
                    closed = True
                if resync_hint is not None or closed:
                    chosen = self._pick_page(context, resync_hint)
                    if chosen is None:
                        return "browser_gone"
                    if chosen is not page:
                        page = chosen
                        cdp = self._detach_screencast(cdp)
                        continue
                try:
                    url = page.url
                    if url != current_url:
                        current_url = url
                        title = ""
                        try:
                            title = page.title()
                        except Exception:
                            title = ""
                        with self._lock:
                            self._tab_info = {"url": url, "title": title}
                            self._pending_publish = True  # surface the first frame of a new page quickly
                except Exception:
                    pass
                with self._lock:
                    pending = self._pending_publish and self._latest
                    if pending:
                        self._pending_publish = False
                        payload = self._latest
                if pending and payload:
                    self._distribute(payload)
                # v1.9: drain manual-interaction commands sent from the Browser panel.
                while True:
                    try:
                        command = self._input_queue.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        self._execute_input(cdp, page, browser, command)
                    except Exception as exc:
                        logger.debug("Manual input command failed: %s", exc)
            return "stopped"
        except Exception as exc:
            if stop_event.is_set():
                return "stopped"
            self._finish_with_error(f"Browser stream failed: {exc}")
            return "error"
        finally:
            self._detach_screencast(cdp)
            try:
                browser.close()  # for a connect_over_cdp attach this only disconnects
            except Exception:
                pass

    def _connect_cdp(self, playwright, port: int):
        endpoint = f"http://127.0.0.1:{port}"
        try:
            return playwright.chromium.connect_over_cdp(endpoint, timeout=15000, no_defaults=True)
        except TypeError:
            # Older Playwright builds do not know the no_defaults kwarg.
            return playwright.chromium.connect_over_cdp(endpoint, timeout=15000)

    def _attach_screencast(self, context, page):
        try:
            cdp = context.new_cdp_session(page)
        except Exception:
            return None
        # Register the handler BEFORE starting the cast so early frames are not missed.
        cdp.on("Page.screencastFrame", lambda frame, session=cdp: self._on_screencast_frame(session, frame))
        try:
            cdp.send("Page.enable")
        except Exception:
            pass
        try:
            cdp.send("Page.startScreencast", {
                "format": "jpeg",
                "quality": JPEG_QUALITY,
                "maxWidth": MAX_WIDTH,
                "maxHeight": MAX_HEIGHT,
            })
        except Exception:
            return None
        return cdp

    def _detach_screencast(self, cdp):
        if cdp is None:
            return None
        try:
            cdp.send("Page.stopScreencast")
        except Exception:
            pass
        try:
            cdp.detach()
        except Exception:
            pass
        return None

    def _execute_input(self, cdp, page, browser, command: dict[str, Any]) -> None:
        """Run one manual-interaction command on the capture thread's own CDP session."""
        kind = str(command.get("type", "")).strip().lower()
        if kind in {"click", "dblclick", "move", "wheel"}:
            x = float(command.get("x", 0))
            y = float(command.get("y", 0))
            if kind == "wheel":
                cdp.send("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": x, "y": y,
                                                      "deltaX": float(command.get("deltaX", 0) or 0),
                                                      "deltaY": float(command.get("deltaY", 0) or 0)})
                return
            button = str(command.get("button", "left") or "left")
            if kind == "move":
                cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y, "buttons": 0})
                return
            count = 2 if kind == "dblclick" else 1
            cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": button, "clickCount": count})
            cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": button, "clickCount": count})
            return
        if kind in {"keydown", "keyup", "press", "type"}:
            text = str(command.get("text", "") or "")
            key = str(command.get("key", "") or "")
            code = str(command.get("code", "") or key)
            try:
                vk = int(command.get("keyCode", 0) or 0)
            except (TypeError, ValueError):
                vk = 0
            if kind == "type" and text:
                cdp.send("Input.insertText", {"text": text})
            elif kind == "press" and key:
                down = {"type": "rawKeyDown", "key": key, "code": code, "windowsVirtualKeyCode": vk}
                if len(key) == 1:
                    down = {"type": "keyDown", "key": key, "code": code, "windowsVirtualKeyCode": vk, "text": key}
                up = {"type": "keyUp", "key": key, "code": code, "windowsVirtualKeyCode": vk}
                cdp.send("Input.dispatchKeyEvent", down)
                cdp.send("Input.dispatchKeyEvent", up)
            elif kind == "keydown" and key:
                params = {"type": "keyDown" if (len(key) == 1 and text) else "rawKeyDown", "key": key, "code": code, "windowsVirtualKeyCode": vk}
                if text:
                    params["text"] = text
                cdp.send("Input.dispatchKeyEvent", params)
            elif kind == "keyup" and key:
                cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": key, "code": code, "windowsVirtualKeyCode": vk})
            return
        if kind == "navigate":
            url = str(command.get("url", "")).strip()
            if url.lower().startswith(("http://", "https://")):
                cdp.send("Page.navigate", {"url": url})
            return
        if kind == "new_tab":
            url = str(command.get("url", "about:blank"))
            try:
                browser_cdp = browser.new_browser_cdp_session()
                target_url = url if url.lower().startswith(("http://", "https://", "about:")) else "about:blank"
                browser_cdp.send("Target.createTarget", {"url": target_url})
            except Exception as exc:
                logger.debug("Could not open a new tab for the viewer: %s", exc)
            return
        if kind == "switch_tab":
            hint = str(command.get("url", command.get("hint", "")) or "").strip() or None
            with self._lock:
                self._resync_hint = hint
                self._resync_requested = True
            return

    def _pick_page(self, context, url_hint: Optional[str]):
        """Choose which tab to mirror: URL hint, then the agent's active tab, then a web page."""
        try:
            pages = [tab for tab in context.pages if not tab.is_closed()]
        except Exception:
            return None
        if not pages:
            return None
        hint = str(url_hint or "").strip().lower()
        if hint:
            for tab in pages:
                try:
                    if hint in str(tab.url or "").lower():
                        return tab
                except Exception:
                    continue
        agent_url = self._agent_active_url()
        if agent_url:
            for tab in pages:
                try:
                    if str(tab.url or "") == agent_url:
                        return tab
                except Exception:
                    continue
        for tab in pages:  # prefer real web content over blank/new-tab targets
            try:
                if str(tab.url or "").startswith(("http://", "https://")):
                    return tab
            except Exception:
                continue
        return pages[-1]

    @staticmethod
    def _agent_active_url() -> Optional[str]:
        """Best-effort read of the agent's currently active tab URL (local property reads only)."""
        try:
            session = browser_automation._browser_sessions.get("default")
            page = session.get("page") if isinstance(session, dict) else None
            if page is not None and not page.is_closed():
                return str(page.url or "")
        except Exception:
            return None
        return None

    # ---- Frame handling ---------------------------------------------------------------------

    def _on_screencast_frame(self, session, frame: dict[str, Any]) -> None:
        try:
            ack_id = frame.get("sessionId")
            if ack_id is not None:
                try:
                    # CDP pauses the cast when frames are not acknowledged.
                    session.send("Page.screencastFrameAck", {"sessionId": ack_id})
                except Exception:
                    pass
            image = frame.get("data")
            if not image:
                return
            now = time.monotonic()
            with self._lock:
                self._seq += 1
                record = {
                    "seq": self._seq,
                    "image": image,
                    "metadata": frame.get("metadata") or {},
                    "ts": round(time.time(), 3),
                }
                payload = json.dumps(record, separators=(",", ":"))
                self._latest = payload
                self._fps.append(now)
                publish_now = (now - self._last_publish) >= self._min_frame_interval
                if publish_now:
                    self._last_publish = now
                    self._pending_publish = False
                else:
                    self._pending_publish = True  # flush the newest frame from the pump tick
            if publish_now:
                self._distribute(payload)
        except Exception as exc:
            logger.debug("Screencast frame handling failed: %s", exc)

    def _distribute(self, payload: str) -> None:
        with self._lock:
            viewers = list(self._subscribers)
        for viewer in viewers:
            self._offer(viewer, payload)

    def _notify_ended(self) -> None:
        with self._lock:
            viewers = list(self._subscribers)
        for viewer in viewers:
            self._offer(viewer, None)

    @staticmethod
    def _offer(viewer: queue.Queue, item: Optional[str]) -> None:
        """Drop-oldest enqueue: a slow viewer keeps only the freshest entries."""
        try:
            if viewer.full():
                try:
                    viewer.get_nowait()
                except queue.Empty:
                    pass
            viewer.put_nowait(item)
        except Exception:
            pass

    # ---- State helpers ----------------------------------------------------------------------

    def _set_state(self, state: str, *, error: Optional[str] = None) -> None:
        with self._lock:
            self._state = state
            if error is not None:
                self._error = error

    def _finish_with_error(self, message: str) -> None:
        logger.error("%s", message)
        self._set_state("error", error=message)
        self._notify_ended()


def handle_stream_task(mode: str, payload: dict[str, Any], session: Any = None) -> dict[str, Any]:
    """Agent-side entry point (routed by browser_automation STREAM_TASKS on the worker thread).

    The Manager owns its own CDP connection, so the live browser session object is accepted
    for interface compatibility but not required.
    """
    if mode == "stream_start":
        return MANAGER.start(url_hint=str((payload or {}).get("url", "")).strip() or None)
    if mode == "stream_stop":
        return MANAGER.stop()
    if mode == "stream_input":
        return MANAGER.dispatch_input(payload or {})
    if mode == "stream_navigate":
        return MANAGER.dispatch_input({"type": "navigate", "url": (payload or {}).get("url", "")})
    if mode == "stream_switch":
        return MANAGER.resync(url_hint=str((payload or {}).get("url", (payload or {}).get("tab", ""))).strip() or None)
    if mode == "stream_pump":
        # The Manager pumps its own CDP connection; nothing to do on the agent worker.
        return {"ok": True, "state": MANAGER.status().get("state")}
    return {"ok": False, "error": f"Unknown stream task '{mode}'."}


# Single shared instance: app.py endpoints and the capture thread all use it.
MANAGER = BrowserStreamManager()
