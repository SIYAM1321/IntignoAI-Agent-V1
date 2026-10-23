"""Pytest bootstrap: make the project root importable (agent.*, tools.*, core.*).

Keeps `python -m pytest tests` working from any working directory and on isolated
Python builds that ignore PYTHONPATH (vendored/embedded interpreters).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Tests must never read or write the user's real standing-waiver store; point the waiver
# module at a fresh per-session location before any test module imports agent.engine.
os.environ.setdefault("INTIGNOAI_WAIVERS_PATH", str(Path(tempfile.mkdtemp(prefix="intignoai-test-waivers-")) / "confirmation_waivers.json"))
