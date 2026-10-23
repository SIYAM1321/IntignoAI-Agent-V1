from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tools import browser_automation as browser
from agent import engine


def test_only_one_persistent_profile_is_created_and_all_names_route_to_default(tmp_path, monkeypatch):
    monkeypatch.setattr(browser, "PROFILES_DIR", tmp_path / "profiles")
    browser.PROFILES_DIR.mkdir(parents=True)
    assert browser.list_profiles()["count"] == 1
    assert browser.create_profile("work")["profile"] == "default"
    assert browser.create_profile("personal")["profile"] == "default"
    assert browser.list_profiles()["count"] == 1
    assert {profile["id"] for profile in browser.list_profiles()["profiles"]} == {"default"}
    assert browser._choose_profile("personal")[0] == "default"


def test_profile_name_traversal_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(browser, "PROFILES_DIR", tmp_path / "profiles")
    with pytest.raises(ValueError):
        browser.create_profile("../outside")


def test_url_validation_rejects_file_and_script_schemes():
    for url in ("file:///etc/passwd", "javascript:alert(1)", "not-a-url"):
        result = browser.perform_browser_task({"task": "scrape", "url": url})
        assert result["ok"] is False
        assert result["status_code"] == 400


def test_current_page_scrape_without_url_requires_an_existing_web_tab(monkeypatch):
    monkeypatch.setattr(browser, "_browser_sessions", {})
    result = browser.perform_browser_task({"task": "scrape"})
    assert result["ok"] is False
    assert result["status_code"] == 400
    assert "unless a web page is already open" in result["error"]


def test_unpacked_extension_requires_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(browser, "PROFILES_DIR", tmp_path / "profiles")
    browser.create_profile("default")
    extension = tmp_path / "extension"
    extension.mkdir()
    with pytest.raises(ValueError, match="manifest.json"):
        browser.add_profile_extension("default", str(extension))
    (extension / "manifest.json").write_text(json.dumps({"manifest_version": 3, "name": "Test", "version": "1.0"}), encoding="utf-8")
    result = browser.add_profile_extension("default", str(extension))
    assert result["ok"] is True
    assert result["extensions"] == [str(extension.resolve())]


def test_xpath_selector_prefix_and_semantic_fallback(tmp_path, monkeypatch):
    class FakeLocator:
        first = None
        def __init__(self, found=False):
            self.found = found
            self.first = self
            self.clicked = False
        def wait_for(self, **_kwargs):
            if not self.found:
                raise RuntimeError("not found")
        def click(self, **_kwargs):
            if not self.found:
                raise RuntimeError("not found")
            self.clicked = True
        def fill(self, *_args, **_kwargs):
            if not self.found:
                raise RuntimeError("not found")
        def bounding_box(self, **_kwargs):
            return {"x": 10, "y": 20, "width": 40, "height": 20} if self.found else None
    class FakeMouse:
        def __init__(self): self.clicked = []
        def click(self, x, y): self.clicked.append((x, y))
        def move(self, x, y): self.clicked.append((x, y))
    class FakeKeyboard:
        def press(self, _key): pass
        def insert_text(self, _text): pass
    class FakePage:
        def __init__(self): self.mouse = FakeMouse(); self.keyboard = FakeKeyboard()
        def locator(self, selector):
            self.last_selector = selector
            return FakeLocator(False)
        def get_by_text(self, text, exact=False):
            return FakeLocator(True)
        def get_by_label(self, text, exact=False): return FakeLocator(False)
        def get_by_placeholder(self, text, exact=False): return FakeLocator(False)
        def get_by_role(self, role, name=None, exact=False): return FakeLocator(False)
    page = FakePage()
    assert browser._selector(page, "//button[@id='go']").first is not None
    assert page.last_selector == "xpath=//button[@id='go']"
    result = browser._do_locator_action(page, {"action": "click", "selector": "#missing", "fallback": {"text": "Continue"}}, 1000)
    assert result["mode"] == "semantic"
    assert page.mouse.clicked == []


