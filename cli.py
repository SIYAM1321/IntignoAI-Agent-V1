"""IntignoAI Agent CLI bridge: drive the agent's browser automation from ANY language.

Examples (PowerShell/CMD/Python/Node/C#/Java/anything that can run a process or HTTP call):
  python cli.py status
  python cli.py browser --data "{\"task\":\"scrape\",\"url\":\"https://example.com\"}"
  python cli.py browser --url https://example.com --action scrape
  type payload.json | python cli.py browser --stdin

While the app is running, any language can also simply POST the same JSON payload to
http://127.0.0.1:2000/api/browser-action (see README.md). The JSON result is printed to stdout;
the process exit code is 0 on success and 1 on failure.
"""
from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="intignoai",
        description="IntignoAI Agent command-line bridge (usable from any programming language).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    browser = sub.add_parser("browser", help="Run one browser automation task; prints the JSON result.")
    browser.add_argument("--data", help="Full JSON payload (same schema as POST /api/browser-action).")
    browser.add_argument("--stdin", action="store_true", help="Read the JSON payload from stdin.")
    browser.add_argument("--url", help="Shorthand: the URL to navigate/scrape.")
    browser.add_argument("--action", help="Shorthand: a single action name (navigate, scrape, screenshot, ...).")
    browser.add_argument("--selector", help="Optional selector for the shorthand action.")
    browser.add_argument("--value", help="Optional value for the shorthand action.")

    sub.add_parser("status", help="Print version and verify the agent stack imports cleanly.")

    args = parser.parse_args()

    if args.command == "status":
        from agent import engine
        from tools import browser_automation  # noqa: F401  (import proves the stack is intact)

        print(json.dumps({"ok": True, "version": engine.VERSION, "browser_tool": "browser_automation"}, default=str))
        return 0

    payload: dict = {}
    try:
        if args.data:
            payload = json.loads(args.data)
        elif args.stdin:
            payload = json.loads(sys.stdin.read() or "{}")
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": f"Invalid JSON payload: {exc}"}, ensure_ascii=False))
        return 1
    if args.url:
        payload.setdefault("url", args.url)
    if args.action:
        payload.setdefault("task", "action")
        shorthand: dict = {"action": args.action}
        if args.selector:
            shorthand["selector"] = args.selector
        if args.value:
            shorthand["value"] = args.value
        payload.setdefault("actions", [shorthand])
    from tools import browser_automation

    result = browser_automation.perform_browser_task(payload)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
