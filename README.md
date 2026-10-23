# IntignoAI Agent v1.13.4

IntignoAI is a local-first AI coding and computer-use agent built with Python, Flask, SQLite, NVIDIA NIM's OpenAI-compatible chat API, and Google's Gemini Interactions API. It runs in your browser at `http://127.0.0.1:2000` while its terminal, filesystem, web, memory, and activity tools run locally on your machine.

> **Machine access warning:** when `FILE_SCOPE=computer`, the agent can read and modify files outside the project and run shell commands as your Windows/macOS/Linux user. Keep the app bound to loopback, do not expose port 2000 to the internet, and review any confirmation prompt for destructive commands. Use a non-admin OS account and `FILE_SCOPE=workspace` for a narrower file boundary.

## What's new in v1.13.4

- **Approval pauses fixed end-to-end.** The `Error: HTTP 500` that appeared when you replied to a
  confirmation (e.g. clicking "Submit application" on LinkedIn) was caused by a missing `import re`
  in `app.py` — every message sent while a confirmation was pending crashed the endpoint. All error
  responses are JSON now, and generic form submissions no longer pause at all.
- **Standing "never ask for confirmation" waivers.** Say it once (English or Banglish) and IntignoAI
  saves the preference permanently (`data/confirmation_waivers.json` + SKILLS.MD + MEMORY.MD) and
  stops pausing for browser actions in that scope. Destructive local tools still confirm.
- **LinkedIn has no CAPTCHA** — the agent no longer reports LinkedIn's invisible reCAPTCHA badge as
  a blocking CAPTCHA, and the built-in LinkedIn flow (search → open each job → full description →
  CSV via `csv_path` → `linkedin_easy_apply`) is documented in SKILLS.MD/MEMORY.MD.
- **Reliable version answers**: `get_python_versions`/`detect_dev_tools` skip Microsoft Store
  execution-alias stubs (WinError 1920) instead of crashing or mis-answering.
- **Provider 5xx retries** for NVIDIA NIM (new) and more retries for Gemini, so a transient model
  API error no longer kills a turn.
- **`check_signin` browser action** verifies an active Google session; after one manual sign-in the
  persistent profile stays signed in. Google's security checks are never bypassed.

## What's new in v1.12.0

- **Chat order**: the conversation now reads response, then logs, then response, then logs, ... then the final response. Tool calls are grouped into collapsible step cards.
- **Advanced model selector** in the composer (model cards + Low/High/Max reasoning).
- **Embedded browser only**: no physical browser window is ever opened while `BROWSER_EMBEDDED_UI=true`.
- **Agent-driven Google sign-in** via the `google_login` action, inside the embedded browser. Give the agent the email and password in your message; if Google asks for 2-step verification or a security check, complete it in the in-app Browser tab and say "continue". Google may still refuse sign-in from headless browsers; this cannot be guaranteed.

## What's new in v1.9.0

- **Live embedded browser inside the UI (no OS window).** The **Browser** tab streams the real Playwright/Chromium browser (headless, CDP screencast). Click/scroll/type in it, navigate via the URL bar, open tabs — the same pages the agent drives. `POST /api/browser/input` + `GET /api/browser/stream/feed` power it; `POST /api/browser-action` and `cli.py` keep any language in control. Google/provider sign-in remains a manual user step in that embedded tab; a provider may reject headless login.
- **LinkedIn, live-validated:** `linkedin_login` (2026 auth DOM), `linkedin_create_post` (new inline TipTap composer — post published in the live test), `linkedin_scrape_jobs` (keywords/location/hours/easy_apply_only filters, full descriptions; live test: 7 real "AI Engineer · New York · last 24 h" jobs → `data/jobs_ai_engineer_ny_24h.csv`), `linkedin_easy_apply` (resume, every question group, contact fields, submit-with-authorization; honestly reports the new "Easy Apply via external ATS" handoffs).
- **YouTube engagement:** `youtube_like` / `youtube_subscribe` / `youtube_comment` with signed-out detection.
- **Image-to-data-entry:** `tools/data_entry.py` + `fill_form` for Microworkers-style tasks (field/page modes, charset hints, NA policy, validation before submit); `tools/job_csv.py` for clean CSV exports.
- Selector/workflow reference from the collected HTML templates: `docs/v19_selectors.md`, `data/v19_selectors.json`.
- Full details: `CHANGELOG.md`, `TEST_REPORT_v1.9.md` (92 tests passing; 4 pre-existing baseline failures unrelated to v1.9).

