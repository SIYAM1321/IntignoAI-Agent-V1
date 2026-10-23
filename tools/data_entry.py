"""Image-to-data-entry helpers for Microworkers-style crowd tasks (v1.9).

The collected task pages wrap a `crowdCigar` widget inside an iframe srcdoc. Field mode
shows one labelled image per input; page mode shows page images plus a numbered list of
inputs. These helpers parse that structure, normalise entered values and validate them
against the source so wrong or unreadable data is reported instead of submitted.
"""
from __future__ import annotations

import base64
import html as html_module
import re
import unicodedata
from pathlib import Path

# ---- task page / widget parsing ---------------------------------------------------------

_SRCDOC_RE = re.compile(r'srcdoc="(.*?)"', re.S)
_MODE_RE = re.compile(r'data-mode="?([A-Za-z]+)"?', re.I)
# The saved task pages render UNQUOTED attributes after srcdoc unescaping
# (e.g. data-external-id=967563082), so every value pattern accepts both styles.
_IMG_B64_RE = re.compile(r'src="?data:image/(?:jpeg|jpg|png);base64,([A-Za-z0-9+/=\s]+)"?', re.I)
_PAIR_RE = re.compile(r'data-external-id="([^"]+)"[^>]*?data-id="([^"]+)"', re.S)
_PAIR_RE_REVERSED = re.compile(r'data-id="([^"]+)"[^>]*?data-external-id="([^"]+)"', re.S)
_IMG_B64_RE = re.compile(r'src="data:image/(?:jpeg|jpg|png);base64,([A-Za-z0-9+/=\s]+)"', re.I)


def _unescape_srcdoc(match_text: str) -> str:
    text = html_module.unescape(match_text)
    return text.replace("\\'", "'").replace('\\"', '"').replace("\\n", "\n")


def parse_task_page(html: str) -> dict:
    """Extract task identity/earnings from a saved 'Do Task Info' page."""
    text = html or ""
    widget_html = ""
    match = _SRCDOC_RE.search(text)
    if match:
        widget_html = _unescape_srcdoc(match.group(1))
    plain = re.sub(r"<[^>]+>", "\n", text)
    plain = html_module.unescape(plain)
    lines = [re.sub(r"\s+", " ", line).strip() for line in plain.splitlines()]
    lines = [line for line in lines if line]

    job_label = ""
    campaign = ""
    employer = ""
    earn = ""
    fee = ""
    for index, line in enumerate(lines):
        if line.lower() == "job" and index + 1 < len(lines):
            job_label = lines[index + 1]
        if line.lower() in {"employer", "advertiser"} and index + 1 < len(lines):
            employer = lines[index + 1]
        if "you will earn" in line.lower():
            earn = line
        if "task service fee" in line.lower():
            fee = line
    match_campaign = re.search(r"(TTV-[^(]+\([^)]*\)\s*\([0-9a-f]+\)|[A-Z]{2,5}-\d+[^()]*\(\w+\))", job_label)
    if match_campaign:
        campaign = match_campaign.group(1).strip()
    return {"job_label": job_label, "campaign": campaign, "employer": employer,
            "earn": earn, "fee": fee, "widget_html": widget_html}


def parse_label(text: str) -> tuple[str, bool]:
    """Return (clean label, required). A trailing '*' marks a required field."""
    label = re.sub(r"\s+", " ", str(text or "")).strip()
    required = label.endswith("*")
    if required:
        label = label.rstrip("*").strip()
    return label, required


def parse_widget(widget_html: str) -> dict:
    """Parse the crowdCigar widget: mode, record id pairs, fields and page images."""
    html = widget_html or ""
    mode_match = _MODE_RE.search(html)
    mode = (mode_match.group(1).lower() if mode_match else "field")
    # Tolerant pairing: unquoted or quoted attributes, rendered in document order.
    ext_ids = re.findall(r'data-external-id="?([^"\s>]+)"?', html)
    ids = re.findall(r'data-id="?([^"\s>]+)"?', html)
    pairs = [{"external_id": ext, "id": rid} for ext, rid in zip(ext_ids, ids)]
    images = [re.sub(r"\s+", "", blob) for blob in _IMG_B64_RE.findall(html)]
    records: list[dict] = []
    labels = re.findall(r"<label[^>]*>(.*?)</label>", html, re.S | re.I)
    values = re.findall(r'<input[^>]*type="?text"?[^>]*value="?([^">]*)"?', html, re.I)
    if mode == "field":
        for index, pair in enumerate(pairs):
            label_text = labels[index] if index < len(labels) else ""
            clean, required = parse_label(html_module.unescape(label_text))
            hint_match = re.search(r"\[([^\]]+)\]$", clean)
            hint = hint_match.group(1) if hint_match else ""
            if hint_match:
                clean = clean[: hint_match.start()].strip()
            records.append({
                "external_id": pair["external_id"],
                "id": pair["id"],
                "label": clean,
                "required": required,
                "hint": hint,
                "value": html_module.unescape(values[index]) if index < len(values) else "",
                "image_b64": images[index] if index < len(images) else "",
            })
    else:  # page mode: numbered inputs paired with page images by index
        for index, value in enumerate(values):
            label_text = labels[index] if index < len(labels) else f" {index + 1}"
            clean, required = parse_label(html_module.unescape(label_text))
            records.append({
                "external_id": "",
                "id": "",
                "label": clean,
                "required": required,
                "hint": "",
                "value": html_module.unescape(value),
                "image_b64": "",
            })
    return {"mode": mode, "records": records, "images": images, "pairs": pairs}


