# IntignoAI Agent v1.12.0

- Chat shows response -> logs -> response -> logs -> final response (text is saved per step, not only at the end).
- Claude-style composer with an advanced model selector and reasoning-effort control.
- Embedded browser only: headless is enforced, no OS browser window is opened, ever.
- New `google_login` action: the agent signs in to Google inside the embedded browser. MFA/CAPTCHA/security checks are handed to you in the in-app Browser tab (never bypassed).
- Browser panel: expand view, human-action banner, "Open browser" button.
- Included `.env` is the one supplied by the user (real keys). Keep it private and do not share the zip.