## What's new in v1.8.0

- **Infinite-loading loop fixed (Manual + Agent mode).** Navigation no longer aborts or restarts on slow sites. `goto` gets a generous budget (`BROWSER_NAV_TIMEOUT_MS`, default 45s), returns a structured `navigation` info object instead of raising, waits passively for late commits, settles briefly for streaming subresources (no more "half-loaded page" reads), and **suppresses redundant re-`goto`s of the same URL** — re-issuing goto while a page is loading is what made pages spin forever in agent mode. Cloudflare-style interstitials are watched for up to ~25s to self-clear (one reload max) before the agent hands the tab to you.
- **CAPTCHA handling, layered.** 1) managed challenges usually self-clear (auto-wait built into every navigation); 2) `solve_captcha` clicks visible "I'm not a robot" checkboxes; 3) if you set `CAPTCHA_SOLVER_PROVIDER=2captcha` and `CAPTCHA_SOLVER_API_KEY` in `.env` (your own paid account), reCAPTCHA v2 / hCaptcha / Turnstile are solved automatically and the answer is injected; 4) otherwise the agent pauses and asks you to finish manually. MFA/2FA and Google sign-in always stay manual.
- **LinkedIn Yes/No fixed.** New `answer_radiogroups` action answers **EVERY** Yes/No/multiple-choice question group on the page (main document + iframes) in one call, verifies each click (`checked=true`), falls back to real-mouse coordinates on the visible label, and lists anything `unanswered` instead of silently skipping it. v1.7.2 only clicked the first pair.
- **YouTube thumbnail → exact video.** `get_images` inventories every image including cross-origin iframes (src, alt, anchor, lazy attrs) and auto-detects YouTube thumbnails/surrounding links. `youtube_find_by_thumbnail` downloads the thumbnail, searches YouTube, ranks candidates by perceptual hash (dHash/aHash, Pillow) and opens + plays the best match in a NEW tab (original tab untouched). Works from any site — including thumbnails hosted on the site itself (e.g. `workupjob.com/image_upload/...`) when you let the model look at the thumbnail first.
- **Full media/HTML capture.** `get_html` returns raw HTML (up to 600k chars/frame, optional `all_frames`), `scroll_until_stable` lazy-loads infinite feeds before scraping, and scraping already covers all frames.
- **Files & tabs.** `upload_file` now also drives the **native file chooser** for custom upload buttons (`trigger_selector`), on top of hidden `input[type=file]` support; `switch_tab` with `tab_id: "previous"` jumps back to the previous tab; bare `press` sends keyboard keys without a selector.
- **Any language can drive the agent.** Same JSON API at `POST /api/browser-action`, plus a new CLI: `python cli.py browser --data '{...}'` / `--stdin` (exit code 0/1). See `cli.py`.
- **Stealth hardening.** The Playwright fallback launches without `--enable-automation` / `--disable-blink-features` leaks and installs environment-consistency patches (navigator.webdriver, languages, plugins, WebGL vendor), so far fewer sites wall the agent behind CAPTCHAs. Native Chrome CDP mode (your visible Chrome with your logins) remains the default for visible sessions.
- **New knobs in `.env`:** `BROWSER_NAV_TIMEOUT_MS` (45s), `BROWSER_CHALLENGE_WAIT_SECONDS` (25s), `CAPTCHA_SOLVER_PROVIDER`, `CAPTCHA_SOLVER_API_KEY`.
- Validation: full pytest suite green (73 passed; 4 manual-signin tests fail identically on the pristine v1.7.2 baseline on machines where the fake browser executable cannot spawn — pre-existing, unrelated), Python 3.9/3.11 compile-checked, and a live headless smoke test confirmed example.com + the exact workupjob.com job URL now navigate and scrape without any hang.

## Existing v1.5 reliability features (preserved in v1.6)

