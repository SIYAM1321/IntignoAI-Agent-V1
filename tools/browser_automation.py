"""Persistent Playwright browser automation for IntignoAI Agent v1.6.

All browser data is stored in one persistent profile under data/browser_profiles/default.
Multiple tabs are supported inside that same profile. Browser artifacts/logs are stored
under data/browser_artifacts. No physical OS mouse is used.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import threading
import time
import queue
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
_profile_dir_env = Path(os.getenv("BROWSER_PROFILE_DIR", str(DATA_DIR / "browser_profiles"))).expanduser()
PROFILES_DIR = (_profile_dir_env if _profile_dir_env.is_absolute() else ROOT / _profile_dir_env).resolve()
ARTIFACTS_DIR = DATA_DIR / "browser_artifacts"
LOG_PATH = ARTIFACTS_DIR / "browser_automation.log"
ERROR_SCREENSHOT = ARTIFACTS_DIR / "error.png"
DEFAULT_HEADLESS = os.getenv("BROWSER_HEADLESS", "false").strip().lower() not in {"0", "false", "no", "off"}
MAX_ACTIONS = 80
MAX_TEXT_CHARS = 30000
MAX_TIMEOUT_MS = 120000
# Navigation gets its own generous budget: slow hosts and bot-check interstitials routinely
# exceed a short action timeout. Timeouts must degrade gracefully instead of triggering a
# goto-retry storm that aborts and restarts the same page load forever (the classic
# "page loads halfway, spinner spins forever" agent-mode loop).
DEFAULT_NAV_TIMEOUT_MS = max(10000, int(os.getenv("BROWSER_NAV_TIMEOUT_MS", "45000")))
# How long a detected CAPTCHA/anti-bot interstitial is watched for self-clearing before the
# agent reports human_action_required. Invisible managed challenges usually clear on a real,
# visible browser without any interaction.
CHALLENGE_WAIT_SECONDS = max(0.0, float(os.getenv("BROWSER_CHALLENGE_WAIT_SECONDS", "25")))
# The same URL may not be re-requested more often than this; re-goto aborts the in-flight load.
NAV_RETRY_COOLDOWN_SECONDS = 12.0
# v1.9 embedded-browser tasks (real browser streamed into the Agent UI via CDP screencast).
# Implemented in tools/browser_stream.py; routed here on the browser worker thread.
STREAM_TASKS = {"stream_start", "stream_stop", "stream_pump", "stream_input", "stream_navigate", "stream_switch"}

ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
PROFILES_DIR.mkdir(parents=True, exist_ok=True)
logger = logging.getLogger("intignoai.browser")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("[Browser] %(levelname)s %(message)s"))
    logger.addHandler(console)

_locks_guard = threading.Lock()
_profile_locks: dict[str, threading.RLock] = {}
# Playwright's sync API is thread-affine; every live browser session is therefore
# owned by one dedicated worker thread and reused for later tool calls.
_browser_sessions: dict[str, dict[str, Any]] = {}
_browser_commands: "queue.Queue[tuple[dict[str, Any], threading.Event, list[dict[str, Any]]]]" = queue.Queue()
_browser_worker_lock = threading.Lock()
_browser_worker_thread: threading.Thread | None = None


class BrowserAutomationError(RuntimeError):
    """Expected, reportable browser action error."""



def _find_native_browser() -> str | None:
    """Locate an installed full browser so the user gets a normal, interactive window.

    The browser is always launched with IntignoAI's dedicated persistent profile, never
    the user's ordinary Chrome User Data directory. The agent attaches over localhost CDP.
    """
    configured = str(os.getenv("BROWSER_EXECUTABLE", "")).strip().strip('"')
    candidates: list[str] = []
    if configured:
        candidates.append(configured)
    channel = str(os.getenv("BROWSER_CHANNEL", "chrome")).strip().lower()
    if os.name == "nt":
        roots = [
            os.getenv("PROGRAMFILES", r"C:\Program Files"),
            os.getenv("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            os.getenv("LOCALAPPDATA", ""),
        ]
        if channel in {"msedge", "edge", "microsoft-edge"}:
            rel = [r"Microsoft\Edge\Application\msedge.exe"]
        else:
            rel = [r"Google\Chrome\Application\chrome.exe", r"Chromium\Application\chrome.exe"]
        candidates.extend(str(Path(root) / item) for root in roots if root for item in rel)
        candidates.extend([shutil.which("chrome") or "", shutil.which("msedge") or ""])
    elif sys.platform == "darwin":
        candidates.extend([
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            shutil.which("google-chrome") or "", shutil.which("chromium") or "",
        ])
    else:
        names = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge", "msedge")
        candidates.extend(filter(None, (shutil.which(name) for name in names)))
    for candidate in candidates:
        try:
            if candidate and Path(candidate).expanduser().is_file():
                return str(Path(candidate).expanduser().resolve())
        except OSError:
            continue
    return None


# Native browser processes started by IntignoAI, keyed by profile slug. Kept at module level so a
# browser that was opened for manual sign-in survives agent detach/attach cycles.
_native_procs: dict[str, dict[str, Any]] = {}
GOOGLE_AUTH_HOSTS = {"accounts.google.com"}


def _embedded_ui_enabled() -> bool:
    """True when the browser must live ONLY inside the IntignoAI Web UI (no OS window)."""
    return str(os.getenv("BROWSER_EMBEDDED_UI", "true")).strip().lower() not in {"0", "false", "no", "off"}


def _is_google_auth_url(value: Any) -> bool:
    """True for Google's account sign-in host, where the user must sign in by hand."""
    try:
        return (urlsplit(str(value or "").strip()).hostname or "").lower() in GOOGLE_AUTH_HOSTS
    except ValueError:
        return False


def _bundled_chromium_path(playwright=None) -> str | None:
    """Path of Playwright's bundled Chromium, used as a plain browser when Chrome/Edge is absent."""
    try:
        if playwright is not None:
            candidate = Path(playwright.chromium.executable_path)
        else:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                candidate = Path(pw.chromium.executable_path)
        return str(candidate) if candidate.is_file() else None
    except Exception as exc:
        logger.debug("Bundled Chromium path unavailable: %s", exc)
        return None


def _spawn_native_browser(executable: str, profile_path: Path, extension_paths: list[str], *, debug_port: bool = True, url: str | None = None, headless: bool = False):
    """Start a normal, interactive browser window on the dedicated profile.

    With debug_port=False the browser has no automation endpoint at all (a completely
    ordinary browser) so the person can sign in to anything, including Google, by hand.
    With headless=True (embedded mode) the browser runs invisibly BUT still exposes the
    debug endpoint, so the Browser tab can mirror the real page over CDP.
    """
    profile_path.mkdir(parents=True, exist_ok=True)
    args = [executable, f"--user-data-dir={profile_path}"]
    # Chromium refuses to start as root without this flag (common in containerized
    # deployments and CI). Keep normal desktop launches unchanged.
    if os.name != "nt":
        try:
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                args.append("--no-sandbox")
        except OSError:
            pass
    if debug_port:
        args += ["--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1"]
    args += ["--no-first-run", "--no-default-browser-check", "--new-window"]
    if headless:
        args += ["--headless=new", "--window-size=1440,900", "--hide-scrollbars"]
    # Do not use --disable-extensions-except: the user keeps the full extension manager.
    if extension_paths:
        args.append("--load-extension=" + ",".join(extension_paths))
    if url:
        args.append(url)
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen(
        args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=creationflags, close_fds=(os.name != "nt"),
    )


def _devtools_open_url(port: int, url: str) -> bool:
    """Open a URL in a new tab through the browser's own HTTP endpoint (no Playwright attached)."""
    from urllib.parse import quote
    for method in ("PUT", "GET"):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?{quote(url, safe=':/?&=%#@+,;~')}", method=method)
            with urllib.request.urlopen(req, timeout=3) as response:
                if response.status == 200:
                    return True
        except (OSError, urllib.error.URLError, TimeoutError):
            continue
    return False


def _native_proc_alive(slug: str) -> bool:
    info = _native_procs.get(slug)
    proc = info.get("process") if info else None
    return bool(proc is not None and proc.poll() is None)


def _graceful_terminate(proc, timeout: float = 8.0) -> bool:
    """Ask the browser to quit so it flushes cookies/session to disk; force-kill only as a last resort."""
    if proc is None or proc.poll() is not None:
        return True
    try:
        if os.name == "nt":
            # Without /F, taskkill sends WM_CLOSE: the same as the user closing the window.
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5, check=False)
        else:
            proc.terminate()
    except Exception:
        pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.2)
    return False


def _terminate_native_proc(slug: str) -> bool:
    info = _native_procs.pop(slug, None)
    proc = info.get("process") if info else None
    if proc is None or proc.poll() is not None:
        return False
    if not _graceful_terminate(proc):
        try: proc.kill()
        except Exception: pass
    return True


def _cdp_browser_close(browser) -> bool:
    """Gracefully quit a CDP-attached browser (Browser.close), which saves the profile."""
    try:
        session = browser.new_browser_cdp_session()
        session.send("Browser.close")
        return True
    except Exception as exc:
        logger.debug("Browser.close over CDP failed: %s", exc)
        return False


def _wait_profile_free(profile_path: Path, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _read_devtools_port(profile_path) is None:
            try: (profile_path / "DevToolsActivePort").unlink(missing_ok=True)
            except OSError: pass
            return True
        time.sleep(0.3)
    return False


def _shutdown_debug_browser(profile_path: Path, slug: str = "default") -> bool:
    """Close any agent-attachable (debug-port) browser on this profile gracefully. True when none remains."""
    session = _browser_sessions.get(slug)
    if session is not None:
        if session.get("connection_mode") == "native_chrome_cdp" and session.get("browser") is not None:
            _cdp_browser_close(session["browser"])
        _detach_browser_session(slug)
    elif _read_devtools_port(profile_path) is not None:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                port = _read_devtools_port(profile_path)
                if port:
                    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=8000, no_defaults=True)
                    _cdp_browser_close(browser)
        except Exception as exc:
            logger.debug("Could not close the debug browser through CDP: %s", exc)
    if _wait_profile_free(profile_path, 6.0):
        return True
    if _native_proc_alive(slug):
        _terminate_native_proc(slug)
        return _wait_profile_free(profile_path, 4.0)
    return False


def _stop_pure_browser(slug: str = "default") -> bool:
    """Quit a manual-only browser (we cannot reach it over CDP) gracefully so the session is saved."""
    if not _native_proc_alive(slug):
        _native_procs.pop(slug, None)
        return True
    ok = _graceful_terminate(_native_procs[slug]["process"], 10.0)
    if ok:
        _native_procs.pop(slug, None)
        time.sleep(0.8)
    return ok


def _read_devtools_port(profile_path: Path) -> int | None:
    port_file = profile_path / "DevToolsActivePort"
    try:
        lines = port_file.read_text(encoding="utf-8").splitlines()
        port = int(lines[0].strip())
        if 1 <= port <= 65535:
            # Validate that the file belongs to a live browser rather than a stale run.
            req = urllib.request.Request(f"http://127.0.0.1:{port}/json/version", method="GET")
            with urllib.request.urlopen(req, timeout=0.45) as response:
                if response.status == 200:
                    return port
    except (OSError, ValueError, IndexError, urllib.error.URLError, TimeoutError):
        return None
    return None


def _launch_native_chrome(playwright, profile_path: Path, extension_paths: list[str], headless: bool = False):
    """Start (or reuse) a normal Chrome/Edge window and attach Playwright via CDP.

    With headless=True the browser runs invisibly with the debug endpoint enabled so the
    embedded Browser tab can mirror it (embedded mode requirement).

    Returns (browser, context, process, created_by_agent), or None when no usable browser
    executable exists so the caller can fall back to Playwright's own launch.
    """
    slug = "default"
    executable = _find_native_browser() or _bundled_chromium_path(playwright)
    if not executable:
        return None
    profile_path.mkdir(parents=True, exist_ok=True)
    active_port = _read_devtools_port(profile_path)
    process = None
    created_by_agent = False
    if active_port is None and _native_procs.get(slug, {}).get("pure") and _native_proc_alive(slug) and not _stop_pure_browser(slug):
        raise BrowserAutomationError(
            "The browser is still open in manual-only mode (no agent connection) and did not close by itself. Close that "
            "browser window yourself so your sign-in is saved, then say 'continue' and the agent will reopen the same profile with your session."
        )
    if active_port is None:
        # Clear only a stale endpoint file. Do not delete profile cookies/history/extensions.
        try:
            (profile_path / "DevToolsActivePort").unlink(missing_ok=True)
        except OSError:
            pass
        try:
            process = _spawn_native_browser(executable, profile_path, extension_paths, debug_port=True, headless=headless)
            created_by_agent = True
            _native_procs[slug] = {"process": process, "pure": False}
        except (OSError, ValueError) as exc:
            logger.warning("Could not launch installed browser %s; using bundled browser if possible: %s", executable, exc)
            return None
        deadline = time.monotonic() + max(4.0, float(os.getenv("BROWSER_START_TIMEOUT_SECONDS", "18")))
        while time.monotonic() < deadline:
            active_port = _read_devtools_port(profile_path)
            if active_port:
                break
            if process.poll() is not None:
                raise BrowserAutomationError(
                    "The browser exited before its local endpoint became ready. If a normal window using the IntignoAI "
                    "profile is open, close it once (your sign-in stays saved), then try again."
                )
            time.sleep(0.2)
        if active_port is None:
            _terminate_native_proc(slug)
            raise BrowserAutomationError(
                "The browser did not expose its local endpoint. The profile may still be open in an older window. "
                "Close the old IntignoAI browser window once and try again."
            )
    try:
        try:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{active_port}", timeout=15000, no_defaults=True)
        except TypeError:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{active_port}", timeout=15000)
        contexts = browser.contexts
        if not contexts:
            try: browser.close()
            except Exception: pass
            raise BrowserAutomationError("The browser connected over CDP but exposed no persistent browser context.")
        return browser, contexts[0], process, created_by_agent
    except Exception:
        if created_by_agent:
            _terminate_native_proc(slug)
        raise


def _detach_browser_session(profile: str) -> bool:
    """Disconnect the agent but leave the user's browser window open and untouched."""
    session = _browser_sessions.pop(profile, None)
    if not session:
        return False
    if session.get("connection_mode") == "native_chrome_cdp":
        try:
            browser = session.get("browser")
            if browser is not None:
                browser.close()  # for a CDP connection this only disconnects
        except Exception as exc:
            logger.debug("Could not disconnect native browser cleanly: %s", exc)
    else:
        # Playwright-launched fallback cannot be handed over; close it so the profile unlocks.
        try:
            session["context"].close()
        except Exception as exc:
            logger.warning("Failed to close fallback browser context: %s", exc)
    try:
        session["playwright"].stop()
    except Exception as exc:
        logger.warning("Failed to stop Playwright after detach: %s", exc)
    return True


MANUAL_SIGNIN_MESSAGE = (
    "The requested sign-in page is open in IntignoAI's embedded Browser tab (no physical window is ever opened). "
    "For Google the agent can sign in itself with google_login when you give it the email and password in your message; "
    "it never bypasses CAPTCHA/MFA/security checks, so complete any such step in the embedded view and then say 'continue'. "
    "Some providers may reject headless/automated browser sessions, so embedded sign-in cannot be guaranteed on every site. "
    "Do not type passwords into chat."
)


def _manual_browser_open(profile_path: Path, extension_paths: list[str], url: str | None = None, pure: bool = True) -> dict[str, Any]:
    """Open the dedicated profile as an ordinary browser with nothing attached and no debug port.

    Google rejects sign-in from browsers that expose automation (including a remote-debugging port), so the
    manual window is a completely normal browser. The agent reattaches later by restarting this same profile.
    """
    slug = "default"
    target = _validate_url(url) if url else "https://accounts.google.com/"
    # A manual-only window is already open: hand the URL to it (Chrome opens a new tab in that window).
    already_pure = bool(_native_procs.get(slug, {}).get("pure")) and _native_proc_alive(slug)
    executable = _find_native_browser()
    bundled = False
    if not executable:
        executable = _bundled_chromium_path()
        bundled = True
    if not executable:
        return {"ok": False, "error": "No Chrome/Edge executable was found. Install Google Chrome (recommended for Google sign-in), or run start.bat/start.sh to download the bundled Chromium."}
    if not already_pure:
        if not _shutdown_debug_browser(profile_path, slug):
            return {"ok": False, "agent_connected": False, "error": "A browser window using the IntignoAI profile is still open and did not close. Close it yourself once (nothing is lost), then click the browser button again."}
        try: (profile_path / "DevToolsActivePort").unlink(missing_ok=True)
        except OSError: pass
    # Plain launch: no debug port and no --load-extension, to keep it indistinguishable from a normal browser.
    process = _spawn_native_browser(executable, profile_path, [], debug_port=False, url=target)
    if not already_pure:
        _native_procs[slug] = {"process": process, "pure": True}
    result: dict[str, Any] = {"ok": True, "mode": "manual_pure", "opened_url": target, "agent_connected": False,
            "human_action_required": True, "human_action": "manual_signin", "message": MANUAL_SIGNIN_MESSAGE}
    if bundled:
        result["warning"] = ("Only the bundled Chromium was found. Google often refuses sign-in in Chromium builds; install Google Chrome "
                             "(or Edge) for reliable Google login.")
    return result


def _browser_is_human_verification(page) -> dict[str, Any] | None:
    """Detect human verification/provider security blocks without trying to defeat them."""
    try:
        url = str(page.url or "")
    except Exception:
        url = ""
    snippets: list[str] = []
    try:
        snippets.append(page.locator("body").inner_text(timeout=1000)[:12000])
    except Exception:
        pass
    try:
        frames = list(page.frames)[1:10]
        for frame in frames:
            try:
                snippets.append(str(frame.url or ""))
                snippets.append(frame.locator("body").inner_text(timeout=500)[:1500])
            except Exception:
                continue
    except Exception:
        pass
    text = " ".join(snippets).lower()
    joined = (url + " " + text).lower()
    if "this browser or app may not be secure" in joined or ("accounts.google.com" in url and "/signin/rejected" in url):
        return {
            "kind": "provider_security_block",
            "message": "Google rejected this sign-in attempt because it saw an automated browser. Do not retry credentials or try to bypass the block. IntignoAI hands the browser to you so you can sign in manually.",
        }
    # v1.13.4: LinkedIn (and many modern sites) embed INVISIBLE reCAPTCHA Enterprise badges and
    # passive challenge-platform scripts on every page. Those never block interaction, so they
    # must NOT be reported as a human-verification wall. Only a VISIBLE, interactive challenge
    # widget or an actual challenge interstitial counts from now on.
    def _captcha_widget_blocks(frame) -> bool:
        try:
            frame_url = str(frame.url or "")
        except Exception:
            return False
        if not re.search(r"captcha|recaptcha|hcaptcha|challenge-platform|turnstile", frame_url, re.I):
            return False
        # Invisible badge/anchor frames and passive script/webworker frames never block a page.
        if re.search(r"size=invisible|/recaptcha/enterprise/(?:anchor|api2?/anchor)|/recaptcha/api2/(?:anchor|logo)|/recaptcha/(?:enterprise/)?(?:api|webworker)", frame_url, re.I):
            return False
        try:
            element = frame.frame_element()
            if not element.is_visible():
                return False
            box = element.bounding_box()
            if not box or box.get("height", 0) < 60 or box.get("width", 0) < 200:
                return False
        except Exception:
            return False
        return True

    blocking_widget = False
    try:
        blocking_widget = any(_captcha_widget_blocks(fr) for fr in list(page.frames)[1:])
    except Exception:
        pass
    captcha_words = ("verify you are human", "verify that you are human", "confirm you are human", "are you a robot", "i'm not a robot", "im not a robot", "complete the captcha", "solve the captcha", "unusual traffic", "just a moment", "checking your browser", "attention required")
    word_hit = any(word in text for word in captcha_words)
    main_text_len = len(snippets[0].strip()) if snippets else 0
    content_rich = main_text_len > 600
    # A challenge wall REPLACES the page content (small body with challenge wording); a normal
    # content-rich page (e.g. LinkedIn job details with its background badge) is NOT blocked.
    challenge_wall = word_hit and not content_rich
    if (blocking_widget and (challenge_wall or word_hit)) or challenge_wall:
        return {
            "kind": "captcha_or_human_check",
            "message": "Please complete the CAPTCHA/human verification manually in the visible browser, then tell IntignoAI to continue. The agent will resume in the same tab/profile; it does not solve or bypass CAPTCHA.",
        }
    mfa_words = ("2-step verification", "two-step verification", "enter the code", "check your phone", "verify it's you", "verify itâ€™s you", "security key", "passkey", "authentication code")
    if any(word in joined for word in mfa_words):
        return {
            "kind": "user_authentication_required",
            "message": "The site is requesting an account verification step (such as MFA/passkey). Complete that step manually in the visible browser, then tell IntignoAI to continue. Do not share one-time codes in chat.",
        }
    return None


def _profile_slug(value: str) -> str:
    value = str(value or "").strip()
    if "/" in value or "\\" in value or value in {".", ".."}:
        raise ValueError("Profile name must not contain path separators or traversal components.")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-_")
    if not slug or slug in {".", ".."} or len(slug) > 64:
        raise ValueError("Profile name must be 1â€“64 characters using letters, numbers, dot, dash or underscore.")
    return slug


def _profile_path(profile: str) -> Path:
    # Resolve the profile root dynamically so runtime/test overrides of
    # BROWSER_PROFILE_DIR take effect even if this module was imported earlier.
    _profile_slug(profile)
    configured = str(os.getenv("BROWSER_PROFILE_DIR", "")).strip()
    if configured:
        raw_root = Path(configured).expanduser()
        profile_root = (raw_root if raw_root.is_absolute() else ROOT / raw_root).resolve()
    else:
        profile_root = PROFILES_DIR
    profile_root.mkdir(parents=True, exist_ok=True)
    path = (profile_root / "default").resolve()
    try:
        path.relative_to(profile_root)
    except ValueError as exc:
        raise ValueError("Invalid browser profile name.") from exc
    return path


def _metadata_path(profile_path: Path) -> Path:
    return profile_path / ".intignoai-profile.json"