def test_json_escaped_css_selector_is_normalized():
    class FakePage:
        def locator(self, selector):
            self.seen = selector
            return type("Locator", (), {"first": None})()
    page = FakePage()
    browser._selector(page, r'input[name=\"ss01[]\"]')
    assert page.seen == 'input[name="ss01[]"]'
    browser._selector(page, r'input[type=\"file\"]')
    assert page.seen == 'input[type="file"]'


def test_upload_file_supports_hidden_input_without_visibility_wait(tmp_path):
    file_path = tmp_path / "resume.pdf"
    file_path.write_bytes(b"fake pdf for test")
    class FakeLocator:
        def __init__(self):
            self.wait_state = None
            self.paths = []
        def wait_for(self, state, timeout):
            self.wait_state = state
            assert state == "attached"  # hidden input must not require visibility
        def set_input_files(self, paths, timeout):
            self.paths = list(paths)
        def evaluate(self, _script):
            return {"tag": "input", "type": "file", "files": [{"name": Path(x).name, "size": Path(x).stat().st_size} for x in self.paths], "accept": ".pdf", "name": "resume", "id": "resume-upload"}
    locator = FakeLocator()
    result = browser._run_locator_action(None, locator, "upload_file", {"action": "upload_file", "filename": str(file_path)}, 1000, "selector")
    assert result["ok"] is True
    assert result["verification"] == "file_selected_in_input"
    assert result["files"][0]["name"] == "resume.pdf"
    assert locator.wait_state == "attached"
    assert locator.paths == [str(file_path.resolve())]


def test_file_input_discovery_prefers_accept_match(tmp_path):
    file_path = tmp_path / "resume.pdf"
    file_path.write_bytes(b"pdf")
    class FakeLocator:
        def __init__(self, info): self.info = info
        def evaluate(self, _script): return self.info
    class FakeInputs:
        def __init__(self, items): self.items = items
        def count(self): return len(self.items)
        def nth(self, index): return self.items[index]
    class FakeFrame:
        def __init__(self, items): self.items = items
        def locator(self, _selector): return FakeInputs(self.items)
    photo = FakeLocator({"id":"photo", "name":"photo", "accept":"image/png,image/jpeg", "label":"Profile photo", "nearby":"Profile photo", "disabled":False})
    resume = FakeLocator({"id":"resume", "name":"resume", "accept":".pdf,.doc,.docx", "label":"Resume", "nearby":"Upload resume", "disabled":False})
    picked = browser._choose_file_input(None, [FakeFrame([photo, resume])], {"file_path":str(file_path)})
    assert picked[0] is resume
    assert picked[1]["id"] == "resume"


def test_file_input_discovery_rejects_ambiguous_inputs(tmp_path):
    file_path = tmp_path / "resume.pdf"
    file_path.write_bytes(b"pdf")
    class FakeLocator:
        def __init__(self, info): self.info = info
        def evaluate(self, _script): return self.info
    class FakeInputs:
        def __init__(self, items): self.items = items
        def count(self): return len(self.items)
        def nth(self, index): return self.items[index]
    class FakeFrame:
        def __init__(self, items): self.items = items
        def locator(self, _selector): return FakeInputs(self.items)
    one = FakeLocator({"id":"resume", "name":"resume", "accept":".pdf", "label":"", "nearby":"", "disabled":False})
    two = FakeLocator({"id":"cover", "name":"cover_letter", "accept":".pdf", "label":"", "nearby":"", "disabled":False})
    with pytest.raises(browser.BrowserAutomationError, match="Multiple file upload inputs"):
        browser._choose_file_input(None, [FakeFrame([one, two])], {"file_path":str(file_path)})


