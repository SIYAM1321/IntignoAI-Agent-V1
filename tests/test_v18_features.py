"""v1.8 regression tests: navigation loop fix, YouTube matching helpers, all-questions answering."""
from __future__ import annotations

import io

import pytest

from tools import browser_automation as browser


# --- YouTube id extraction -------------------------------------------------------------------

def test_extract_youtube_id_from_all_common_url_shapes():
    cases = {
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ": "dQw4w9WgXcQ",
        "https://www.youtube.com/watch?app=desktop&v=dQw4w9WgXcQ&t=3s": "dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ": "dQw4w9WgXcQ",
        "https://www.youtube.com/embed/dQw4w9WgXcQ": "dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ": "dQw4w9WgXcQ",
        "https://www.youtube.com/live/dQw4w9WgXcQ": "dQw4w9WgXcQ",
        "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg": "dQw4w9WgXcQ",
        "https://img.youtube.com/vi/dQw4w9WgXcQ/default.jpg": "dQw4w9WgXcQ",
        "https://workupjob.com/image_upload/post_thumb/52595423193261019123041_post_thumb.png": None,
    }
    for value, expected in cases.items():
        assert browser._extract_youtube_id(value) == expected, value


# --- perceptual hash matching ----------------------------------------------------------------

def _png_bytes(color_left, color_right):
    from PIL import Image

    image = Image.new("RGB", (160, 90))
    for x in range(160):
        for y in range(90):
            image.putpixel((x, y), color_left if x < 80 else color_right)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_identical_images_hash_distance_zero_and_distinct_images_apart():
    data = _png_bytes((255, 0, 0), (0, 0, 255))
    hashes = browser._image_hashes(data)
    assert hashes is not None
    assert browser._hash_distance(hashes, browser._image_hashes(data)) == 0
    other = browser._image_hashes(_png_bytes((0, 255, 0), (255, 255, 0)))
    assert browser._hash_distance(hashes, other) > 0


def test_unrecoverable_image_returns_none():
    assert browser._image_hashes(b"not-an-image") is None


# --- multi-question matching (LinkedIn Yes/No fix) -------------------------------------------

def test_match_group_answer_prefers_best_question_match():
    answers = [
        {"question": "Are you legally authorized to work in this country?", "answer": "Yes"},
        {"question": "Are you willing to relocate?", "answer": "No"},
    ]
    assert browser._match_group_answer("Are you legally authorized to work in this country? *", "", answers) == "Yes"
    assert browser._match_group_answer("are you willing to relocate", "", answers) == "No"
    assert browser._match_group_answer("completely unrelated question", "", answers) is None


def test_match_group_answer_falls_back_to_bare_answer():
    assert browser._match_group_answer("Anything?", "", [{"answer": "Yes"}]) == "Yes"
    assert browser._match_group_answer("Anything?", "", ["No"]) == "No"


def test_pick_group_option_resolves_yes_no_labels_case_insensitively():
    options = [
        {"index": 0, "label": "Yes", "value": "Yes", "visible": True, "disabled": False},
        {"index": 1, "label": "No", "value": "No", "visible": True, "disabled": False},
    ]
    assert browser._pick_group_option(options, "yes")["index"] == 0
    assert browser._pick_group_option(options, "NO")["index"] == 1
    assert browser._pick_group_option(options, "maybe") is None


def test_pick_group_option_skips_disabled_and_matches_values():
    options = [
        {"index": 0, "label": "", "value": "true", "visible": True, "disabled": True},
        {"index": 1, "label": "", "value": "true", "visible": True, "disabled": False},
    ]
    chosen = browser._pick_group_option(options, "Yes")
    assert chosen is not None and chosen["index"] == 1


# --- navigation fallback (the infinite-loading fix) -------------------------------------------

