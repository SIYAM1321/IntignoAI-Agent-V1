# IntignoAI Agent v1.9.0 — live test report

Date: 2026-10-20 · Environment: Windows, installed Chrome (headless, embedded mode), Python 3.11 venv

## Automated tests
- `python -m pytest tests -q` → **92 passed, 4 failed**.
- The 4 failures (`tests/test_manual_signin.py`) fail **identically on the pristine v1.7.2
  baseline** on this machine (`WinError 193` when the test's fake browser executable is
  spawned) — pre-existing environment issue, unrelated to v1.9 changes.
- New in v1.9: `tests/test_v19_units.py` (19 tests) covers job CSV export (BOM/CRLF/formula
  guard), image-to-data-entry parsing/validation, and runs probe tests against the real
  collected fixture HTML files when present.
- Python 3.9 + 3.11 compile checks pass for all modules.

## Embedded browser stream (live)
- App started on 127.0.0.1:2001; `POST /api/browser/stream/start` launched the real installed
  Chrome headless (with debug endpoint) and attached the mirror: state `starting → capturing`.
- `GET /api/browser/stream/feed` delivered 3 JPEG frames + 5 status events in 10 s for the
  static example.com page (frames update on repaint, as designed).
- `POST /api/browser/input` navigate + click accepted; status tracked the page
  (`title: "Example Domain", url: https://example.com/`).

## LinkedIn hands-on test (real account, as requested)
Credential handling: supplied once via environment variable to the test harness; used only
inside LinkedIn's login form; never written to files, logs or the CSV.

| Phase | Result |
| --- | --- |
| Login | ✅ Logged in; landed on `linkedin.com/feed/` (2026 auth DOM handled: hidden prerendered field copies, visible-copy selection, Enter submit) |
| Create post | ✅ Post published to the real feed (new inline TipTap composer handled; legacy dialog fallback kept) |
| Job search | ✅ `AI Engineer` · `New York, New York, United States` · last 24 h (`f_TPR=r86400`) · Easy Apply only (`f_AL=true`) |
| Scrape | ✅ 7 real jobs with full descriptions, apply-button labels, IDs and canonical URLs |
| CSV | ✅ `data/jobs_ai_engineer_ny_24h.csv` (12 columns, UTF-8-BOM, Excel-safe) |
| Easy Apply | ⚠️ Executed on 3 jobs with per-job honest outcomes: the current LinkedIn flow for these listings hands off to an **external ATS** (URL gains `applicantTrackingSystemName=Lever`) instead of opening the classic dialog — the agent reports `ats_external` and does **not** submit on third-party sites. One job's new apply surface did not open a dialog and was reported with diagnostics (`dialog_not_open`). No fabricated applications. |

### Honest limitations (also documented in README)
1. LinkedIn's "Easy Apply" label increasingly routes to external ATS pages (Lever/Greenhouse…).
   The agent fills LinkedIn-native dialogs automatically; for ATS handoffs it stops and reports
   (the user can finish on the ATS page — it is a third-party site with its own rules).
2. Application questions that require personal/legal data the user did not supply are listed in
   `unanswered`/`missing` instead of being invented.
3. YouTube like/subscribe/comment require a signed-in Google account; when signed out the
   actions report `requires_login`. (Google sign-in itself stays manual by Google's policy.)
4. CAPTCHAs/MFA: layered handling (self-clear watch → checkbox click → optional user-configured
   2captcha service → human handoff). Never fabricated as solved.