def test_navigation_timeout_does_not_mistake_old_same_origin_page_for_success():
    """v1.8 contract: a timeout on a DIFFERENT page is reported structurally, never raised.

    v1.7.2 raised here, which made the model retry the identical goto forever (the reported
    infinite loading loop). The structured result tells the model the tab is not usable yet
    instead of triggering another restart of the same navigation.
    """
    class FakePage:
        url = "https://example.test/old"

        def goto(self, _url, **_kwargs):
            raise RuntimeError("Timeout 1000ms exceeded")

        def wait_for_url(self, predicate=None, timeout=None):
            raise RuntimeError("Timeout 1000ms exceeded")

        def wait_for_load_state(self, state=None, **_kwargs):
            return None

        def wait_for_timeout(self, ms):
            return None

    response, fallback_used, info = browser._navigate_page(FakePage(), "https://example.test/new", "networkidle", 1000)
    assert response is None
    assert fallback_used is False
    assert info["navigation_timeout"] is True
    assert info["retry_recommended"] is False


def test_activity_arguments_redact_password_controls():
    safe = engine._safe_activity_arguments("browser_action", {
        "actions": [
            {"action": "fill", "selector": "input[type=password]", "value": "dont-log-this"},
            {"action": "fill", "selector": "#email", "value": "person@example.test"},
        ]
    })
    assert safe["actions"][0]["value"] == "[REDACTED]"
    assert safe["actions"][1]["value"] == "person@example.test"


def test_multiple_tabs_share_one_profile_and_close_runs_after_previous_actions(tmp_path, monkeypatch):
    import sys
    import types

    monkeypatch.setattr(browser, "PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setenv("BROWSER_USE_NATIVE_CHROME", "false")
    browser._browser_sessions.clear()
    state = {"launches": 0, "context_closes": 0, "playwright_stops": 0, "launch_args": None}

    class FakeResponse:
        status = 200

    class FakePage:
        def __init__(self, context):
            self.context = context
            self.url = "about:blank"
            self.closed = False
            self.handlers = {}
            self.mouse = types.SimpleNamespace(click=lambda *_: None, move=lambda *_: None, wheel=lambda *_: None)
            self.keyboard = types.SimpleNamespace(press=lambda *_: None, insert_text=lambda *_: None)

        def is_closed(self): return self.closed
        def set_default_timeout(self, _timeout): pass
        def on(self, event, callback): self.handlers[event] = callback
        def wait_for_timeout(self, _ms): pass
        def goto(self, url, **_kwargs): self.url = url; return FakeResponse()
        def title(self): return self.url.rsplit("/", 1)[-1] or "Blank"
        def close(self): self.closed = True
        def screenshot(self, path, **_kwargs): Path(path).write_bytes(b"fake png")

    class FakeContext:
        def __init__(self):
            self.pages = [FakePage(self)]
            self.closed = False
            self.handlers = {}

        def is_closed(self): return self.closed
        def on(self, event, callback): self.handlers[event] = callback
        def new_page(self):
            page = FakePage(self)
            self.pages.append(page)
            if "page" in self.handlers: self.handlers["page"](page)
            return page
        def close(self):
            self.closed = True
            state["context_closes"] += 1
            for page in self.pages: page.close()

    class FakeChromium:
        def launch_persistent_context(self, **kwargs):
            state["launches"] += 1
            state["launch_args"] = kwargs
            self.context = FakeContext()
            return self.context

    class FakePlaywright:
        def __init__(self): self.chromium = FakeChromium()
        def stop(self): state["playwright_stops"] += 1

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: types.SimpleNamespace(start=lambda: FakePlaywright())
    playwright_pkg = types.ModuleType("playwright")
    playwright_pkg.__path__ = []
    playwright_pkg.sync_api = sync_api
    monkeypatch.setitem(sys.modules, "playwright", playwright_pkg)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)

    try:
        first = browser._perform_browser_task_impl({
            "task": "action", "profile": "work", "headless": False,
            "actions": [
                {"action": "new_tab", "url": "https://example.test/one"},
                {"action": "new_tab", "url": "https://example.test/two"},
                {"action": "list_tabs"},
            ],
        })
        assert first["ok"] is True
        assert len(first["tabs"]) == 3  # initial tab + two opened tabs
        assert len(first["actions"][-1]["tabs"]) == 3
        assert browser.list_profiles()["count"] == 1

        # Omit URL to inspect the current active tab, as requested in several
        # historical log turns; do not relaunch or discard the persistent page.
        scraped = browser._perform_browser_task_impl({"task": "scrape", "profile": "default", "headless": False})
        assert scraped["ok"] is True
        assert scraped["data"]["url"] == "https://example.test/two"
        assert state["launches"] == 1

        second = browser._perform_browser_task_impl({
            "task": "action", "profile": "default", "headless": False,
            "actions": [
                {"action": "switch_tab", "tab_index": 0},
                {"action": "navigate", "url": "https://example.test/updated"},
                {"action": "list_tabs"},
                {"action": "close_browser"},
            ],
        })
        assert second["ok"] is True
        assert second["browser_closed"] is True
        assert [item["action"] for item in second["actions"]] == ["switch_tab", "navigate", "list_tabs", "close_browser"]
        listed = second["actions"][2]["tabs"]
        assert listed[0]["url"] == "https://example.test/updated"
        assert state["launches"] == 1  # every tab/task reused the same context/profile
        assert state["launch_args"]["user_data_dir"] == str(browser.PROFILES_DIR / "default")
        assert state["context_closes"] == 1
        assert state["playwright_stops"] == 1
    finally:
        browser._close_browser_session("default")


