"""Idempotent first-run helper for the Playwright Chromium browser binary."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from tools.browser_automation import install_playwright_browser
    result = install_playwright_browser()
except Exception as exc:
    print(f"[IntignoAI] Playwright setup failed: {exc}", file=sys.stderr)
    raise SystemExit(1)

if not result.get("ok"):
    print("[IntignoAI] " + result.get("error", "Playwright setup failed."), file=sys.stderr)
    print("[IntignoAI] Check internet access and run start.bat/start.sh again after fixing the issue.", file=sys.stderr)
    raise SystemExit(1)
print("[IntignoAI] Playwright Chromium is ready.")
