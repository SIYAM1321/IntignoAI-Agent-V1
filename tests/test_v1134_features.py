"""v1.13.4 feature tests: confirmation waivers, approval policy, pending-state safety."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("GOOGLE_API_KEY", "test-key")
_TMP = tempfile.mkdtemp(prefix="intignoai-v1134-")
os.environ.setdefault("WORKSPACE", _TMP)

from agent import engine, waivers  # noqa: E402
from agent import knowledge as _knowledge  # noqa: E402
from agent.memory import PendingConfirmation, PendingToolCall  # noqa: E402

# Keep test writes away from the real SKILLS.MD / MEMORY.MD templates.
_knowledge.MEMORY_PATH = Path(_TMP) / "MEMORY-test.md"
_knowledge.SKILLS_PATH = Path(_TMP) / "SKILLS-test.md"


def _clear_waiver_store():
    store = Path(waivers.WAIVERS_PATH)
    store.parent.mkdir(parents=True, exist_ok=True)
    if store.exists():
        store.unlink()


class WaiverDetectionTests(unittest.TestCase):
    def test_english_and_banglish_phrases_are_detected(self):
        self.assertIsNotNone(waivers.detect_waiver_request("never ask me for confirmation again"))
        self.assertIsNotNone(waivers.detect_waiver_request("ar kokhono confirmation chaibe na"))
        self.assertIsNotNone(waivers.detect_waiver_request("ei kaj e confirm korar dorkar nai"))
        self.assertIsNotNone(waivers.detect_waiver_request("confirmation lagbe na, direct submit koro"))
        self.assertIsNotNone(waivers.detect_waiver_request("proceed without confirmation from now on"))

    def test_normal_task_requests_are_not_waivers(self):
        self.assertIsNone(waivers.detect_waiver_request("scrape linkedin jobs and create a csv"))
        self.assertIsNone(waivers.detect_waiver_request("yes, submit the application"))
        self.assertIsNone(waivers.detect_waiver_request(""))

    def test_scope_is_site_specific_or_global(self):
        self.assertEqual(waivers.detect_waiver_request("linkedin e ar kokhono confirmation chaibe na")["scope"], "linkedin")
        self.assertEqual(waivers.detect_waiver_request("google e o confirmation lagbe na")["scope"], "google")
        self.assertEqual(waivers.detect_waiver_request("sob kaj e ar confirmation chaibe na")["scope"], "global")
        self.assertEqual(waivers.detect_waiver_request("never ask for confirmation")["scope"], "global")


class WaiverPersistenceTests(unittest.TestCase):
    def setUp(self):
        _clear_waiver_store()

    def tearDown(self):
        _clear_waiver_store()

    def test_persist_is_deduplicated_and_waiver_allows_matches_scope(self):
        report = waivers.persist_waiver("linkedin", source_message="linkedin e ar confirmation chaibe na")
        self.assertTrue(report["ok"])
        self.assertFalse(report["duplicate"])
        again = waivers.persist_waiver("linkedin")
        self.assertTrue(again["duplicate"])

        self.assertFalse(waivers.waiver_allows("delete_file", {"path": "anything"}))
        self.assertFalse(waivers.waiver_allows("browser_action", {"actions": [{"action": "click", "selector": "#unrelated"}]}))
        self.assertTrue(waivers.waiver_allows("browser_action", {
            "url": "https://www.linkedin.com/jobs/view/4453023551/",
            "actions": [{"action": "click", "selector": "button:has-text('Submit application')",
                          "fallback": {"name": "Submit application", "role": "button"}}],
        }))

    def test_global_waiver_covers_browser_actions_only(self):
        waivers.persist_waiver("global")
        self.assertTrue(waivers.waiver_allows("browser_action", {"actions": [{"action": "click", "text": "Pay now"}]}))
        self.assertFalse(waivers.waiver_allows("run_terminal", {"command": "rm -rf folder"}))
        self.assertFalse(waivers.waiver_allows("delete_directory", {"path": "folder"}))


class ConfirmationPolicyTests(unittest.TestCase):
    def setUp(self):
        _clear_waiver_store()

    def tearDown(self):
        _clear_waiver_store()

    def test_linkedin_submit_application_click_runs_without_approval(self):
        batch = {"actions": [
            {"action": "click", "selector": "button:has-text('Submit application')",
             "fallback": {"name": "Submit application", "role": "button"}},
            {"action": "wait", "ms": 3000},
            {"action": "scrape", "max_chars": 2000},
        ]}
        self.assertIs(engine.needs_confirmation("browser_action", batch), False)

    def test_consequential_clicks_still_require_approval(self):
        self.assertIs(engine.needs_confirmation("browser_action", {"actions": [{"action": "click", "text": "Pay now"}]}), True)
        self.assertIs(engine.needs_confirmation("browser_action", {"actions": [{"action": "click", "text": "Message recruiter"}]}), True)
        self.assertIs(engine.needs_confirmation("browser_action", {"actions": [{"action": "click", "text": "Create account"}]}), True)
        self.assertIs(engine.needs_confirmation("browser_action", {"actions": [{"action": "submit"}]}), True)
        self.assertIs(engine.needs_confirmation("delete_file", {"path": "x"}), True)
        self.assertIs(engine.needs_confirmation("run_terminal", {"command": "Remove-Item thing -Recurse -Force"}), True)

    def test_engine_note_user_confirmation_preference_persists_and_skips_duplicates(self):
        result = engine.note_user_confirmation_preference("ar kokhono confirmation chaibe na, always proceed")
        self.assertIsNotNone(result)
        self.assertEqual(result["scope"], "global")
        self.assertTrue(Path(waivers.WAIVERS_PATH).exists())
        duplicate = engine.note_user_confirmation_preference("never ask for confirmation")
        self.assertTrue(duplicate["duplicate"])

    def test_waived_batch_never_pauses_even_for_money_clicks(self):
        waivers.persist_waiver("global")
        batch = {"actions": [{"action": "click", "text": "Pay now"}]}
        self.assertIs(engine.needs_confirmation("browser_action", batch), False)


class PendingConfirmationSafetyTests(unittest.TestCase):
    def test_current_returns_none_instead_of_raising(self):
        empty = PendingConfirmation(interaction_id="i", calls=[], results={})
        self.assertIsNone(empty.current)
        exhausted = PendingConfirmation(
            interaction_id="i",
            calls=[PendingToolCall("browser_action", "browser_action", {}, "c1", "a1")],
            results={}, awaiting_index=1,
        )
        self.assertIsNone(exhausted.current)
        valid = PendingConfirmation(
            interaction_id="i",
            calls=[PendingToolCall("browser_action", "browser_action", {}, "c1", "a1")],
            results={}, awaiting_index=0,
        )
        self.assertEqual(valid.current.call_id, "c1")

    def test_confirm_pending_clears_stale_state_with_friendly_error(self):
        import asyncio  # noqa: F401  (import cost only; engine threads are daemonized)

        session = engine.Session()
        session.pending = PendingConfirmation(interaction_id="i", calls=[], results={})
        with self.assertRaises(ValueError) as ctx:
            engine.confirm_pending(session, True, lambda _text: None)
        self.assertIn("no longer valid", str(ctx.exception))
        self.assertIsNone(session.pending)


if __name__ == "__main__":
    unittest.main()
