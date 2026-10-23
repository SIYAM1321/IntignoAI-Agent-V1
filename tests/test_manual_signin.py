"""Embedded-only manual sign-in/browser routing tests.

The tests use a local browser binary when one is available, and replace navigation
with a data URL so they never require access to external websites or open a UI window.
"""
import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class ManualSigninTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"<!doctype html><title>IntignoAI Embedded Test</title><body>embedded test</body>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *_args):
                pass
        cls.http_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.http_thread = threading.Thread(target=cls.http_server.serve_forever, daemon=True)
        cls.http_thread.start()
        cls.local_url = f"http://127.0.0.1:{cls.http_server.server_port}/"
        executable = shutil.which("chromium") or shutil.which("google-chrome") or shutil.which("chrome") or shutil.which("msedge")
        if not executable:
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as p:
                    candidate = Path(p.chromium.executable_path)
                executable = str(candidate) if candidate.is_file() else None
            except Exception:
                executable = None
        if not executable:
            raise unittest.SkipTest("No local Chromium/Chrome binary available for embedded browser tests")
        os.environ["BROWSER_EXECUTABLE"] = executable
        os.environ["BROWSER_PROFILE_DIR"] = str(cls.tmp / "profiles")
        os.environ["BROWSER_EMBEDDED_UI"] = "true"
        os.environ["BROWSER_HEADLESS"] = "true"
        os.environ["BROWSER_START_TIMEOUT_SECONDS"] = "8"
        from tools import browser_automation as b
        cls.b = b
        cls.original_spawn = b._spawn_native_browser
        cls.original_navigate = b._navigate_page
        cls.launch_calls = []

        def record_spawn(*args, **kwargs):
            cls.launch_calls.append({"headless": kwargs.get("headless", False), "debug_port": kwargs.get("debug_port", True)})
            return cls.original_spawn(*args, **kwargs)

        def local_navigation(page, target_url, wait_until, timeout):
            # Keep these tests deterministic and offline. The routing contract does not
            # depend on the page being allowed to navigate in this sandbox.
            return None, False, {"test_navigation": True, "requested_url": target_url}

        b._spawn_native_browser = record_spawn
        b._navigate_page = local_navigation

    @classmethod
    def tearDownClass(cls):
        cls.b._spawn_native_browser = cls.original_spawn
        cls.b._navigate_page = cls.original_navigate
        for name in ("BROWSER_EXECUTABLE", "BROWSER_PROFILE_DIR", "BROWSER_EMBEDDED_UI", "BROWSER_HEADLESS", "BROWSER_START_TIMEOUT_SECONDS"):
            os.environ.pop(name, None)
        cls.http_server.shutdown()
        cls.http_server.server_close()

    def tearDown(self):
        self.b.perform_browser_task({"actions": [{"action": "close_browser"}]})
        time.sleep(0.2)

    def test_google_url_detection(self):
        b = self.b
        self.assertTrue(b._is_google_auth_url("https://accounts.google.com/v3/signin/rejected?x=1"))
        self.assertFalse(b._is_google_auth_url("https://mail.google.com/"))
        self.assertFalse(b._is_google_auth_url("not a url"))

    def test_manual_signin_is_handed_to_embedded_browser_not_physical_window(self):
        self.launch_calls.clear()
        with patch.object(self.b, "_manual_browser_open", side_effect=AssertionError("physical browser must never open")):
            result = self.b.perform_browser_task({"task": "manual_signin"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["human_action"], "manual_signin")
        self.assertIn("embedded Browser tab", result["message"])
        self.assertTrue(result.get("headless"))
        self.assertTrue(self.launch_calls)
        self.assertTrue(self.launch_calls[-1]["headless"])
        self.assertTrue(self.launch_calls[-1]["debug_port"])
        self.assertFalse(self.b._native_procs.get("default", {}).get("pure", False))

    def test_google_signin_navigation_uses_embedded_manual_handoff(self):
        with patch.object(self.b, "_manual_browser_open", side_effect=AssertionError("physical browser must never open")):
            result = self.b.perform_browser_task({"actions": [{"action": "navigate", "url": "https://accounts.google.com/v3/signin/identifier"}]})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["human_action"], "manual_signin")
        self.assertIn("embedded Browser panel", result["note"])
        self.assertTrue(result.get("headless"))

    def test_second_manual_handoff_reuses_embedded_session(self):
        first_result = self.b.perform_browser_task({"task": "manual_signin", "url": "https://example.com"})
        self.assertTrue(first_result["ok"], first_result)
        first_context = self.b._browser_sessions["default"]["context"]
        second_result = self.b.perform_browser_task({"task": "manual_signin", "url": "https://github.com/login"})
        self.assertTrue(second_result["ok"], second_result)
        self.assertIs(self.b._browser_sessions["default"]["context"], first_context)
        self.assertFalse(self.b._native_procs.get("default", {}).get("pure", False))

    def test_release_is_idempotent_and_batch_misuse_rejected(self):
        self.assertTrue(self.b.perform_browser_task({"task": "release"})["ok"])
        bad = self.b.perform_browser_task({"actions": [{"action": "manual_signin"}, {"action": "wait", "ms": 1}]})
        self.assertFalse(bad["ok"])


if __name__ == "__main__":
    unittest.main()
