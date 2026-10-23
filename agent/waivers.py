"""v1.13.4: Standing confirmation-waiver store.

When the person says something like "never ask me for confirmation again" (English or
Banglish), IntignoAI persists that preference here and honors it on every later turn:
``browser_action`` calls inside the covered scope stop pausing for approval. The waiver
NEVER covers file/directory deletion, port killing, browser extension installs or
dangerous shell commands — those always stay behind explicit confirmation.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
WAIVERS_PATH = Path(os.getenv("INTIGNOAI_WAIVERS_PATH", str(ROOT / "data" / "confirmation_waivers.json"))).expanduser().resolve()
_lock = threading.RLock()

# scope key -> lowercase needles that tie a browser task to that scope
SCOPE_MATCHERS: dict[str, tuple[str, ...]] = {
    "linkedin": ("linkedin",),
    "google": ("google", "gmail", "youtube", "accounts.google.com"),
    "facebook": ("facebook", "fb.com"),
    "instagram": ("instagram",),
    "twitter": ("twitter", "x.com"),
    "github": ("github",),
    "upwork": ("upwork",),
    "fiverr": ("fiverr",),
    "amazon": ("amazon",),
}

_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bnever\s+(?:ask|asks|asking|request|requests|require|requires)\b[^.!?]{0,50}\bconfirm",
        r"\b(?:do\s+not|don'?t|dont)\s+(?:ask|asks|asking|request|require)\b[^.!?]{0,50}\bconfirm",
        r"\bno\s+more\s+(?:asking\s+(?:for\s+)?|for\s+)?confirm",
        r"\bwithout\s+(?:any\s+|further\s+)?(?:asking|confirmation|confirm)",
        r"\bconfirm(?:ation)?\s+(?:ar\s+)?kokhono\s+chaibe?\s+na\b",
        r"\b(?:ar\s+)?kokhono\s+confirm(?:ation)?\s+chaibe?\s+na\b",
        r"\bconfirm(?:ation)?\s+chaibe?\s+na\b",
        r"\bconfirm(?:ation)?\s+korar\s+(?:dorkar|proyojon|jor)\s+(?:nai|nei|na)\b",
        r"\bconfirmation\s+(?:lagbe|labe|dorkar)\s+na\b",
        r"\bar\s+confirm(?:ation)?\s+lagbe\s+na\b",
        r"\bconfirm\s+korba?\s+na\b",
        r"\bconfirm(?:ation)?\s+chara\b",
        r"\bsob\s+somoy\s+(?:confirm(?:ation)?\s+chara|without\s+confirm)",
        r"\balways\s+(?:proceed|continue|allow|approve)\b[^.!?]{0,40}\b(?:without|no\s+confirm)",
        r"\bconfirmation\s+(?:off|disable[d]?)\b",
    )
]

_GLOBAL_HINTS = re.compile(r"\b(all|everything|every\s+task|global|sob\s+kaj|shob\s+kaj|any\s+task)\b", re.IGNORECASE)


def detect_waiver_request(message: str) -> dict[str, str] | None:
    """Return {"scope": "global"|"linkedin"|..., "matched": hit} when the message grants a
    standing no-confirmation preference, else None. A named site in the message narrows the
    scope to that site; otherwise the waiver is global."""
    text = str(message or "")
    if not text.strip():
        return None
    matched = None
    for pattern in _PATTERNS:
        hit = pattern.search(text)
        if hit:
            matched = hit.group(0)
            break
    if not matched:
        return None
    lowered = text.lower()
    for scope, needles in SCOPE_MATCHERS.items():
        if any(needle in lowered for needle in needles):
            return {"scope": scope, "matched": matched}
    return {"scope": "global", "matched": matched}


def load_waivers() -> list[dict[str, Any]]:
    try:
        payload = json.loads(WAIVERS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    items = payload.get("waivers") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict) and item.get("scope")]


def persist_waiver(scope: str, source_message: str = "") -> dict[str, Any]:
    """Save one waiver (deduplicated by scope) and return a small report."""
    scope = str(scope or "global").strip().lower() or "global"
    record = {"scope": scope, "added_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "source": str(source_message or "")[:200]}
    with _lock:
        current = load_waivers()
        if any(str(item.get("scope", "")).lower() == scope for item in current):
            return {"ok": True, "scope": scope, "duplicate": True, "waivers": current, "path": str(WAIVERS_PATH)}
        current.append(record)
        try:
            WAIVERS_PATH.parent.mkdir(parents=True, exist_ok=True)
            WAIVERS_PATH.write_text(json.dumps({"waivers": current}, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "scope": scope, "error": str(exc), "path": str(WAIVERS_PATH)}
    return {"ok": True, "scope": scope, "duplicate": False, "waivers": current, "path": str(WAIVERS_PATH)}


def _batch_context(args: dict[str, Any]) -> str:
    parts: list[str] = []
    url = str(args.get("url", ""))
    if url:
        parts.append(url)
    actions = args.get("actions")
    if isinstance(actions, list):
        for item in actions:
            if not isinstance(item, dict):
                continue
            parts.extend(str(item.get(key, "")) for key in ("selector", "target", "text", "value", "label", "placeholder", "name", "url", "frame_url_contains"))
            fallback = item.get("fallback")
            if isinstance(fallback, dict):
                parts.extend(str(v) for v in fallback.values())
    return " ".join(parts).lower()


def waiver_allows(canonical_tool: str, args: dict[str, Any]) -> bool:
    """True when a standing waiver covers this call. Only ``browser_action`` is waivable."""
    if str(canonical_tool or "").strip() != "browser_action":
        return False
    waivers = load_waivers()
    if not waivers:
        return False
    scopes = {str(item.get("scope", "")).strip().lower() for item in waivers}
    if "global" in scopes:
        return True
    context = _batch_context(args)
    for scope in scopes - {"global"}:
        for needle in SCOPE_MATCHERS.get(scope, ()):
            if needle in context:
                return True
    return False