- **Hard API request cap:** local requests are limited to a maximum of 10 per rolling 60-second window. The cap is stored in SQLite and shared across restarts/processes that use this project. Settings cannot raise it above 10.
- **Provider-aware cooldowns:** `429` responses parse `Retry-After` or Gemini's `retryDelay`; the cooldown survives an app restart. Transient network/5xx errors use bounded exponential backoff with jitter.
- **Tool handoff recovery:** function names and call IDs are kept intact. If Gemini rejects a function-result handoff with the “function response turn comes immediately after a function call turn” protocol error, the already-collected tool outputs are reused in a fresh recovery prompt rather than rerunning the commands.
- **Turn recovery:** checkpoints and partial assistant output are persisted. Use **Resume turn** after retryable interruptions; dismiss a checkpoint if you no longer want to continue it. To avoid accidentally repeating side effects, automatic resume is not offered for a crash during an uncertain tool-execution phase.
- **Conversation continuity:** the latest Gemini interaction ID is chained into the next user turn.
- **Append-only knowledge:** `MEMORY.MD` and `SKILLS.MD` are loaded into the system prompt. New entries are appended with dedupe markers; previous entries are not overwritten. Common API-key/private-key patterns are rejected.
- **URL fetch + search fallback:** fetches public HTTP(S) pages, extracts readable text and handles redirects/charset; private/local network destinations are blocked. If a page cannot be retrieved, the tool can fall back to DuckDuckGo search results.
- **Multiple web searches:** use `web_search` for one query or `web_search_many` for a list of queries in one tool call; queries are processed sequentially for predictable request load.
- **Machine diagnostics:** reports OS, CPU, RAM/disk where supported, Python and installed developer tools with cross-platform fallbacks.
- **Attachments:** up to 8 files per message, 10 MB each, 30 MB total. Supports code/text files across common programming languages, images, PDFs, and supported audio/video inputs. The model can only interpret formats supported by the configured Gemini API; binary formats that are not supported are reported/referenced instead of magically converted.
- **Settings panel:** configure the Google API key, model, workspace, retry count, and request cap without sending the full saved key back to the browser.
- **Knowledge endpoints, cancel/resume controls, improved activity timelines, and local request protections** for loopback host/Origin checks.

The project preserves persistent chat history, chat search/rename/pin/delete, SQLite activity logs, terminal process management, streamed terminal output, syntax-highlighted code, Markdown, and responsive desktop/mobile layouts. The persistent Playwright browser now uses a **single default profile** across every website and task; multi-tab workflows use multiple tabs instead of separate profiles. It supports semantic fallback locators, iframe-targeted inputs, visible sign-in flows, tab selection/management, browser navigation/history, screenshots, page inspection, file upload/download and smoke tests.

## Requirements

- Python **3.9 or later**
- An **NVIDIA NIM API key** for the default GLM-5.3-Flash model: <https://build.nvidia.com/> (configure in Settings).
- A Google Gemini API key only if you select Gemini 3.5 Flash Lite: <https://aistudio.google.com/app/apikey>
- Internet access for the selected model provider and web search/fetch tools

Dependencies are in `requirements.txt` (`Flask`, `python-dotenv`, `psutil`, `playwright`). On Python 3.9, `greenlet==3.2.4` is pinned because that release provides Windows wheels; greenlet 3.2.5 does not publish Windows wheels and may otherwise attempt a source build that requires Microsoft Visual C++ Build Tools. The startup scripts install dependencies and the Playwright Chromium binary on first launch. No Node.js frontend build is required.

## Start on Windows 10/11

1. Extract the ZIP to a folder you can write to.
2. Double-click `start.bat`, or run these commands in PowerShell:

   ```powershell
   py -3 -m venv myenv
   .\myenv\Scripts\python.exe -m pip install -r requirements.txt
   .\myenv\Scripts\python.exe app.py
   ```
3. Open <http://127.0.0.1:2000>.
4. Open **Settings** (gear icon), paste your NVIDIA NIM API key for the default GLM-5.3-Flash model, and save. To use Gemini 3.5 Flash Lite, also add a Google API key.

