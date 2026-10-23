# IntignoAI Agent v1.11.0

- Chronological interleaving of persisted assistant messages and execution events in the chat timeline; live text continues to stream during execution.
- Model-adjacent reasoning selector: Low, High, Max (NVIDIA GLM-5.3-Flash always reasons; "Off"/"Medium" are not supported by this model's published endpoint).
- Cleaner embedded browser: removed manual Sync button and the separate type-and-submit input; click a page field then type using the focused browser view. Escape shifts focus to composer.
- Browser remains headless within IntignoAI for ordinary flows; Google sign-in can require a manual security check in embedded view, and success is never guaranteed.
- Python 3.9 macOS start.sh, installs dependencies and Chromium, opens *only the application UI* on port 2000.
- Included .env with the requested keys and defaults but **placeholder API secrets**. User-provided credentials were deliberately not republished in this distributable archive: rotate exposed API keys and paste newly issued keys into .env locally.
- Test status: 89 passed, 8 skipped (local test environment); Python compile, JS syntax, Bash syntax verified. Live Google/LinkedIn authentication was not exercised.