def _read_metadata(profile_path: Path) -> dict[str, Any]:
    try:
        data = json.loads(_metadata_path(profile_path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _write_metadata(profile_path: Path, data: dict[str, Any]) -> None:
    profile_path.mkdir(parents=True, exist_ok=True)
    path = _metadata_path(profile_path)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    temp.replace(path)


def _profile_lock(slug: str) -> threading.RLock:
    with _locks_guard:
        return _profile_locks.setdefault(slug, threading.RLock())


def list_profiles() -> dict[str, Any]:
    """Return the single canonical profile; never reads cookies or browser history."""
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    canonical = PROFILES_DIR / "default"
    # One-time, non-destructive compatibility migration: if an older build left
    # one named profile and no default, reuse its on-disk browser data. If there
    # are several legacy folders, prefer an existing default or promote one
    # stable folder; other folders are left untouched but never exposed/used.
    if not canonical.exists():
        legacy = sorted(
            (p for p in PROFILES_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")),
            key=lambda p: p.name.lower(),
        )
        if legacy:
            try:
                legacy[0].rename(canonical)
                logger.info("Reused one legacy browser profile as the single default profile.")
            except OSError as exc:
                logger.warning("Could not migrate legacy profile; creating default instead: %s", exc)
    canonical.mkdir(parents=True, exist_ok=True)
    metadata = _read_metadata(canonical)
    extensions = metadata.get("extensions", [])
    if not isinstance(extensions, list):
        extensions = []
    if not metadata.get("name"):
        metadata.update({"name": "IntignoAI Main Browser", "extensions": extensions})
        _write_metadata(canonical, metadata)
    profiles = [{
        "id": "default",
        "name": metadata.get("name") or "IntignoAI Main Browser",
        "extensions": len(extensions),
        "persistent": True,
        "last_used": metadata.get("last_used"),
        "tabs_supported": True,
    }]
    return {
        "ok": True,
        "count": 1,
        "profiles": profiles,
        "prompt": None,
        "policy": "single persistent profile; use multiple tabs instead of creating more profiles",
        "profile_directory": str(PROFILES_DIR),
    }


def create_profile(name: str) -> dict[str, Any]:
    """Compatibility shim: never create extra profiles; reuse the one browser."""
    _profile_slug(name)
    current = list_profiles()
    path = PROFILES_DIR / "default"
    metadata = _read_metadata(path)
    if not metadata:
        _write_metadata(path, {
            "name": "IntignoAI Main Browser", "extensions": [],
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    logger.info("Single-profile policy: reusing default browser profile (requested name ignored).")
    return {
        "ok": True, "profile": "default", "reused": True,
        "message": "IntignoAI uses one persistent browser profile. Open another tab instead of creating another profile.",
        "profiles": current["profiles"],
    }


def add_profile_extension(profile: str, extension_path: str) -> dict[str, Any]:
    _profile_slug(profile)
    slug = "default"
    profile_path = _profile_path(slug)
    if not profile_path.is_dir():
        raise ValueError(f"Browser profile '{slug}' was not found. Create it first.")
    ext = Path(str(extension_path)).expanduser().resolve()
    if not ext.is_dir() or not (ext / "manifest.json").is_file():
        raise ValueError("Extension path must be an unpacked browser-extension folder containing manifest.json.")
    try:
        manifest = json.loads((ext / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("The extension's manifest.json is unreadable or invalid JSON.") from exc
    if not isinstance(manifest, dict) or not manifest.get("manifest_version"):
        raise ValueError("The extension manifest is missing manifest_version.")
    metadata = _read_metadata(profile_path)
    extensions = metadata.get("extensions", [])
    if not isinstance(extensions, list):
        extensions = []
    if str(ext) not in extensions:
        extensions.append(str(ext))
    metadata.update({"extensions": extensions, "name": metadata.get("name") or slug})
    _write_metadata(profile_path, metadata)
    logger.info("Registered extension for profile %s: %s", slug, ext)
    return {"ok": True, "profile": slug, "extensions": extensions}


def _choose_profile(profile: str | None) -> tuple[str | None, dict[str, Any] | None]:
    if profile:
        _profile_slug(profile)
    # Creating/listing a profile is intentionally idempotent and always returns
    # the one persistent browser profile.
    list_profiles()
    return "default", None


def _validate_url(url: str) -> str:
    value = str(url or "").strip()
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("URL must be a complete http:// or https:// address.")
    if parts.username or parts.password:
        raise ValueError("URLs containing embedded usernames/passwords are not accepted.")
    return value


# --- v1.8: bot-detection hardening -------------------------------------------------------
# Standard, well-known browser-environment consistency patches used by mainstream test
# automation frameworks. They make an automated browser report the same environment a
# normal browser reports, which dramatically reduces false-positive CAPTCHA/bot walls on
# sites the user is allowed to use. They do not defeat any challenge content.
STEALTH_INIT_SCRIPT = """
(() => {
  try { Object.defineProperty(navigator, 'webdriver', {get: () => undefined, configurable: true}); } catch (e) {}
  try { window.chrome = window.chrome || { runtime: {}, app: { isInstalled: false } }; } catch (e) {}
  try { Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en'], configurable: true}); } catch (e) {}
  try { Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5], configurable: true}); } catch (e) {}
  try { if (navigator.hardwareConcurrency && navigator.hardwareConcurrency < 4) Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 4, configurable: true}); } catch (e) {}
  try {
    const original = window.Permissions && Permissions.prototype && Permissions.prototype.query;
    if (original) { Permissions.prototype.query = function (parameters) {
      if (parameters && parameters.name === 'notifications') {
        return Promise.resolve({state: (window.Notification && Notification.permission) || 'prompt'});
      }
      return original.call(this, parameters);
    }; }
  } catch (e) {}
  try {
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function (parameter) {
      if (parameter === 37445) return 'Intel Inc.';
      if (parameter === 37446) return 'Intel Iris OpenGL Engine';
      return getParameter.call(this, parameter);
    };
  } catch (e) {}
})();
"""


def _apply_stealth(context) -> None:
    """Install environment-consistency patches for every future document in the context."""
    try:
        context.add_init_script(STEALTH_INIT_SCRIPT)
    except Exception as exc:
        logger.debug("Stealth init script could not be installed: %s", exc)


def _ensure_challenge_resolved(page, max_seconds: float | None = None) -> dict[str, Any] | None:
    """Watch a CAPTCHA/anti-bot interstitial for self-clearing; never bypass its content.

    Invisible managed challenges (Cloudflare Turnstile, passive checks) usually clear on a
    real visible browser within seconds. Polls quietly, reloads at most once for stuck
    interstitials (what a human does), and returns the still-present verification info or
    None when the page is usable again.
    """
    budget = float(max_seconds) if max_seconds is not None else CHALLENGE_WAIT_SECONDS
    verification = _browser_is_human_verification(page)
    if verification is None or budget <= 0:
        return verification
    deadline = time.monotonic() + budget
    reloaded = False
    while verification is not None and time.monotonic() < deadline:
        time.sleep(1.5)
        if not reloaded and (deadline - time.monotonic()) < budget * 0.45:
            title = ""
            try:
                title = (page.title() or "").lower()
            except Exception:
                pass
            if "just a moment" in title or "attention required" in title or "checking your browser" in title:
                try:
                    page.reload(wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass
            reloaded = True
        verification = _browser_is_human_verification(page)
    if verification is None:
        logger.info("Human-verification interstitial cleared by itself; continuing automatically.")
    return verification


def _frame_page_offset(frame) -> dict[str, Any]:
    """Page-viewport offset of a child frame (0,0 for the main document)."""
    offset: dict[str, Any] = {"x": 0.0, "y": 0.0}
    try:
        current = frame
        for _ in range(8):
            parent = getattr(current, "parent_frame", None)
            if parent is None:
                break
            element = current.frame_element()
            box = element.bounding_box() if element is not None else None
            if box:
                offset["x"] += float(box.get("x", 0) or 0)
                offset["y"] += float(box.get("y", 0) or 0)
            current = parent
    except Exception:
        return {"x": None, "y": None}
    return offset


# --- v1.8: image inventory, YouTube thumbnail matching -----------------------------------

_YT_ID_PATTERN = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^#]*&)?v=|embed/|shorts/|live/|v/)|youtu\.be/|i\.ytimg\.com/vi/|img\.youtube\.com/vi/)([A-Za-z0-9_-]{11})"
)


def _extract_youtube_id(value: str) -> str | None:
    match = _YT_ID_PATTERN.search(str(value or ""))
    return match.group(1) if match else None


_IMAGE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0.0.0 Safari/537.36"
)


def _download_image_bytes(url: str, context=None) -> bytes | None:
    """Download an image via plain HTTP first, then through the browser context (cookies/UA)."""
    try:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": _IMAGE_USER_AGENT,
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            },
        )
        with urllib.request.urlopen(request, timeout=12) as response:
            if response.status == 200:
                return response.read()
    except Exception as exc:
        logger.debug("Direct image download failed (%s): %s", url, exc)
    if context is not None:
        try:
            response = context.request.get(url, timeout=15000)
            if response.ok:
                return response.body()
        except Exception as exc:
            logger.debug("Browser-context image download failed (%s): %s", url, exc)
    return None


def _load_image(data: bytes):
    from PIL import Image, ImageOps

    image = Image.open(io.BytesIO(data))
    image = ImageOps.exif_transpose(image).convert("RGB")
    return image


def _crop_center(image, fraction: float = 0.10):
    width, height = image.size
    dx, dy = int(width * fraction), int(height * fraction)
    return image.crop((dx, dy, max(dx + 1, width - dx), max(dy + 1, height - dy)))


def _dhash(image, hash_size: int = 8) -> int:
    from PIL import Image

    small = image.convert("L").resize((hash_size + 1, hash_size), Image.LANCZOS)
    pixels = list(small.getdata())
    bits = 0
    for row in range(hash_size):
        base = row * (hash_size + 1)
        for col in range(hash_size):
            if pixels[base + col] > pixels[base + col + 1]:
                bits |= 1 << (row * hash_size + col)
    return bits


def _ahash(image, hash_size: int = 8) -> int:
    from PIL import Image

    small = image.convert("L").resize((hash_size, hash_size), Image.LANCZOS)
    pixels = list(small.getdata())
    average = sum(pixels) / max(1, len(pixels))
    bits = 0
    for index, value in enumerate(pixels):
        if value >= average:
            bits |= 1 << index
    return bits


def _image_hashes(data: bytes) -> list[tuple[str, int]] | None:
    """Perceptual hashes robust to letterboxing/cropping: full and center-cropped variants."""
    try:
        image = _load_image(data)
    except Exception:
        return None
    cropped = _crop_center(image)
    return [
        ("dhash", _dhash(image)),
        ("dhash_crop", _dhash(cropped)),
        ("ahash_crop", _ahash(cropped)),
    ]


def _hash_distance(reference: list[tuple[str, int]], candidate: list[tuple[str, int]]) -> int:
    candidate_map = dict(candidate)
    best = 64
    for kind, value in reference:
        if kind in candidate_map:
            best = min(best, bin(value ^ candidate_map[kind]).count("1"))
    return best


_IMAGE_INVENTORY_JS = r"""(limit) => {
  const abs = (u) => { try { return new URL(u, location.href).href; } catch (e) { return u; } };
  const visible = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return !!(r.width && r.height && s.visibility !== 'hidden' && s.display !== 'none'); };
  const rectOf = (el) => { const r = el.getBoundingClientRect(); return {x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height)}; };
  const out = [];
  document.querySelectorAll('img').forEach((img) => {
    const srcsetFirst = (img.getAttribute('srcset') || '').split(',')[0].trim().split(/\s+/)[0] || '';
    const src = img.currentSrc || img.src || img.getAttribute('data-src') || img.getAttribute('data-original') || img.getAttribute('data-lazy-src') || srcsetFirst;
    if (!src) return;
    const anchor = img.closest('a');
    out.push({tag: 'img', src: abs(src), alt: (img.alt || '').slice(0, 200), title: (img.title || '').slice(0, 200),
      width: img.naturalWidth || null, height: img.naturalHeight || null, visible: visible(img), rect: rectOf(img),
      anchor: anchor ? abs(anchor.getAttribute('href') || '') : '', lazy: img.getAttribute('loading') === 'lazy'});
  });
  document.querySelectorAll('[style*="background-image"]').forEach((el) => {
    const match = /url\(["']?(.*?)["']?\)/.exec(el.getAttribute('style') || '');
    if (match && match[1]) {
      const anchor = el.closest('a');
      out.push({tag: 'css-background', src: abs(match[1]), alt: (el.getAttribute('aria-label') || '').slice(0, 200),
        visible: visible(el), rect: rectOf(el), anchor: anchor ? abs(anchor.getAttribute('href') || '') : ''});
    }
  });
  document.querySelectorAll('video[poster]').forEach((v) => out.push({tag: 'video-poster', src: abs(v.getAttribute('poster')), visible: visible(v), rect: rectOf(v)}));
  document.querySelectorAll('source[srcset]').forEach((s) => {
    const first = (s.getAttribute('srcset') || '').split(',')[0].trim().split(/\s+/)[0];
    if (first) out.push({tag: 'source', src: abs(first), visible: false, rect: {x: 0, y: 0, width: 0, height: 0}});
  });
  return out.slice(0, limit);
}"""


def _collect_images(page, limit: int = 200) -> dict[str, Any]:
    """Inventory every image on the page INCLUDING cross-origin iframes, with provenance."""
    limit = max(10, min(600, int(limit)))
    images: list[dict[str, Any]] = []
    try:
        frames = list(page.frames)[:12]
    except Exception:
        frames = []
    for frame_index, frame in enumerate(frames):
        try:
            items = frame.evaluate(_IMAGE_INVENTORY_JS, limit)
        except Exception as exc:
            logger.debug("Image inventory failed in frame %s: %s", frame_index, exc)
            continue
        for item in items or []:
            if not isinstance(item, dict) or not item.get("src"):
                continue
            item["frame_index"] = frame_index
            item["frame_url"] = str(getattr(frame, "url", "") or "")
            images.append(item)
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in images:
        src = str(item.get("src", ""))
        if src in seen:
            continue
        seen.add(src)
        unique.append(item)
    youtube: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in unique:
        video_id = _extract_youtube_id(str(item.get("src", ""))) or _extract_youtube_id(str(item.get("anchor", "")))
        if video_id and video_id not in seen_ids:
            seen_ids.add(video_id)
            youtube.append({
                "video_id": video_id,
                "watch_url": f"https://www.youtube.com/watch?v={video_id}",
                "image_src": item.get("src"),
                "evidence": "thumbnail_url" if _extract_youtube_id(str(item.get("src", ""))) else "surrounding_link",
            })
    return {"count": len(unique), "images": unique[:limit], "youtube_videos": youtube}


def _collect_youtube_search_results(page, max_ids: int) -> list[dict[str, str]]:
    try:
        html = page.content()
    except Exception:
        return []
    videos: list[dict[str, str]] = []
    seen: set[str] = set()
    match = re.search(r"ytInitialData\s*=\s*(\{.*?\});\s*</script>", html, re.S)
    if not match:
        match = re.search(r"ytInitialData\s*=\s*(\{.*?\});", html, re.S)
    if match:
        try:
            data = json.loads(match.group(1))

            def walk(node):
                if len(videos) >= max_ids * 2:
                    return
                if isinstance(node, dict):
                    renderer = node.get("videoRenderer") or node.get("gridVideoRenderer") or node.get("compactVideoRenderer")
                    if isinstance(renderer, dict) and renderer.get("videoId"):
                        video_id = str(renderer.get("videoId"))
                        if video_id not in seen:
                            seen.add(video_id)
                            title = ""
                            try:
                                runs = renderer.get("title", {}).get("runs", [])
                                title = "".join(str(run.get("text", "")) for run in runs)[:160]
                            except Exception:
                                title = ""
                            videos.append({"video_id": video_id, "title": title})
                    for value in node.values():
                        walk(value)
                elif isinstance(node, list):
                    for value in node:
                        walk(value)

            walk(data)
        except Exception as exc:
            logger.debug("ytInitialData parse failed: %s", exc)
    if not videos:
        for video_id in re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', html):
            if video_id not in seen:
                seen.add(video_id)
                videos.append({"video_id": video_id, "title": ""})
    return videos[:max_ids]


def _try_play_video(page) -> dict[str, Any]:
    """Best-effort play of the current YouTube watch page, with a real-mouse fallback."""
    try:
        outcome = page.evaluate(
            """async () => {
          const accept = document.querySelector('button[aria-label*="Accept"], button[aria-label*="accept all" i]');
          if (accept && accept.offsetParent) { try { accept.click(); } catch (e) {} }
          const video = document.querySelector('video.html5-main-video') || document.querySelector('video');
          if (!video) return {ok: false, reason: 'no_video_element_yet'};
          try { video.muted = false; } catch (e) {}
          try { await video.play(); } catch (e) {
            const big = document.querySelector('.ytp-large-play-button');
            if (big) { try { big.click(); } catch (e2) {} }
            try { await video.play(); } catch (e3) { return {ok: false, reason: 'play_rejected_' + ((e3 && e3.name) || 'unknown')}; }
          }
          return {ok: !video.paused, paused: !!video.paused, time: video.currentTime};
        }"""
        )
        if isinstance(outcome, dict) and outcome.get("ok"):
            return outcome
    except Exception as exc:
        logger.debug("JS play attempt failed: %s", exc)
    # Real mouse click on the player = a trusted user gesture; autoplay policies allow it.
    for selector in (".ytp-large-play-button", ".ytp-play-button", "button[title*='Play' i]"):
        try:
            box = page.locator(selector).first.bounding_box()
            if box and box.get("width", 0) > 0:
                page.mouse.click(float(box["x"]) + float(box["width"]) / 2, float(box["y"]) + float(box["height"]) / 2)
                try:
                    page.wait_for_timeout(1200)
                except Exception:
                    pass
                state = page.evaluate("() => { const v = document.querySelector('video'); return v ? {ok: !v.paused, paused: !!v.paused} : {ok: false}; }")
                if isinstance(state, dict) and state.get("ok"):
                    return {**state, "mode": "mouse_click"}
        except Exception:
            continue
    return {"ok": False, "reason": "video did not start; it may need one manual click"}


def _youtube_find_by_thumbnail(context, session, page, action: dict[str, Any]) -> dict[str, Any]:
    """Match a web-page thumbnail to the EXACT YouTube video (perceptual hash + vision query)
    and open + play it. Keeps the current tab untouched; work happens in a new YouTube tab.
    """
    image_url = str(action.get("image_url", action.get("src", ""))).strip()
    image_path = str(action.get("image_path", action.get("file_path", ""))).strip().strip('"')
    query = str(action.get("query", action.get("text", ""))).strip()
    max_results = max(1, min(12, int(action.get("max_results", 6))))
    threshold = max(0, min(40, int(action.get("match_threshold", 14))))
    should_play = bool(action.get("play", True))
    if not image_url and not image_path:
        raise BrowserAutomationError(
            "youtube_find_by_thumbnail needs image_url (the page's <img src>) or image_path (a local file)."
        )
    direct_id = _extract_youtube_id(image_url)
    reference_bytes: bytes | None = None
    reference_source = image_url or image_path
    if image_path:
        path = Path(image_path).expanduser()
        if path.is_file():
            reference_bytes = path.read_bytes()
    if reference_bytes is None and image_url:
        reference_bytes = _download_image_bytes(image_url, context)
    if reference_bytes is None and not direct_id:
        raise BrowserAutomationError(
            f"The thumbnail could not be downloaded ({reference_source}). Open the image in a tab and use a screenshot instead, or pass image_path."
        )
    if direct_id:
        result: dict[str, Any] = {
            "ok": True, "matched": True, "video_id": direct_id,
            "watch_url": f"https://www.youtube.com/watch?v={direct_id}",
            "evidence": "thumbnail URL itself identifies the video",
        }
        if should_play:
            player = context.new_page()
            _register_page(session, player)
            try:
                _navigate_page(player, result["watch_url"], "domcontentloaded", 25000)
                try:
                    player.wait_for_timeout(2500)
                except Exception:
                    pass
                outcome = _try_play_video(player)
                try:
                    player.bring_to_front()
                except Exception:
                    pass
                result.update({"played": bool(outcome.get("ok")), "play_detail": outcome, "tab_id": _register_page(session, player)})
            except Exception as exc:
                result["play_error"] = str(exc)[:250]
        return result
    if not query:
        raise BrowserAutomationError(
            "The thumbnail is not YouTube-hosted, so a `query` is required. LOOK at the thumbnail "
            "(screenshot/your vision) or read its alt text/anchor text and pass a short query naming the "
            "video title, channel, or distinctive on-screen text."
        )
    reference_hashes = _image_hashes(reference_bytes) if reference_bytes else None
    if not reference_hashes:
        raise BrowserAutomationError("The reference thumbnail could not be decoded as an image.")
    search_page = context.new_page()
    _register_page(session, search_page)
    search_url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query)
    _navigate_page(search_page, search_url, "domcontentloaded", 25000)
    try:
        search_page.wait_for_timeout(1200)
    except Exception:
        pass
    videos = _collect_youtube_search_results(search_page, max(12, max_results * 4))
    if not videos:
        return {"ok": False, "matched": False, "query": query, "reason": "youtube_search_returned_no_videos",
                "message": "YouTube returned no parseable results; try a more specific query."}
    scored: list[dict[str, Any]] = []
    for video in videos[: max_results * 4]:
        video_id = str(video.get("video_id") or "")
        if not video_id:
            continue
        thumbnail = _download_image_bytes(f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg", context)
        if not thumbnail:
            continue
        candidate_hashes = _image_hashes(thumbnail)
        if not candidate_hashes:
            continue
        distance = _hash_distance(reference_hashes, candidate_hashes)
        scored.append({**video, "distance": distance, "watch_url": f"https://www.youtube.com/watch?v={video_id}"})
    scored.sort(key=lambda item: item["distance"])
    candidates = scored[:max_results]
    best = candidates[0] if candidates else None
    matched = bool(best and best["distance"] <= threshold)
    result = {
        "ok": True, "matched": matched, "query": query, "reference_image": reference_source,
        "match_threshold": threshold, "best_distance": best["distance"] if best else None,
        "candidates": candidates, "tab_id": _register_page(session, search_page),
        "note": "Candidates are ranked by perceptual-hamming distance to the page thumbnail (0 = identical).",
    }
    if matched and should_play and best:
        try:
            _navigate_page(search_page, best["watch_url"], "domcontentloaded", 25000)
            try:
                search_page.wait_for_timeout(2500)
            except Exception:
                pass
            outcome = _try_play_video(search_page)
            try:
                search_page.bring_to_front()
            except Exception:
                pass
            result.update({"played": bool(outcome.get("ok")), "play_detail": outcome, "watch_url": best["watch_url"], "video_id": best["video_id"]})
        except Exception as exc:
            result["play_error"] = str(exc)[:250]
    elif not matched:
        result["message"] = (
            "No candidate reached the similarity threshold. Look at the candidates visually (screenshot), "
            "refine `query`, or raise match_threshold slightly."
        )
    return result


def _normalize_selector(selector: str) -> str:
    """Normalize common JSON-escaped CSS strings emitted by small models.

    Some tool-call generations accidentally retain backslashes before quotes, e.g.
    ``input[type=\"file\"]``. Browsers see those backslashes literally and
    reject the selector even though the intended selector is valid CSS.
    """
    value = str(selector or "").strip()
    value = value.replace('\\"', '"').replace("\\'", "'")
    return value


def _selector(page, selector: str):
    selector = _normalize_selector(selector)
    if not selector:
        raise ValueError("This action needs a CSS selector or XPath in 'selector'.")
    # Playwright understands XPath auto-detection, but prefixing absolute paths is clearer.
    if selector.startswith("/") or selector.startswith("(//") or selector.startswith("xpath="):
        return page.locator(selector if selector.startswith("xpath=") else f"xpath={selector}")
    return page.locator(selector)


def _fallback_locator(page, fallback: Any):
    """Build a semantic fallback locator from role, label, placeholder, text or CSS."""
    if isinstance(fallback, str) and fallback.strip():
        return page.get_by_text(fallback.strip(), exact=False)
    if not isinstance(fallback, dict):
        return None
    if fallback.get("role"):
        return page.get_by_role(str(fallback["role"]), name=fallback.get("name"), exact=bool(fallback.get("exact", False)))
    if fallback.get("label"):
        return page.get_by_label(str(fallback["label"]), exact=bool(fallback.get("exact", False)))
    if fallback.get("placeholder"):
        return page.get_by_placeholder(str(fallback["placeholder"]), exact=bool(fallback.get("exact", False)))
    if fallback.get("text"):
        return page.get_by_text(str(fallback["text"]), exact=bool(fallback.get("exact", False)))
    if fallback.get("selector"):
        return _selector(page, str(fallback["selector"]))
    return None


def _run_locator_action(page, locator, kind: str, action: dict[str, Any], timeout: int, source: str) -> dict[str, Any]:
    state = str(action.get("state", "visible"))
    value = action.get("value", action.get("text", ""))

    # File inputs are very often intentionally hidden behind an upload button. A
    # Playwright/CDP set_input_files operation does not require a human click or a
    # visible input, only an attached input[type=file]. Waiting for "visible" here
    # was the source of the upload failures in Agent Logs.txt.
    if kind == "upload_file":
        raw_paths = action.get("paths", action.get("file_paths", action.get("file_path")))
        if raw_paths is None:
            raw_paths = action.get("filename", value)
        paths = raw_paths if isinstance(raw_paths, list) else [raw_paths]
        resolved_paths = []
        for raw in paths:
            path_text = str(raw or "").strip()
            if not path_text:
                continue
            path = Path(path_text).expanduser()
            if not path.is_file():
                raise ValueError(f"upload_file could not find local file: {path_text}")
            resolved_paths.append(str(path.resolve()))
        if not resolved_paths:
            raise ValueError("upload_file needs an existing local path in file_path, paths, filename or value.")
        locator.wait_for(state="attached", timeout=timeout)
        locator.set_input_files(resolved_paths, timeout=timeout)
        try:
            selected = locator.evaluate("el => ({tag:el.tagName.toLowerCase(), type:(el.type||''), files:Array.from(el.files||[]).map(f=>({name:f.name,size:f.size,type:f.type})), accept:el.accept||'', name:el.name||'', id:el.id||''})")
        except Exception:
            selected = {"files": [{"name": Path(item).name} for item in resolved_paths]}
        if not selected.get("files"):
            raise BrowserAutomationError("The browser accepted the upload command but the input reports no selected file. The website may be using a custom uploader or the selector may be wrong.")
        return {"ok": True, "action": kind, "mode": "semantic" if source != "selector" else "selector", "target": source,
                "verification": "file_selected_in_input", "file_count": len(selected.get("files", [])),
                "files": selected.get("files", []), "input": {k: selected.get(k) for k in ("tag", "type", "accept", "name", "id")}}

    if kind == "wait_for_selector":
        locator.wait_for(state=state, timeout=timeout)
    else:
        locator.wait_for(state="visible", timeout=timeout)
        if kind == "click":
            locator.click(timeout=timeout, force=bool(action.get("force", False)))
        elif kind == "fill":
            locator.fill(str(value), timeout=timeout)
        elif kind == "type":
            text = str(value)
            if bool(action.get("clear", False)):
                locator.fill("", timeout=timeout)
            if hasattr(locator, "press_sequentially"):
                locator.press_sequentially(text, timeout=timeout, delay=max(0, min(250, int(action.get("key_delay_ms", 0)))))
            else:
                locator.click(timeout=timeout)
                page.keyboard.insert_text(text)
        elif kind == "hover":
            locator.hover(timeout=timeout)
        elif kind == "press":
            locator.press(str(value), timeout=timeout)
        elif kind == "check":
            locator.check(timeout=timeout, force=bool(action.get("force", False)))
        elif kind == "uncheck":
            locator.uncheck(timeout=timeout, force=bool(action.get("force", False)))
        elif kind == "select":
            locator.select_option(value, timeout=timeout)
        elif kind == "focus":
            locator.focus(timeout=timeout)
        elif kind == "submit":
            locator.evaluate("el => { const form = el instanceof HTMLFormElement ? el : el.form; if (!form) throw new Error('No form was associated with this element'); form.requestSubmit(el instanceof HTMLButtonElement || el instanceof HTMLInputElement ? el : undefined); }")
        else:
            raise ValueError(f"Unsupported locator action: {kind}")
    result = {"ok": True, "action": kind, "mode": "semantic" if source != "selector" else "selector", "target": source}
    # Verify state without reading or returning field values (particularly passwords).
    if kind in {"fill", "type", "check", "uncheck", "select"}:
        try:
            state_info = locator.evaluate("el => ({tag:el.tagName.toLowerCase(),type:(el.type||''),has_value:('checked' in el && ['checkbox','radio'].includes(el.type)) ? !!el.checked : (el.tagName==='SELECT' ? !!el.value : String(el.value||'').length>0),value_length:('value' in el ? String(el.value||'').length : null),checked:('checked' in el) ? !!el.checked : null,selected_text:el.tagName==='SELECT' ? (el.selectedOptions[0]?.textContent||'').trim().slice(0,120) : null})")
            result["verification"] = state_info
        except Exception:
            result["verification"] = "action_completed; field state could not be read"
    return result

def _semantic_fallbacks(page_or_frame, action: dict[str, Any], kind: str):
    """Yield explicit and inferred semantic locators, including localized inputs."""
    fallback = action.get("fallback")
    if fallback is not None:
        try:
            candidate = _fallback_locator(page_or_frame, fallback)
            if candidate is not None:
                yield candidate
        except Exception as exc:
            logger.debug("Could not construct supplied fallback locator: %s", exc)

    if kind == "upload_file":
        # Prefer a semantic label when the caller supplied one. The generic file-input
        # fallback is resolved separately so multiple upload controls aren't confused.
        for key in ("label", "aria_label", "placeholder"):
            term = str(action.get(key, "") or "").strip()
            if term:
                try:
                    yield page_or_frame.get_by_label(term, exact=False)
                except Exception as exc:
                    logger.debug("Could not build semantic file-input locator for %r: %s", term, exc)
        return

    terms = []
    term_keys = ("target", "label", "placeholder", "name", "aria_label")
    if kind not in {"fill", "type"}:
        term_keys = (*term_keys, "text")
    for key in term_keys:
        value = action.get(key)
        if isinstance(value, str) and value.strip():
            terms.append(value.strip())
    seen = set()
    for term in terms:
        if not term or term in seen:
            continue
        seen.add(term)
        if kind in {"fill", "type"}:
            factories = (
                lambda t=term: page_or_frame.get_by_label(t, exact=False),
                lambda t=term: page_or_frame.get_by_placeholder(t, exact=False),
                lambda t=term: page_or_frame.get_by_role("textbox", name=t, exact=False),
                lambda t=term: page_or_frame.get_by_role("combobox", name=t, exact=False),
                lambda t=term: page_or_frame.get_by_text(t, exact=False),
            )
        else:
            factories = (
                lambda t=term: page_or_frame.get_by_role("button", name=t, exact=False),
                lambda t=term: page_or_frame.get_by_role("link", name=t, exact=False),
                lambda t=term: page_or_frame.get_by_role("checkbox", name=t, exact=False),
                lambda t=term: page_or_frame.get_by_text(t, exact=False),
            )
        for factory in factories:
            try:
                yield factory()
            except Exception as exc:
                logger.debug("Could not build inferred locator for %r: %s", term, exc)

    if kind in {"fill", "type"}:
        context = " ".join(str(action.get(k, "")) for k in ("selector", "target", "label", "placeholder", "name", "aria_label")).lower()
        value = str(action.get("value", action.get("text", "")))
        # A field id/name often changes between locales and releases. Infer a likely
        # auth field from selector intent or input shape, then probe a short list.
        password_intent = bool(re.search(r"pass(word)?|pwd|current-password|new-password", context))
        email_intent = bool(re.search(r"email|e-mail|username|user-name|identifier|session_key|login", context)) or ("@" in value and " " not in value)
        if password_intent:
            selectors = [
                'input[type="password"]', 'input[autocomplete="current-password"]',
                'input[autocomplete="new-password"]', 'input[name*="pass" i]',
                'input[id*="pass" i]', 'input[aria-label*="pass" i]',
            ]
        elif email_intent:
            selectors = [
                'input[type="email"]', 'input[autocomplete="username"]',
                'input[name*="email" i]', 'input[id*="email" i]',
                'input[name*="user" i]', 'input[id*="user" i]',
                'input[name*="session_key" i]', 'input[id*="identifier" i]',
                'input[aria-label*="email" i]', 'input[placeholder*="email" i]',
                'input[type="text"]',
            ]
        else:
            selectors = []
        for css in selectors:
            try:
                yield page_or_frame.locator(css).first
            except Exception as exc:
                logger.debug("Could not build inferred control locator %s: %s", css, exc)


def _frame_candidates(page, action: dict[str, Any]):
    """Return main page and child frames, optionally prioritizing a requested frame."""
    try:
        frames = list(page.frames)
    except Exception:
        frames = []
    main_frame = getattr(page, "main_frame", None)
    if main_frame is not None and all(frame is not main_frame for frame in frames):
        frames.insert(0, main_frame)
    selected: list[Any] = []
    frame_index = action.get("frame_index")
    frame_url_contains = str(action.get("frame_url_contains", "")).strip().lower()
    frame_name = str(action.get("frame_name", "")).strip().lower()
    if frame_index is not None:
        try:
            index = int(frame_index)
            if 0 <= index < len(frames): selected.append(frames[index])
        except (ValueError, TypeError):
            pass
    if frame_url_contains or frame_name:
        for frame in frames:
            try:
                if (not frame_url_contains or frame_url_contains in str(frame.url).lower()) and (not frame_name or frame_name in str(frame.name).lower()):
                    if frame not in selected: selected.append(frame)
            except Exception:
                continue
    # Main document first, then visible/content frames. Playwright locators work with
    # cross-origin iframes without page JavaScript/CORS fetches; browser policy still
    # governs inaccessible/sandboxed content.
    if main_frame is not None and main_frame not in selected:
        selected.append(main_frame)
    for frame in frames:
        try:
            if frame not in selected and str(frame.url or "") not in {"", "about:blank"}:
                selected.append(frame)
        except Exception:
            continue
    return selected[:12]


def _choose_file_input(page, frames: list[Any], action: dict[str, Any]):
    """Choose an attached native file input safely; reject ambiguous controls."""
    raw_paths = action.get("paths", action.get("file_paths", action.get("file_path", action.get("filename", action.get("value", action.get("text", ""))))))
    paths = raw_paths if isinstance(raw_paths, list) else [raw_paths]
    extensions = [Path(str(item or "")).suffix.lower() for item in paths if str(item or "").strip()]
    hints = " ".join(str(action.get(k, "") or "") for k in ("target", "label", "placeholder", "name", "aria_label", "text")).lower()
    fallback = action.get("fallback")
    if isinstance(fallback, dict):
        hints += " " + " ".join(str(fallback.get(k, "") or "") for k in ("name", "text", "label", "placeholder", "selector")).lower()
    hint_tokens = {x for x in re.findall(r"[a-z0-9]+", hints) if len(x) > 2}
    candidates: list[dict[str, Any]] = []
    unique_frames = []
    for frame in frames:
        if all(frame is not seen for seen in unique_frames):
            unique_frames.append(frame)
    for frame in unique_frames:
        try:
            inputs = frame.locator('input[type="file"]')
            count = min(int(inputs.count()), 40)
        except Exception:
            continue
        for idx in range(count):
            try:
                locator = inputs.nth(idx)
                info = locator.evaluate("el => ({id:el.id||'',name:el.name||'',accept:el.accept||'',aria_label:el.getAttribute('aria-label')||'',title:el.title||'',label:(el.closest('label')?.innerText||Array.from(el.labels||[]).map(l=>l.innerText).join(' ')||'').trim().slice(0,160),nearby:(el.parentElement?.innerText||'').trim().slice(0,160),multiple:!!el.multiple,disabled:!!el.disabled})")
                if info.get("disabled"):
                    continue
                descriptor = " ".join(str(info.get(k, "") or "") for k in ("id", "name", "accept", "aria_label", "title", "label", "nearby")).lower()
                score = 0
                for token in hint_tokens:
                    if token in descriptor:
                        score += 5
                accept = str(info.get("accept", "")).lower()
                for ext in extensions:
                    if ext and ext in accept:
                        score += 8
                    elif ext == ".pdf" and "application/pdf" in accept:
                        score += 8
                    elif ext in {".doc", ".docx"} and ("word" in accept or "officedocument" in accept):
                        score += 8
                    elif accept and ext and any(t in accept for t in ("image/", ".png", ".jpg", ".jpeg")) and ext not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                        score -= 8
                candidates.append({"locator": locator, "frame": frame, "info": info, "score": score})
            except Exception as exc:
                logger.debug("Could not inspect upload input %s: %s", idx, exc)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]["locator"], candidates[0]["info"]
    candidates.sort(key=lambda x: x["score"], reverse=True)
    top = candidates[0]
    second_score = candidates[1]["score"]
    if top["score"] > second_score:
        return top["locator"], top["info"]
    # Don't blindly put a resume into a profile-photo or cover-letter field. Return the
    # visible candidate descriptors so the model can select/ask instead of guessing.
    details = [{k: item["info"].get(k, "") for k in ("id", "name", "accept", "aria_label", "label", "nearby")} for item in candidates[:12]]
    raise BrowserAutomationError("Multiple file upload inputs match equally; refusing to attach to the wrong field. Inspect these candidates and pass the correct `selector`/`label`: " + json.dumps(details, ensure_ascii=False))


def _resolve_upload_paths(action: dict[str, Any]) -> list[str]:
    """Resolve/validate local upload paths from any accepted payload field."""
    raw_paths = action.get("paths", action.get("file_paths", action.get("file_path", action.get("filename", action.get("value", action.get("text", ""))))))
    paths = raw_paths if isinstance(raw_paths, list) else [raw_paths]
    resolved: list[str] = []
    for raw in paths:
        path_text = str(raw or "").strip()
        if not path_text:
            continue
        path = Path(path_text).expanduser()
        if not path.is_file():
            raise ValueError(f"upload_file could not find local file: {path_text}")
        resolved.append(str(path.resolve()))
    if not resolved:
        raise ValueError("upload_file needs an existing local path in file_path, paths, filename or value.")
    return resolved


def _upload_via_file_chooser(page, action: dict[str, Any], timeout: int, selector_error: str | None) -> dict[str, Any]:
    """Custom uploaders that create the file input on click: click the visible trigger and
    feed the native file chooser (works when no static input[type=file] exists)."""
    trigger = str(action.get("trigger_selector", "")).strip()
    resolved_paths = _resolve_upload_paths(action)
    with page.expect_file_chooser(timeout=timeout) as chooser_info:
        _selector(page, trigger).first.click(timeout=timeout)
    chooser = chooser_info.value
    chooser.set_files(resolved_paths)
    return {"ok": True, "action": "upload_file", "mode": "file_chooser", "trigger": trigger,
            "file_count": len(resolved_paths), "files": [{"name": Path(item).name} for item in resolved_paths],
            "selector_error": selector_error}


def _do_locator_action(page, action: dict[str, Any], timeout: int) -> dict[str, Any]:
    kind = str(action.get("action", action.get("type", ""))).strip().lower()
    selector = str(action.get("selector", action.get("target_selector", ""))).strip()
    if not selector and isinstance(action.get("target"), str) and kind in {"click", "hover", "focus", "submit", "wait_for_selector"} and action.get("target_is_selector"):
        selector = str(action["target"]).strip()
    value = action.get("value", action.get("text", ""))
    coords_present = action.get("x") is not None and action.get("y") is not None
    primary_error = None
    frames = _frame_candidates(page, action)
    if not frames:
        frames = [page]  # compatible fallback for lightweight adapters/test doubles
    explicit_frame_selector = str(action.get("frame_selector", "")).strip()

    if selector:
        # Preserve the caller's explicit selector priority, but if it misses in the
        # main document, try the exact CSS/XPath selector in child frames as well.
        primary_targets = []
        if explicit_frame_selector:
            try: primary_targets.append(page.frame_locator(explicit_frame_selector))
            except Exception as exc: primary_error = str(exc)
            primary_targets.extend(frame for frame in frames if frame not in primary_targets)
        else:
            primary_targets.append(page)
            main_frame = getattr(page, "main_frame", None)
            primary_targets.extend(frame for frame in frames if frame is not main_frame and frame not in primary_targets)
        for target_index, target in enumerate(primary_targets[:9]):
            try:
                locator = _selector(target, selector).first
                attempt_timeout = timeout if target_index == 0 else max(500, min(1100, timeout))
                return _run_locator_action(page, locator, kind, action, attempt_timeout, "selector")
            except Exception as exc:
                primary_error = str(exc)
                logger.debug("Selector action failed in one frame (%s, %s): %s", kind, selector, primary_error[:250])
                continue
        logger.warning("Selector action failed in every candidate frame (%s, %s): %s", kind, selector, (primary_error or "not found")[:400])

    if kind == "upload_file" and str(action.get("trigger_selector", "")).strip():
        # Custom uploaders create/replace the file input when a visible button is clicked;
        # drive the native file chooser instead of hunting for a static <input type=file>.
        return _upload_via_file_chooser(page, action, timeout, primary_error)

    # Accessible/fuzzy fallback across frames, using a short per-candidate budget so
    # a missing field doesn't cost 15s for every locator in every frame.
    fallback_timeout = max(500, min(750, timeout))
    fallback_deadline = time.monotonic() + 7.0
    for target in frames[:8]:
        for candidate_index, candidate in enumerate(_semantic_fallbacks(target, action, kind)):
            if candidate_index >= 12 or time.monotonic() >= fallback_deadline:
                break
            remaining_ms = int(max(0, fallback_deadline - time.monotonic()) * 1000)
            if remaining_ms < 500:
                break
            try:
                return _run_locator_action(page, candidate.first, kind, action, min(fallback_timeout, remaining_ms), "fallback")
            except Exception as exc:
                primary_error = f"{primary_error or 'No selector was supplied.'}; fallback: {exc}"
                continue

    if kind == "upload_file":
        try:
            chosen = _choose_file_input(page, frames, action)
            if chosen:
                file_locator, _file_info = chosen
                return _run_locator_action(page, file_locator, kind, action, timeout, "fallback")
        except BrowserAutomationError:
            raise
        except Exception as exc:
            primary_error = f"{primary_error or ''}; file-input discovery: {exc}".strip("; ")

    if coords_present:
        x, y = float(action["x"]), float(action["y"])
        if bool(action.get("coordinates_relative_to_frame")):
            try:
                index = int(action.get("frame_index", 0))
                frame = list(page.frames)[index]
                element = frame.frame_element()
                box = element.bounding_box()
                if box:
                    x += float(box.get("x", 0)); y += float(box.get("y", 0))
            except Exception as exc:
                logger.debug("Could not translate frame-relative coordinates: %s", exc)
        if kind in {"click", "mouse_click", "submit"}:
            page.mouse.click(x, y)
        elif kind in {"hover", "mouse_move"}:
            page.mouse.move(x, y)
        elif kind in {"fill", "type"}:
            page.mouse.click(x, y)
            if action.get("clear", kind == "fill"):
                page.keyboard.press("Control+A")
            page.keyboard.insert_text(str(value))
        elif kind == "press":
            page.mouse.click(x, y)
            page.keyboard.press(str(value))
        else:
            raise BrowserAutomationError(f"Coordinate fallback does not support action '{kind}'.")
        return {"ok": True, "action": kind, "mode": "coordinate", "coordinates": {"x": x, "y": y}, "selector_error": primary_error}

    raise BrowserAutomationError(
        f"Action '{kind}' could not find a usable target in the main page or {max(0, len(frames)-1)} child frame(s). "
        f"Selector error: {primary_error or 'no selector supplied'}. Inspect the page/frames or provide x/y coordinates."
    )

def _register_page(session: dict[str, Any], page) -> str:
    """Assign a stable tab id and attach diagnostics to a newly seen page."""
    object_key = id(page)
    session.setdefault("tab_ids", {})
    session.setdefault("tab_pages", {})
    session.setdefault("diagnostic_pages", set())
    if object_key not in session["tab_ids"]:
        next_id = int(session.get("next_tab_number", 1))
        tab_id = f"tab-{next_id}"
        session["next_tab_number"] = next_id + 1
        session["tab_ids"][object_key] = tab_id
        session["tab_pages"][tab_id] = page
    else:
        tab_id = session["tab_ids"][object_key]
    if object_key not in session["diagnostic_pages"]:
        session["diagnostic_pages"].add(object_key)
        try:
            page.on("console", lambda msg, ref=session: ref["console_errors"].append(msg.text[:500]) if msg.type == "error" and len(ref["console_errors"]) < 50 else None)
            page.on("pageerror", lambda exc, ref=session: ref["page_errors"].append(str(exc)[:1000]) if len(ref["page_errors"]) < 50 else None)
            page.on("requestfailed", lambda req, ref=session: ref["failed_requests"].append(f"{req.method} {req.url[:400]}: {req.failure}") if len(ref["failed_requests"]) < 50 else None)
            page.on("response", lambda resp, ref=session: ref["bad_responses"].append({"status": resp.status, "url": resp.url[:400]}) if resp.status >= 400 and len(ref["bad_responses"]) < 50 else None)
        except Exception as exc:
            logger.debug("Could not attach diagnostics to browser tab %s: %s", tab_id, exc)
    return tab_id


def _list_tabs(context, session: dict[str, Any]) -> list[dict[str, Any]]:
    tabs = []
    active = session.get("page")
    for index, tab in enumerate(list(context.pages)):
        if tab.is_closed():
            continue
        tab_id = _register_page(session, tab)
        try:
            title = tab.title()
        except Exception:
            title = ""
        tabs.append({"tab_id": tab_id, "index": index, "url": tab.url, "title": title, "active": tab is active})
    return tabs


def _select_tab(context, session: dict[str, Any], action: dict[str, Any]):
    pages = [tab for tab in list(context.pages) if not tab.is_closed()]
    if not pages:
        page = context.new_page()
        _register_page(session, page)
        return page
    tab_id = action.get("tab_id")
    if str(tab_id or "").strip().lower() in {"previous", "prev", "previous_tab", "last_tab"}:
        previous = session.get("prev_page")
        if previous is not None and not previous.is_closed() and previous in pages:
            return previous
        if len(pages) >= 2:
            return pages[-2]
        raise BrowserAutomationError("No previous tab is tracked yet. Open a second tab first, then switch back.")
    tab_index = action.get("tab_index")
    title_contains = str(action.get("title_contains", "")).strip().lower()
    url_contains = str(action.get("url_contains", "")).strip().lower()
    if tab_id:
        for tab in pages:
            if _register_page(session, tab) == str(tab_id):
                return tab
        raise BrowserAutomationError(f"Browser tab '{tab_id}' was not found. Available tabs: {_list_tabs(context, session)}")
    if tab_index is not None:
        try:
            index = int(tab_index)
            if index < 0 or index >= len(pages):
                raise IndexError
            return pages[index]
        except (ValueError, TypeError, IndexError):
            raise BrowserAutomationError(f"Invalid tab_index {tab_index!r}; there are {len(pages)} open tabs.")
    if title_contains or url_contains:
        for tab in pages:
            try:
                title = tab.title().lower()
            except Exception:
                title = ""
            if (not title_contains or title_contains in title) and (not url_contains or url_contains in tab.url.lower()):
                return tab
        raise BrowserAutomationError("No open browser tab matches title_contains/url_contains.")
    active = session.get("page")
    if active is not None and not active.is_closed() and active in pages:
        return active
    return pages[-1]

def _scrape_page(page, max_chars: int = MAX_TEXT_CHARS) -> dict[str, Any]:
    max_chars = max(500, min(MAX_TEXT_CHARS, int(max_chars)))
    try:
        title = page.title()
    except Exception:
        title = ""
    text = ""
    links = []
    headings = []
    forms = []
    try:
        text = page.locator("body").inner_text(timeout=5000)[:max_chars]
    except Exception as exc:
        text = f"[Could not read page text: {exc}]"
    try:
        links = page.locator("a[href]").evaluate_all("els => els.slice(0, 100).map(a => ({text:(a.innerText||a.getAttribute('aria-label')||'').trim().slice(0,200), href:a.href}))")
    except Exception:
        pass
    try:
        headings = page.locator("h1,h2,h3").evaluate_all("els => els.slice(0, 100).map(e => ({tag:e.tagName.toLowerCase(), text:(e.innerText||'').trim().slice(0,300)}))")
    except Exception:
        pass
    try:
        forms = page.locator("form").evaluate_all("els => els.slice(0, 50).map(f => ({action:f.action, method:(f.method||'get').toUpperCase(), fields:Array.from(f.querySelectorAll('input,select,textarea,button')).slice(0,50).map(e=>({tag:e.tagName.toLowerCase(),type:e.type||'',name:e.name||'',placeholder:e.placeholder||'',label:(e.labels&&e.labels[0]?e.labels[0].innerText:'').trim(),required:!!e.required}))}))")
    except Exception:
        pass
    controls = []
    frame_reports = []
    try:
        frames = list(page.frames)[:12]
    except Exception:
        frames = []
    for frame_index, frame in enumerate(frames):
        try:
            # Locator engines pierce open shadow roots; DOM querySelectorAll in the
            # previous inspect implementation did not, which hid LinkedIn controls.
            frame_controls = frame.locator('input,textarea,select,button,[role="textbox"],[contenteditable="true"]').evaluate_all("els => els.slice(0,80).map(e => { const labels = Array.from(e.labels||[]).map(l => (l.innerText||'').trim()).filter(Boolean); const parentText = (e.closest('label')?.innerText || e.parentElement?.innerText || '').trim().slice(0,100); const r=e.getBoundingClientRect(); return {tag:e.tagName.toLowerCase(),type:(e.getAttribute('type')||'').toLowerCase(),id:e.id||'',name:e.getAttribute('name')||'',placeholder:e.getAttribute('placeholder')||'',aria_label:e.getAttribute('aria-label')||'',autocomplete:e.getAttribute('autocomplete')||'',role:e.getAttribute('role')||'',label:labels[0]||'',nearby_text:parentText,visible:!!(e.getClientRects().length && getComputedStyle(e).visibility!=='hidden' && getComputedStyle(e).display!=='none'),rect:{x:Math.round(r.x),y:Math.round(r.y),width:Math.round(r.width),height:Math.round(r.height),center_x:Math.round(r.x+r.width/2),center_y:Math.round(r.y+r.height/2)}}; })")
            frame_offset = {"x": 0, "y": 0}
            try:
                current = frame
                for _ in range(8):
                    parent = getattr(current, "parent_frame", None)
                    if parent is None:
                        break
                    frame_element = current.frame_element()
                    box = frame_element.bounding_box()
                    if box:
                        frame_offset["x"] += float(box.get("x", 0))
                        frame_offset["y"] += float(box.get("y", 0))
                    current = parent
            except Exception:
                frame_offset = {"x": None, "y": None}
            for control in frame_controls:
                control["frame_index"] = frame_index
                control["coordinate_space"] = "page_viewport" if frame_index == 0 else "frame_viewport"
                if frame_index > 0 and frame_offset.get("x") is not None and isinstance(control.get("rect"), dict):
                    rect = control["rect"]
                    control["page_coordinates"] = {
                        "center_x": round(frame_offset["x"] + float(rect.get("center_x", 0))),
                        "center_y": round(frame_offset["y"] + float(rect.get("center_y", 0))),
                    }
            if frame_index == 0:
                controls.extend(frame_controls[:60])
            try:
                frame_text = frame.locator("body").inner_text(timeout=500)[:2500]
            except Exception:
                frame_text = ""
            frame_reports.append({"frame_index": frame_index, "url": str(frame.url or ""), "name": str(getattr(frame, "name", "") or ""), "is_main": frame_index == 0, "coordinate_space": "page_viewport" if frame_index == 0 else "frame_viewport", "frame_offset": frame_offset, "text": frame_text, "controls": frame_controls[:35]})
        except Exception as exc:
            frame_reports.append({"frame_index": frame_index, "url": str(getattr(frame, "url", "") or ""), "name": str(getattr(frame, "name", "") or ""), "error": str(exc)[:250], "controls": []})
    # Preserve backwards-compatible top-level fields while adding actionable control
    # inventory for localized forms, shadow DOM, and cross-origin iframes.
    return {"url": page.url, "title": title, "text": text, "headings": headings, "links": links, "forms": forms, "controls": controls, "frames": frame_reports}



_nav_attempts: dict[int, dict[str, Any]] = {}
_nav_attempts_guard = threading.Lock()


def _record_nav_attempt(page, target_url: str) -> bool:
    """Record a goto attempt; return True when the SAME url was attempted moments ago.

    Re-issuing goto while the browser is still loading the same URL aborts the in-flight
    request and restarts loading from zero â€” the classic agent-mode 'page loads halfway,
    spinner forever' loop. Callers use this to wait passively instead of restarting.
    """
    key = id(page)
    now = time.monotonic()
    with _nav_attempts_guard:
        state = _nav_attempts.get(key) or {}
        repeated = state.get("url") == target_url and (now - float(state.get("at", 0.0))) < NAV_RETRY_COOLDOWN_SECONDS
        _nav_attempts[key] = {"url": target_url, "at": now}
    return repeated


def _passive_wait_for_target(page, target_url: str, budget_ms: int) -> bool:
    """Wait WITHOUT touching navigation until the target site commits (same-host URL)."""
    try:
        target_host = (urlsplit(target_url).hostname or "").lower()
        if not target_host:
            return False
        page.wait_for_url(
            lambda url: (urlsplit(str(url or "")).hostname or "").lower() == target_host,
            timeout=max(1000, budget_ms),
        )
        return True
    except Exception:
        return False


def _settle_after_navigation(page, timeout: int) -> None:
    """Give streaming subresources a short, capped window so the page reads fully loaded.

    domcontentloaded returns while images, CSS and late scripts are still streaming; that is
    what made agent-mode pages look 'half loaded'. Waiting best-effort for `load` (capped)
    keeps navigation non-blocking while avoiding premature scrapes.
    """
    try:
        page.wait_for_load_state("load", timeout=max(1500, min(9000, int(timeout) // 4)))
    except Exception:
        pass
    try:
        page.wait_for_timeout(250)
    except Exception:
        pass


def _navigate_page(page, target_url: str, wait_until: str, timeout: int):
    """Navigate without waiting forever, without restart storms, and without raising on timeout.

    Returns (response_or_None, fallback_used, info). A timed-out navigation NEVER aborts the
    task: the page is left exactly as the browser has it and the structured info tells the
    model what happened, so it can inspect the live tab instead of blindly repeating the
    same goto (which would restart the load forever).
    """
    wait_until = str(wait_until or "domcontentloaded").lower().strip()
    if wait_until not in {"commit", "domcontentloaded", "load", "networkidle"}:
        wait_until = "domcontentloaded"
    timeout = max(int(timeout), DEFAULT_NAV_TIMEOUT_MS) if wait_until != "commit" else max(int(timeout), 10000)
    previous_url = str(getattr(page, "url", "") or "")
    repeated = _record_nav_attempt(page, target_url)
    if repeated:
        # The same URL was requested within the cooldown: the earlier attempt is probably
        # still loading. Wait passively instead of aborting and restarting the request.
        if _passive_wait_for_target(page, target_url, min(12000, timeout)):
            _settle_after_navigation(page, timeout)
            logger.warning("Skipped a redundant goto to %s; the earlier load continued to %s.", target_url, page.url)
            return None, True, {"recovered": True, "repeated_goto_suppressed": True, "url": str(getattr(page, "url", "") or ""),
                                "note": "The same URL was already loading; the agent waited instead of restarting it."}
    try:
        response = page.goto(target_url, wait_until=wait_until, timeout=timeout)
        _settle_after_navigation(page, timeout)
        return response, False, {"recovered": False, "repeated_goto_suppressed": False,
                                 "url": str(getattr(page, "url", "") or target_url)}
    except Exception as exc:
        message = str(exc).lower()
        if "timeout" not in message and "timed out" not in message:
            raise
        current_url = str(getattr(page, "url", "") or "")
        target = urlsplit(target_url)
        current = urlsplit(current_url)
        # Redirects such as /login -> /login/ and Google /signin -> /v3/signin are
        # legitimate. Don't require literal string equality when the same origin is active.
        same_origin = bool(target.netloc and current.netloc and target.scheme == current.scheme and target.netloc.lower() == current.netloc.lower())
        target_path = (target.path or "/").rstrip("/")
        current_path = (current.path or "/").rstrip("/")
        same_target_path = same_origin and target_path == current_path and target.query == current.query
        same_origin_redirect = same_origin and current_url != previous_url
        reached_target = current_url == target_url or same_target_path or same_origin_redirect
        if reached_target and current_url not in {"", "about:blank"}:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=min(3000, max(500, timeout)))
            except Exception:
                pass
            _settle_after_navigation(page, timeout)
            logger.warning("Navigation to %s reached %s but `%s` timed out; keeping the page usable.", target_url, current_url, wait_until)
            return None, True, {"recovered": True, "repeated_goto_suppressed": False, "url": current_url,
                                "note": f"Navigation timed out during '{wait_until}' after reaching the site; the loaded page stays usable."}
        # Not obviously at the target yet: give the commit a passive grace window instead of failing.
        if _passive_wait_for_target(page, target_url, min(20000, max(8000, timeout))):
            _settle_after_navigation(page, timeout)
            logger.warning("Navigation to %s committed late (%s) and was kept usable.", target_url, page.url)
            return None, True, {"recovered": True, "repeated_goto_suppressed": repeated, "url": str(getattr(page, "url", "") or ""),
                                "note": "Navigation was slow but completed during the grace window."}
        logger.warning("Navigation to %s did not commit in time; reporting a recoverable navigation_timeout.", target_url)
        return None, False, {"recovered": False, "navigation_timeout": True, "retry_recommended": False,
                             "url": str(getattr(page, "url", "") or previous_url), "error": str(exc)[:400],
                             "note": ("Navigation did not finish. Do NOT immediately repeat the identical goto: inspect the "
                                      "current tab (scrape/screenshot), wait with action wait, or retry once after several seconds.")}


def _automation_snapshot(page, purpose: str = "pre-action", *, screenshot: bool = True) -> dict[str, Any]:
    """Compact, secret-safe page/form snapshot used before browser mutations."""
    snapshot: dict[str, Any] = {"purpose": purpose, "url": str(getattr(page, "url", "") or "")}
    try:
        snapshot["title"] = page.title()
    except Exception:
        snapshot["title"] = ""
    try:
        fields = page.locator('input:not([type="hidden"]), textarea, select, [contenteditable="true"]').evaluate_all("els => els.slice(0,80).map(el => { const labels=Array.from(el.labels||[]).map(x=>(x.innerText||'').trim()).filter(Boolean); const type=(el.getAttribute('type')||el.tagName.toLowerCase()).toLowerCase(); const checked=['checkbox','radio'].includes(type) ? !!el.checked : null; const files=type==='file' ? Array.from(el.files||[]).map(f=>({name:f.name,size:f.size})) : []; const groupChecked=type==='radio' && el.name ? Array.from(document.querySelectorAll('input[type=radio]')).some(r=>r.name===el.name && r.checked) : false; const valuePresent=type==='file' ? files.length>0 : type==='radio' ? groupChecked : checked!==null ? checked : el.tagName==='SELECT' ? !!el.value : (el.isContentEditable ? !!(el.innerText||'').trim() : String(el.value||'').trim().length>0); const r=el.getBoundingClientRect(); const style=getComputedStyle(el); return {tag:el.tagName.toLowerCase(),type,id:el.id||'',name:el.getAttribute('name')||'',label:labels[0]||el.getAttribute('aria-label')||'',placeholder:el.getAttribute('placeholder')||'',required:!!el.required || el.getAttribute('aria-required')==='true',visible:!!(el.getClientRects().length && style.visibility!=='hidden' && style.display!=='none'),disabled:!!el.disabled,has_value:!!valuePresent,checked,selected_text:el.tagName==='SELECT'?(el.selectedOptions[0]?.textContent||'').trim().slice(0,80):'',file_count:files.length,rect:{x:Math.round(r.x),y:Math.round(r.y),width:Math.round(r.width),height:Math.round(r.height)}}; })")
        snapshot["fields"] = fields
    except Exception as exc:
        snapshot["field_inspection_error"] = str(exc)[:180]
    try:
        snapshot["body_text"] = page.locator("body").inner_text(timeout=1000)[:1600]
    except Exception:
        snapshot["body_text"] = ""
    try:
        snapshot["file_inputs"] = page.locator('input[type="file"]').evaluate_all("els => els.slice(0,20).map(el=>({id:el.id||'',name:el.name||'',accept:el.accept||'',multiple:!!el.multiple,visible:!!(el.getClientRects().length && getComputedStyle(el).display!=='none' && getComputedStyle(el).visibility!=='hidden')}))")
    except Exception:
        snapshot["file_inputs"] = []
    if screenshot:
        try:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            path = ARTIFACTS_DIR / f"{re.sub(r'[^a-zA-Z0-9_-]+', '-', purpose)[:24]}-{stamp}.png"
            page.screenshot(path=str(path), full_page=False, timeout=3500, animations="disabled")
            snapshot["screenshot"] = str(path)
        except Exception as exc:
            snapshot["screenshot_error"] = str(exc)[:180]
    # Keep response compact while preserving all required controls that might need answers.
    fields = snapshot.get("fields", [])
    snapshot["field_count"] = len(fields)
    snapshot["missing_required_fields"] = [
        {"label": f.get("label") or f.get("name") or f.get("id") or f.get("type"), "type": f.get("type"), "name": f.get("name"), "placeholder": f.get("placeholder")}
        for f in fields if f.get("required") and f.get("visible") and not f.get("disabled") and not f.get("has_value")
    ][:30]
    snapshot["fields"] = fields[:24]
    snapshot["file_inputs"] = snapshot.get("file_inputs", [])[:12]
    return snapshot

def _auto_handoff(slug: str, page, verification: dict[str, Any] | None, profile_path: Path, extension_paths: list[str]) -> dict[str, Any] | None:
    """If the agent ended up on Google sign-in (or Google rejected the session), hand control to the person."""
    try:
        page_url = str(page.url or "") if page is not None else ""
    except Exception:
        page_url = ""
    rejected = bool(verification and verification.get("kind") == "provider_security_block")
    if not (rejected or _is_google_auth_url(page_url)):
        return None
    if _embedded_ui_enabled():
        # v1.12: never open a physical browser. The sign-in page stays in the embedded
        # Browser tab, where the person can interact with it directly.
        if rejected:
            return {"human_action_required": True, "human_action": "manual_signin", "embedded_only": True,
                    "message": "The provider blocked this headless session. Everything stays inside the embedded Browser tab: open it and complete the check there, or try a different sign-in method. No physical browser was opened."}
        return {"human_action_required": True, "human_action": "manual_signin", "embedded_only": True,
                "message": "Google sign-in is open in the embedded Browser tab. If Google asks for a security check, complete it in that tab and say 'continue'."}
    try:
        outcome = _manual_browser_open(profile_path, extension_paths, "https://accounts.google.com/")
    except Exception as exc:
        logger.warning("Automatic hand-over to the user failed: %s", exc)
        return None
    outcome.update({
        "browser_released_to_user": True, "human_action_required": True, "human_action": "manual_signin",
        "message": "Google does not allow sign-in inside an automated browser, so IntignoAI disconnected and opened a fresh Google sign-in tab for you. " + MANUAL_SIGNIN_MESSAGE,
    })
    outcome.pop("ok", None)
    return outcome


# --- v1.8: all-questions (Yes/No/multi-choice) answering ---------------------------------

_QUESTION_GROUPS_JS = r"""() => {
  const text = (el) => ((el && (el.innerText || el.textContent)) || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => { if (!el) return false; const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return !!(r.width && r.height && s.visibility !== 'hidden' && s.display !== 'none'); };
  const rectOf = (el) => { const r = el.getBoundingClientRect(); return {x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height)}; };
  const labelFor = (el) => {
    const byFor = el.id ? document.querySelector('label[for="' + (window.CSS && CSS.escape ? CSS.escape(el.id) : el.id) + '"]') : null;
    const wrap = el.closest('label');
    return text(byFor || wrap) || el.getAttribute('aria-label') || el.value || '';
  };
  const questionFor = (first) => {
    const fieldset = first.closest('fieldset');
    const legend = fieldset ? fieldset.querySelector('legend') : null;
    if (legend && text(legend)) return text(legend).slice(0, 300);
    const labelledby = first.getAttribute('aria-labelledby');
    if (labelledby) { const t = text(document.getElementById(labelledby)); if (t) return t.slice(0, 300); }
    if (first.getAttribute('aria-label')) return first.getAttribute('aria-label').slice(0, 300);
    let node = first.closest('fieldset, [role="radiogroup"], [role="group"], li, td, section, div');
    for (let i = 0; i < 8 && node; i++) {
      const heading = node.querySelector('h1,h2,h3,h4,h5,h6,[data-testid*="question"],.field-label,legend');
      const t = heading ? text(heading) : '';
      if (t && t.length > 3 && !(heading.contains(first))) return t.slice(0, 300);
      node = node.parentElement;
    }
    return (first.name || first.id || '').slice(0, 160);
  };
  const groups = new Map();
  const allByType = {radio: Array.from(document.querySelectorAll('input[type="radio"]')), checkbox: Array.from(document.querySelectorAll('input[type="checkbox"]'))};
  document.querySelectorAll('input[type="radio"], input[type="checkbox"]').forEach((el) => {
    const key = (el.type === 'radio' && el.name) ? 'radio:' + el.name : 'check:' + (el.name || el.id || groups.size);
    if (!groups.has(key)) groups.set(key, {type: el.type, name: el.name || el.id || '', question: '', first: el, options: []});
    const g = groups.get(key);
    const hitTarget = el.closest('label') || el;
    g.options.push({index: g.options.length, gi: allByType[el.type] ? allByType[el.type].indexOf(el) : -1, value: el.value || '', label: labelFor(el).slice(0, 200),
      checked: !!el.checked, visible: visible(hitTarget), rect: rectOf(hitTarget), disabled: !!el.disabled});
  });
  const out = [];
  groups.forEach((g) => {
    if (!g.options.length) return;
    g.question = questionFor(g.first);
    delete g.first;
    out.push(g);
  });
  return out;
}"""

_ANSWER_YES_WORDS = {"yes", "y", "true", "1", "confirm", "agree", "accept", "ha", "haan"}
_ANSWER_NO_WORDS = {"no", "n", "false", "0", "decline", "disagree", "reject", "na", "nahi"}


def _match_group_answer(question: str, group_name: str, answers: Any):
    """Pick the best matching user/model answer for one question group (pure function)."""
    if not isinstance(answers, list):
        return None
    q = (question or "").lower().strip()
    n = (group_name or "").lower()
    best_value = None
    best_score = 0
    for item in answers:
        if not isinstance(item, dict):
            if isinstance(item, str) and item.strip() and best_score == 0:
                best_value = item
            continue
        answer_text = str(item.get("question", item.get("q", "")) or "").lower().strip()
        value = item.get("answer", item.get("value", item.get("a")))
        if value is None or not str(value).strip():
            continue
        value = str(value)
        if not answer_text:
            if best_score == 0:
                best_value = value
            continue
        score = 0
        if answer_text in q or (q and q in answer_text):
            score = 60 + min(len(answer_text), 60)
        elif n and answer_text in n:
            score = 40
        else:
            q_tokens = set(re.findall(r"[a-z0-9]{3,}", q))
            a_tokens = set(re.findall(r"[a-z0-9]{3,}", answer_text))
            overlap = len(q_tokens & a_tokens)
            if overlap:
                score = 20 + overlap * 5
        if score > best_score:
            best_score = score
            best_value = value
    return best_value


def _pick_group_option(options: list[dict[str, Any]], desired: str):
    """Resolve 'Yes'/'No'/'label text' to the concrete option element descriptor."""
    wanted = str(desired or "").strip().lower()
    if not wanted:
        return None
    for opt in options:
        if opt.get("disabled"):
            continue
        label = str(opt.get("label") or "").strip().lower()
        value = str(opt.get("value") or "").strip().lower()
        if wanted in {label, value}:
            return opt
    for opt in options:
        if opt.get("disabled"):
            continue
        label = str(opt.get("label") or "").strip().lower()
        value = str(opt.get("value") or "").strip().lower()
        if wanted and (wanted in label or wanted in value):
            return opt
    word_set = _ANSWER_YES_WORDS if wanted in _ANSWER_YES_WORDS else (_ANSWER_NO_WORDS if wanted in _ANSWER_NO_WORDS else None)
    if word_set:
        for opt in options:
            if opt.get("disabled"):
                continue
            label = str(opt.get("label") or "").strip().lower()
            value = str(opt.get("value") or "").strip().lower()
            first_word = label.split(" ")[0] if label else ""
            if label in word_set or first_word in word_set or value in word_set:
                return opt
    return None


def _click_group_option(frame, group: dict[str, Any], chosen: dict[str, Any], page) -> dict[str, Any]:
    """Click one option with verification; escalate JS click -> real mouse coordinates."""
    name = str(group.get("name") or "")
    element_type = str(group.get("type") or "radio")
    index = int(chosen.get("index", 0))
    selector_js = """([name, type, idx, gi]) => {
      const all = Array.from(document.querySelectorAll('input[type="' + type + '"]'));
      const els = name ? all.filter(el => el.name === name) : all;
      const el = name ? els[idx] : all[gi];
      if (!el) return {found: false};
      el.click();
      return {found: true, checked: !!el.checked};
    }"""
    verify_js = """([name, type, idx, gi]) => {
      const all = Array.from(document.querySelectorAll('input[type="' + type + '"]'));
      const els = name ? all.filter(el => el.name === name) : all;
      const el = name ? els[idx] : all[gi];
      return {found: !!el, checked: !!(el && el.checked)};
    }"""
    try:
        outcome = frame.evaluate(selector_js, [name, element_type, index, int(chosen.get("gi", -1))])
        if outcome and outcome.get("found") and outcome.get("checked"):
            return {"verified": True, "mode": "js_click"}
    except Exception as exc:
        logger.debug("JS group click failed (%s): %s", name, exc)
    # Real-mouse fallback on the visible label hit area (what a human actually clicks).
    try:
        rect = chosen.get("rect") or {}
        x, y = rect.get("x"), rect.get("y")
        width, height = rect.get("width", 0), rect.get("height", 0)
        if x is None or not width or not height:
            return {"verified": False, "mode": "coordinate_click", "error": "No clickable geometry for the chosen option."}
        offset = _frame_page_offset(frame)
        if offset.get("x") is None:
            return {"verified": False, "mode": "coordinate_click", "error": "Frame offset unavailable for coordinate click."}
        page.mouse.click(float(offset["x"]) + float(x) + min(12, float(width) / 2), float(offset["y"]) + float(y) + min(12, float(height) / 2))
        verify = frame.evaluate(verify_js, [name, element_type, index, int(chosen.get("gi", -1))])
        return {"verified": bool(verify and verify.get("checked")), "mode": "coordinate_click"}
    except Exception as exc:
        return {"verified": False, "mode": "coordinate_click", "error": str(exc)[:200]}


def _answer_question_groups(page, action: dict[str, Any]) -> dict[str, Any]:
    """Answer EVERY Yes/No/multiple-choice question group on the page (main doc + iframes).

    Fixes the v1.7.2 flaw where only the first Yes/No pair was clicked. The model supplies
    one answer per question; each click is verified via the input's checked state and falls
    back to real-mouse coordinates on the visible label when JS clicks do not register.
    """
    answers = action.get("answers", action.get("groups", []))
    if isinstance(answers, str):
        try:
            answers = json.loads(answers)
        except ValueError:
            answers = []
    default_answer = str(action.get("default_answer", "") or "").strip()
    include_hidden = bool(action.get("include_hidden", False))
    results: list[dict[str, Any]] = []
    unanswered: list[dict[str, Any]] = []
    frames = _frame_candidates(page, action)[:6]
    for frame_index, frame in enumerate(frames):
        try:
            groups = frame.evaluate(_QUESTION_GROUPS_JS)
        except Exception as exc:
            logger.debug("Question-group collection failed in frame %s: %s", frame_index, exc)
            continue
        for group in groups or []:
            if not isinstance(group, dict) or not group.get("options"):
                continue
            options = list(group.get("options") or [])
            selectable = [o for o in options if (o.get("visible") or include_hidden) and not o.get("disabled")]
            if not selectable:
                selectable = [o for o in options if not o.get("disabled")]
            if not selectable:
                continue
            question = str(group.get("question", "") or "")
            desired = _match_group_answer(question, str(group.get("name", "")), answers)
            if desired is None and default_answer:
                desired = default_answer
            chosen = _pick_group_option(selectable, str(desired)) if desired is not None else None
            if chosen is None:
                unanswered.append({
                    "frame_index": frame_index, "question": question[:300], "name": str(group.get("name", "")),
                    "options": [str(o.get("label") or o.get("value") or "") for o in selectable],
                    "reason": "no matching answer supplied" if desired is None else f"answer '{desired}' did not match any option label/value",
                })
                continue
            click = _click_group_option(frame, group, chosen, page)
            results.append({
                "frame_index": frame_index, "question": question[:300], "name": str(group.get("name", "")),
                "chosen": str(chosen.get("label") or chosen.get("value") or ""),
                "verified": bool(click.get("verified")), "mode": click.get("mode", "js_click"),
                **({"error": click["error"]} if click.get("error") else {}),
            })
    verified_count = sum(1 for item in results if item.get("verified"))
    return {
        "ok": True, "action": "answer_radiogroups", "groups_found": len(results) + len(unanswered),
        "answered": verified_count, "results": results, "unanswered": unanswered,
        "note": ("EVERY question group found was processed. 'verified' means the input reports checked=true after "
                 "the click. Provide answers for anything in 'unanswered' and call this action again."),
    }


# --- v1.8: lazy-load scrolling, raw HTML extraction ---------------------------------------

def _scroll_until_stable(page, action: dict[str, Any]) -> dict[str, Any]:
    """Scroll a page like a human until height stops growing (lazy/infinite lists)."""
    pause_ms = max(120, min(3000, int(action.get("pause_ms", 700))))
    max_scrolls = max(1, min(200, int(action.get("max_scrolls", 40))))
    stabilize_rounds = max(1, min(6, int(action.get("stabilize_rounds", 2))))
    per_scroll = float(action.get("delta_y", 900) or 900)
    selector = str(action.get("selector", "")).strip()
    if selector:
        try:
            _selector(page, selector).first.scroll_into_view_if_needed(timeout=4000)
        except Exception as exc:
            logger.debug("scroll_into_view failed for %s: %s", selector, exc)
    scrolled = 0
    stable = 0
    last_height = -1
    last_y = -1
    while scrolled < max_scrolls and stable < stabilize_rounds:
        metrics = page.evaluate("() => ({h: document.documentElement.scrollHeight, y: Math.round(window.scrollY + window.innerHeight)})")
        height = int(metrics.get("h", 0))
        y = int(metrics.get("y", 0))
        if height == last_height and y == last_y:
            stable += 1
        else:
            stable = 0
        last_height, last_y = height, y
        if stable >= stabilize_rounds:
            break
        try:
            page.mouse.wheel(0, per_scroll)
        except Exception:
            page.evaluate("(dy) => window.scrollBy(0, dy)", per_scroll)
        page.wait_for_timeout(pause_ms)
        scrolled += 1
    final = page.evaluate("() => ({height: document.documentElement.scrollHeight, scroll_y: Math.round(window.scrollY)})")
    return {"ok": True, "action": "scroll_until_stable", "scrolls": scrolled,
            "final_height": final.get("height"), "scroll_y": final.get("scroll_y")}


def _collect_html(page, action: dict[str, Any]) -> dict[str, Any]:
    """Full raw HTML (optionally per-frame) after any requested lazy-load scrolling."""
    max_chars = max(2000, min(600000, int(action.get("max_html_chars", action.get("max_chars", 200000)))))
    include_frames = bool(action.get("all_frames", action.get("include_iframes", False)))
    frame_hint = str(action.get("frame_url_contains", action.get("frame_selector", ""))).strip().lower()
    parts: list[dict[str, Any]] = []
    try:
        frames = list(page.frames)[:10]
    except Exception:
        frames = []
    for index, frame in enumerate(frames):
        frame_url = str(getattr(frame, "url", "") or "")
        if frame_hint and frame_hint not in frame_url.lower() and frame_hint != str(getattr(frame, "name", "")).lower():
            continue
        if index == 0 and frame_hint:
            continue
        try:
            html = frame.content()
        except Exception as exc:
            parts.append({"frame_index": index, "url": frame_url, "error": str(exc)[:200]})
            continue
        parts.append({"frame_index": index, "url": frame_url, "name": str(getattr(frame, "name", "") or ""), "html": html[:max_chars]})
    if not parts:
        parts.append({"error": "No matching frame for get_html."})
    total = sum(len(part.get("html", "")) for part in parts)
    return {"ok": True, "action": "get_html", "parts": parts, "total_chars": total,
            "note": "HTML truncated at max_html_chars per frame; raise the budget or use per-frame hints if needed."}


# --- v1.8: CAPTCHA / human-verification support -------------------------------------------

def _detect_captcha_widgets(page) -> list[dict[str, Any]]:
    js = """() => {
      const out = [];
      const push = (type, sitekey, extra) => { if (sitekey || extra) out.push(Object.assign({type, sitekey: sitekey || ''}, extra || {})); };
      document.querySelectorAll('.g-recaptcha[data-sitekey]').forEach(el => push('recaptcha_v2', el.getAttribute('data-sitekey')));
      document.querySelectorAll('[data-sitekey]').forEach(el => {
        const cls = (el.className || '').toString();
        if (cls.includes('h-captcha') || el.hasAttribute('data-hcaptcha-widget-id')) push('hcaptcha', el.getAttribute('data-sitekey'));
        else if (cls.includes('cf-turnstile') || el.hasAttribute('data-turnstile-sitekey')) push('turnstile', el.getAttribute('data-sitekey'));
      });
      document.querySelectorAll('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="challenges.cloudflare.com"]').forEach(f => {
        const src = f.getAttribute('src') || '';
        const sitekeyMatch = (/[?&]k=([A-Za-z0-9_-]{20,})/.exec(src) || /sitekey=([A-Za-z0-9_-]{20,})/i.exec(src) || [])[1] || '';
        push(src.includes('recaptcha') ? 'recaptcha_iframe' : (src.includes('hcaptcha') ? 'hcaptcha_iframe' : 'challenge_iframe'), sitekeyMatch, {src: src.slice(0, 300)});
      });
      return out;
    }"""
    widgets: list[dict[str, Any]] = []
    for frame in _frame_candidates(page, {})[:6]:
        try:
            for item in frame.evaluate(js) or []:
                if isinstance(item, dict) and item not in widgets:
                    widgets.append(item)
        except Exception:
            continue
    return widgets[:10]


def _try_click_checkbox_challenge(page) -> dict[str, Any]:
    """One human-equivalent attempt to click a visible 'I'm not a robot' style checkbox."""
    attempts: list[str] = []
    try:
        frames = list(page.frames)[1:8]
    except Exception:
        frames = []
    for frame in frames:
        frame_url = str(getattr(frame, "url", "") or "")
        try:
            if "recaptcha" in frame_url and "anchor" in frame_url:
                frame.locator(".recaptcha-checkbox-border").click(timeout=3000)
                return {"ok": True, "target": "recaptcha_checkbox"}
            if "challenges.cloudflare.com" in frame_url:
                frame.locator("input[type='checkbox']").click(timeout=3000)
                return {"ok": True, "target": "turnstile_checkbox"}
            if "hcaptcha" in frame_url and "checkbox" in frame_url:
                frame.locator("#checkbox").click(timeout=3000)
                return {"ok": True, "target": "hcaptcha_checkbox"}
        except Exception as exc:
            attempts.append(str(exc)[:120])
    return {"ok": False, "attempts": attempts[:5]}


def _two_captcha_solve(solver_key: str, challenge_type: str, sitekey: str, page_url: str) -> str | None:
    """Optional paid-solver path (user's own 2Captcha account). Returns the answer or None."""
    method_map = {"recaptcha_v2": "userrecaptcha", "recaptcha": "userrecaptcha", "hcaptcha": "hcaptcha", "turnstile": "turnstile"}
    method = method_map.get(challenge_type)
    if not method:
        return None
    params: dict[str, str] = {"key": solver_key, "pageurl": page_url, "json": "1"}
    if method == "userrecaptcha":
        params.update({"method": "userrecaptcha", "googlekey": sitekey})
    elif method == "hcaptcha":
        params.update({"method": "hcaptcha", "sitekey": sitekey})
    else:
        params.update({"method": "turnstile", "sitekey": sitekey})
    try:
        request = urllib.request.Request(
            "https://2captcha.com/in.php",
            data=urllib.parse.urlencode(params).encode(),
            headers={"User-Agent": _IMAGE_USER_AGENT},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        if payload.get("status") != 1:
            logger.warning("CAPTCHA service rejected the task: %s", payload)
            return None
        task_id = payload.get("request")
    except Exception as exc:
        logger.warning("CAPTCHA submit failed: %s", exc)
        return None
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        time.sleep(6)
        try:
            poll = urllib.request.Request(
                "https://2captcha.com/res.php?" + urllib.parse.urlencode({"key": solver_key, "action": "get", "id": task_id, "json": "1"}),
                headers={"User-Agent": _IMAGE_USER_AGENT},
            )
            with urllib.request.urlopen(poll, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
            if payload.get("status") == 1:
                return str(payload.get("request") or "")
            if "CAPCHA_NOT_READY" not in str(payload.get("request", "")):
                logger.warning("CAPTCHA poll stopped: %s", payload)
                return None
        except Exception as exc:
            logger.debug("CAPTCHA poll failed: %s", exc)
    return None


def _inject_captcha_answer(page, challenge_type: str, challenge_answer: str) -> dict[str, Any]:
    js = """([type, responseText]) => {
      const filled = [];
      const setField = (el) => { if (el && !filled.includes(el)) { el.value = responseText; filled.push(el); } };
      let callback_called = false;
      if (type === 'recaptcha_v2' || type === 'recaptcha') {
        document.querySelectorAll('#g-recaptcha-response, [name="g-recaptcha-response"]').forEach(setField);
        try {
          const clients = window.___grecaptcha_cfg && window.___grecaptcha_cfg.clients;
          if (clients) { for (const clientKey of Object.keys(clients)) {
            const iterate = (node) => { if (!node || callback_called || typeof node !== 'object') return;
              for (const key of Object.keys(node)) { const value = node[key];
                if (value && typeof value === 'object') {
                  if (typeof value.callback === 'function') { try { value.callback(responseText); callback_called = true; } catch (e) {} }
                  iterate(value);
                } } };
            iterate(clients[clientKey]);
          } }
        } catch (e) {}
        return {submitted: filled.length > 0, callback_called};
      }
      if (type === 'hcaptcha') {
        document.querySelectorAll('[name="h-captcha-response"], [name="g-recaptcha-response"]').forEach(setField);
        filled.forEach(el => el.dispatchEvent(new Event('input', {bubbles: true})));
        return {submitted: filled.length > 0, callback_called};
      }
      if (type === 'turnstile') {
        document.querySelectorAll('[name="cf-turnstile-response"]').forEach(setField);
        filled.forEach(el => el.dispatchEvent(new Event('input', {bubbles: true})));
        return {submitted: filled.length > 0, callback_called};
      }
      return {submitted: false};
    }"""
    try:
        return page.evaluate(js, [challenge_type, challenge_answer]) or {"submitted": False}
    except Exception as exc:
        return {"submitted": False, "error": str(exc)[:200]}


def _infer_solver_target(widgets: list[dict[str, Any]]) -> dict[str, str]:
    for widget in widgets:
        if widget.get("sitekey") and widget.get("type") in {"recaptcha_v2", "hcaptcha", "turnstile"}:
            return {"type": str(widget.get("type")), "sitekey": str(widget.get("sitekey"))}
    for widget in widgets:
        src = str(widget.get("src", ""))
        key_match = re.search(r"[?&]k=([A-Za-z0-9_-]{20,})", src) or re.search(r"sitekey=([A-Za-z0-9_-]{20,})", src, re.I)
        if "recaptcha" in src and key_match:
            return {"type": "recaptcha_v2", "sitekey": key_match.group(1)}
        if "hcaptcha" in src and key_match:
            return {"type": "hcaptcha", "sitekey": key_match.group(1)}
    return {"type": "", "sitekey": ""}


def _solve_captcha(context, page, action: dict[str, Any]) -> dict[str, Any]:
    """Layered CAPTCHA handling: auto-clear watch -> checkbox click -> optional solver service -> human.

    The solver service is used ONLY when the user configured their own account in .env
    (CAPTCHA_SOLVER_PROVIDER=2captcha plus CAPTCHA_SOLVER_API_KEY). Nothing is fabricated:
    the result honestly reports whether the challenge actually resolved.
    """
    widgets = _detect_captcha_widgets(page)
    page_url = str(getattr(page, "url", "") or "")
    checkbox = _try_click_checkbox_challenge(page)
    if checkbox.get("ok"):
        try:
            page.wait_for_timeout(3000)
        except Exception:
            pass
        if _browser_is_human_verification(page) is None:
            return {"ok": True, "action": "solve_captcha", "resolved": True, "resolved_by": "checkbox_click", "widgets": widgets}
    provider = str(os.getenv("CAPTCHA_SOLVER_PROVIDER", "") or action.get("provider") or "").strip().lower()
    solver_key = str(os.getenv("CAPTCHA_SOLVER_API_KEY", "")).strip()
    if not provider or not solver_key:
        return {
            "ok": False, "action": "solve_captcha", "resolved": False, "widgets": widgets,
            "checkbox_attempt": checkbox, "human_action_required": True,
            "message": ("No CAPTCHA solver service is configured. Complete the challenge manually in the visible "
                        "browser and say 'continue'. To enable automated solving, set CAPTCHA_SOLVER_PROVIDER=2captcha "
                        "and CAPTCHA_SOLVER_API_KEY in .env (your own paid account)."),
        }
    if provider != "2captcha":
        return {"ok": False, "action": "solve_captcha", "resolved": False, "message": f"Unsupported CAPTCHA_SOLVER_PROVIDER '{provider}'; use '2captcha'."}
    sitekey = str(action.get("sitekey", "") or "").strip()
    challenge_type = str(action.get("challenge_type", "") or "").strip().lower()
    if not sitekey or not challenge_type:
        inferred = _infer_solver_target(widgets)
        sitekey = sitekey or inferred.get("sitekey", "")
        challenge_type = challenge_type or inferred.get("type", "")
    if not sitekey or not challenge_type:
        return {"ok": False, "action": "solve_captcha", "resolved": False, "widgets": widgets, "human_action_required": True,
                "message": "Could not detect a solvable CAPTCHA sitekey on this page; complete it manually."}
    challenge_answer = _two_captcha_solve(solver_key, challenge_type, sitekey, page_url)
    if not challenge_answer:
        return {"ok": False, "action": "solve_captcha", "resolved": False, "widgets": widgets, "human_action_required": True,
                "message": "The solver service did not return a result in time; complete the challenge manually."}
    injection = _inject_captcha_answer(page, challenge_type, challenge_answer)
    resolved = bool(injection.get("submitted"))
    try:
        if resolved:
            page.wait_for_timeout(1500)
            resolved = _browser_is_human_verification(page) is None
    except Exception:
        pass
    return {"ok": resolved, "action": "solve_captcha", "resolved": resolved, "challenge_type": challenge_type,
            "response_length": len(challenge_answer), "injection": injection, "widgets": widgets,
            **({"human_action_required": True} if not resolved else {})}


def _detect_verification(page) -> dict[str, Any]:
    verification = _browser_is_human_verification(page)
    return {"ok": True, "action": "detect_verification", "verification_present": verification is not None,
            "verification": verification, "widgets": _detect_captcha_widgets(page)}


# --- v1.9: atomic form filling ------------------------------------------------------------

def _fill_form(page, action: dict[str, Any], timeout: int) -> dict[str, Any]:
    """Fill many fields atomically with per-field verification (data-entry / application forms).

    Each field: {selector | label | placeholder | name, value, type: fill|type|select|check|uncheck|upload_file, clear}.
    Continues past individual failures (reported per field) so the model can repair only what broke.
    """
    fields = action.get("fields")
    if not isinstance(fields, list) or not fields:
        raise BrowserAutomationError("fill_form needs a non-empty `fields` array of {selector|label|placeholder|name, value, type}.")
    results: list[dict[str, Any]] = []
    for index, field in enumerate(fields[:60]):
        if not isinstance(field, dict):
            results.append({"index": index, "ok": False, "error": "field must be an object"})
            continue
        kind = str(field.get("type", "fill")).strip().lower()
        sub_action = {k: v for k, v in field.items() if k != "type"}
        sub_action["action"] = "upload_file" if kind == "upload_file" else kind
        target = str(field.get("selector") or field.get("label") or field.get("name") or field.get("placeholder") or kind)
        try:
            result = _do_locator_action(page, sub_action, timeout)
            results.append({"index": index, "ok": True, "target": target[:120],
                            "mode": result.get("mode"), "verification": result.get("verification")})
        except Exception as exc:
            results.append({"index": index, "ok": False, "target": target[:120], "error": str(exc)[:280]})
    filled = sum(1 for item in results if item.get("ok"))
    submitted = False
    submit_selector = str(action.get("submit_selector", "")).strip()
    if submit_selector and filled == len(results):
        try:
            _do_locator_action(page, {"action": "click", "selector": submit_selector}, timeout)
            submitted = True
        except Exception as exc:
            results.append({"index": len(results), "ok": False, "target": submit_selector[:120], "error": f"submit failed: {str(exc)[:200]}"})
    return {"ok": filled > 0 and filled == len(results), "action": "fill_form", "filled": filled,
            "total_fields": len(results), "submitted": submitted, "results": results}


# --- v1.9: YouTube engagement (like / subscribe / comment) ---------------------------------

def _sign_in_dialog_visible(page) -> bool:
    for css in ("yt-sign-in-dialog-view-model", "yt-confirm-dialog-view-model[dialog][aria-label*='Sign in' i]"):
        try:
            if page.locator(css).first.is_visible(timeout=800):
                return True
        except Exception:
            continue
    try:
        body = page.locator("body").inner_text(timeout=800)[:3000].lower()
        return "sign in to" in body and ("like" in body or "subscribe" in body or "comment" in body)
    except Exception:
        return False


def _youtube_like(page) -> dict[str, Any]:
    candidates = (
        "ytd-segmented-like-dislike-button-view-model like-button-view-model button",
        "ytd-watch-metadata like-button-view-model button",
        "ytd-menu-renderer ytd-toggle-button-renderer button[aria-label*='like' i]",
        "button[aria-label*='like this video' i]",
        "button[title*='Like' i]",
    )
    last_error = None
    for css in candidates:
        try:
            button = page.locator(css).first
            if button.count() == 0:
                continue
            pressed = button.get_attribute("aria-pressed")
            if str(pressed).strip().lower() == "true":
                return {"ok": True, "action": "youtube_like", "already_liked": True, "selector": css}
            button.click(timeout=6000)
            try:
                page.wait_for_timeout(700)
            except Exception:
                pass
            now = button.get_attribute("aria-pressed")
            liked = str(now).strip().lower() == "true"
            if not liked and pressed is None:
                liked = False
            if not liked and _sign_in_dialog_visible(page):
                return {"ok": False, "action": "youtube_like", "requires_login": True,
                        "message": "YouTube wants a signed-in account to like; complete sign-in (embedded or manual) and retry.", "selector": css}
            return {"ok": liked, "action": "youtube_like", "liked": liked, "selector": css,
                    **({"note": "pressed state not exposed by this layout; verify visually"} if now is None else {})}
        except Exception as exc:
            last_error = exc
            continue
    raise BrowserAutomationError(f"youtube_like could not find a like button on this page. Last error: {last_error}")


def _youtube_subscribe(page) -> dict[str, Any]:
    candidates = (
        "#subscribe-button ytd-subscribe-button-view-model button",
        "#subscribe-button ytd-subscribe-button-renderer button",
        "#subscribe-button button[aria-subscribed]",
        "ytd-watch-metadata #subscribe-button button",
        "button[aria-label*='Subscribe' i]",
    )
    last_error = None
    for css in candidates:
        try:
            button = page.locator(css).first
            if button.count() == 0:
                continue
            state = str(button.get_attribute("aria-subscribed") or "").strip().lower()
            label = str(button.get_attribute("aria-label") or "").strip().lower()
            if state == "true" or "subscribed" in label:
                return {"ok": True, "action": "youtube_subscribe", "already_subscribed": True, "selector": css}
            button.click(timeout=6000)
            try:
                page.wait_for_timeout(900)
            except Exception:
                pass
            state_after = str(button.get_attribute("aria-subscribed") or "").strip().lower()
            label_after = str(button.get_attribute("aria-label") or "").strip().lower()
            subscribed = state_after == "true" or "subscribed" in label_after
            if not subscribed and _sign_in_dialog_visible(page):
                return {"ok": False, "action": "youtube_subscribe", "requires_login": True,
                        "message": "YouTube wants a signed-in account to subscribe; complete sign-in and retry.", "selector": css}
            return {"ok": subscribed, "action": "youtube_subscribe", "subscribed": subscribed, "selector": css}
        except Exception as exc:
            last_error = exc
            continue
    raise BrowserAutomationError(f"youtube_subscribe could not find the subscribe button. Last error: {last_error}")


def _youtube_comment(page, action: dict[str, Any]) -> dict[str, Any]:
    text = str(action.get("text", action.get("value", ""))).strip()
    if not text:
        raise BrowserAutomationError("youtube_comment needs the comment text in `text`.")
    # Open the inline comment editor (watch page bottom comments section).
    opened = False
    for css in ("ytd-comments-entry-point-header-renderer #placeholder-area", "#placeholder-area", "ytd-comment-simplebox-renderer #placeholder-area"):
        try:
            area = page.locator(css).first
            if area.count():
                area.scroll_into_view_if_needed(timeout=3000)
                area.click(timeout=4000)
                opened = True
                break
        except Exception:
            continue
    if not opened:
        raise BrowserAutomationError("youtube_comment could not open the comment editor (is this a watch page with comments enabled?).")
    try:
        page.wait_for_timeout(600)
    except Exception:
        pass
    editor = page.locator("#contenteditable-root").first
    try:
        editor.wait_for(state="visible", timeout=5000)
    except Exception as exc:
        raise BrowserAutomationError(f"Comment editor did not appear after clicking the placeholder: {exc}")
    editor.click(timeout=4000)
    page.keyboard.insert_text(text)
    try:
        page.wait_for_timeout(400)
    except Exception:
        pass
    for css in ("#submit-button", "ytd-button-renderer#submit-button button", "button[aria-label='Comment']"):
        try:
            submit = page.locator(css).first
            if submit.count() and submit.is_enabled(timeout=2000):
                submit.click(timeout=4000)
                try:
                    page.wait_for_timeout(1200)
                except Exception:
                    pass
                if _sign_in_dialog_visible(page):
                    return {"ok": False, "action": "youtube_comment", "requires_login": True,
                            "message": "Commenting needs a signed-in account; complete sign-in and retry."}
                cleared = False
                try:
                    cleared = (editor.inner_text(timeout=1500) or "").strip() == ""
                except Exception:
                    pass
                return {"ok": True, "action": "youtube_comment", "submitted": True, "editor_cleared": cleared,
                        "note": "Verify the comment under the video (sort by Newest) if certainty is required."}
        except Exception:
            continue
    raise BrowserAutomationError("youtube_comment could not find the Comment submit button.")


# --- v1.9: LinkedIn adapters (login, posting, job scraping, Easy Apply) ---------------------


def _linkedin_is_logged_in(page) -> bool:
    """Inverted check: an auth surface (login URL, credential field, join wall) means logged out.

    v1.9 note: the logged-in header classes change across LinkedIn releases (and the guest jobs
    layout has none at all), so detecting the ABSENCE of auth surfaces is far more stable.
    """
    url = str(getattr(page, "url", "") or "")
    if "/login" in url or "authwall" in url or "/signup" in url or "checkpoint/challenge" in url:
        return False
    try:
        if page.locator("input[type='password']").count():
            return False
    except Exception:
        pass
    try:
        body = page.locator("body").inner_text(timeout=1500)[:5000].lower()
        if ("sign in" in body and "join linkedin" in body) or "sign up | linkedin" in body or "login | linkedin" in body:
            return False
    except Exception:
        pass
    return True


def _first_visible_locator(scope, selectors, per_timeout: int = 3500):
    """Return the first VISIBLE match across candidate CSS selectors.

    Modern LinkedIn renders a hidden prerendered copy next to the live copy of every auth
    field (React useId '_R_...' ids). Choosing '.first' blindly targets the hidden one; this
    helper skips it and returns (locator, css_used).
    """
    last_error = None
    for css in selectors:
        try:
            locator = scope.locator(css)
            count = min(locator.count(), 12)
            for idx in range(count):
                candidate = locator.nth(idx)
                try:
                    candidate.wait_for(state="visible", timeout=per_timeout)
                    return candidate, css
                except Exception as exc:
                    last_error = exc
                    continue
        except Exception as exc:
            last_error = exc
    raise BrowserAutomationError(f"No visible element matched any of {list(selectors)}. Last error: {str(last_error)[:200]}")


def _linkedin_login(page, action: dict[str, Any]) -> dict[str, Any]:
    """Sign in on LinkedIn's own login form. Credentials are used in-form only and never logged."""
    email = str(action.get("email", action.get("value", ""))).strip()
    password = str(action.get("password", "")).strip()
    if not email or not password:
        raise BrowserAutomationError("linkedin_login needs `email` and `password` (used only inside the login form; never logged or stored).")
    if "/login" not in str(getattr(page, "url", "")):
        _navigate_page(page, "https://www.linkedin.com/login", "domcontentloaded", 30000)
    form_appeared = False
    for attempt in range(2):
        try:
            # state=attached: the 2026 login page renders a HIDDEN copy of each field first,
            # so the default 'visible' state waits on the hidden element and never returns.
            page.wait_for_selector("#username, input[type='email'], input[autocomplete*='username']", state="attached", timeout=20000)
            form_appeared = True
            break
        except Exception:
            if attempt == 0:
                # Cold load or an authwall detour: one direct retry before giving up.
                try:
                    page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=30000)
                    try:
                        page.wait_for_load_state("load", timeout=8000)
                    except Exception:
                        pass
                except Exception:
                    pass
    if not form_appeared:
        if _linkedin_is_logged_in(page):
            return {"ok": True, "action": "linkedin_login", "already_logged_in": True, "url": page.url}
        body_head = ""
        page_title = ""
        try:
            body_head = page.locator("body").inner_text(timeout=2000)[:300]
        except Exception:
            pass
        try:
            page_title = (page.title() or "")[:120]
        except Exception:
            pass
        return {"ok": False, "action": "linkedin_login", "url": str(getattr(page, "url", "") or ""),
                "page_title": page_title, "body_head": body_head,
                "message": "The LinkedIn login form did not appear after two attempts. Inspect the embedded Browser tab (challenge/authwall possible) and retry."}
    try:
        username_loc, _used_css = _first_visible_locator(page, (
            "input[autocomplete='username webauthn']",
            "input[autocomplete='username']",
            "input[type='email']",
            "#username",
        ))
        username_loc.fill(email, timeout=10000)
        password_loc, _used_css2 = _first_visible_locator(page, (
            "input[autocomplete='current-password']",
            "input[type='password']",
            "#password",
        ))
        password_loc.fill(password, timeout=10000)
    except BrowserAutomationError:
        raise
    submitted = False
    try:
        password_loc.press("Enter", timeout=6000)
        submitted = True
    except Exception:
        pass
    if not submitted:
        try:
            buttons = page.get_by_role("button", name=re.compile(r"sign in|log ?in|সাইন ইন", re.I))
            for idx in range(min(buttons.count(), 8)):
                candidate = buttons.nth(idx)
                try:
                    if candidate.is_visible():
                        candidate.click(timeout=6000)
                        submitted = True
                        break
                except Exception:
                    continue
        except Exception:
            pass
    if not submitted:
        raise BrowserAutomationError("Could not submit the LinkedIn login form (no visible submit control responded).")
    try:
        page.wait_for_timeout(2500)
    except Exception:
        pass
    url = str(page.url or "")
    lowered = url.lower()
    if "checkpoint" in lowered or "challenge" in lowered or "collect" in lowered:
        return {"ok": False, "action": "linkedin_login", "requires_human": True, "url": url,
                "message": "LinkedIn requested an extra verification step (PIN/CAPTCHA). Complete it in the embedded Browser tab, then continue."}
    if _linkedin_is_logged_in(page) or "/feed" in url:
        return {"ok": True, "action": "linkedin_login", "logged_in": True, "url": url}
    error_text = ""
    try:
        error_text = page.locator("#error, .form__input-error, .artdeco-inline-feedback--error").first.inner_text(timeout=1500)[:200]
    except Exception:
        pass
    return {"ok": False, "action": "linkedin_login", "url": url,
            "error": error_text or "Login did not reach the feed; the credentials may be wrong or a challenge appeared."}


def _google_is_logged_in(page) -> bool:
    """Signed in when we are on a non-auth Google/other page and no credential field is showing."""
    url = str(getattr(page, "url", "") or "")
    if _is_google_auth_url(url):
        return False
    try:
        if page.locator("input[type='password'], input[type='email'][name='identifier']").count():
            return False
    except Exception:
        pass
    return True


def _google_challenge_kind(page) -> str | None:
    url = str(getattr(page, "url", "") or "").lower()
    try:
        body = page.locator("body").inner_text(timeout=1500)[:4000].lower()
    except Exception:
        body = ""
    if "this browser or app may not be secure" in body or "/signin/rejected" in url:
        return "blocked"
    if any(k in url for k in ("/challenge", "/signin/v2/challenge", "speedbump", "/deniedsigninrejected")):
        return "challenge"
    if any(k in body for k in ("2-step verification", "verify it", "confirm it", "enter the code", "check your phone", "captcha", "type the text you hear or see")):
        return "challenge"
    return None


def _google_login(page, action: dict[str, Any]) -> dict[str, Any]:
    """Sign in to Google on Google's own form, inside the embedded browser.

    Credentials come from the person's current message, are typed only into Google's
    form, and are never logged or stored. 2-step verification, CAPTCHAs and "unusual
    activity" checks are NOT bypassed: they are returned as requires_human so the person
    completes them inside the embedded Browser tab (no physical window is ever opened).
    """
    email = str(action.get("email", action.get("value", ""))).strip()
    password = str(action.get("password", "")).strip()
    if not email or not password:
        raise BrowserAutomationError("google_login needs `email` and `password` from the user's current message (typed only into Google's own form; never logged or stored).")
    cont = str(action.get("continue_url", "")).strip()
    target = "https://accounts.google.com/signin/v2/identifier?service=mail&flowName=GlifWebSignIn&flowEntry=ServiceLogin"
    _navigate_page(page, target, "domcontentloaded", 30000)
    try:
        page.wait_for_timeout(1200)
    except Exception:
        pass
    if _google_is_logged_in(page) and "myaccount" in str(page.url).lower():
        return {"ok": True, "action": "google_login", "already_logged_in": True, "url": page.url}
    blocked = _google_challenge_kind(page)
    if blocked == "blocked":
        return {"ok": False, "action": "google_login", "requires_human": True, "embedded_only": True, "url": str(page.url),
                "message": "Google rejected this embedded browser session. Open the Browser tab to see the page; no physical browser was opened. You can retry or complete the step there."}
    # Step 1: identifier
    try:
        id_loc, _ = _first_visible_locator(page, ("input[type='email']", "input[name='identifier']", "#identifierId"), per_timeout=8000)
        id_loc.fill(email, timeout=10000)
        id_loc.press("Enter", timeout=6000)
    except BrowserAutomationError:
        if _google_is_logged_in(page):
            return {"ok": True, "action": "google_login", "already_logged_in": True, "url": page.url}
        raise
    try:
        page.wait_for_timeout(2200)
    except Exception:
        pass
    kind = _google_challenge_kind(page)
    if kind:
        return {"ok": False, "action": "google_login", "requires_human": True, "embedded_only": True, "url": str(page.url), "stage": "identifier",
                "message": "Google asked for a check after the email step (CAPTCHA or verification). Complete it in the embedded Browser tab, then say 'continue'."}
    # Step 2: password
    try:
        pw_loc, _ = _first_visible_locator(page, ("input[type='password']", "input[name='Passwd']", "input[name='password']"), per_timeout=12000)
    except BrowserAutomationError:
        err = ""
        try:
            err = page.locator("[aria-live='assertive'], .o6cuMc, .Ekjuhf").first.inner_text(timeout=1500)[:200]
        except Exception:
            pass
        return {"ok": False, "action": "google_login", "url": str(page.url), "stage": "identifier",
                "error": err or "The password step did not appear. The account may not exist, or Google shows another screen; check the embedded Browser tab."}
    pw_loc.fill(password, timeout=10000)
    pw_loc.press("Enter", timeout=6000)
    try:
        page.wait_for_timeout(3500)
    except Exception:
        pass
    kind = _google_challenge_kind(page)
    url = str(page.url or "")
    if kind == "blocked":
        return {"ok": False, "action": "google_login", "requires_human": True, "embedded_only": True, "url": url, "stage": "password",
                "message": "Google blocked sign-in from this headless browser. Check the embedded Browser tab; nothing was opened outside the app."}
    if kind == "challenge":
        return {"ok": False, "action": "google_login", "requires_human": True, "embedded_only": True, "url": url, "stage": "verification",
                "message": "Google needs a verification step (for example 2-step verification on your phone). Complete it inside the embedded Browser tab, then say 'continue'. The browser stays inside the Web UI."}
    if _google_is_logged_in(page):
        if cont:
            try:
                _navigate_page(page, _validate_url(cont), "domcontentloaded", 30000)
            except Exception:
                pass
        return {"ok": True, "action": "google_login", "logged_in": True, "url": str(page.url)}
    wrong = ""
    try:
        wrong = page.locator("[aria-live='assertive'], .o6cuMc, .Ekjuhf").first.inner_text(timeout=1500)[:200]
    except Exception:
        pass
    return {"ok": False, "action": "google_login", "url": url, "stage": "password",
            "error": wrong or "Sign-in did not complete; the password may be wrong or Google is showing an extra screen. Check the embedded Browser tab."}


def _google_signin_status(page) -> dict[str, Any]:
    """v1.13.4: verify whether this profile already holds an active Google session.

    Checks the page state and the persistent profile's Google session cookies, and when in
    doubt probes accounts.google.com once. This is how the agent confirms "already signed
    in" after the person completed a one-time manual sign-in inside the embedded Browser tab
    (the profile then stays signed in, so later runs need no sign-in at all).
    """
    signed_in = False
    try:
        signed_in = bool(_google_is_logged_in(page))
    except Exception:
        signed_in = False
    cookie_names: set[str] = set()
    try:
        cookies = page.context.cookies("https://accounts.google.com", "https://mail.google.com")
        cookie_names = {str(c.get("name") or "") for c in cookies}
        if {"SID", "HSID", "SSID"}.issubset(cookie_names):
            signed_in = True
    except Exception:
        pass
    if not signed_in:
        try:
            _navigate_page(page, "https://accounts.google.com/", "domcontentloaded", 30000)
            signed_in = bool(_google_is_logged_in(page))
        except Exception:
            pass
    return {"ok": True, "action": "check_signin", "signed_in": bool(signed_in),
            "session_cookies": sorted(n for n in cookie_names if n),
            "url": str(getattr(page, "url", "") or ""),
            "message": ("Google session is active in this profile; no sign-in needed." if signed_in else
                        "No active Google session in this profile. Run google_login (or browser_manual_signin); if Google shows a security check, complete it once inside the embedded Browser tab — the persistent profile then stays signed in for future runs.")}



def _linkedin_create_post(page, action: dict[str, Any]) -> dict[str, Any]:
    text = str(action.get("text", action.get("value", ""))).strip()
    if not text:
        raise BrowserAutomationError("linkedin_create_post needs the post text in `text`.")
    if not _linkedin_is_logged_in(page):
        return {"ok": False, "action": "linkedin_create_post", "requires_login": True, "url": page.url,
                "message": "LinkedIn is not signed in in this profile; run linkedin_login first."}
    clicked = False
    # 2026 feed: the trigger is a div[role=button] wrapping an inner aria-label="Start a post"
    # (stable container id: draft-text-replaceable-component); older builds used a <button>.
    for css in (
        "div[role='button']:has(div[aria-label='Start a post'])",
        "#draft-text-replaceable-component",
        "button[aria-label*='Start a post' i]",
        ".share-box-feed__trigger",
        "button[aria-label*='Create a post' i]",
    ):
        try:
            trigger = page.locator(css).first
            if trigger.count():
                trigger.click(timeout=5000)
                clicked = True
                break
        except Exception:
            continue
    if not clicked:
        raise BrowserAutomationError("Could not find the 'Start a post' button on the LinkedIn feed.")
    editor = None
    # 2026 composer: the editor is an INLINE TipTap/ProseMirror surface in the feed share box
    # (componentkey=ShareBox_textEditor); older builds used a Quill dialog.
    for css in (
        "[data-testid='ui-core-tiptap-text-editor-wrapper'] div[contenteditable='true']",
        "div[contenteditable='true'][role='textbox']",
        "[componentkey='ShareBox_textEditor']",
        "[role='dialog'] .ql-editor[contenteditable='true']",
        ".share-creation-state__editor .ql-editor",
        ".ql-editor[contenteditable='true']",
    ):
        try:
            candidate = page.locator(css).first
            candidate.wait_for(state="visible", timeout=5000)
            editor = candidate
            break
        except Exception:
            continue
        try:
            candidate = page.locator(css).first
            candidate.wait_for(state="visible", timeout=6000)
            editor = candidate
            break
        except Exception:
            continue
    if editor is None:
        raise BrowserAutomationError("The LinkedIn post composer did not open.")
    editor.click(timeout=4000)
    try:
        editor.fill(text, timeout=6000)
    except Exception:
        page.keyboard.insert_text(text)
    try:
        page.wait_for_timeout(700)
    except Exception:
        pass
    dialog = page.locator("[role='dialog']").last
    posted = False
    for css in (
        "button[aria-label='Post']",
        "[role='dialog'] button[aria-label*='Post' i]",
        "button[aria-label*='Post' i]",
    ):
        try:
            post_button = page.locator(css).first
            if post_button.count() and post_button.is_visible() and post_button.is_enabled(timeout=2500):
                post_button.click(timeout=6000)
                posted = True
                break
        except Exception:
            continue
    if not posted:
        try:
            candidates = page.get_by_role("button", name="Post", exact=True)
            for idx in range(min(candidates.count(), 6)):
                candidate = candidates.nth(idx)
                if candidate.is_visible():
                    candidate.click(timeout=5000)
                    posted = True
                    break
        except Exception as exc:
            raise BrowserAutomationError(f"Could not click the Post button: {exc}")
    try:
        page.wait_for_timeout(2500)
    except Exception:
        pass
    toast = ""
    try:
        toast = page.locator(".artdeco-toast-item--success, .artdeco-toast-item").first.inner_text(timeout=3000)[:160]
    except Exception:
        pass
    dialog_open = True
    try:
        dialog_open = page.locator("[role='dialog'] .ql-editor, div[componentkey='ShareBox_textEditor']").first.is_visible(timeout=1200)
    except Exception:
        dialog_open = False
    return {"ok": bool(posted and not dialog_open), "action": "linkedin_create_post",
            "posted": bool(posted and not dialog_open), "toast": toast,
            "preview": text[:80], "url": page.url}


_LINKEDIN_CARD_JS = r"""() => {
  const cards = Array.from(document.querySelectorAll('li[data-occludable-job-id], .jobs-search-results__list-item, .scaffold-layout__list-container > li')).slice(0, 80);
  const text = (el) => ((el && el.innerText) || '').trim();
  const out = [];
  for (const card of cards) {
    const link = card.querySelector('a[href*="/jobs/view/"], a.base-card__full-link, a.job-card-container__link');
    if (!link) continue;
    const href = link.href || '';
    const match = /\/jobs\/view\/([^\/?#]+)/.exec(href);
    if (!match) continue;
    const title = text(card.querySelector('.job-card-list__title, .artdeco-entity-lockup__title, .base-search-card__title'));
    const company = text(card.querySelector('.job-card-container__primary-link .job-card-container__company-name, .artdeco-entity-lockup__subtitle li:first-child, .base-search-card__subtitle'));
    const location = text(card.querySelector('.job-card-container__metadata, .artdeco-entity-lockup__caption li, .job-search-card__location'));
    const timeEl = card.querySelector('.job-card-container__listed-time time, time');
    const posted = (timeEl && (timeEl.getAttribute('datetime') || timeEl.innerText)) || text(card.querySelector('.job-card-container__listed-time, .job-search-card__listdate'));
    out.push({job_id: match[1], title, company, location, posted: String(posted).trim(),
              url: 'https://www.linkedin.com/jobs/view/' + match[1] + '/'});
  }
  return out;
}"""

_LINKEDIN_DETAIL_JS = r"""() => {
  const pick = (sel) => { const el = document.querySelector(sel); return el ? (el.innerText || '').trim() : ''; };
  const desc = document.querySelector('.jobs-description__content .show-more-less-html__markup, .show-more-less-html__markup, .jobs-box__html-content');
  const apply = document.querySelector('.jobs-apply-button--top-card, button[aria-label^="Easy Apply"], button.jobs-apply-button');
  const applyLabel = ((apply && (apply.getAttribute('aria-label') || apply.innerText)) || '');
  const applied = /applied/i.test(applyLabel) || !!document.querySelector('.top-card-layout__card .jobs-s-apply span[aria-label*="Applied" i]');
  const insights = Array.from(document.querySelectorAll('.job-details-jobs-unified-top-card__job-insight, .job-details-jobs-unified-top-card__job-insight--highlight')).map(e => (e.innerText || '').trim()).filter(Boolean);
  return {
    description_text: desc ? desc.innerText : pick('.jobs-description__content'),
    description_html: desc ? desc.innerHTML.slice(0, 40000) : '',
    apply_button_label: applyLabel.slice(0, 160),
    easy_apply: /easy apply/i.test(applyLabel),
    applied,
    insights,
    posted_exact: pick('.job-details-jobs-unified-top-card__posted-date, .jobs-unified-top-card__posted-date'),
    applicants: pick('.job-details-jobs-unified-top-card__applicant-count'),
  };
}"""


def _linkedin_scrape_jobs(page, action: dict[str, Any]) -> dict[str, Any]:
    keywords = str(action.get("keywords", action.get("query", ""))).strip() or "AI Engineer"
    location = str(action.get("location", "")).strip() or "New York, New York, United States"
    hours = max(1, min(604800, int(action.get("hours", 86400))))
    easy_only = bool(action.get("easy_apply_only", False))
    max_jobs = max(1, min(60, int(action.get("max_jobs", 25))))
    include_description = bool(action.get("include_description", True))
    params = {"keywords": keywords, "location": location, "f_TPR": f"r{hours}", "position": "1", "pageNum": "0"}
    if easy_only:
        params["f_AL"] = "true"
    search_url = "https://www.linkedin.com/jobs/search/?" + urllib.parse.urlencode(params)
    _navigate_page(page, search_url, "domcontentloaded", 30000)
    if not _linkedin_is_logged_in(page):
        return {"ok": False, "action": "linkedin_scrape_jobs", "requires_login": True, "url": page.url,
                "message": "LinkedIn redirected to login/authwall. Run action linkedin_login first, then scrape again."}
    _scroll_until_stable(page, {"pause_ms": 450, "max_scrolls": 30, "stabilize_rounds": 2})
    cards = page.evaluate(_LINKEDIN_CARD_JS) or []
    unique: dict[str, dict[str, Any]] = {}
    for card in cards:
        if not isinstance(card, dict) or not card.get("job_id"):
            continue
        unique.setdefault(str(card["job_id"]), card)
    jobs = list(unique.values())[:max_jobs]
    if include_description:
        for job in jobs:
            try:
                _navigate_page(page, str(job["url"]), "domcontentloaded", 25000)
                detail = page.evaluate(_LINKEDIN_DETAIL_JS) or {}
                job.update({k: detail.get(k) for k in ("description_text", "description_html", "easy_apply", "applied", "insights", "posted_exact", "applicants", "apply_button_label")})
            except Exception as exc:
                job["detail_error"] = str(exc)[:200]
    csv_path = str(action.get("csv_path", "")).strip()
    csv_report = None
    if csv_path and jobs:
        # v1.13.4: one-call scrape → CSV export (Excel-friendly utf-8-sig BOM, CRLF, formula guard).
        try:
            from tools.job_csv import write_jobs_csv
            target = Path(csv_path).expanduser()
            if not target.is_absolute():
                target = ROOT / target
            csv_report = write_jobs_csv(jobs, str(target))
        except Exception as exc:
            csv_report = {"ok": False, "error": str(exc), "path": str(csv_path)}
    result = {"ok": True, "action": "linkedin_scrape_jobs", "count": len(jobs), "jobs": jobs,
              "search_url": search_url, "filters": {"keywords": keywords, "location": location, "hours": hours, "easy_apply_only": easy_only}}
    if csv_path:
        result["csv"] = csv_report or {"ok": False, "error": "No jobs scraped; nothing to export."}
    return result


_EASY_APPLY_STEP_BUTTONS = (
    "button[aria-label='Continue to next step']",
    "button[aria-label='Review your application']",
    "button[aria-label='Submit application']",
    "button[aria-label='Review']",
    "button[aria-label='Next']",
)


def _linkedin_easy_apply(page, action: dict[str, Any]) -> dict[str, Any]:
    """Drive LinkedIn Easy Apply for one job: steps, questions, resume, submit (verifiable)."""
    job_url = str(action.get("job_url", action.get("url", ""))).strip()
    if not job_url:
        raise BrowserAutomationError("linkedin_easy_apply needs `job_url` (from linkedin_scrape_jobs).")
    resume_path = str(action.get("resume_path", action.get("file_path", ""))).strip().strip('"')
    contact = action.get("contact") if isinstance(action.get("contact"), dict) else {}
    answers = action.get("answers") if isinstance(action.get("answers"), list) else []
    do_submit = bool(action.get("submit", False))
    if resume_path and not Path(resume_path).expanduser().is_file():
        raise BrowserAutomationError(f"resume_path does not exist: {resume_path}")
    _navigate_page(page, job_url, "domcontentloaded", 30000)
    if not _linkedin_is_logged_in(page):
        return {"ok": False, "action": "linkedin_easy_apply", "requires_login": True, "url": page.url,
                "message": "Not signed in to LinkedIn; run linkedin_login first."}
    apply_button = None
    for css in ("button[aria-label^='Easy Apply']", ".jobs-apply-button--top-card", "button.jobs-apply-button"):
        try:
            candidate = page.locator(css).first
            if candidate.count():
                apply_button = candidate
                break
        except Exception:
            continue
    if apply_button is None:
        return {"ok": False, "action": "linkedin_easy_apply", "url": page.url, "reason": "no_apply_button",
                "message": "No Easy Apply button on this job (it may redirect to an external site or require LinkedIn premium)."}
    try:
        label = str(apply_button.get_attribute("aria-label") or apply_button.inner_text(timeout=1500) or "")
        if "applied" in label.lower():
            return {"ok": True, "action": "linkedin_easy_apply", "already_applied": True, "url": page.url}
    except Exception:
        pass
    apply_button.click(timeout=8000)
    dialog = page.locator("[role='dialog'].jobs-easy-apply-dialog, [role='dialog'], .artdeco-modal, [data-test-modal]")
    try:
        dialog.first.wait_for(state="visible", timeout=12000)
    except Exception:
        url_now = str(getattr(page, "url", "") or "")
        if "applicantTrackingSystemName" in url_now or "/jobs/apply" in url_now:
            return {"ok": False, "action": "linkedin_easy_apply", "ats_external": True, "url": url_now,
                    "message": ("LinkedIn redirected this application to an external ATS (the URL now carries applicantTrackingSystemName). "
                                "The agent does not submit on third-party ATS sites automatically; use a native Easy Apply job instead.")}
        try:
            apply_button.click(timeout=6000)
            dialog.first.wait_for(state="visible", timeout=10000)
        except Exception as exc:
            body_head = ""
            try:
                body_head = page.locator("body").inner_text(timeout=1500)[:250]
            except Exception:
                pass
            return {"ok": False, "action": "linkedin_easy_apply", "url": str(getattr(page, "url", "") or ""),
                    "reason": "dialog_not_open", "body_head": body_head,
                    "message": (f"Easy Apply was clicked but no application dialog appeared ({str(exc)[:160]}). "
                                "The job may use a new LinkedIn apply surface; inspect the embedded Browser tab.")}
    step_log: list[dict[str, Any]] = []
    unanswered: list[dict[str, Any]] = []
    missing: list[str] = []
    submitted = False
    applied = False
    last_signature = ""
    try:
        for step in range(1, 16):
            signature = ""
            try:
                signature = (dialog.first.inner_text(timeout=2500) or "")[:400]
            except Exception:
                pass
            if signature and signature == last_signature:
                step_log.append({"step": step, "note": "step did not change; stopping to avoid a loop"})
                break
            last_signature = signature
            # 1) Resume upload step.
            try:
                file_input = dialog.first.locator("input[type='file']").first
                if file_input.count():
                    if resume_path:
                        file_input.set_input_files(str(Path(resume_path).expanduser().resolve()), timeout=8000)
                        step_log.append({"step": step, "resume_uploaded": True})
                    else:
                        preselected = False
                        try:
                            section_text = (dialog.first.inner_text(timeout=1500) or "").lower()
                            preselected = (("profile resume" in section_text) or ("use resume" in section_text)) and dialog.first.locator("input[type='radio']:checked, input[type='checkbox']:checked").count() > 0
                        except Exception:
                            preselected = False
                        if preselected:
                            step_log.append({"step": step, "resume": "using the profile resume (preselected)"})
                        else:
                            missing.append("resume/CV upload (no resume_path provided)")
            except Exception as exc:
                step_log.append({"step": step, "resume_error": str(exc)[:160]})
            # 2) Answer every question group inside the dialog.
            try:
                groups = page.evaluate(_QUESTION_GROUPS_JS) or []
                for group in groups or []:
                    if not isinstance(group, dict) or not group.get("options"):
                        continue
                    selectable = [o for o in group["options"] if o.get("visible") and not o.get("disabled")]
                    if not selectable:
                        continue
                    question = str(group.get("question", "") or "")
                    desired = _match_group_answer(question, str(group.get("name", "")), answers)
                    chosen = _pick_group_option(selectable, str(desired)) if desired is not None else None
                    if chosen is None:
                        unanswered.append({"question": question[:250],
                                           "options": [str(o.get("label") or o.get("value") or "") for o in selectable]})
                        continue
                    click = _click_group_option(page, group, chosen, page)
                    step_log.append({"step": step, "question": question[:120],
                                     "chosen": str(chosen.get("label") or ""), "verified": bool(click.get("verified"))})
            except Exception as exc:
                step_log.append({"step": step, "questions_error": str(exc)[:160]})
            # 3) Empty visible text fields inside the dialog (contact info etc.).
            try:
                empties = dialog.first.locator("input:not([type='hidden']):not([type='file']), textarea").evaluate_all(
                    "els => els.filter(el => !el.disabled && el.offsetParent !== null && !el.value).slice(0, 12).map(el => ({id: el.id, name: el.name, type: el.type, ph: el.placeholder, label: (el.labels && el.labels[0] && el.labels[0].innerText) || ''}))") or []
                for field in empties:
                    context = " ".join(str(field.get(k, "") or "") for k in ("id", "name", "ph", "label")).lower()
                    value = ""
                    if "email" in context:
                        value = str(contact.get("email", "") or "")
                    elif re.search(r"phone|mobile", context):
                        value = str(contact.get("phone", "") or "")
                    elif "name" in context and contact.get("full_name"):
                        value = str(contact.get("full_name"))
                    target_sel = f"#{field['id']}" if field.get("id") else (f"input[name='{field.get('name')}']" if field.get("name") else None)
                    if value and target_sel:
                        try:
                            dialog.first.locator(target_sel).first.fill(value, timeout=4000)
                            step_log.append({"step": step, "filled": target_sel, "value_kind": "contact"})
                        except Exception:
                            missing.append(f"could not fill {context[:60]!r}")
                    else:
                        missing.append(f"empty required field: {context[:80]!r} (no data supplied)")
            except Exception as exc:
                step_log.append({"step": step, "fields_error": str(exc)[:160]})
            # 4) Validation errors visible?
            try:
                errors = dialog.first.locator(".artdeco-inline-feedback--error").evaluate_all(
                    "els => els.filter(el => el.offsetParent !== null).map(el => (el.innerText || '').trim()).filter(Boolean).slice(0, 6)") or []
                if errors:
                    step_log.append({"step": step, "validation_errors": errors})
                    break
            except Exception:
                pass
            # 5) Advance.
            advanced = False
            for css in _EASY_APPLY_STEP_BUTTONS:
                try:
                    button = dialog.first.locator(css).first
                    if button.count() and button.is_enabled(timeout=1500):
                        if "submit" in css:
                            if not do_submit:
                                return {"ok": True, "action": "linkedin_easy_apply", "ready_to_submit": True,
                                        "url": page.url, "steps": step_log, "unanswered": unanswered, "missing": missing,
                                        "message": "Application filled up to the final Submit. Pass submit:true (explicit authorization) to submit it."}
                            button.click(timeout=6000)
                            submitted = True
                            try:
                                page.wait_for_timeout(2500)
                            except Exception:
                                pass
                            success_text = ""
                            try:
                                success_text = page.locator("[role='dialog'], .artdeco-modal__content").first.inner_text(timeout=3000)[:400]
                            except Exception:
                                pass
                            applied = bool(re.search(r"application (was |has been )?sent|your application", success_text, re.I))
                            step_log.append({"step": step, "submitted": True, "applied": applied, "success_text": success_text[:200]})
                            advanced = True
                            break
                        button.click(timeout=6000)
                        advanced = True
                        try:
                            page.wait_for_timeout(1800)
                        except Exception:
                            pass
                        break
                except Exception:
                    continue
            if applied:
                break
            if not advanced:
                try:
                    dismissible = dialog.first.locator("button[aria-label='Dismiss']").first.is_visible(timeout=800)
                    if dismissible and not missing and not unanswered:
                        break
                except Exception:
                    pass
                step_log.append({"step": step, "note": "no Next/Review/Submit button found; stopping"})
                break
            if submitted:
                break
    finally:
        pass
    return {"ok": applied or (submitted and not missing), "action": "linkedin_easy_apply", "url": page.url,
            "applied": applied, "submitted": submitted, "steps": step_log, "unanswered": unanswered, "missing": missing}


def _perform_browser_task_impl(payload: dict[str, Any]) -> dict[str, Any]:
    """Execute browser actions in one persistent profile with multi-tab support."""
    payload = payload if isinstance(payload, dict) else {}
    action_results: list[dict[str, Any]] = []
    automation_audit: list[dict[str, Any]] = []
    page = None
    session = None
    slug = "default"
    try:
        mode = str(payload.get("task", payload.get("mode", "action"))).strip().lower()
        headless = payload.get("headless", DEFAULT_HEADLESS)
        if not isinstance(headless, bool):
            headless = str(headless).strip().lower() not in {"0", "false", "no", "off"}
        # v1.9 embedded mode: the real browser lives INSIDE the Agent UI (no OS window) unless
        # the caller explicitly overrides headless (e.g. Google sign-in needs a real window).
        # v1.12: a per-call `headless=false` override is ignored in embedded mode, so a
        # physical browser window can never appear.
        if _embedded_ui_enabled():
            headless = True
        timeout = max(500, min(MAX_TIMEOUT_MS, int(payload.get("timeout_ms", 15000))))
        actions = payload.get("actions")
        if actions is None:
            actions = []
            if payload.get("action"):
                actions.append({k: v for k, v in payload.items() if k in {
                    "action", "type", "url", "selector", "target", "value", "text", "x", "y", "fallback",
                    "state", "clear", "timeout_ms", "wait_until", "ms", "delta_x", "delta_y", "filename",
                    "file_path", "file_paths", "paths", "full_page", "max_chars", "tab_id", "tab_index", "frame_selector", "urls", "new_tab",
                    "label", "placeholder", "name", "aria_label", "exact", "force", "key_delay_ms",
                    "frame_index", "frame_url_contains", "frame_name", "coordinates_relative_to_frame",
                    "image_url", "image_path", "query", "play", "max_results", "match_threshold", "limit", "max_images",
                    "answers", "groups", "default_answer", "include_hidden",
                    "all_frames", "include_iframes", "max_html_chars", "max_scrolls", "pause_ms", "stabilize_rounds",
                    "trigger_selector", "sitekey", "challenge_type", "provider", "key", "email", "password", "code",
                }})
        if not isinstance(actions, list) or len(actions) > MAX_ACTIONS:
            raise ValueError(f"actions must be a list containing at most {MAX_ACTIONS} items.")
        for action in actions:
            if not isinstance(action, dict):
                raise ValueError("Each browser action must be a JSON object.")
        url = str(payload.get("url", "")).strip()
        if url:
            url = _validate_url(url)
        manual_modes = {"manual_signin", "manual", "open_for_user", "handoff_to_user"}
        release_modes = {"release", "release_browser", "detach_browser"}
        if mode not in {"action", "scrape", "test", "inspect"} | manual_modes | release_modes | STREAM_TASKS:
            raise ValueError("task must be one of: action, scrape, test, inspect, manual_signin, release, stream_*.")
        if mode in {"scrape", "test", "inspect"} and not url:
            # These tasks may inspect the current tab without a URL when a live
            # persistent browser page already exists. This fixes a recurring log
            # failure where the model requested a current-page scrape without
            # redundantly repeating the current URL.
            current_session = _browser_sessions.get("default")
            has_open_web_tab = False
            if current_session:
                try:
                    current_context = current_session.get("context")
                    # Playwright BrowserContext has no is_closed() method; reading pages
                    # is the portable way to tell whether a live context still responds.
                    has_open_web_tab = bool(current_context and any(
                        not tab.is_closed() and str(tab.url).startswith(("http://", "https://"))
                        for tab in current_context.pages
                    ))
                except Exception:
                    has_open_web_tab = False
            if not has_open_web_tab:
                raise ValueError(f"A URL is required for task '{mode}' unless a web page is already open in the persistent browser.")
        close_names = {"close_browser", "browser_close", "close_window", "close"}
        requested_profile = str(payload.get("profile", "")).strip() or None
        chosen, choice_error = _choose_profile(requested_profile)
        if choice_error:
            return choice_error
        slug = str(chosen)
        # A single explicit close is handled without starting the browser again.
        if len(actions) == 1 and str(actions[0].get("action", actions[0].get("type", ""))).strip().lower() in close_names:
            had_session = _close_browser_session(slug)
            had_session = _terminate_native_proc(slug) or had_session
            return {
                "ok": True, "task": mode, "profile": slug, "browser_closed": True,
                "actions": [{"ok": True, "action": "close_browser", "was_open": had_session}],
                "message": "Browser closed." if had_session else "Browser was already closed.",
            }
        profile_path = _profile_path(slug)
        profile_path.mkdir(parents=True, exist_ok=True)
        metadata = _read_metadata(profile_path)
        extension_paths = metadata.get("extensions", [])
        extension_paths = [p for p in extension_paths if isinstance(p, str) and Path(p).is_dir() and (Path(p) / "manifest.json").is_file()] if isinstance(extension_paths, list) else []
        logger.info("Starting task=%s profile=default headless=%s url=%s action_count=%d", mode, headless, url or "(current page)", len(actions))
    except (TypeError, ValueError) as exc:
        return {"ok": False, "status_code": 400, "error": str(exc)}

    # ---- Manual sign-in / hand-over to the person --------------------------------------
    def _action_kind(item: dict[str, Any]) -> str:
        return str(item.get("action", item.get("type", ""))).strip().lower()
    kinds = [_action_kind(a) for a in actions]
    manual_action = any(k in manual_modes | {"manual_signin", "open_for_user", "handoff_to_user"} for k in kinds)
    release_action = any(k in release_modes for k in kinds)
    if (manual_action or release_action) and len(actions) > 1:
        return {"ok": False, "status_code": 400, "error": "manual_signin / release_browser must be the only action in a browser call."}
    google_targets: list[str] = [url] if url and _is_google_auth_url(url) else []
    for item in actions:
        if _action_kind(item) in {"navigate", "goto", "new_tab", "open_tab"}:
            candidate = str(item.get("url", item.get("value", ""))).strip()
            if candidate and _is_google_auth_url(candidate):
                google_targets.append(candidate)
        elif _action_kind(item) == "open_tabs":
            google_targets.extend(str(u) for u in (item.get("urls") or []) if _is_google_auth_url(u))
    embedded_manual_handoff = False
    if any(k in {"google_login", "login_google", "google_signin"} for k in kinds):
        google_targets = []  # the agent signs in itself, inside the embedded browser
    if mode in manual_modes or manual_action or (mode in {"action", "scrape", "test", "inspect"} and google_targets):
        embedded_enabled = str(os.getenv("BROWSER_EMBEDDED_UI", "true")).strip().lower() not in {"0", "false", "no", "off"}
        if embedded_enabled:
            # Never spawn a physical OS browser in embedded mode. Navigate only; the user can
            # manually interact with the streamed Browser panel for sign-in/checkpoint steps.
            target = (url if url and mode in manual_modes else None) or (google_targets[0] if google_targets else None)
            if not target and actions:
                target = str(actions[0].get("url", actions[0].get("value", ""))).strip() or None
            if not target and url:
                target = url
            target = _validate_url(target or "https://accounts.google.com/")
            mode = "action"
            url = ""
            actions = [{"action": "navigate", "url": target}]
            manual_action = False
            google_targets = []
            embedded_manual_handoff = True
        else:
            with _profile_lock(slug):
                try:
                    target = (url if url and mode in manual_modes else None) or (google_targets[0] if google_targets else None)
                    if not target and actions:
                        target = str(actions[0].get("url", actions[0].get("value", ""))).strip() or None
                    if not target and url:
                        target = url
                    outcome = _manual_browser_open(profile_path, extension_paths, target, True)
                    if google_targets and mode == "action":
                        outcome["note"] = ("Google sign-in pages are never driven by the agent: Google rejects sign-in from automated browsers. "
                                           "The page was opened for you to complete manually; other requested actions were not run.")
                    outcome.setdefault("task", mode); outcome["profile"] = slug
                    logger.info("Manual sign-in hand-over: %s", {k: outcome.get(k) for k in ("ok", "mode", "opened_url")})
                    return outcome
                except (ValueError, BrowserAutomationError) as exc:
                    return {"ok": False, "status_code": 400, "error": str(exc)}
                except Exception as exc:
                    logger.exception("Manual browser open failed")
                    return {"ok": False, "status_code": 500, "error": f"Could not open the browser for manual use: {exc}"}
    if mode in release_modes or release_action:
        with _profile_lock(slug):
            was = _detach_browser_session(slug)
        return {"ok": True, "task": mode, "profile": slug, "agent_connected": False, "was_connected": was,
                "message": "The agent has disconnected. The browser window stays open and is fully under your manual control. Tell IntignoAI to continue when you want it to reconnect."}

    if mode in STREAM_TASKS:
        session = _browser_sessions.get(slug)
        if session is None:
            return {"ok": False, "status_code": 409, "error": "No live embedded browser session yet. Run any browser_action first (for example navigate to a URL), then open the Browser tab."}
        try:
            from tools import browser_stream as _stream_mod
        except Exception as exc:
            return {"ok": False, "status_code": 501, "error": f"browser_stream module unavailable: {exc}"}
        try:
            return _stream_mod.handle_stream_task(mode, payload, session)
        except Exception as exc:
            logger.exception("Stream task failed: %s", mode)
            return {"ok": False, "status_code": 500, "error": str(exc)[:400]}

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        logger.exception("Playwright is unavailable")
        return {"ok": False, "status_code": 503, "error": "Playwright is not installed in the active Python environment. Run start.bat/start.sh again to install dependencies.", "details": str(exc)}

    with _profile_lock(slug):
        try:
            session = _browser_sessions.get(slug)
            tabs_to_restore: list[str] = []
            if session is not None:
                try:
                    context = session["context"]
                    try:
                        live_pages = [candidate for candidate in context.pages if not candidate.is_closed()]
                    except Exception as context_exc:
                        raise RuntimeError("Browser context was closed by the user.") from context_exc
                    if not live_pages:
                        restored_page = context.new_page()
                        _register_page(session, restored_page)
                        session["page"] = restored_page
                    # Only recreate when the caller explicitly requests a change in
                    # visibility. Preserve open web URLs so a headless->visible change
                    # does not silently discard the user's tab work.
                    if _embedded_ui_enabled() and not session.get("headless", True):
                        raise RuntimeError("Visible browser session found in embedded mode; reopening it headless inside the Web UI.")
                    if "headless" in payload and session.get("headless", DEFAULT_HEADLESS) != headless:
                        # Never tear down the user's manually interactive browser simply
                        # because a tool call disagrees about visibility. Reuse the live
                        # profile/session and report its real mode.
                        logger.info("Keeping existing browser visibility=%s instead of restarting a live user session.", session.get("headless"))
                        headless = bool(session.get("headless", False))
                    page = _select_tab(context, session, payload)
                except Exception as exc:
                    logger.warning("Existing browser session was stale; reopening it: %s", exc)
                    _close_browser_session(slug)
                    session = None
            if session is None:
                playwright = sync_playwright().start()
                browser = None
                browser_process = None
                connection_mode = "playwright_launch"
                use_native = str(os.getenv("BROWSER_USE_NATIVE_CHROME", "true")).strip().lower() not in {"0", "false", "no", "off"}
                if not headless and use_native:
                    try:
                        native = _launch_native_chrome(playwright, profile_path, extension_paths)
                    except Exception:
                        try: playwright.stop()
                        except Exception: pass
                        raise
                    if native is not None:
                        browser, context, browser_process, owned_process = native
                        connection_mode = "native_chrome_cdp"
                        logger.info("Attached to visible native browser via localhost CDP; manual browsing/extensions remain enabled.")
                    else:
                        owned_process = False
                elif headless and use_native and str(os.getenv("BROWSER_EMBEDDED_UI", "true")).strip().lower() not in {"0", "false", "no", "off"}:
                    # Embedded mode: run the REAL installed browser headless but with its local
                    # debug endpoint, so the Browser tab can mirror and interact with the page.
                    try:
                        native = _launch_native_chrome(playwright, profile_path, extension_paths, headless=True)
                    except Exception:
                        try: playwright.stop()
                        except Exception: pass
                        raise
                    if native is not None:
                        browser, context, browser_process, owned_process = native
                        connection_mode = "native_chrome_cdp"
                        logger.info("Attached to headless embedded browser via localhost CDP (Browser tab mirror ready).")
                    else:
                        owned_process = False
                else:
                    owned_process = False
                if connection_mode == "playwright_launch":
                    # The bundled fallback remains available on machines without a
                    # supported installed Chrome/Edge executable. Never disable the
                    # user's installed extensions; --load-extension is additive only.
                    args = []
                    if extension_paths:
                        args.append("--load-extension=" + ",".join(extension_paths))
                    # Realistic automation fingerprint: without these, Chromium advertises
                    # 'supported by automation' and many sites wall the agent behind CAPTCHAs.
                    args.append("--disable-blink-features=AutomationControlled")
                    launch_options: dict[str, Any] = {
                        "user_data_dir": str(profile_path), "headless": headless, "args": args,
                        "ignore_default_args": ["--enable-automation"],
                        "viewport": {"width": 1365, "height": 900}, "accept_downloads": True,
                    }
                    channel = str(os.getenv("BROWSER_CHANNEL", "chrome")).strip()
                    if channel and channel.lower() not in {"auto", "bundled", "none"}:
                        launch_options["channel"] = channel
                    # When Playwright's downloaded browser isn't installed (a common
                    # offline setup), use an installed Chromium-compatible executable
                    # for headless/testing fallback. Visible login still prefers native CDP.
                    if channel.lower() in {"bundled", "none", "auto"}:
                        available_browser = _find_native_browser()
                        if available_browser:
                            launch_options["executable_path"] = available_browser
                    try:
                        context = playwright.chromium.launch_persistent_context(**launch_options)
                    except Exception as launch_exc:
                        if "channel" not in launch_options or launch_options.get("channel") == "chromium":
                            raise
                        logger.warning("Browser channel %r failed; retrying a compatible local/bundled Chromium: %s", channel, launch_exc)
                        launch_options.pop("channel", None)
                        if not launch_options.get("executable_path"):
                            available_browser = _find_native_browser()
                            if available_browser:
                                launch_options["executable_path"] = available_browser
                        context = playwright.chromium.launch_persistent_context(**launch_options)
                    browser = None
                    browser_process = None
                    owned_process = False
                page = context.pages[0] if context.pages else context.new_page()
                _apply_stealth(context)
                session = {
                    "playwright": playwright, "browser": browser, "context": context, "page": page, "headless": headless,
                    "connection_mode": connection_mode, "browser_process": browser_process, "owned_process": owned_process,
                    "console_errors": [], "page_errors": [], "failed_requests": [], "bad_responses": [],
                    "tab_ids": {}, "tab_pages": {}, "diagnostic_pages": set(), "next_tab_number": 1,
                }
                _browser_sessions[slug] = session
                _register_page(session, page)
                try:
                    context.on("page", lambda new_page, ref=session: _register_page(ref, new_page))
                except Exception as exc:
                    logger.debug("Context does not support page event hook in this browser adapter: %s", exc)
                for restore_url in tabs_to_restore:
                    try:
                        restored = context.new_page()
                        _register_page(session, restored)
                        restored.goto(restore_url, wait_until="domcontentloaded", timeout=timeout)
                        page = restored
                    except Exception as restore_exc:
                        logger.warning("Could not restore a previously open tab %s: %s", restore_url, restore_exc)
                session["page"] = page
            context = session["context"]
            if not session.get("stealth_installed"):
                # Also covers re-attached native Chrome sessions: applies to future documents.
                _apply_stealth(context)
                session["stealth_installed"] = True
            # Reset diagnostics for this command, then collect across every tab.
            for key in ("console_errors", "page_errors", "failed_requests", "bad_responses"):
                session.setdefault(key, [])
                session[key].clear()
            page = _select_tab(context, session, payload)
            session["page"] = page
            page.set_default_timeout(timeout)
            navigation_status = None
            navigation_info: dict[str, Any] | None = None
            if url:
                response, _nav_fallback, navigation_info = _navigate_page(page, url, str(payload.get("wait_until", "domcontentloaded")), timeout)
                navigation_status = response.status if response else None
                challenge = _ensure_challenge_resolved(page)
                if challenge:
                    navigation_info = {**(navigation_info or {}), "challenge": challenge}
            browser_closed = False

            for index, action in enumerate(actions):
                kind = str(action.get("action", action.get("type", ""))).strip().lower()
                logger.info("Action %d/%d: %s", index + 1, len(actions), kind or "(missing)")
                per_timeout = max(500, min(MAX_TIMEOUT_MS, int(action.get("timeout_ms", timeout))))
                if kind in close_names:
                    _close_browser_session(slug)
                    browser_closed = True
                    action_results.append({"ok": True, "action": "close_browser", "browser_closed": True})
                    break

                tab_selector_keys = {"tab_id", "tab_index", "title_contains", "url_contains"}
                if kind == "switch_tab" or any(action.get(key) is not None and action.get(key) != "" for key in tab_selector_keys):
                    previous_page = session.get("page")
                    page = _select_tab(context, session, action)
                    if page is not previous_page and previous_page is not None and not previous_page.is_closed():
                        session["prev_page"] = previous_page
                    session["page"] = page
                elif page is None or page.is_closed():
                    page = _select_tab(context, session, {})
                    session["page"] = page

                if kind in {"navigate", "goto", "new_tab", "open_tab", "open_tabs", "reload", "go_back", "back", "go_forward", "forward", "click", "fill", "type", "check", "uncheck", "select", "submit", "upload_file"} and page is not None and not page.is_closed():
                    # Inspect and screenshot the actual tab selected for this action.
                    # Field values are deliberately not captured, only whether populated.
                    try:
                        audit = _automation_snapshot(page, f"before-{index+1}-{kind}", screenshot=True)
                        automation_audit.append({"action_index": index + 1, "action": kind, "before": audit})
                    except Exception as audit_exc:
                        automation_audit.append({"action_index": index + 1, "action": kind, "before_error": str(audit_exc)[:200]})

                if kind in {"navigate", "goto"}:
                    if action.get("new_tab"):
                        page = context.new_page()
                        _register_page(session, page)
                        session["page"] = page
                    target_url = _validate_url(str(action.get("url", action.get("value", ""))))
                    response, nav_fallback, nav_info = _navigate_page(page, target_url, str(action.get("wait_until", "domcontentloaded")), per_timeout)
                    navigation_status = response.status if response else navigation_status
                    challenge = _ensure_challenge_resolved(page)
                    try: page.bring_to_front()
                    except Exception: pass
                    nav_entry = {"ok": True, "action": kind, "url": page.url, "status": navigation_status, "tab_id": _register_page(session, page), "wait_fallback_used": nav_fallback, "navigation": nav_info}
                    if challenge:
                        nav_entry["challenge"] = challenge
                    action_results.append(nav_entry)
                elif kind in {"new_tab", "open_tab"}:
                    target_url = str(action.get("url", action.get("value", ""))).strip()
                    page = context.new_page()
                    _register_page(session, page)
                    session["page"] = page
                    status = None
                    if target_url:
                        response, nav_fallback, nav_info = _navigate_page(page, _validate_url(target_url), str(action.get("wait_until", "domcontentloaded")), per_timeout)
                        status = response.status if response else None
                    else:
                        nav_fallback = False
                        nav_info = None
                    challenge = _ensure_challenge_resolved(page) if target_url else None
                    try: page.bring_to_front()
                    except Exception: pass
                    nav_entry = {"ok": True, "action": "new_tab", "tab_id": _register_page(session, page), "url": page.url, "status": status, "wait_fallback_used": nav_fallback, "navigation": nav_info}
                    if challenge:
                        nav_entry["challenge"] = challenge
                    action_results.append(nav_entry)
                elif kind == "open_tabs":
                    urls = action.get("urls", action.get("value", []))
                    if isinstance(urls, str):
                        urls = [urls]
                    if not isinstance(urls, list) or not urls:
                        raise BrowserAutomationError("open_tabs needs a non-empty `urls` array.")
                    opened = []
                    for target_url in urls[:20]:
                        tab = context.new_page()
                        _register_page(session, tab)
                        response, nav_fallback, nav_info = _navigate_page(tab, _validate_url(str(target_url)), str(action.get("wait_until", "domcontentloaded")), per_timeout)
                        opened.append({"tab_id": _register_page(session, tab), "url": tab.url, "status": response.status if response else None, "wait_fallback_used": nav_fallback, "navigation": nav_info})
                        page = tab
                        try: page.bring_to_front()
                        except Exception: pass
                    session["page"] = page
                    action_results.append({"ok": True, "action": kind, "opened_tabs": opened})
                elif kind == "list_tabs":
                    action_results.append({"ok": True, "action": kind, "tabs": _list_tabs(context, session)})
                elif kind in {"switch_tab", "focus_browser", "bring_to_front"}:
                    # Switching only the internal Page reference made the API report the
                    # right tab while the visible Chrome window kept showing another tab.
                    # bring_to_front activates the real visible tab in the browser UI.
                    try:
                        page.bring_to_front()
                    except Exception as focus_exc:
                        logger.debug("Browser tab focus request was not acknowledged: %s", focus_exc)
                    session["page"] = page
                    action_results.append({"ok": True, "action": kind, "tab_id": _register_page(session, page), "url": page.url, "title": page.title(), "brought_to_front": True})
                elif kind == "close_tab":
                    open_pages = [p for p in context.pages if not p.is_closed()]
                    if len(open_pages) <= 1:
                        raise BrowserAutomationError("The last browser tab cannot be closed with close_tab. Use close_browser to close the browser.")
                    closed_id = _register_page(session, page)
                    page.close()
                    session.get("tab_pages", {}).pop(closed_id, None)
                    session.get("tab_ids", {}).pop(id(page), None)
                    page = next((p for p in reversed(context.pages) if not p.is_closed()), None)
                    session["page"] = page
                    action_results.append({"ok": True, "action": kind, "closed_tab_id": closed_id, "active_tab_id": _register_page(session, page) if page else None})
                elif kind in {"go_back", "back"}:
                    response = page.go_back(wait_until=str(action.get("wait_until", "domcontentloaded")), timeout=per_timeout)
                    navigation_status = response.status if response else navigation_status
                    action_results.append({"ok": True, "action": kind, "url": page.url, "status": navigation_status})
                elif kind in {"go_forward", "forward"}:
                    response = page.go_forward(wait_until=str(action.get("wait_until", "domcontentloaded")), timeout=per_timeout)
                    navigation_status = response.status if response else navigation_status
                    action_results.append({"ok": True, "action": kind, "url": page.url, "status": navigation_status})
                elif kind == "reload":
                    response = page.reload(wait_until=str(action.get("wait_until", "domcontentloaded")), timeout=per_timeout)
                    navigation_status = response.status if response else navigation_status
                    action_results.append({"ok": True, "action": kind, "url": page.url, "status": navigation_status})
                elif kind in {"wait", "sleep"}:
                    ms = max(0, min(60000, int(action.get("ms", action.get("value", 1000)))))
                    page.wait_for_timeout(ms)
                    action_results.append({"ok": True, "action": kind, "waited_ms": ms})
                elif kind == "wait_for_load_state":
                    state = str(action.get("state", "load"))
                    if state not in {"load", "domcontentloaded", "networkidle"}:
                        raise BrowserAutomationError("wait_for_load_state state must be load, domcontentloaded or networkidle.")
                    try:
                        page.wait_for_load_state(state, timeout=per_timeout)
                        action_results.append({"ok": True, "action": kind, "state": state})
                    except Exception as wait_exc:
                        if "timeout" in str(wait_exc).lower() or "timed out" in str(wait_exc).lower():
                            logger.warning("Load-state wait for %s timed out at %s; keeping tab usable.", state, page.url)
                            action_results.append({"ok": False, "action": kind, "state": state, "timed_out": True, "recoverable": True, "current_url": page.url, "message": "Load-state wait timed out; background requests may keep the page active. Continue using the current page and inspect its controls."})
                        else:
                            raise
                elif kind == "wait_for_url":
                    pattern = str(action.get("url", action.get("value", ""))).strip()
                    if not pattern:
                        raise BrowserAutomationError("wait_for_url needs a URL or pattern in `url`/`value`.")
                    try:
                        page.wait_for_url(pattern, timeout=per_timeout)
                        action_results.append({"ok": True, "action": kind, "url": page.url})
                    except Exception as wait_exc:
                        if "timeout" in str(wait_exc).lower() or "timed out" in str(wait_exc).lower():
                            action_results.append({"ok": False, "action": kind, "timed_out": True, "recoverable": True, "current_url": page.url, "message": "The expected URL was not reached before timeout; the current tab remains open."})
                        else:
                            raise
                elif kind == "screenshot":
                    file_name = Path(str(action.get("filename", "screenshot.png"))).name
                    if not file_name.lower().endswith(".png"):
                        file_name += ".png"
                    output = ARTIFACTS_DIR / file_name
                    page.screenshot(path=str(output), full_page=bool(action.get("full_page", True)))
                    action_results.append({"ok": True, "action": kind, "screenshot": str(output), "tab_id": _register_page(session, page)})
                elif kind in {"scrape", "extract", "inspect", "test"}:
                    action_results.append({"ok": True, "action": kind, "data": _scrape_page(page, action.get("max_chars", MAX_TEXT_CHARS))})
                elif kind in {"focus_browser", "bring_to_front"}:
                    try: page.bring_to_front()
                    except Exception as focus_exc: logger.debug("Could not focus browser tab: %s", focus_exc)
                    session["page"] = page
                    action_results.append({"ok": True, "action": kind, "tab_id": _register_page(session, page), "brought_to_front": True})
                elif kind == "scroll":
                    dx = float(action.get("delta_x", 0))
                    dy = float(action.get("delta_y", action.get("value", 600)))
                    page.mouse.wheel(dx, dy)
                    action_results.append({"ok": True, "action": kind, "delta_x": dx, "delta_y": dy})
                elif kind in {"scroll_until_stable", "lazy_load", "scroll_full_page"}:
                    action_results.append(_scroll_until_stable(page, action))
                elif kind in {"get_images", "extract_images", "find_images"}:
                    action_results.append({"ok": True, "action": kind, **_collect_images(page, int(action.get("limit", action.get("max_images", 200))))})
                elif kind in {"get_html", "page_html", "raw_html"}:
                    action_results.append(_collect_html(page, action))
                elif kind in {"answer_radiogroups", "answer_yes_no", "answer_questions"}:
                    action_results.append(_answer_question_groups(page, action))
                elif kind in {"youtube_find_by_thumbnail", "find_youtube_by_thumbnail", "play_thumbnail_video"}:
                    action_results.append(_youtube_find_by_thumbnail(context, session, page, action))
                elif kind in {"detect_verification", "detect_captcha"}:
                    action_results.append(_detect_verification(page))
                elif kind in {"wait_verification_clear", "wait_captcha_clear"}:
                    verification = _ensure_challenge_resolved(page, max_seconds=min(120.0, per_timeout / 1000.0))
                    action_results.append({"ok": verification is None, "action": kind, "cleared": verification is None, "verification": verification})
                elif kind in {"solve_captcha", "captcha_solve"}:
                    action_results.append(_solve_captcha(context, page, action))
                elif kind in {"fill_form", "fill_fields"}:
                    action_results.append(_fill_form(page, action, per_timeout))
                elif kind in {"youtube_like", "like_video"}:
                    action_results.append(_youtube_like(page))
                elif kind in {"youtube_subscribe", "subscribe_channel"}:
                    action_results.append(_youtube_subscribe(page))
                elif kind in {"youtube_comment", "comment_video"}:
                    action_results.append(_youtube_comment(page, action))
                elif kind in {"google_login", "login_google", "google_signin"}:
                    action_results.append(_google_login(page, action))
                elif kind in {"check_signin", "google_signin_status", "signin_status"}:
                    action_results.append(_google_signin_status(page))
                elif kind in {"linkedin_login", "login_linkedin"}:
                    action_results.append(_linkedin_login(page, action))
                elif kind in {"linkedin_create_post", "linkedin_post", "create_post"}:
                    action_results.append(_linkedin_create_post(page, action))
                elif kind in {"linkedin_scrape_jobs", "linkedin_job_search", "scrape_linkedin_jobs"}:
                    action_results.append(_linkedin_scrape_jobs(page, action))
                elif kind in {"linkedin_easy_apply", "easy_apply"}:
                    action_results.append(_linkedin_easy_apply(page, action))
                elif kind in {"mouse_click", "mouse_move"} and not action.get("selector") and not action.get("target"):
                    if action.get("x") is None or action.get("y") is None:
                        raise BrowserAutomationError(f"{kind} requires x and y coordinates.")
                    x, y = float(action["x"]), float(action["y"])
                    if kind == "mouse_click":
                        page.mouse.click(x, y)
                    else:
                        page.mouse.move(x, y)
                    action_results.append({"ok": True, "action": kind, "mode": "coordinate", "coordinates": {"x": x, "y": y}})
                elif kind == "download":
                    selector = str(action.get("selector", "")).strip()
                    if not selector:
                        raise BrowserAutomationError("download needs a clickable element CSS/XPath in `selector`.")
                    downloads_dir = DATA_DIR / "browser_downloads"
                    downloads_dir.mkdir(parents=True, exist_ok=True)
                    with page.expect_download(timeout=per_timeout) as download_info:
                        _selector(page, selector).first.click(timeout=per_timeout)
                    download = download_info.value
                    suggested = Path(str(action.get("filename") or download.suggested_filename or "download.bin")).name
                    if not suggested or suggested in {".", ".."}:
                        suggested = "download.bin"
                    output = downloads_dir / suggested
                    download.save_as(str(output))
                    action_results.append({"ok": True, "action": kind, "filename": suggested, "path": str(output), "url": download.url})
                elif kind in {"press", "keyboard_press"} and not action.get("selector") and not action.get("target") and action.get("x") is None:
                    key_name = str(action.get("value", action.get("key", ""))).strip()
                    if not key_name:
                        raise BrowserAutomationError("press needs a key name in `value`, e.g. 'Enter', 'Escape' or 'Control+A'.")
                    page.keyboard.press(key_name)
                    action_results.append({"ok": True, "action": kind, "key": key_name, "mode": "keyboard"})
                elif kind in {"click", "fill", "type", "hover", "press", "check", "uncheck", "select", "wait_for_selector", "focus", "submit", "upload_file"}:
                    tabs_before = {id(p) for p in context.pages}
                    try:
                        action_result = _do_locator_action(page, action, per_timeout)
                        action_results.append(action_result)
                        if kind in {"fill", "type", "check", "uncheck", "select", "upload_file"}:
                            for audit_item in reversed(automation_audit):
                                if audit_item.get("action_index") == index + 1:
                                    audit_item["result"] = {k: v for k, v in action_result.items() if k not in {"files"} }
                                    if kind == "upload_file":
                                        audit_item["result"]["files"] = action_result.get("files", [])
                                    break
                    except Exception as action_exc:
                        # A stale/missing optional wait is diagnostic, not a reason to
                        # strand the browser or block the rest of an action batch. The
                        # next operation gets its own fresh page audit and fallbacks.
                        if kind == "wait_for_selector":
                            action_results.append({"ok": False, "action": kind, "recoverable": True, "error": str(action_exc)[:1000], "current_url": page.url})
                            logger.warning("Recoverable wait_for_selector failure; continuing remaining actions: %s", str(action_exc)[:300])
                            continue
                        raise
                    # If a click launched a target=_blank/popup tab, naturally follow it.
                    new_pages = [p for p in context.pages if not p.is_closed() and id(p) not in tabs_before]
                    if new_pages:
                        page = new_pages[-1]
                        _register_page(session, page)
                        session["page"] = page
                else:
                    raise BrowserAutomationError(f"Unknown browser action '{kind}'. See README.md for supported actions.")

            if browser_closed:
                return {"ok": True, "task": mode, "profile": slug, "browser_closed": True, "actions": action_results, "message": "Browser closed after running the earlier actions in this sequence."}

            console_errors = session["console_errors"]
            page_errors = session["page_errors"]
            failed_requests = session["failed_requests"]
            bad_responses = session["bad_responses"]
            tabs = _list_tabs(context, session)
            result: dict[str, Any] = {
                "ok": True, "task": mode, "profile": slug, "headless": headless,
                "url": page.url, "title": page.title(), "navigation_status": navigation_status,
                "navigation": navigation_info,
                "actions": action_results, "tabs": tabs, "screenshot_directory": str(ARTIFACTS_DIR),
                "browser_kept_open": True,
            }
            if mode in {"scrape", "inspect"}:
                result["data"] = _scrape_page(page, payload.get("max_chars", MAX_TEXT_CHARS))
            if mode == "action" and automation_audit:
                result["automation_audit"] = automation_audit[-12:]
                try:
                    result["postflight"] = _automation_snapshot(page, "after-action-batch", screenshot=True)
                except Exception as postflight_exc:
                    result["postflight_error"] = str(postflight_exc)[:200]
            if mode == "test":
                report_data = _scrape_page(page, payload.get("max_chars", MAX_TEXT_CHARS))
                result["data"] = report_data
                result["test_report"] = {
                    "passed": not page_errors and not console_errors and not failed_requests and not bad_responses and not (navigation_status is not None and navigation_status >= 400),
                    "navigation_status": navigation_status,
                    "console_errors": console_errors[:],
                    "page_errors": page_errors[:],
                    "failed_requests": failed_requests[:],
                    "http_error_responses": bad_responses[:],
                    "findings": ([ ("JavaScript console errors", len(console_errors)) ] if console_errors else []) + ([ ("Uncaught page errors", len(page_errors)) ] if page_errors else []) + ([ ("Failed network requests", len(failed_requests)) ] if failed_requests else []) + ([ ("HTTP error responses", len(bad_responses)) ] if bad_responses else []),
                }
            if mode == "test" and result.get("test_report", {}).get("passed") is False:
                logger.error("Browser smoke test reported failure for %s", page.url)
                try:
                    page.screenshot(path=str(ERROR_SCREENSHOT), full_page=True, timeout=5000)
                    result["test_report"]["error_screenshot"] = str(ERROR_SCREENSHOT)
                    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                    page.screenshot(path=str(ARTIFACTS_DIR / f"error-{stamp}.png"), full_page=True, timeout=5000)
                except Exception as screenshot_exc:
                    logger.warning("Could not capture smoke-test screenshot: %s", screenshot_exc)
            session["page"] = page
            verification = _browser_is_human_verification(page)
            if verification:
                result["human_action_required"] = True
                result["human_action"] = verification["kind"]
                result["message"] = verification["message"]
            handoff = _auto_handoff(slug, page, verification, profile_path, extension_paths)
            if handoff:
                result.update(handoff)
            if embedded_manual_handoff:
                result.update({
                    "human_action_required": True, "human_action": "manual_signin",
                    "message": MANUAL_SIGNIN_MESSAGE,
                    "note": "Opened only inside the embedded Browser panel. The agent will not automate the credential step; if the provider blocks embedded/headless sign-in, use a supported sign-in method or provider-approved workflow.",
                })
            _write_metadata(profile_path, {**metadata, "name": metadata.get("name") or "IntignoAI Main Browser", "extensions": extension_paths, "last_used": datetime.now(timezone.utc).isoformat()})
            logger.info("Completed task=%s profile=default current_url=%s tab_count=%d (browser retained open)", mode, page.url, len(tabs))
            return result
        except Exception as exc:
            logger.exception("Browser task failed: %s", exc)
            screenshot_path = None
            if page is not None:
                try:
                    page.screenshot(path=str(ERROR_SCREENSHOT), full_page=True, timeout=5000)
                    screenshot_path = str(ERROR_SCREENSHOT)
                    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                    archive_path = ARTIFACTS_DIR / f"error-{stamp}.png"
                    page.screenshot(path=str(archive_path), full_page=True, timeout=5000)
                except Exception as screenshot_exc:
                    logger.warning("Could not capture error screenshot: %s", screenshot_exc)
            failure = {
                "ok": False, "status_code": 500, "profile": "default", "headless": headless,
                "error": str(exc), "actions_completed": action_results,
                "tabs": _list_tabs(session["context"], session) if session and session.get("context") else [],
                "error_screenshot": screenshot_path, "log_file": str(LOG_PATH),
            }
            if "manual-only mode" in str(exc):
                failure.update({"human_action_required": True, "human_action": "close_manual_browser", "message": str(exc)})
            if page is not None:
                verification = _browser_is_human_verification(page)
                if verification:
                    failure["human_action_required"] = True
                    failure["human_action"] = verification["kind"]
                    failure["message"] = verification["message"]
                handoff = _auto_handoff(slug, page, verification, profile_path, extension_paths)
                if handoff:
                    failure.update(handoff)
            return failure
        finally:
            # The browser remains open unless an explicit close_browser action ran.
            pass

def _close_browser_session(profile: str) -> bool:
    session = _browser_sessions.pop(profile, None)
    if not session:
        return False
    cdp_mode = session.get("connection_mode") == "native_chrome_cdp"
    if cdp_mode:
        # A CDP default context cannot reliably be context.close()'d. Disconnect
        # Playwright first; only terminate the exact Chrome process this agent started
        # when the user explicitly asked to close the browser.
        try:
            browser = session.get("browser")
            if browser is not None:
                browser.close()
        except Exception as exc:
            logger.debug("Could not disconnect native browser cleanly: %s", exc)
        _terminate_native_proc(profile)
        process = session.get("browser_process")
        if session.get("owned_process") and process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=4)
            except Exception as exc:
                logger.warning("Native browser did not terminate cleanly: %s", exc)
                try: process.kill()
                except Exception: pass
    else:
        try:
            session["context"].close()
        except Exception as exc:
            logger.warning("Failed to close browser context for %s: %s", profile, exc)
    try:
        session["playwright"].stop()
    except Exception as exc:
        logger.warning("Failed to stop Playwright for %s: %s", profile, exc)
    return True


def _browser_command_worker() -> None:
    global _browser_worker_thread
    while True:
        payload, done, result_box = _browser_commands.get()
        try:
            result_box.append(_perform_browser_task_impl(payload))
        except BaseException as exc:
            logger.exception("Unexpected browser worker failure")
            result_box.append({"ok": False, "status_code": 500, "error": str(exc)})
        finally:
            done.set()
            _browser_commands.task_done()


def perform_browser_task(payload: dict[str, Any]) -> dict[str, Any]:
    """Run Playwright work on its owner thread while preserving visible windows."""
    global _browser_worker_thread
    with _browser_worker_lock:
        if _browser_worker_thread is None or not _browser_worker_thread.is_alive():
            _browser_worker_thread = threading.Thread(target=_browser_command_worker, name="IntignoAI-Browser", daemon=True)
            _browser_worker_thread.start()
    completed = threading.Event()
    result_box: list[dict[str, Any]] = []
    _browser_commands.put((payload if isinstance(payload, dict) else {}, completed, result_box))
    if not completed.wait(timeout=600):
        return {"ok": False, "status_code": 504, "error": "Browser command timed out waiting for the browser worker."}
    return result_box[0] if result_box else {"ok": False, "status_code": 500, "error": "Browser worker returned no result."}


def install_playwright_browser() -> dict[str, Any]:
    """Ensure Chromium exists; invoked by the first-run launch scripts."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
        if executable.is_file():
            return {"ok": True, "installed": True, "executable": str(executable)}
    except Exception as exc:
        logger.warning("Playwright browser check failed; install will be attempted: %s", exc)
    import subprocess
    import sys
    command = [sys.executable, "-m", "playwright", "install", "chromium"]
    logger.info("Installing Playwright Chromium: %s", " ".join(command))
    completed = subprocess.run(command, cwd=str(ROOT), check=False)
    if completed.returncode:
        return {"ok": False, "installed": False, "error": f"Playwright Chromium install exited with code {completed.returncode}."}
    return {"ok": True, "installed": True}