The ZIP ships `.env.example`, not a real `.env` and not a live personal key. Copy it to `.env`, then configure `NVIDIA_API_KEY` and/or `GOOGLE_API_KEY`. Keep `.env` private and never upload it with populated keys to a public repository.

## Start on macOS / Linux

```bash
chmod +x start.sh
./start.sh
```

Or install and run manually:

```bash
python3 -m venv myenv
./myenv/bin/python -m pip install -r requirements.txt
./myenv/bin/python app.py
```

On all platforms the server defaults to `127.0.0.1:2000`. `HOST` and `PORT` may be configured, but do not expose this local machine-control service to an untrusted network. Local API routes intentionally reject non-loopback hosts.

## Rate-limit behavior and troubleshooting

The app enforces **at most 10 Gemini HTTP requests in any rolling 60-second period from this IntignoAI installation**. It cannot stop other applications that independently use the same API key/project, and Google can apply tighter limits by model, tier, tokens, daily usage, or service state. A `429` from Google triggers a persisted cooldown even if the app's own counter has remaining capacity.

A user message is not guaranteed to equal one API request. A simple text turn often uses one request, but a turn that calls tools may need a model request, a tool-result handoff, and additional model requests. These all count toward the rolling cap. The activity panel distinguishes model and tool events; the status indicator reports remaining local request capacity in the current window, not Google account quota.

Useful checks:

- Check the Settings dialog shows the intended model and that the key is configured (the UI only shows whether it is configured and its last four characters).
- Inspect the Activity panel for provider `429`, a local safety cap, network/timeout errors, or protocol handoff errors.
- Wait for the displayed cooldown; repeated manual clicks cannot bypass the persistent limiter.
- Check Google's current model/tier quota in AI Studio: <https://ai.google.dev/gemini-api/docs/rate-limits>.
- If a turn fails, use **Resume turn** when offered. If recovery is no longer needed, dismiss it.

## Configuration

The app creates/uses `.env` in the project root. Main settings:

```dotenv
GOOGLE_API_KEY=
GEMINI_MODEL=gemini-3.5-flash-lite
GEMINI_API_URL=https://generativelanguage.googleapis.com/v1beta/interactions?alt=sse
GEMINI_MAX_REQUESTS_PER_MINUTE=10
GEMINI_MAX_RETRIES=1
MAX_TOOL_STEPS=50
MAX_TOOL_RESULT_CHARS=30000
TERMINAL_TIMEOUT_SECONDS=600
TERMINAL_INITIAL_WAIT_SECONDS=2
TERMINAL_SHELL=auto
WORKSPACE=~/Desktop/AI-Agent-Workspace
FILE_SCOPE=computer
HOST=127.0.0.1
PORT=2000
```

The server-side limiter clamps settings to `1..10`; even a stale or manually edited `.env` value cannot make the request cap exceed 10. The database stores rate-limit state in `data/rate_limit.sqlite3`, and chat/activity/recovery data in `data/intignoai.db` by default. Do not delete these databases while the application is running. An optional `INTIGNOAI_DB_PATH` can relocate the chat database.

- `FILE_SCOPE=computer`: file tools may address absolute paths anywhere the current user can access.
- `FILE_SCOPE=workspace`: file tools restrict their supported operations to the configured workspace.
- `WORKSPACE`: defaults to `~/Desktop/AI-Agent-Workspace`; change it in Settings.
- `GEMINI_MAX_RETRIES`: maximum retry count after the first request, bounded to `0..2`.

## Tools and memory

Available tools include file read/list/create/update/delete/move/copy/rename operations, terminal execution and long-running process controls, port checks and confirmed exact-port listener termination, system stats, developer-tool detection, single/multiple web search, public URL fetching, and append-only knowledge updates.

`MEMORY.MD` and `SKILLS.MD` are read when building each system prompt. Memory/skill writes append a dated heading and unique content marker; duplicate entries are skipped, and secret-like content is rejected. These safeguards reduce accidental loss/leakage but are not a guarantee that every possible secret format is detected.

## Browser automation (v1.6.5)

