# Changelog

## v1.13.4 (2026-10-22)

### Approval pause fixed end-to-end (the "Error: HTTP 500" on Submit Application)
- Root cause: `app.py` used `re.match` in the natural-language approval branch but never
  imported `re`. The first message sent while a confirmation was pending raised `NameError`
  → Flask returned a bare HTML 500, which the UI rendered as "Error: HTTP 500", and the
  pending action could never be resolved. `import re` added; all API endpoints now answer
  errors as JSON (`HTTPException` + catch-all handlers), so the UI always shows the real
  reason instead of an opaque HTTP code.
- Generic browser form submissions ("Submit application", "submit", "confirm") no longer
  request approval: they are the normal flow. Approval gates remain for pay/purchase/
  transfer/delete/send-message/sign-up style actions and for destructive local tools.
- Pending-confirmation state hardening: `PendingConfirmation.current` can no longer raise
  IndexError (stale state now clears with a friendly message), `confirm_pending` validates
  the pending call before spawning the resume thread, and resume/confirm paths catch all
  exceptions into JSON responses.

### Standing "never ask for confirmation" waivers (Skills + Memory auto-update)
- New `agent/waivers.py`: when the user says "never ask for confirmation" (or Banglish
  equivalents like "ar kokhono confirmation chaibe na", "confirm korar dorkar nai"), the
  engine saves a permanent waiver to `data/confirmation_waivers.json` AND appends matching
  entries to SKILLS.MD and MEMORY.MD automatically. Scopes: global or site-specific
  (linkedin, google, facebook, ...).
- `needs_confirmation` honors waivers before anything else; waivers never bypass
  delete_file/delete_directory/kill_port/extension installs/dangerous terminal commands.
- Saying "never ask again" while an action is pending approves that action once and stores
  the preference. The pause message now teaches this option.

### CAPTCHA false positives fixed (LinkedIn)
- `_browser_is_human_verification` no longer treats LinkedIn's invisible reCAPTCHA Enterprise
  badge / passive challenge-platform frames as a blocking CAPTCHA. Detection now requires a
  visible, interactive challenge widget (real bounding box) or an actual challenge
  interstitial (challenge wording on a content-poor page). Normal LinkedIn job pages are
  never reported as CAPTCHA-blocked anymore; the agent stops mentioning CAPTCHA on LinkedIn.

### Model provider 5xx resilience
- NVIDIA NIM requests now retry HTTP 500/502/503/504 and network errors with backoff
  (`NVIDIA_MAX_RETRIES`, default 2); Gemini retries default raised 1 → 3 (cap 4,
  `GEMINI_MAX_RETRIES`). A transient provider hiccup no longer kills the turn right after
  an approved action.

### Version detection fixed (python / node / java / toolchain questions)
- `get_python_versions` crashed with `[WinError 1920]` on the Microsoft Store
  `python3.EXE` execution-alias stub. Both `get_python_versions` and `detect_dev_tools` now
  skip WindowsApps stubs proactively, never let one broken candidate fail the whole tool,
  and report skipped aliases explicitly — no more wrong first answers followed by manual
  terminal fallbacks.

### LinkedIn jobs → CSV in one call + Google sign-in verification
- `linkedin_scrape_jobs` accepts `csv_path`: the same call that searches and scrapes every
  job (with full descriptions) now writes the Excel-friendly CSV (BOM, CRLF, formula guard)
  and returns a `csv` report with path/rows.
- New `check_signin` browser action verifies an active Google session (page state + profile
  session cookies + probe) so the agent confirms "already signed in" instead of guessing.
  Google's security checks are still never bypassed: the supported path is one manual
  sign-in inside the embedded Browser tab, after which the persistent profile stays signed
  in (see RELEASE_NOTES_v1.13.4.md).

## v1.12.0 (2026-10-21)

### Chat timeline: response -> logs -> response -> logs -> final response
- The engine used to collect all streamed text for a turn and save it as ONE message at the very end,
  so every tool log appeared above the answer. Text spoken before each tool call is now saved as its
  own assistant message at that moment (`_flush_segment`), with the time the text started streaming.
- The chat merges messages and tool activity by timestamp, so it reads: response, logs, response,
  logs, ... final response. Consecutive tool calls are grouped into one collapsible "Ran N steps" card;
  each step expands to show its input, output and error. Open/closed state survives live updates.
- A "Thinking..." indicator shows while the model works and no text/tool is active.
- Rendering is now keyed per message, so streaming no longer re-renders (and flickers) the whole chat.

### Model selector
- New Claude-style picker in the composer: model cards (provider, description, capability badges), a
  check on the selected model, keyboard navigation (arrows, Esc) and a Low / High / Max reasoning
  control that only appears for models that support it. Choices persist in localStorage.
- Composer redesigned as a single rounded card (attach, embedded-browser badge, model picker, send/stop).

### Embedded browser only
- `BROWSER_EMBEDDED_UI=true` now forces headless for every call; a per-call `headless=false` is ignored,
  and any older visible session is reopened headless. The automatic "hand over to the user" path no
  longer opens a physical Chrome window; the sign-in page stays in the in-app Browser tab.
- New `google_login` browser action: the agent signs in to Google itself, inside the embedded browser,
  using the email/password from the user's CURRENT message (typed only into Google's form; redacted in
  logs/activity; never stored). 2-step verification, CAPTCHA and security checks are NOT bypassed:
  they return `requires_human`, an in-chat card and a banner ask the user to finish that step in the
  embedded Browser tab (clicking/typing happens inside the Web UI), then say "continue".
- Browser panel: wider default width, Expand button (large in-app overlay), human-action banner,
  "Open browser" button on the chat card, cleaner toolbar and empty state.

