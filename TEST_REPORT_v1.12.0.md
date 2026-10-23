# Test report v1.12.0

- `pytest`: 103 passed, 3 skipped (sandbox: Python 3.12, Chromium 141).
- New `tests/test_v112_features.py` (9 tests): segment flushing and persisted timestamps, a full fake-model turn
  (text -> tool -> text -> tool -> final) asserting chronological interleaving, embedded-only handoff,
  `google_login` schema/validation/redaction, headless enforcement.
- UI verified in headless Chromium: timeline order `user, response, logs, response, logs, response, logs, final`, collapsible
  step cards, model picker, human-action card/banner; no page errors.
- `google_login` verified against a LOCAL MOCK of Google's two-step form (success, 2-step challenge hand-off, wrong password).
- NOT verified: real Google / LinkedIn sign-in, and the macOS `start.sh` flow. Google may refuse sign-in from a headless
  browser; in that case the agent reports it and nothing is opened outside the app.