def test_chat_attachments_include_exact_local_path_for_browser_upload(tmp_path):
    file_path = tmp_path / "resume.pdf"
    file_path.write_bytes(b"fake pdf")
    request_input, display = engine._prepare_user_input("Apply for this job", [{"name": "resume.pdf", "path": str(file_path)}])
    text = request_input[0]["text"] if isinstance(request_input, list) else request_input
    assert str(file_path.resolve()) in text
    assert "browser upload" in text.lower()
    assert "resume.pdf" in display


def test_agent_registers_browser_tools_and_confirmation_for_submit():
    names = {tool.name for tool in engine.TOOLS}
    assert {"browser_profiles", "browser_create_profile", "browser_add_extension", "browser_action", "browser_scrape", "browser_test"}.issubset(names)
    assert engine.needs_confirmation("browser_add_extension", {"profile": "default", "extension_path": "extension"}) is True
    assert engine.needs_confirmation("browser_action", {"actions": [{"action": "submit"}]}) is True
    assert engine.needs_confirmation("browser_action", {"actions": [{"action": "fill", "selector": "#name"}]}) is False
    # v1.13.4: generic form submits (job applications etc.) no longer pause...
    assert engine.needs_confirmation("browser_action", {"actions": [{"action": "click", "selector": "button[type=submit]"}]}) is False
    assert engine.needs_confirmation("browser_action", {"actions": [
        {"action": "click", "selector": "button:has-text('Submit application')", "fallback": {"name": "Submit application", "role": "button"}},
        {"action": "wait", "ms": 3000},
        {"action": "scrape", "max_chars": 2000},
    ]}) is False
    # ...while money/deletion/signup-class clicks still pause.
    assert engine.needs_confirmation("browser_action", {"actions": [{"action": "click", "text": "Pay now"}]}) is True
    assert "kill_port" in names
    assert engine.needs_confirmation("kill_port", {"port": 5000}) is True



def test_login_submit_does_not_trigger_redundant_confirmation():
    login_batch = {"actions": [
        {"action": "fill", "selector": "input#email", "value": "user@example.test"},
        {"action": "fill", "selector": "input#password", "value": "not-a-real-secret"},
        {"action": "click", "selector": "button[type=submit]"},
    ]}
    assert engine.needs_confirmation("browser_action", login_batch) is False
    assert engine.needs_confirmation("browser_action", {"actions": [{"action": "click", "text": "Sign in"}]}) is False
    mixed_login_and_payment = {"actions": login_batch["actions"] + [{"action": "click", "text": "Pay now"}]}
    assert engine.needs_confirmation("browser_action", mixed_login_and_payment) is True
    signup_batch = {"actions": [
        {"action": "fill", "selector": "input#email", "value": "user@example.test"},
        {"action": "fill", "selector": "input#password", "value": "not-a-real-secret"},
        {"action": "click", "selector": "button[type=submit]", "text": "Create account"},
    ]}
    assert engine.needs_confirmation("browser_action", signup_batch) is True
    assert engine.needs_confirmation("browser_action", {"actions": [{"action": "submit"}]}) is True