`start.bat` (Windows) and `start.sh` (macOS/Linux) check for the Python dependency and automatically run `tools/ensure_playwright.py`. That helper installs Playwright's bundled Chromium browser if it is missing. The browser is stored in Playwright's normal per-user browser cache; project-specific persistent profiles are stored under `data/browser_profiles/`. First installation needs internet access. On some Linux distributions, Chromium may additionally need OS libraries that are not installed by Python's package installer.

Visible browser mode prefers an installed Chrome/Edge process started as a normal interactive browser on the dedicated `data/browser_profiles/default` data directory, then attaches the agent over a localhost-only Chrome DevTools Protocol endpoint. This leaves ordinary manual navigation and Chrome's extension manager available. If no supported installed browser is found, the app falls back to Playwright's bundled Chromium. The app never uses the user's everyday Chrome `User Data` directory as its automation profile. Close a browser window left open by a prior app version once before upgrading if it locks this profile. `BROWSER_HEADLESS=false` is the default. CSS/XPath locators are tried first, then accessible label/role/placeholder fallbacks, locale-agnostic email/password field heuristics and child-frame search. Inspect output now includes visible controls across open frames (without field values), frame URLs/names, labels/ids/placeholders, viewport bounding boxes/centers and coordinate-space hints. This gives the agent grounded click coordinates when selectors are unavailable; coordinates still cannot guarantee success on moving/occluded targets. `frame_selector`, `frame_index`, `frame_url_contains`, and `frame_name` can target a particular frame; `coordinates_relative_to_frame` is supported for viewport mouse coordinates. Use `switch_tab` with `focus_browser`/`bring_to_front` to show a selected tab in the visible browser. Failures are logged to `data/browser_artifacts/browser_automation.log` and, when a page is available, capture `data/browser_artifacts/error.png` plus a timestamped copy. Error responses preserve the actions already completed and current open-tab information for diagnosis.

Examples (send JSON):

```json
{"url":"https://example.com","headless":true,"actions":[{"action":"click","selector":"text=More information"},{"action":"wait","ms":500},{"action":"screenshot","filename":"example.png"}]}
```

```json
{"task":"scrape","url":"https://example.com","profile":"default","headless":true}
```

```json
{"task":"test","url":"http://127.0.0.1:8000","headless":false}
```

**Single-profile policy:** all websites and tasks use `data/browser_profiles/default`. `POST /api/browser-profiles` is retained for API compatibility but returns/reuses this same profile, even if an older client passes a different name. It never creates a second profile. Cookies/local storage persist in the one profile, while tabs can be opened for parallel research and multi-step tasks. Older named profile folders are not deleted; if the default folder does not exist, one existing legacy folder can be promoted to default and other legacy folders remain untouched/inactive.

Supported multi-tab actions include `new_tab`/`open_tab`, `open_tabs` (up to 20 URLs per action), `list_tabs`, `switch_tab`, `focus_browser`/`bring_to_front`, and `close_tab`. Use `tab_id` or `tab_index` to target a particular tab. `navigate` also supports `new_tab: true`. Browser navigation actions include `go_back`, `go_forward`, `reload`, `wait_for_load_state`, and `wait_for_url`; page controls include `fill`, `type`, `click`, `check`, `uncheck`, `select`, `press`, `upload_file`, `download`, screenshots and scraping. Keep the user's normal sign-in flow in the visible profile. A user may need to complete Google prompts, CAPTCHA or MFA/2FA manually; the tool returns `human_action_required` with an instruction to complete the step in the visible browser and then tell the agent to continue. These checks are not bypassed. Google can still reject a session it considers automated; no supported browser driver can guarantee that every account will sign in or that every website allows automation.

Example: open multiple research tabs and then inspect their current state:

```json
{"headless":false,"actions":[{"action":"open_tabs","urls":["https://example.com","https://www.wikipedia.org","https://www.python.org"]},{"action":"list_tabs"}]}
```

The `browser_test` agent tool is a browser-level smoke test, not a substitute for a comprehensive automated test suite. Treat page content as untrusted data. Browser automation can affect logged-in accounts, so the agent should only perform requested actions; explicit `submit`/`send`/`delete`/`purchase`/`confirm` actions—and click targets that look like consequential submit/pay buttons—require confirmation. Keep this machine-control app bound to loopback and do not expose it publicly.

