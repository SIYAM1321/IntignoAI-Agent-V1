# IntignoAI persistent memory

Entries added by the agent or user are appended below. Do not remove earlier entries.

<!-- intignoai-entry:v1134-linkedin-nocaptcha -->
## LinkedIn has no CAPTCHA (user correction, permanent)

LinkedIn pages embed only an invisible reCAPTCHA Enterprise badge that never blocks anything. Do not pause, warn about, or mention CAPTCHA on LinkedIn unless the tool explicitly reports human_action_required. Job workflow: linkedin_scrape_jobs (search → open each job one by one → full description) → CSV via csv_path → linkedin_easy_apply with submit authorization.

<!-- intignoai-entry:v1134-no-confirm-waiver -->
## User prefers no confirmation pauses (standing waiver)

The user wants scrape/apply style flows to run without confirmation pauses. Saying "never ask for confirmation" (or Banglish equivalents such as "ar kokhono confirmation chaibe na") saves a permanent waiver to data/confirmation_waivers.json automatically; the engine then runs browser actions in that scope without asking. Destructive local tools (file deletion, kill_port, dangerous commands) still require explicit confirmation.

<!-- intignoai-entry:v1134-version-facts -->
## Version questions: always verify with tools

The user's machine has multiple Python installs plus venvs and a Microsoft Store python3 stub that errors with WinError 1920. Never answer version questions from memory; use get_python_versions / detect_dev_tools (they skip WindowsApps stubs since v1.13.4) and quote the actual output.