def test_native_browser_lookup_and_verification_detection_helpers_exist():
    assert callable(browser._find_native_browser)
    assert callable(browser._browser_is_human_verification)
    assert browser.DEFAULT_HEADLESS is False


def test_frame_candidates_prioritize_requested_frame():
    class FakeFrame:
        def __init__(self, url, name=""):
            self.url = url
            self.name = name
    class FakePage:
        def __init__(self):
            self.main_frame = FakeFrame("https://example.test", "main")
            self.frames = [self.main_frame, FakeFrame("https://auth.example.test/frame", "auth")]
    page = FakePage()
    frames = browser._frame_candidates(page, {"frame_url_contains": "auth.example.test"})
    assert frames[0].url == "https://auth.example.test/frame"
    assert page.main_frame in frames


def test_password_field_heuristic_searches_child_iframes():
    class FakeLocator:
        def __init__(self, found=False, frame=None):
            self.found = found
            self.first = self
            self.frame = frame
        def wait_for(self, **_kwargs):
            if not self.found: raise RuntimeError("not found")
        def fill(self, value, **_kwargs):
            if not self.found: raise RuntimeError("not found")
            self.frame.filled = value
        def click(self, **_kwargs):
            if not self.found: raise RuntimeError("not found")
    class FakeFrame:
        def __init__(self, url, password_found=False):
            self.url, self.name, self.password_found = url, "", password_found
            self.filled = None
        def locator(self, selector):
            found = self.password_found and selector == 'input[type="password"]'
            return FakeLocator(found, self)
        def get_by_label(self, *_args, **_kwargs): return FakeLocator(False, self)
        def get_by_placeholder(self, *_args, **_kwargs): return FakeLocator(False, self)
        def get_by_role(self, *_args, **_kwargs): return FakeLocator(False, self)
        def get_by_text(self, *_args, **_kwargs): return FakeLocator(False, self)
    main = FakeFrame("https://www.linkedin.com/login")
    auth_frame = FakeFrame("https://auth.example.test/frame", password_found=True)
    class FakePage(FakeFrame):
        def __init__(self):
            super().__init__("https://www.linkedin.com/login")
            self.main_frame = main
            self.frames = [main, auth_frame]
            self.mouse = type("Mouse", (), {"click": lambda *_a: None, "move": lambda *_a: None})()
            self.keyboard = type("Keyboard", (), {"press": lambda *_a: None, "insert_text": lambda *_a: None})()
        def locator(self, selector): return main.locator(selector)
    page = FakePage()
    result = browser._do_locator_action(page, {
        "action": "fill", "selector": "#password", "value": "test-value"
    }, 900)
    assert result["mode"] == "semantic"
    assert auth_frame.filled == "test-value"


def test_google_security_rejection_is_reported_for_human_action():
    class FakeLocator:
        def inner_text(self, **_kwargs): return "Couldn’t sign you in. This browser or app may not be secure."
    class FakePage:
        url = "https://accounts.google.com/v3/signin/rejected"
        frames = []
        def locator(self, _selector): return FakeLocator()
    detected = browser._browser_is_human_verification(FakePage())
    assert detected is not None
    assert detected["kind"] == "provider_security_block"
    assert "Do not retry" in detected["message"]


