from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MEMORY_PATH = Path(os.getenv("INTIGNOAI_MEMORY_PATH", str(ROOT / "MEMORY.MD"))).expanduser().resolve()
SKILLS_PATH = Path(os.getenv("INTIGNOAI_SKILLS_PATH", str(ROOT / "SKILLS.MD"))).expanduser().resolve()
_lock = threading.RLock()

_SECRET_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z_-]{25,}"),
    re.compile(r"(?i)\b(api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|passwd|secret)\s*[:=]\s*[^\s,;]{6,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]


def _check_content(content: str) -> str:
    value = str(content or "").strip()
    if not value:
        raise ValueError("Content is required.")
    if len(value) > 12000:
        raise ValueError("Knowledge entry must be 12,000 characters or fewer.")
    if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
        raise ValueError("This entry looks like it may contain a secret or credential. It was not saved.")
    return value


def _cross_process_lock(path: Path):
    """Best-effort cross-process lock with stdlib-only Windows/POSIX support."""
    class Lock:
        def __enter__(self):
            self.path = path.with_suffix(path.suffix + ".lock")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = open(self.path, "a+b")
            try:
                if os.name == "nt":
                    import msvcrt
                    self.handle.seek(0)
                    if self.path.stat().st_size == 0:
                        self.handle.write(b"0")
                        self.handle.flush()
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
            except Exception:
                pass
            return self
        def __exit__(self, exc_type, exc, tb):
            try:
                if os.name == "nt":
                    import msvcrt
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            self.handle.close()
    return Lock()


def append_entry(kind: str, content: str, *, title: str = "") -> dict[str, Any]:
    content = _check_content(content)
    kind = str(kind or "memory").lower().strip()
    if kind not in {"memory", "skill"}:
        raise ValueError("kind must be 'memory' or 'skill'.")
    target = MEMORY_PATH if kind == "memory" else SKILLS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    safe_title = re.sub(r"\s+", " ", str(title or "").strip())[:160]
    if any(pattern.search(safe_title) for pattern in _SECRET_PATTERNS):
        raise ValueError("The title may contain a secret or credential. It was not saved.")
    digest = hashlib.sha256((kind + "\0" + safe_title.casefold() + "\0" + content.casefold()).encode("utf-8")).hexdigest()[:16]
    marker = f"<!-- intignoai-entry:{digest} -->"
    with _lock, _cross_process_lock(target):
        current = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
        if marker in current:
            return {"ok": True, "kind": kind, "path": str(target), "duplicate": True, "entry_id": digest}
        heading = safe_title or ("Memory" if kind == "memory" else "Skill")
        section = f"\n\n{marker}\n## {heading}\n\n{content}\n"
        # Append only: never truncate or replace previous entries.
        with open(target, "a", encoding="utf-8", newline="\n") as stream:
            stream.write(section)
            stream.flush()
            os.fsync(stream.fileno())
    return {"ok": True, "kind": kind, "path": str(target), "duplicate": False, "entry_id": digest}


def read_knowledge(kind: str = "both", max_chars: int = 40000) -> dict[str, Any]:
    kind = str(kind or "both").lower()
    result: dict[str, Any] = {}
    pairs = []
    if kind in {"both", "memory"}:
        pairs.append(("memory", MEMORY_PATH))
    if kind in {"both", "skill", "skills"}:
        pairs.append(("skills", SKILLS_PATH))
    max_chars = max(1000, min(int(max_chars), 100000))
    for label, path in pairs:
        try:
            text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        except OSError as exc:
            text = f"[Could not read: {exc}]"
        result[label] = {"path": str(path), "exists": path.exists(), "characters": len(text), "content": text[-max_chars:]}
    return result