## Manual sign-in and shared browser control (v1.7.1+; v1.7.2 shows the browser-opened message as an info notice, not an Error)

**Problem fixed:** Google showed *"Couldn't sign you in – This browser or app may not be secure"*. Google refuses sign-in in browsers that expose automation, which includes a Chrome started with a remote-debugging port even if you type manually. v1.7.0 still opened the manual window with that port; v1.7.1 does not.

**How it works**

- Click the 🌐 button in the top bar (or `POST /api/browser/open`). IntignoAI first quits any agent-attached browser gracefully (profile saved), then opens **a completely plain Chrome/Edge window** on the dedicated profile `data/browser_profiles/default`: **no `--remote-debugging-port`, no automation flags, no `--load-extension`, nothing attached**. Sign in to Google or anything else and create your browser profile/sessions normally. You can visit any website the usual way.
- When finished, tell the agent **"continue"**. On its next browser call it closes the plain window gracefully (so cookies/session are flushed), reopens the same profile with the local agent endpoint, and attaches. Your signed-in sessions remain. After that you and the agent share the same visible window and tabs.
- **Google is never automated.** If the agent is asked to open `accounts.google.com`, or a Google sign-in/rejection page appears during a task, IntignoAI closes the attached browser gracefully, opens a fresh plain Google sign-in window, and returns `human_action_required: manual_signin` (`browser_released_to_user: true`).
- Tools: `browser_manual_signin` (optional `url`), `browser_release_to_user`; also `browser_action` actions `manual_signin` / `release_browser` (single action in the call) and tasks `manual_signin` / `release`.
- **Use real Google Chrome or Microsoft Edge.** If only Playwright's bundled Chromium exists, the result carries a warning because Google often refuses Chromium builds.
- If the plain window cannot be closed automatically, the agent reports `close_manual_browser`; close the window yourself and say continue.
- `close_browser` quits the browser process IntignoAI started, gracefully first.

**Honest limits:** Google's checks are unpublished and change. After sign-in, the agent attaches over a debug port; your Google session cookies are already saved, but a site (including Google pages) can still challenge an automated session. Complete CAPTCHA/2-step prompts yourself. No tool can guarantee every site.

## Local HTTP endpoints

