# Test Report — IntignoAI Agent v1.6.5

## Automated checks executed

| Command/check | Result |
| --- | --- |
| `python -m compileall -q .` | PASS — Python source compiled. |
| `python -m pytest -q -rs --disable-warnings` | PASS — **53 passed, 6 skipped**. |
| Optional live Chromium helper test in `tests/test_browser_automation.py` | PASS — runs a real locally installed Chromium-compatible browser against in-memory pages; verified locale-agnostic email/password locating, iframe access, control inspection, and security-block detection. No credentials or external websites were used. |
| `python tests/test_integration_fake_gemini.py` | PASS — fake Gemini tool/function handoff integration completed. |
| `node --check static/app.js` | PASS — JavaScript syntax check (Node available). |
| `python -m py_compile app.py agent/*.py core/*.py tools/*.py` | PASS — all listed Python sources compiled. |
| ZIP integrity (`zipfile.testzip`) | PASS — checked when packaging. |

## Skipped / blocked checks

Six Flask route/HTTP tests are skipped because Flask is not installed in this build runtime. An attempt to install `Flask>=3.1,<4` failed because the runtime could not resolve package-host DNS. `requirements.txt` includes Flask; run `start.bat` or install project requirements on the target machine before app/API tests.

A separate `perform_browser_task` smoke attempt using an ephemeral local HTTP server was blocked by this sandbox's browser/network policy with `net::ERR_BLOCKED_BY_ADMINISTRATOR` for loopback navigation. The in-memory real-Chromium helper test above did pass. Native visible Chrome/Edge CDP startup, manually installing an extension through the GUI, and real external website navigation were not verified on this build host.

## Changes specifically covered

- One persistent `default` profile and multi-tab routing remain covered by existing regression tests.
- Tab switch/focus actions call `bring_to_front` to activate the selected page.
- Locator fallback scans the main document and child frames and applies locale-agnostic email/password field heuristics.
- Inspect output reports form controls and frame text/controls without extracting current input values.
- `networkidle`/`load` timeout can fall back to DOMContentLoaded when the target URL is already reached.
- CAPTCHA/MFA/Google security rejection is surfaced as a manual-human-action request; the agent does not solve or bypass it.
- Auth batches with supplied email/password can submit a login form without a redundant confirmation; create-account/payment/delete/send/other consequential actions keep their confirmation requirement.
- Previous tests for terminal lifecycle, profile reuse, files, knowledge, limiter, SSE, model handoff and browser tool contracts remain passing.

## Not claimed

No real Google/Gmail or LinkedIn account login was tested. Google may still reject a CDP/debugging session, and no tool can guarantee every account or website will allow automation. CAPTCHA/MFA, provider security checks, closed shadow roots, site-level blocks, login policies and manual verification remain genuine limits. Native visible Chrome/Edge launch and extension-manager usability still need a final check on the user's Windows machine. The code does not bypass security checks or claim universal/100% success.


## v1.6.5 regression checks

| Check | Result |
| --- | --- |
| `python -m py_compile tools/browser_automation.py agent/engine.py agent/prompts.py` | PASS |
| `python -m pytest -q` | PASS — **53 passed, 6 skipped** |
| Escaped file-input CSS normalization | PASS |
| `upload_file` on a hidden input via `set_input_files` and file-state verification | PASS (mocked regression test) |
| Navigation timeout must not mistake an unchanged, different same-origin URL for the requested target | PASS |
| Chat attachment absolute path reaches browser-upload instructions | PASS |

External Google/LinkedIn login, CAPTCHA/MFA, real employer application submission, and successful upload acceptance by every site are not claimed as tested. The tested guarantee is that the backend selects a local file into a matching attached native file input and verifies selection—not that every site's own network validation or upload endpoint will accept it.