### Other
- Fixed a stale test that hard-coded the app version; added `tests/test_v112_features.py`.

## v1.9.0 (2026-10-20)

### Embedded browser (critical feature)
- The real Playwright-controlled browser now lives INSIDE the Agent UI. Normal automation runs
  the installed Chrome headless WITH its local debug endpoint; a CDP screencast (JPEG frames,
  `Page.startScreencast`) is streamed into a new **Browser** tab beside Activity Logs
  (`GET /api/browser/stream/feed`, SSE).
- Manual interaction in the Browser tab: click / double-click / scroll directly on the mirrored
  view, URL bar (Enter = navigate), New tab, text box + Enter to type, all forwarded through
  `POST /api/browser/input` and executed on the stream's own CDP session (thread-safe).
- Stream controls: Start/Stop stream, "Sync active tab" follows whichever tab the agent uses;
  status shows state, fps, viewers, current URL/title. Tab switching never tears down the
  session. No OS browser window appears during normal automation.
- Exception (documented): Google sign-in still opens a plain OS window because Google rejects
  headless/automated sign-in; everything else is embedded.

### LinkedIn automation (live-validated with a real account)
- `linkedin_login` — 2026 auth DOM (hidden prerendered field copies, TipTap-era markup),
  visible-copy selection, locale-independent submit, checkpoint detection.
- `linkedin_create_post` — new inline TipTap composer (Start-a-post trigger + ProseMirror editor
  + Post button). **Live test: post published to the real feed.**
- `linkedin_scrape_jobs` — keywords/location/hours(=86400 for last 24 h)/easy_apply_only filters,
  card extraction, per-job full descriptions, apply-button labels, applicant counts.
  **Live test: 7 real "AI Engineer, New York, last 24 h, Easy Apply" jobs scraped → `data/jobs_ai_engineer_ny_24h.csv`.**
- `linkedin_easy_apply` — full Easy Apply driver: resume upload (file or preselected profile
  resume), every question group with verified clicks, contact fields, step navigation, validation
  errors, submit only with explicit authorization; honest per-job outcomes incl. the new
  LinkedIn "Easy Apply via external ATS" handoff (`ats_external`, e.g. Lever) which the agent
  reports instead of submitting off-platform.

### YouTube & social media
- `youtube_like` / `youtube_subscribe` / `youtube_comment` with signed-out detection
  (`requires_login`); comment flow opens the inline editor, types and submits.
- Posting/management workflows for any site ride the generic browser engine (fill_form,
  click/type/select/upload, keyboard, coordinates fallback).

### Image-to-data-entry (Microworkers-style)
- `tools/data_entry.py`: task-page + crowdCigar widget parser (field/page modes, unquoted
  attributes), charset hints, value normalisation, confusable/case/numeric comparison,
  NA handling, submission validation and image extraction — wrong or unreadable data is
  reported, never silently submitted. `fill_form` fills many fields with per-field verification.
- `tools/job_csv.py`: saved-page + live-scrape job parsing and Excel-friendly CSV export
  (BOM, CRLF, formula-injection guard).

### Platform
- New selector/workflow reference: `docs/v19_selectors.md` + `data/v19_selectors.json`
  (built from the collected HTML templates and live checks).
- Engine schema + system prompts updated for every new action, credential hygiene
  (credentials only from the current user message, never stored/logged) and the embedded
  browser rules.
- Version 1.9.0; `.env` gains `BROWSER_EMBEDDED_UI`, `BROWSER_NAV_TIMEOUT_MS`,
  `BROWSER_CHALLENGE_WAIT_SECONDS`, `CAPTCHA_SOLVER_*`.
- Any-language access: `POST /api/browser-action` + `cli.py`.

### Tests
- 92 tests passing (19 new v1.9 unit tests incl. real-fixture probes). The 4 failing
  manual-signin tests fail identically on the pristine v1.7.2 baseline on this machine
  (fake browser executable cannot spawn) — pre-existing, unrelated.
- Live embedded-stream smoke test: frames streamed, navigate + click inputs accepted,
  status tracking correct.
- Live LinkedIn hands-on test (real account, headless embedded browser): login ✓, post ✓,
  search+scrape+CSV ✓, Easy Apply attempts executed with honest per-job outcomes
  (see TEST_REPORT_v1.9.md).

## v1.8.0 (2026-10-20)
- Infinite-loading loop fixed (structured navigation, re-goto suppression, challenge self-clear
  watch), layered CAPTCHA handling, LinkedIn all-questions answering, YouTube thumbnail →
  exact video matcher, iframe-wide image/HTML capture, native file-chooser uploads,
  previous-tab switching, CLI, stealth hardening.


## v1.10.0 — GLM provider, chat-native logs and embedded browser
- Added NVIDIA NIM OpenAI-compatible streaming adapter for `z-ai/glm-5.3-flash`, including native function/tool calling and streamed user-facing text.
- GLM is the default model; Gemini 3.5 Flash Lite remains selectable per message in the composer.
- Added a GLM reasoning-effort toggle. GLM-5.3-Flash always reasons at provider level, so unchecked mode sends the documented low reasoning budget instead of claiming reasoning is truly disabled.
- Added separate NVIDIA API-key settings, keeping credentials in local `.env` and out of browser responses.
- Moved execution details into an expandable live timeline in chat; legacy approval buttons are replaced with conversational “yes, proceed” / “no, cancel” replies.
- Automatically activates the embedded Browser tab when agent browser tools start; retries the stream startup race, defaults automation to headless, and disables physical browser auto-launch on app startup. Fixed stale-session detection to use supported Playwright APIs.
- Clipboard-pasted images in the composer are uploaded as attachments. Activity/tool inputs and outputs stream into an expandable timeline in the chat with no per-entry output clipping.