The app serves its UI and the following local JSON endpoints:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/status` | App/model state, limiter snapshot, workspace, system/tool status, browser status |
| `GET /api/browser-profiles` | List persistent browser profiles and extension counts |
| `POST /api/browser-profiles` | Compatibility endpoint; returns/reuses the single `default` profile (`{"name":"default"}`) |
| `POST /api/browser-profiles/<profile>/extensions` | Register an unpacked extension folder (`{"extension_path":"C:/path/to/extension"}`) |
| `POST /api/browser-action` and `POST /browser-action` | Run Playwright actions, scrape a URL, or smoke-test a website |
| `POST /api/browser/open` | Navigate the embedded Browser tab to a page for manual interaction (`{"url":"..."}`) |
| `POST /api/browser/release` | Temporarily yield browser interaction to the user inside the embedded Browser tab |
| `GET /api/chats` | List persisted chats |
| `GET /api/chats/<id>` | Load one chat, messages, and activity |
| `POST /api/session` | Create a chat session |
| `POST /api/chat` | Start an assistant turn |
| `POST /api/upload` | Upload message attachments |
| `POST /api/cancel` | Cancel the active turn |
| `POST /api/resume` | Resume a recoverable turn |
| `DELETE /api/recovery` | Dismiss recovery checkpoint |
| `GET/POST /api/settings` | Read/save non-secret settings and API key securely |
| `GET/POST /api/knowledge` | Read or append memory/skills entries |
| `GET /api/activity/stream` | Server-sent event stream for activity and output |
| `GET /api/terminals` | List known terminal processes |
| `GET/POST /api/workspace` | Read/update the workspace |
| `PATCH /api/chats/<id>` | Rename a chat |
| `POST /api/chats/<id>/pin` | Pin/unpin a chat |
| `DELETE /api/chats/<id>` | Delete a chat and its attachment folder |

These endpoints are intended only for the local UI, not as a public API. Use localhost/127.0.0.1 and do not port-forward the service.

## Tests

Run the project tests with:

```bash
python -m pip install -r requirements.txt
python -m pip install pytest
python -m pytest -q
python tests/test_integration_fake_gemini.py
python -m py_compile app.py agent/*.py core/*.py tools/*.py
node --check static/app.js
```

The automated suite covers the rate limiter, persistent state, append-only knowledge, attachments, URL validation, handoff recovery, core tool behavior, SSE parsing, and fake-Gemini function-call integration. The full HTTP route tests require Flask and Werkzeug installed. No live Gemini call is part of the local test suite; the real API key, account-tier limits, web availability, and browser appearance should be verified on the target machine.

`AGENT_LOG_REVIEW.md` documents recurring agent issues and how they are addressed. `TEST_REPORT_v1.10.0.md` records the exact test results for this release.

## Data and privacy

Chat histories, activity events, rate-limit records, uploaded attachments, and recovery checkpoints are stored locally under `data/`. The app sends user prompts and attached content to the configured Gemini API; URL fetching/search tools contact the requested websites/search provider. Avoid attaching secrets unless necessary. API keys are kept in `.env` and never returned in full by the settings endpoint.


## v1.6.2 updates

- Queue follow-up messages while a chat is running; queued tasks execute one-by-one without cancelling the current task.
- The composer remains editable during execution and uses one icon control that switches between Send and Stop.
- Multiple chats can run independently; users can switch chats or start a new chat while another chat is busy.
- Browser automation reuses a persistent browser session and leaves browser windows open unless an explicit `close_browser` action is requested. The default local browser mode is visible (`BROWSER_HEADLESS=false`); an existing headless session is promoted to a visible one when the user explicitly requests it.
- Active chats can be deleted while running: the current turn is cancelled, the chat is hidden immediately, then data is removed once execution has safely stopped.
- Added drag-to-resize handles for the chat-history and activity panels, a left-side New chat control, hamburger navigation, and a mobile activity drawer.
- Improved Activity Details “Copy all” feedback and clipboard fallback.


### v1.6.5 browser reliability updates

- Navigation defaults to `domcontentloaded`; `load`/`networkidle` timeouts on a tab that has reached the target/redirect URL are recoverable instead of blocking the rest of a workflow. A missed `wait_for_selector` is recorded and the remaining actions can continue to use semantic field fallbacks.
- Before navigation, click, form input, submission or file selection, browser actions capture a local screenshot plus secret-safe form state (`automation_audit`). A `postflight` snapshot follows action batches; snapshots show whether visible required fields are filled without returning their actual values.
- `upload_file` accepts `file_path`, `file_paths`, `paths`, `filename`, or `value`. It uses `set_input_files` on an attached native file input, including a hidden input, without clicking the page's upload button. It returns the selected file names/count to verify browser-level file selection. The site's own upload request/validation must still complete successfully.
- Chat attachments now pass their exact local paths into the agent input so a resume/cover-letter attached to the chat can be selected in a form. The agent asks the user for missing required job-application fields rather than fabricating answers, and verifies the resulting state before reporting completion.
- Google's account security blocks and LinkedIn CAPTCHA/MFA are still manual. The browser automation does not bypass them, and site-specific custom upload widgets may not expose a native file input.


## v1.10.0 model selection and embedded browser
The composer offers **GLM-5.3-Flash (NVIDIA NIM)** and **Gemini 3.5 Flash Lite** per message. GLM is the default. Add `NVIDIA_API_KEY` in Settings or the local `.env`; keep keys out of source code and chat messages. For GLM, the Think checkbox selects maximum versus low reasoning effort; the model's provider currently always enables reasoning, so this is a speed/depth control rather than a true off switch.

Tool actions, status and stdout/stderr previews are shown as expandable entries in the chat timeline. Destructive or externally consequential actions continue to require user approval, but approval is given conversationally by replying `yes, proceed` or `no, cancel`, not via a confirmation button. The embedded browser is headless by default and becomes visible in the Web UI Browser tab; CAPTCHA, MFA and site-side security checks may still require user intervention, and no site login or automation can be guaranteed to succeed on every website.
