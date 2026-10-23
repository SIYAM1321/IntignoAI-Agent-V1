# Test Report — IntignoAI Agent v1.10.0

## Automated checks

| Check | Result |
|---|---|
| `python -m compileall -q .` | PASS |
| `node --check static/app.js` | PASS |
| `python -m pytest -q` | PASS — **89 passed, 8 skipped, 9 warnings** |
| NVIDIA adapter streamed text + request reasoning-effort mock | PASS |
| NVIDIA adapter native tool-call parsing mock | PASS |
| Embedded manual sign-in handoff tests | PASS — headless mode and no physical-window helper; session reuse |
| Live local Chromium headless CDP/browser-stream smoke | PASS — browser started headless, BrowserStreamManager reached `capturing`, then stopped cleanly |
| Secret scan for the user's pasted NVIDIA key | PASS — the supplied key is not present in source/release files |

## Skips and warnings

Eight tests are skipped by their existing optional-dependency/platform guards. The test suite emitted nine Pillow deprecation warnings for `Image.Image.getdata`; they did not affect results.

The browser-stream smoke test used the locally installed Chromium binary and an `about:blank` page only. No real Google, LinkedIn, YouTube, or other external account login, live NVIDIA/Gemini API request, job application, social post, or external-site scrape was performed for this release.

## Validation scope

The tests validate local provider-adapter parsing and the existing automation/tool contracts. They do not guarantee that remote providers accept a model request or headless login, that every website's CAPTCHA/MFA permits automation, or that installing every language runtime will be possible in every OS/network environment.