class _TimeoutPage:
    """goto always times out; the URL commits immediately (slow resources, real host)."""

    def __init__(self, url: str):
        self.url = url
        self._mouse = self._FakeMouse()

    class _FakeMouse:
        def wheel(self, *_args, **_kwargs):
            return None

    @property
    def mouse(self):
        return self._mouse

    def goto(self, target, wait_until=None, timeout=None):
        raise TimeoutError(f"Timeout {timeout}ms exceeded")

    def wait_for_load_state(self, state=None, timeout=None):
        return None

    def wait_for_url(self, predicate=None, timeout=None):
        return None

    def wait_for_timeout(self, ms):
        return None

    def title(self):
        return "t"


def test_navigation_timeout_with_committed_url_is_recoverable_not_fatal(monkeypatch):
    monkeypatch.setattr(browser, "NAV_RETRY_COOLDOWN_SECONDS", 0.0)
    page = _TimeoutPage("https://example.com/job/1")
    response, fallback, info = browser._navigate_page(page, "https://example.com/job/1", "domcontentloaded", 15000)
    assert response is None
    assert fallback is True
    assert info["recovered"] is True


class _NeverCommitsPage(_TimeoutPage):
    def wait_for_load_state(self, state=None, timeout=None):
        raise TimeoutError("Timeout exceeded")

    def wait_for_url(self, predicate=None, timeout=None):
        raise TimeoutError("Timeout exceeded")


def test_navigation_timeout_without_commit_reports_structured_timeout(monkeypatch):
    monkeypatch.setattr(browser, "NAV_RETRY_COOLDOWN_SECONDS", 0.0)
    page = _NeverCommitsPage("about:blank")
    response, fallback, info = browser._navigate_page(page, "https://example.com/job/1", "domcontentloaded", 15000)
    assert response is None
    assert fallback is False
    assert info["navigation_timeout"] is True
    assert info["retry_recommended"] is False
    assert "Do NOT immediately repeat" in info["note"]


def test_repeated_goto_within_cooldown_is_suppressed(monkeypatch):
    monkeypatch.setattr(browser, "NAV_RETRY_COOLDOWN_SECONDS", 30.0)

    class _LateCommitPage(_TimeoutPage):
        def wait_for_url(self, predicate=None, timeout=None):
            self.url = "https://example.com/job/1"
            return True

    page = _LateCommitPage("about:blank")
    _first = browser._navigate_page(page, "https://example.com/job/1", "domcontentloaded", 15000)
    page.url = "https://example.com/job/1"
    _response, fallback, info = browser._navigate_page(page, "https://example.com/job/1", "domcontentloaded", 15000)
    assert fallback is True
    assert info["repeated_goto_suppressed"] is True


# --- lazy-load scroll loop --------------------------------------------------------------------

def test_scroll_until_stable_terminates_when_height_stops_growing():
    class _ScrollPage(_TimeoutPage):
        def __init__(self):
            super().__init__("https://example.com/feed")
            self.reads = 0
            self.height = 1000

        def evaluate(self, script, *args):
            if "scroll_y" in script:
                return {"height": self.height, "scroll_y": 0}
            self.reads += 1
            if self.reads <= 2:
                self.height += 400
            return {"h": self.height, "y": self.height}

    page = _ScrollPage()
    result = browser._scroll_until_stable(page, {"pause_ms": 1, "max_scrolls": 25})
    assert result["ok"] is True
    assert result["scrolls"] <= 6
    assert result["final_height"] == page.height


# --- previous-tab selection -------------------------------------------------------------------

def test_select_tab_previous_falls_back_to_second_last_tab():
    class _Tab:
        def __init__(self, name):
            self.name = name

        def is_closed(self):
            return False

    class _Context:
        def __init__(self, tabs):
            self.pages = tabs

    first, second = _Tab("a"), _Tab("b")
    context = _Context([first, second])
    session = {"prev_page": None}
    chosen = browser._select_tab(context, session, {"tab_id": "previous"})
    assert chosen is first
