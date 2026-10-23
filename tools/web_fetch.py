from __future__ import annotations

import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

from tools.web_search import duckduckgo_search


class _PageText(HTMLParser):
    BLOCKED = {"script", "style", "noscript", "svg", "canvas", "nav", "footer", "header", "form"}
    BLOCKS = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr", "section", "article", "main"}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.title_mode = False
        self.title = []
        self.parts = []
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self.BLOCKED:
            self.skip += 1
        if tag == "title" and self.skip == 0:
            self.title_mode = True
        if not self.skip and tag in self.BLOCKS:
            self.parts.append("\n")
    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title": self.title_mode = False
        if tag in self.BLOCKED and self.skip:
            self.skip -= 1
        if not self.skip and tag in self.BLOCKS:
            self.parts.append("\n")
    def handle_data(self, data):
        if self.skip: return
        text = re.sub(r"\s+", " ", data).strip()
        if not text: return
        if self.title_mode: self.title.append(text)
        else: self.parts.append(text + " ")


def _validate_public_url(url: str) -> str:
    parsed = urllib.parse.urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http:// and https:// URLs are supported.")
    if not parsed.hostname:
        raise ValueError("A valid URL hostname is required.")
    host = parsed.hostname.strip("[]").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal")):
        raise PermissionError("Local/private network URLs are blocked by the web-fetch tool.")
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            addresses = {ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
        except OSError as exc:
            raise ValueError(f"Could not resolve URL host: {exc}") from exc
    if not addresses or any(not addr.is_global for addr in addresses):
        raise PermissionError("URLs resolving to private, loopback, link-local or reserved IP addresses are blocked.")
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.params, parsed.query, ""))


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_url(url: str, max_chars: int = 24000, search_fallback: bool = True) -> dict[str, Any]:
    """Fetch and extract readable text from a public web page, with optional search fallback."""
    max_chars = max(1000, min(int(max_chars), 60000))
    try:
        safe_url = _validate_public_url(url)
        request = urllib.request.Request(safe_url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; IntignoAI/2.0; +local-agent)",
            "Accept": "text/html,application/xhtml+xml,text/plain,application/json;q=0.9,*/*;q=0.5",
            "Accept-Language": "en-US,en;q=0.8",
        })
        opener = urllib.request.build_opener(_SafeRedirect())
        with opener.open(request, timeout=18) as response:
            final_url = _validate_public_url(response.geturl())
            content_type = response.headers.get_content_type()
            charset = response.headers.get_content_charset() or "utf-8"
            raw = response.read(4_000_001)
            if len(raw) > 4_000_000:
                raw = raw[:4_000_000]
                truncated_bytes = True
            else:
                truncated_bytes = False
        payload = raw.decode(charset, errors="replace")
        if content_type in {"text/html", "application/xhtml+xml"} or "<html" in payload[:1000].lower():
            parser = _PageText()
            parser.feed(payload)
            title = " ".join(parser.title).strip()
            text = re.sub(r"[ \t]+", " ", "".join(parser.parts))
            text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
        else:
            title = ""
            text = payload.strip()
        if not text:
            raise ValueError("The page was fetched but no readable text was found (it may require JavaScript).")
        return {"ok": True, "url": safe_url, "final_url": final_url, "title": title, "content_type": content_type,
                "text": text[:max_chars], "characters": len(text), "truncated": len(text) > max_chars or truncated_bytes,
                "encoding": charset}
    except Exception as exc:
        result: dict[str, Any] = {"ok": False, "url": str(url), "error": str(exc)}
        if search_fallback:
            try:
                parsed = urllib.parse.urlparse(str(url or ""))
                # Only search-fallback for URLs that pass the same public-host check.
                # Do not leak localhost/internal URLs into an external search query.
                if parsed.hostname and parsed.scheme in {"http", "https"}:
                    safe_url = _validate_public_url(str(url))
                    safe_parsed = urllib.parse.urlparse(safe_url)
                    query = f"site:{safe_parsed.hostname} {safe_parsed.path.replace('/', ' ')}"
                    result["search_fallback"] = duckduckgo_search(query, max_results=5)
            except Exception as fallback_exc:
                if not isinstance(fallback_exc, PermissionError):
                    result["search_fallback_error"] = str(fallback_exc)
        return result
