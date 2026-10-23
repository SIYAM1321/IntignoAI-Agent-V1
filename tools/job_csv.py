"""LinkedIn job HTML -> clean CSV export (v1.9). Stdlib only.

Works from live scrapes (dict records) and from saved LinkedIn HTML files (the collected
fixtures use fullwidth ｜ / ： in filenames and obfuscated CSS classes, so parsing anchors on
stable structures: <title>, canonical /jobs/view/<id> links and the expandable-text-box).
"""
from __future__ import annotations

import csv
import html as html_module
import io
import json
import re
import unicodedata
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

CSV_COLUMNS = [
    "title", "company", "location", "workplace_type", "employment_type", "posted_text",
    "applicants_text", "job_id", "job_url", "description", "saved_at", "source_file",
]

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None)


def clean_text(value, one_line: bool = False) -> str:
    """Strip zero-width/invisible characters and collapse whitespace."""
    if value is None:
        return ""
    text = str(value).translate(_ZERO_WIDTH)
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if one_line:
        text = re.sub(r"\s+", " ", text)
    else:
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_BLOCK_TAGS = {"p", "div", "section", "li", "ul", "ol", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}


def html_to_text(html: str) -> str:
    """Readable text from a description fragment: <br>/<li>/block ends become newlines."""

    class _Text(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts: list[str] = []

        def handle_starttag(self, tag, attrs):
            if tag == "br":
                self.parts.append("\n")
            elif tag == "li":
                self.parts.append("\n- ")
            elif tag in _BLOCK_TAGS:
                self.parts.append("\n")

        def handle_endtag(self, tag):
            if tag in _BLOCK_TAGS and tag != "br":
                self.parts.append("\n")

        def handle_data(self, data):
            if data:
                self.parts.append(data)

    parser = _Text()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:
        return clean_text(html_module.unescape(html or ""))
    return clean_text("".join(parser.parts))


def parse_saved_name(filename: str) -> dict:
    """'AI Engineer ｜ GEI Consultants ｜ LinkedIn (10_20_2026 8：01：44 PM).html' -> parts.

    Handles fullwidth U+FF5C separators and U+FF1A time colons; tolerates ordinary '|' and ':'.
    """
    stem = Path(str(filename)).stem
    parts = [part.strip() for part in re.split(r"[\uff5c|]", stem)]
    parts = [part for part in parts if part]
    title = parts[0] if parts else stem
    company = ""
    if len(parts) >= 3:
        company = parts[1]
    elif len(parts) == 2 and parts[1].lower() != "linkedin":
        company = parts[1]
    saved_at = ""
    match = re.search(r"\((\d{1,2})_(\d{1,2})_(\d{4})\s+(\d{1,2})\uff1a(\d{1,2})\uff1a(\d{1,2})\s*([AP]M)?\)", stem)
    if not match:
        match = re.search(r"\((\d{1,2})_(\d{1,2})_(\d{4})\s+(\d{1,2}):(\d{1,2}):(\d{1,2})\s*([AP]M)?\)", stem)
    if match:
        month, day, year, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
        meridiem = (match.group(7) or "").upper()
        if meridiem == "PM" and hour < 12:
            hour += 12
        if meridiem == "AM" and hour == 12:
            hour = 0
        try:
            saved_at = datetime(year, month, day, hour, minute, second).isoformat()
        except ValueError:
            saved_at = ""
    # Drop copy suffixes like ' (1)' from the title.
    title = re.sub(r"\s*\(\d+\)$", "", title).strip()
    return {"title": clean_text(title, one_line=True), "company": clean_text(company, one_line=True), "saved_at": saved_at}


def clean_job_url(url: str) -> str:
    match = re.search(r"/jobs/view/(\d+)", str(url or ""))
    return f"https://www.linkedin.com/jobs/view/{match.group(1)}/" if match else str(url or "")


class _JobPageParser(HTMLParser):
    """Collect <title>, first /jobs/view/<id> link, ordered text and the description block.

    The description anchor is data-testid="expandable-text-box" (logged-in layout) with
    guest-layout fallbacks handled after parsing (show-more-less-html__markup etc.).
    """

    def __init__(self, limit: int = 400_000):
        super().__init__(convert_charrefs=True)
        self.limit = limit
        self.title = ""
        self.in_title = False
        self.job_id = ""
        self.pieces: list[str] = []
        self._in_description = 0
        self._description_parts: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        self._depth += 1
        attrs_dict = dict(attrs)
        if tag == "title":
            self.in_title = True
        if tag == "a" and not self.job_id:
            href = attrs_dict.get("href") or ""
            match = re.search(r"/jobs/view/(\d+)", href)
            if match:
                self.job_id = match.group(1)
        testid = attrs_dict.get("data-testid") or ""
        if testid == "expandable-text-box":
            self._in_description += 1
        elif tag == "h2" and self._in_description:
            # 'About the job' block ends at the next section heading.
            self._in_description = 0
        if self._in_description and tag in _BLOCK_TAGS:
            self._description_parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if self._in_description and tag in _BLOCK_TAGS and tag != "br":
            self._description_parts.append("\n")
        self._depth = max(0, self._depth - 1)

    def handle_data(self, data):
        if not data:
            return
        if self.in_title:
            self.title += data
        elif self._in_description:
            if sum(len(part) for part in self._description_parts) < self.limit:
                self._description_parts.append(data)
        else:
            self.pieces.append(data)


_LOCATION_HINT = re.compile(r",|remote|hybrid|on-site|united states|new york|\bny\b|\barea\b", re.I)


def _looks_like_location(piece: str, company: str, title: str) -> bool:
    low = piece.lower()
    if not piece or len(piece) > 80:
        return False
    if company and low in company.lower():
        return False
    if title and low in title.lower():
        return False
    if re.search(r"^\d+[+-]?\s*(week|month|day|hour|minute)s?\s*ago$", low):
        return False
    if "applicant" in low or "ago" in low:
        return False
    return bool(_LOCATION_HINT.search(piece) or re.match(r"^[A-Z][\w .'-]+(?:,\s*[A-Z]{2})?$", piece))


def parse_job_html(html: str, source_file: str | None = None) -> dict:
    """Parse one LinkedIn job page (logged-in layout, guest layout, or ATS variants)."""
    parser = _JobPageParser()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:
        pass
    title_text = clean_text(parser.title, one_line=True)
    # <title> shape: 'AI Engineer | GEI Consultants, Inc. | LinkedIn'
    segments = [clean_text(seg, one_line=True) for seg in (title_text.split("|") if title_text else [])]
    title = segments[0] if segments else ""
    company = segments[1] if len(segments) >= 3 else ""
    meta_line = ""
    description = clean_text("".join(parser._description_parts))
    if not description:
        match = re.search(r'<div[^>]*class="[^"]*(?:show-more-less-html__markup|description__text)[^"]*"[^>]*>(.*?)</div>', html or "", re.S)
        if match:
            description = html_to_text(match.group(1))
    # Header meta pieces: 'Toronto, ON', '1 month ago', 'Over 100 applicants', 'Full-time'...
    ordered = [clean_text(piece, one_line=True) for piece in parser.pieces]
    ordered = [piece for piece in ordered if piece]
    location = ""
    posted_text = ""
    applicants_text = ""
    workplace_type = ""
    employment_type = ""
    for index, piece in enumerate(ordered[:40]):
        if not location and _looks_like_location(piece, company, title):
            location = piece
            continue
        if not posted_text and re.search(r"\b(ago|posted)\b", piece, re.I) and len(piece) < 40:
            posted_text = piece
            continue
        if not applicants_text and re.search(r"applicant", piece, re.I) and len(piece) < 60:
            applicants_text = piece
            continue
        if not employment_type and re.fullmatch(r"(full|part)[-\s]?time|contract|temporary|internship", piece, re.I):
            employment_type = piece
            continue
        if not workplace_type and re.fullmatch(r"(on-site|hybrid|remote)", piece, re.I):
            workplace_type = piece.title()
            continue
        if location and not meta_line:
            meta_line = piece
    return {
        "title": title,
        "company": company,
        "location": location,
        "workplace_type": workplace_type,
        "employment_type": employment_type,
        "posted_text": posted_text,
        "applicants_text": applicants_text,
        "job_id": parser.job_id,
        "job_url": clean_job_url(f"https://www.linkedin.com/jobs/view/{parser.job_id}/") if parser.job_id else "",
        "description": description,
        "saved_at": "",
        "source_file": source_file or "",
    }


def parse_job_file(path) -> dict:
    path = Path(path)
    raw = path.read_bytes()
    charset = "utf-8"
    head = raw[:4096].decode("utf-8", "replace").lower()
    match = re.search(r'charset=["\']?([\w-]+)', head)
    if match:
        charset = match.group(1)
    try:
        html = raw.decode(charset, "replace")
    except LookupError:
        html = raw.decode("utf-8", "replace")
    record = parse_job_html(html, source_file=str(path))
    from_name = parse_saved_name(path.name)
    for key, value in from_name.items():
        if not record.get(key):
            record[key] = value
    return record


def jobs_from_paths(paths) -> tuple[list[dict], list[str]]:
    jobs: list[dict] = []
    errors: list[str] = []
    for path in paths:
        try:
            jobs.append(parse_job_file(path))
        except Exception as exc:
            errors.append(f"{path}: {exc}")
    return jobs, errors


def jobs_from_directory(directory) -> tuple[list[dict], list[str]]:
    directory = Path(directory)
    paths = sorted(directory.glob("**/*.html")) if directory.is_dir() else []
    return jobs_from_paths(paths)


def _formula_guard(cell):
    text = "" if cell is None else str(cell)
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


def export_jobs_csv(jobs, handle) -> int:
    """Write jobs to an open file handle as Excel-friendly CSV (utf-8-sig BOM, CRLF)."""
    writer = csv.writer(handle, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    count = 0
    for job in jobs or []:
        if not isinstance(job, dict):
            continue
        row = []
        for column in CSV_COLUMNS:
            value = job.get(column, "")
            if column in {"description"}:
                value = clean_text(value)
            elif column in {"job_url"}:
                value = clean_job_url(value)
            else:
                value = clean_text(value, one_line=True)
            row.append(_formula_guard(value))
        writer.writerow(row)
        count += 1
    return count


def write_jobs_csv(jobs, path) -> dict:
    """Write jobs to `path` and return a small report dict (used by tests and tooling)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        count = export_jobs_csv(jobs, handle)
    return {"ok": True, "path": str(path), "rows": count, "columns": CSV_COLUMNS}
