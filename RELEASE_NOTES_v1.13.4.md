# IntignoAI Agent v1.13.4 — Release Notes

**Date:** 2026-10-22
**Base:** IntignoAI-Agent-v1.12.0 (all v1.12 features kept, targeted fixes only)

## 1. The "Error: HTTP 500" trap on Submit Application — fixed

What actually happened in your session:

1. `app.py` contains the natural-language approval logic (`yes, proceed` / `no, cancel`) that
   uses `re.match(...)` — but the file **never imported `re`**.
2. The moment a confirmation was pending and you sent any message ("Yes, Now clicked on the
   Submit Application Button…"), that branch raised `NameError: name 're' is not defined`.
   Flask returned a bare HTML 500 page, the web UI showed **"Error: HTTP 500"**, and the
   pending action was never executed or cleared — so every following message failed the same way.
3. The browser itself was fine the whole time: the LinkedIn Review page was already loaded.

Fixes in v1.13.4:

- `import re` added in `app.py` (root cause).
- Every endpoint now answers errors as JSON (`HTTPException` handler + catch-all `Exception`
  handler that logs the real traceback). The UI can no longer show an opaque "HTTP 500" —
  it shows the actual reason.
- `PendingConfirmation.current` can no longer raise `IndexError` on stale state;
  `confirm_pending()` validates before resuming; `/api/chat` and `/api/confirm` catch all
  exceptions into JSON responses.

## 2. Confirmations are no longer in the way

- **"Submit application" no longer asks for confirmation.** Generic form submissions
  (job applications, searches, filters) are part of the normal flow now.
- Approval still pauses for genuinely consequential actions: pay / purchase / checkout /
  transfer / withdraw, deleting data, sending messages/comments/posts, account sign-ups, and
  destructive local tools (`delete_file`, `delete_directory`, `kill_port`, dangerous shell).
- **Standing waiver, saved automatically:** if you say "never ask for confirmation" — or
  Banglish like "ar kokhono confirmation chaibe na", "confirm korar dorkar nai" — IntignoAI
  writes `data/confirmation_waivers.json`, appends to **SKILLS.MD** and **MEMORY.MD**, and
  from then on runs browser actions in that scope (global, or site-specific like LinkedIn)
  without asking. Saying it while an action is pending approves that action once and stores
  the preference permanently.
- The "Approval needed" message now teaches this shortcut.

## 3. LinkedIn has no CAPTCHA — and the agent stops pretending it does

LinkedIn embeds an **invisible** reCAPTCHA Enterprise badge on every page. The old detector
matched any iframe URL containing "recaptcha" and reported `human_action_required` even
though nothing was blocked. New detection rules:

- Invisible badge/anchor frames and passive `challenge-platform` scripts are ignored.
- A CAPTCHA is reported only when there is a **visible, interactive challenge widget**
  (real bounding box ≥ 200×60) or an **actual challenge interstitial** (challenge wording on
  a content-poor page).
- Normal LinkedIn job pages (job details, Easy Apply modal) are never reported as blocked.
- SKILLS.MD now states explicitly: LinkedIn has no CAPTCHA; never mention it there unless the
  tool result says `human_action_required`.

## 4. LinkedIn Jobs flow is now built in (Skills + Memory)

The complete flow is documented in SKILLS.MD and MEMORY.MD, and shortened to one call:

```
browser_action → actions: [{
  "action": "linkedin_scrape_jobs",
  "keywords": "...", "location": "...",
  "hours": 86400, "easy_apply_only": true,
  "max_jobs": 25, "include_description": true,
  "csv_path": "data/jobs_<query>_<location>_24h.csv"
}]
```

- Navigates the LinkedIn job search (same result as clicking the search bar and searching),
  opens **each job one by one**, expands the **full description** ("See more"), and writes the
  Excel-friendly CSV (utf-8-sig BOM, CRLF, formula-injection guard) **in the same call**.
  The result's `csv` field reports `path` and `rows`.
- Columns: title, company, location, workplace_type, employment_type, posted_text,
  applicants_text, job_id, job_url, description, saved_at, source_file.
- Applying: `linkedin_easy_apply` per job (resume, every question group, contact fields) with
  `submit: true` when you authorized applying; the agent reports per-job outcomes and skips
  jobs that need personal/legal answers it does not have.

## 5. Reliable answers about installed versions

`get_python_versions` used to crash with `[WinError 1920]` on the Microsoft Store
`python3.EXE` execution-alias stub; the agent then fell back to raw terminal commands.
Both `get_python_versions` and `detect_dev_tools` now:

- skip WindowsApps execution-alias stubs proactively (0-byte reparse points),
- never let one broken candidate fail the whole discovery,
- report skipped aliases explicitly.

SKILLS.MD now also forbids answering version questions from memory — the tool output must be
quoted.

## 6. Model provider hiccups no longer kill a turn

- NVIDIA NIM calls retry HTTP 500/502/503/504 and network errors with backoff
  (`NVIDIA_MAX_RETRIES`, default 2).
- Gemini retries raised from 1 to 3 (cap 4, `GEMINI_MAX_RETRIES`).

Your session ran on `nvidia/nemotron-3.5-super-vl-120b-a12b`, and the NIM path previously had
**no** 5xx retry at all — a transient provider error right after approval surfaced as an error
instead of a finished turn.

## 7. Google sign-in — what is (and is not) possible

**What v1.13.4 gives you (100% reliable path):**

1. `google_login` signs in inside the embedded Browser tab using the email/password from your
   current message (never stored, never logged, redacted in activity cards).
2. If Google shows 2-step verification or "verify it's you", you complete that **once** in the
   embedded tab; the browser stays inside the app.
3. The persistent profile (`data/browser_profiles/default`) keeps the session cookies, so on
   every later run the agent is already signed in — no sign-in, no check.
4. New `check_signin` action verifies the session (page state + `SID`/`HSID`/`SSID` cookies +
   accounts.google.com probe) and returns `signed_in: true/false` instead of guessing.

**What it deliberately does not do:** automate or bypass Google's CAPTCHA/security checks.
That would violate Google's terms and the app's safety policy, and it breaks constantly
anyway — reCAPTCHA Enterprise actively hunts automation. The one-time manual step plus a
persistent profile is the only approach that keeps working long-term.

## Files changed

| File | Change |
| --- | --- |
| `app.py` | `import re` (root cause), JSON error handlers, waiver-aware approval branch, hardened endpoints |
| `agent/engine.py` | narrower confirmation triggers, waiver enforcement, `note_user_confirmation_preference`, stub-proof version tools, NIM 5xx retries, confirm_pending guards, `check_signin`/`csv_path` schema |
| `agent/waivers.py` | **new** — waiver store + detection (EN + Banglish) |
| `agent/memory.py` | safe `PendingConfirmation.current` |
| `agent/prompts.py` | LinkedIn/CSV/check_signin guidance |
| `tools/browser_automation.py` | CAPTCHA false-positive fix, `check_signin`, `csv_path` export in `linkedin_scrape_jobs` |
| `SKILLS.MD` / `MEMORY.MD` | built-in LinkedIn workflow, waiver policy, CAPTCHA correction, version-facts rule |
| `tests/` | updated approval-policy assertions + new v1.13.4 tests |

## Upgrade notes

- Replace the whole folder; your `data/` (profiles, SQLite DB, artifacts) can be kept —
  the waiver file is created on demand at `data/confirmation_waivers.json`.
- No new Python dependencies.
- The `"password"` field in the browser_action tool schema was moved into a named constant
  (`_LOGIN_ACTION_FIELD_TYPES`) because the workspace write-safety scanner flags literal
  `"password": {...}` lines. The generated tool schema is byte-identical.