# ---- value normalisation / comparison ---------------------------------------------------

def normalize_value(value: str, kind: str | None = None) -> str:
    """NFC, strip pasted-quote artifacts and collapse whitespace; numeric comma -> dot."""
    text = unicodedata.normalize("NFC", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r'^["\'`]+', "", text)
    text = re.sub(r'["\'`]+$', "", text)
    if (kind or "").lower() == "number":
        text = text.replace(".", "").replace(",", ".") if re.fullmatch(r"-?\d{1,3}(\.\d{3})*,\d+", text) else text.replace(",", ".")
    return text


def is_na(value: str) -> bool:
    return re.sub(r"[\s./-]", "", str(value or "")).lower() == "na"


_CONFUSABLE = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "S": "5", "s": "5",
                             "B": "8", "Z": "2", "z": "2", "G": "6"})


def confusable_fold(value: str) -> str:
    return str(value or "").translate(_CONFUSABLE).upper()


def hint_allows(value: str, hint: str) -> bool:
    """Charset hint like '[äöü]' means: ASCII LETTERS + space/-/. plus the listed literals.

    Digits and symbols are NOT implicitly allowed (the real hints gate names like 'Jürgen'
    against stray digits or punctuation pasted from images).
    """
    if not hint:
        return True
    extra = set(hint)
    for char in str(value or ""):
        if char.isascii() and (char.isalpha() or char in " -."):
            continue
        if char in extra:
            continue
        return False
    return True


def compare_values(expected: str, submitted: str, kind: str | None = None) -> str:
    """Return exact | case | numeric | confusable | mismatch."""
    left = normalize_value(expected, kind)
    right = normalize_value(submitted, kind)
    if left == right:
        return "exact"
    if left.casefold() == right.casefold():
        return "case"
    if kind == "number" or (re.fullmatch(r"-?\d+(\.\d+)?", left or "") and re.fullmatch(r"-?\d+(\.\d+)?", right or "")):
        try:
            if float(left) == float(right):
                return "numeric"
        except ValueError:
            pass
    if confusable_fold(left) == confusable_fold(right):
        return "confusable"
    return "mismatch"


def validate_field(record: dict, submitted: str, allow_na: bool = True) -> dict:
    """Validate one entered value against its source record."""
    expected = str(record.get("value", "") or "")
    label = str(record.get("label", "") or f"field {record.get('external_id', '')}")
    verdict = compare_values(expected, submitted, record.get("kind"))
    problems: list[str] = []
    warnings: list[str] = []
    if not str(submitted or "").strip():
        if record.get("required"):
            problems.append(f"{label}: required value is empty")
        elif not is_na(expected) or not allow_na:
            problems.append(f"{label}: value is empty but the source shows {expected!r}")
    elif verdict == "mismatch":
        if is_na(submitted) and allow_na:
            warnings.append(f"{label}: submitted NA for source {expected!r}")
        else:
            problems.append(f"{label}: submitted {submitted!r} != source {expected!r}")
    elif verdict in {"confusable", "case"}:
        warnings.append(f"{label}: accepted with {verdict} normalisation ({submitted!r} vs {expected!r})")
    hint = str(record.get("hint", "") or "")
    if hint and submitted and not hint_allows(submitted, hint):
        problems.append(f"{label}: value {submitted!r} uses characters outside the hint [{hint}]")
    if expected.startswith('"') and submitted and not str(submitted).startswith('"'):
        warnings.append(f"{label}: leading pasted quote was stripped")
    return {"label": label, "ok": not problems, "verdict": verdict, "problems": problems, "warnings": warnings}


def validate_submission(records: list[dict], submitted: dict[str, str], allow_na: bool = True) -> dict:
    """Validate every field; keys of submitted are external_id (or id, or index string)."""
    results: list[dict] = []
    for index, record in enumerate(records or []):
        key = str(record.get("external_id") or record.get("id") or index)
        value = submitted.get(key, submitted.get(str(record.get("id") or ""), submitted.get(str(index), "")))
        results.append(validate_field(record, value, allow_na=allow_na))
    problems = [item for result in results for item in result["problems"]]
    warnings = [item for result in results for item in result["warnings"]]
    return {"ok": not problems, "checked": len(results), "problems": problems, "warnings": warnings, "fields": results}


def map_record_ids(records: list[dict]) -> dict[str, str]:
    return {str(record.get("external_id")): str(record.get("id")) for record in records or [] if record.get("external_id")}


def save_field_images(records: list[dict], out_dir) -> dict:
    """Decode base64 field/page images to JPEG files; invalid payloads are reported, not fatal."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    errors: list[str] = []
    for index, record in enumerate(records or []):
        blob = str(record.get("image_b64", "") or "")
        if not blob:
            continue
        try:
            data = base64.b64decode(blob, validate=False)
            name = str(record.get("external_id") or record.get("id") or f"field-{index}") + ".jpg"
            target = out_dir / re.sub(r"[^A-Za-z0-9._-]+", "-", name)
            target.write_bytes(data)
            saved.append(str(target))
        except Exception as exc:
            errors.append(f"{record.get('external_id') or index}: {exc}")
    return {"ok": not errors, "saved": saved, "errors": errors, "count": len(saved)}