def test_navigation_networkidle_timeout_can_fall_back_to_domcontentloaded():
    class FakePage:
        url = "https://example.test/app"
        def goto(self, _url, **_kwargs): raise RuntimeError("Timeout 1000ms exceeded")
        def wait_for_load_state(self, state, **_kwargs): assert state == "domcontentloaded"
        def wait_for_timeout(self, ms): return None
    response, fallback_used, info = browser._navigate_page(FakePage(), "https://example.test/app", "networkidle", 1000)
    assert response is None
    assert fallback_used is True
    assert info["recovered"] is True


def test_real_chromium_helpers_on_in_memory_pages_when_browser_is_available(tmp_path):
    """Local-only real-browser regression: no network, credentials or external websites."""
    playwright_api = pytest.importorskip("playwright.sync_api")
    executable = browser._find_native_browser()
    if not executable:
        pytest.skip("No installed Chromium/Chrome/Edge executable for the optional live helper smoke test")
    args = ["--no-sandbox"] if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0 else []
    try:
        pw = playwright_api.sync_playwright().start()
        chromium = pw.chromium.launch(headless=True, executable_path=executable, args=args)
    except Exception as exc:
        try: pw.stop()
        except Exception: pass
        pytest.skip(f"Installed browser could not start headlessly in this environment: {exc}")
    try:
        page = chromium.new_page()
        page.set_content("""<!doctype html><html><body>
        <label>Email Address <input type='email' id='localized_account' autocomplete='username'></label>
        <label>Password <input type='password' id='localized_secret' autocomplete='current-password'></label>
        <button type='submit'>Sign in</button></body></html>""")
        email = browser._do_locator_action(page, {"action": "fill", "selector": "#email", "value": "qa@example.test"}, 1200)
        password = browser._do_locator_action(page, {"action": "fill", "selector": "#password", "value": "fake-test-only"}, 1200)
        assert email["ok"] and email["mode"] == "semantic"
        assert password["ok"] and password["mode"] == "semantic"
        inspected = browser._scrape_page(page)
        email_control = next(control for control in inspected.get("controls", []) if control.get("id") == "localized_account")
        assert email_control.get("coordinate_space") == "page_viewport"
        assert isinstance(email_control.get("rect", {}).get("center_x"), int)

        page.set_content("""<!doctype html><html><body><h1>Parent</h1>
        <iframe id='auth-frame' srcdoc=\"&lt;!doctype html&gt;&lt;html&gt;&lt;body&gt;&lt;h2&gt;Frame content&lt;/h2&gt;&lt;label&gt;Password &lt;input type='password' id='frame_password'&gt;&lt;/label&gt;&lt;/body&gt;&lt;/html&gt;\"></iframe>
        </body></html>""")
        framed = browser._do_locator_action(page, {"action": "fill", "selector": "#frame_password", "value": "frame-test"}, 1200)
        framed_inspect = browser._scrape_page(page)
        assert framed["ok"]
        assert any("Frame content" in frame.get("text", "") for frame in framed_inspect.get("frames", []))
        child_frame = next(frame for frame in framed_inspect.get("frames", []) if frame.get("frame_index") == 1)
        assert child_frame.get("coordinate_space") == "frame_viewport"

        page.set_content("<html><body><h1>Couldn’t sign you in</h1><p>This browser or app may not be secure.</p></body></html>")
        signal = browser._browser_is_human_verification(page)
        assert signal and signal["kind"] == "provider_security_block"
    finally:
        chromium.close()
        pw.stop()

def test_browser_flask_routes_are_local_and_exposed():
    pytest.importorskip("flask")
    pytest.importorskip("werkzeug")
    from app import app as flask_app
    flask_app.testing = True
    client = flask_app.test_client()
    profiles_response = client.get("/api/browser-profiles")
    assert profiles_response.status_code == 200
    assert "profiles" in profiles_response.get_json()
    invalid_action = client.post("/browser-action", json={"task": "scrape", "url": "file:///private"})
    assert invalid_action.status_code == 400
    assert invalid_action.get_json()["ok"] is False
    status = client.get("/api/status").get_json()
    assert status["version"] == (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
    assert "browser_automation" in status
