# IntignoAI Agent v1.8.0 — fixes & additions

Release date: 2026-10-20 · Base: v1.7.2

## 1. Infinite loading / half-loaded pages fixed (Manual + Agent mode)

**Symptom.** In agent mode `https://workupjob.com/job/...` (and several other sites) loaded halfway and
then spun forever. The same URL opened fine manually.

**Root causes found in v1.7.2**

1. `_navigate_page` re-raised `TimeoutError` when `goto` had not committed yet. The model saw a tool
   error and re-sent the identical `goto`. Re-issuing `goto` **aborts the in-flight load** and restarts
   it from zero — so the page could never finish. This is the loop you saw.
2. The default navigation timeout was tied to the 15 s action timeout; slow hosts never committed.
3. After `domcontentloaded` there was no short settle window, so the agent read pages while images/CSS
   were still streaming ("half loaded" content).
4. A Cloudflare / managed-challenge interstitial that self-clears was reported as a hard human handoff
   immediately.

**Fix (tools/browser_automation.py)**

- `_navigate_page` now returns `(response, fallback_used, info)` and **never raises on timeout**:
  - dedicated navigation budget `BROWSER_NAV_TIMEOUT_MS` (default 45 s);
  - passive commit waiting (up to 20 s) for slow hosts;
  - best-effort `load` settle (capped 9 s) after `domcontentloaded`;
  - structured `navigation_timeout` result with `retry_recommended: false` and explicit guidance.
- **Re-goto suppression:** the same URL cannot be re-requested within 12 s; the agent waits for the
  existing load instead of restarting it. Every result now carries `navigation: {...}`.
- **Challenge self-clear watcher:** after navigation, detected interstitials are watched for
  `BROWSER_CHALLENGE_WAIT_SECONDS` (default 25 s), one reload max, before `human_action_required`.
- **Stealth hardening:** the Playwright fallback launches with `ignore_default_args=["--enable-automation"]`
  and `--disable-blink-features=AutomationControlled`, plus an init script normalising
  `navigator.webdriver`, languages, plugins, WebGL vendor and permissions — far fewer false-positive
  bot walls. Native Chrome CDP mode (your signed-in visible Chrome) stays the default.

Live verified: `https://workupjob.com/job/4157397658586f556c3857345732306b3464647458513d3d` navigates and
scrapes in seconds (headless smoke test; visible profile keeps your login and shows the job).

## 2. CAPTCHA handling (layered, honest)

- `detect_verification` — reports challenge type/widgets (reCAPTCHA v2, hCaptcha, Turnstile, provider block).
- `wait_verification_clear` — waits for managed challenges to pass.
- `solve_captcha` — clicks the visible "I'm not a robot" checkbox; if `CAPTCHA_SOLVER_PROVIDER=2captcha` +
  `CAPTCHA_SOLVER_API_KEY` are configured in `.env` (your own paid account), it submits the sitekey,
  polls for the answer and injects it (reCAPTCHA v2 / hCaptcha / Turnstile).
- Without a configured service the agent pauses and asks you to finish manually — it never fabricates success.
- MFA/2FA and Google sign-in remain human-only.

## 3. LinkedIn Yes/No — now every question, not just the first

New action `answer_radiogroups` (`answer_yes_no`, `answer_questions`):

- collects every radio/checkbox **group** on the page and in iframes, with question text (legend,
  aria-labelledby, nearest heading), option labels, visibility and click geometry;
- matches each group against `answers: [{question, answer}]` (fuzzy, token overlap) and resolves
  `Yes`/`No`/labels/values;
- clicks by JS, verifies `checked=true`, and falls back to **real-mouse coordinates on the visible label**
  when JS clicks are suppressed; supports nameless radio groups via global index;
- returns per-question `verified` flags plus an `unanswered` list so nothing is silently skipped.

## 4. YouTube thumbnail → exact video, from any site

- `get_images` inventories all images **including cross-origin iframes** (src/currentSrc/data-src/srcset,
  alt, title, anchor href, natural size, rect, lazy flags) and returns `youtube_videos` when a thumbnail
  URL or its surrounding link already identifies the video.
- `youtube_find_by_thumbnail` (`play_thumbnail_video`): downloads the page thumbnail (or a local file),
  searches YouTube with the `query` the model derives from *looking at* the thumbnail, fetches each
  candidate's `hqdefault` image, ranks them by perceptual hashing (dHash + center-crop dHash + aHash,
  Pillow), opens the best match in a **new tab** and plays it (JS play with a real-mouse click fallback for
  autoplay policies). The original tab is untouched.
- Direct `i.ytimg.com/vi/<id>/...` thumbnails short-circuit to the exact video id.

## 5. More page power

- `scroll_until_stable` (`lazy_load`) — human-like scrolling until height stabilises, for infinite feeds
  and lazy images; run it before scraping long lists.
- `get_html` (`page_html`) — raw HTML up to 600k chars per frame, `all_frames: true` for every iframe.
- `keyboard_press` / bare `press` — real key presses without a selector.
- `switch_tab` with `tab_id: "previous"` — jump back to the previous tab (the agent remembers it).
- `upload_file` with `trigger_selector` — drives the **native file chooser** for custom upload buttons
  (in addition to hidden `input[type=file]` support).
- Scraping already reads all frames; images/HTML now do too.

## 6. Any-language access

- `POST /api/browser-action` accepts the same JSON payload as the agent.
- New `cli.py`: `python cli.py browser --data '{...}'`, `--stdin`, or `--url/--action` shorthand;
  exit code 0/1. Works from Python, Node, C#, Java, PowerShell — anything that can run a process or HTTP.

## 7. Tests

- `tests/test_v18_features.py` — 10 new tests (YouTube id shapes, pHash distance, answer matching,
  option resolution incl. disabled/named-less radios, navigation timeout contracts, re-goto suppression,
  lazy-scroll termination, previous-tab selection).
- Suite: **73 passed**. The 4 failing `test_manual_signin.py` cases fail identically on the pristine
  v1.7.2 baseline on this machine (fake browser executable → `WinError 193`) and are unrelated to this
  release. Python 3.9 and 3.11 compile-checked; live headless smoke test on example.com and the exact
  workupjob.com job URL passed.
