from __future__ import annotations

import html
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

SEARCH_URL = "https://html.duckduckgo.com/html/?q={query}&kl={region}"


class _DDGParser(HTMLParser):
    """Small dependency-free parser for DuckDuckGo's non-JS HTML results."""
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._title_active = False
        self._snippet_active = False
        self._title_depth = 0
        self._snippet_depth = 0
        self._current_index: int | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        attr = dict(attrs)
        classes = set((attr.get("class") or "").split())

        if tag == "a" and "result__a" in classes:
            href = html.unescape(attr.get("href") or "").strip()
            self.results.append({"title": "", "url": href, "snippet": ""})
            self._current_index = len(self.results) - 1
            self._title_active = True
            self._title_depth = 1
            return

        if self._current_index is not None and "result__snippet" in classes:
            self._snippet_active = True
            self._snippet_depth = 1
            return

        if self._title_active:
            self._title_depth += 1
        elif self._snippet_active:
            self._snippet_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self._title_active:
            self._title_depth -= 1
            if self._title_depth <= 0:
                self._title_active = False
        elif self._snippet_active:
            self._snippet_depth -= 1
            if self._snippet_depth <= 0:
                self._snippet_active = False

    def handle_data(self, data: str) -> None:
        if self._current_index is None:
            return
        text = re.sub(r"\s+", " ", data).strip()
        if not text:
            return
        item = self.results[self._current_index]
        if self._title_active:
            item["title"] += (" " if item["title"] else "") + text
        elif self._snippet_active:
            item["snippet"] += (" " if item["snippet"] else "") + text


def _clean_redirect(url: str) -> str:
    url = html.unescape(url)
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    if "uddg" in query and query["uddg"]:
        return urllib.parse.unquote(query["uddg"][0])
    return url


def duckduckgo_search(query: str, max_results: int = 8, region: str = "wt-wt") -> dict[str, Any]:
    query = str(query or "").strip()
    if not query:
        raise ValueError("Search query is required.")
    max_results = max(1, min(int(max_results), 20))
    region = str(region or "wt-wt")
    url = SEARCH_URL.format(query=urllib.parse.quote_plus(query), region=urllib.parse.quote_plus(region))
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 Chrome/153 Safari/537.36 IntignoAI/2.0",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.8",
        "Cache-Control": "no-cache",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            payload = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"DuckDuckGo search HTTP {exc.code}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"DuckDuckGo search failed: {exc.reason}") from exc

    parser = _DDGParser()
    parser.feed(payload)
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in parser.results:
        link = _clean_redirect(item["url"])
        if not link or link in seen or not re.match(r"^https?://", link, re.I):
            continue
        title = re.sub(r"\s+", " ", item["title"]).strip()
        if not title:
            continue
        seen.add(link)
        results.append({"title": title, "url": link, "snippet": re.sub(r"\s+", " ", item["snippet"]).strip()})
        if len(results) >= max_results:
            break

    return {
        "ok": True,
        "query": query,
        "region": region,
        "count": len(results),
        "results": results,
        "search_engine": "DuckDuckGo",
    }
