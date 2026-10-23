"""v1.9 unit tests: job CSV export + image-to-data-entry helpers (stdlib-only modules)."""
from __future__ import annotations

import base64
import csv
import io
import json
from pathlib import Path

import pytest

from tools import data_entry, job_csv

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"


# ---- job_csv ----------------------------------------------------------------------------

def test_parse_saved_name_handles_fullwidth_separators_and_time():
    info = job_csv.parse_saved_name("AI Engineer ｜ GEI Consultants, Inc. ｜ LinkedIn (10_20_2026 8：01：44 PM).html")
    assert info["title"] == "AI Engineer"
    assert info["company"] == "GEI Consultants, Inc."
    assert info["saved_at"].startswith("2026-10-20T20:01:44")


def test_parse_saved_name_plain_separators_and_copy_suffix():
    info = job_csv.parse_saved_name("Senior AI Engineer | CI Financial | LinkedIn (10_20_2026 8:11:04 PM) (1).html")
    assert info["title"].startswith("Senior AI Engineer")


def test_clean_job_url_canonicalises():
    assert job_csv.clean_job_url("https://www.linkedin.com/jobs/view/4480430754/?trk=x") == "https://www.linkedin.com/jobs/view/4480430754/"
    assert job_csv.clean_job_url("nonsense") == "nonsense"


def test_html_to_text_keeps_structure():
    text = job_csv.html_to_text("<p>Hello <strong>world</strong></p><ul><li>One</li><li>Two</li></ul>")
    assert "Hello world" in text
    assert "- One" in text and "- Two" in text


LOGGED_IN_PAGE = """<html><head><title>AI Engineer | GEI Consultants, Inc. | LinkedIn</title></head><body>
<a href="/jobs/view/4399999999/?trk=abc">apply</a>
<div><span>New York, NY (Hybrid)</span></div>
<div><span>2 weeks ago</span></div>
<div><span>Over 100 applicants</span></div>
<div><span>Full-time</span></div>
<h2>About the job</h2>
<div data-testid="expandable-text-box"><p>Build <strong>AI</strong> systems.</p><ul><li>Python</li></ul></div>
<h2>Requirements</h2>
</body></html>"""


def test_parse_job_html_logged_in_layout():
    record = job_csv.parse_job_html(LOGGED_IN_PAGE, "fixture.html")
    assert record["title"] == "AI Engineer"
    assert record["company"] == "GEI Consultants, Inc."
    assert record["job_id"] == "4399999999"
    assert record["job_url"].endswith("/jobs/view/4399999999/")
    assert "Build AI systems." in record["description"]
    assert "Requirements" not in record["description"]
    assert record["location"].startswith("New York")
    assert record["posted_text"] == "2 weeks ago"


GUEST_PAGE = """<html><head><title>Senior AI Engineer | CI Financial | LinkedIn</title></head><body>
<a href="https://www.linkedin.com/jobs/view/4481111111/">x</a>
<div class="show-more-less-html__markup">Lead <b>ML</b> platforms.</div>
</body></html>"""


def test_parse_job_html_guest_fallback_description():
    record = job_csv.parse_job_html(GUEST_PAGE)
    assert record["job_id"] == "4481111111"
    assert "Lead ML platforms." in record["description"]


def test_export_jobs_csv_bom_crlf_and_formula_guard(tmp_path):
    jobs = [{
        "title": "=EVIL()", "company": "Ünïcode GmbH", "location": "New York, NY",
        "job_url": "https://www.linkedin.com/jobs/view/1/", "description": "line1\nline2",
    }]
    path = tmp_path / "jobs.csv"
    info = job_csv.write_jobs_csv(jobs, path)
    assert info["rows"] == 1
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # utf-8-sig BOM
    text = raw.decode("utf-8-sig")
    assert "\r\n" in text
    assert "'=EVIL()" in text
    rows = list(csv.DictReader(io.StringIO(text)))
    assert rows[0]["company"] == "Ünïcode GmbH"
    assert "line1" in rows[0]["description"]


# ---- data_entry -------------------------------------------------------------------------

def test_parse_label_required_flag():
    assert data_entry.parse_label(" Is there a year? If yes, what is the year of the first date?*") == (
        "Is there a year? If yes, what is the year of the first date?", True)
    assert data_entry.parse_label("Name") == ("Name", False)


def test_normalize_value_strips_pasted_quotes_and_number_commas():
    assert data_entry.normalize_value('"Heinz') == "Heinz"
    assert data_entry.normalize_value(" 336,70 ", kind="number") == "336.70"
    assert data_entry.normalize_value("0,00", kind="number") == "0.00"


def test_hint_allows_charset_literals():
    assert data_entry.hint_allows("Jürgen", "äöü")
    assert not data_entry.hint_allows("Jørgen", "äöü")
    assert not data_entry.hint_allows("H4cker", "äöü")
    assert data_entry.hint_allows("H4cker", "")


def test_compare_values_verdicts():
    assert data_entry.compare_values("Jürgen", "Jürgen") == "exact"
    assert data_entry.compare_values("Jürgen", "JÜRGEN") == "case"
    assert data_entry.compare_values("336,70", "336.7", kind="number") == "numeric"
    assert data_entry.compare_values("X9944", "X9944") == "exact"
    assert data_entry.compare_values("ABC", "XYZ") == "mismatch"


def test_validate_submission_reports_missing_and_mismatch():
    records = [
        {"external_id": "e1", "label": "Name", "value": "Jürgen", "required": True, "hint": "äöü"},
        {"external_id": "e2", "label": "Year", "value": "1998", "required": True},
    ]
    submission = {"e1": "jurgen", "e2": ""}
    result = data_entry.validate_submission(records, submission)
    assert result["ok"] is False
    joined = " | ".join(result["problems"])
    assert "Name" in joined
    assert "Year" in joined


def test_validate_field_accepts_case_and_reports_warning():
    record = {"external_id": "e1", "label": "Name", "value": "Jürgen", "required": True}
    result = data_entry.validate_field(record, "JÜRGEN")
    assert result["ok"] is True
    assert result["warnings"]


FIELD_WIDGET = """<div id="verify" data-type="crowdCigar" data-mode="field"><div id="job-table">
<div class="input-data" data-external-id="ext-1" data-id="id-1"></div>
<div class="input-data" data-external-id="ext-2" data-id="id-2"></div>
<div class="vf-field"><img src="data:image/jpeg;base64,QUJD"><label>Name [äöü]</label><input type="text" value="Jürgen"></div>
<div class="vf-field"><img src="data:image/jpeg;base64,REVG"><label>City</label><input type="text" value="Köln"></div>
</div></div>"""


def test_parse_widget_field_mode():
    widget = data_entry.parse_widget(FIELD_WIDGET)
    assert widget["mode"] == "field"
    assert [record["external_id"] for record in widget["records"]] == ["ext-1", "ext-2"]
    assert widget["records"][0]["label"] == "Name"
    assert widget["records"][0]["hint"] == "äöü"
    assert widget["records"][0]["value"] == "Jürgen"
    assert len(widget["images"]) == 2


PAGE_WIDGET = """<div id="verify" data-mode="page">
<div class="input-data" data-external-id="p1" data-id="i1"></div>
<div class="vf-field"><label> 1</label><input type="text" value="0,00"></div>
<div class="vf-field"><label> 2*</label><input type="text" value="X9944"></div>
</div>"""


def test_parse_widget_page_mode_pairs_by_index():
    widget = data_entry.parse_widget(PAGE_WIDGET)
    assert widget["mode"] == "page"
    assert len(widget["records"]) == 2
    assert widget["records"][1]["required"] is True
    assert widget["records"][1]["value"] == "X9944"


def test_parse_task_page_reads_job_label_and_widget():
    srcdoc = FIELD_WIDGET.replace('"', "&quot;")
    page = f"""<html><body><label>Job</label><div>TTV-Data Entry from images (E502-1033) (1ad3cd71ad82)</div>
<div>You will earn $0.03000</div><div>Task Service Fee $0.003</div>
<iframe srcdoc="{srcdoc}"></iframe></body></html>"""
    parsed = data_entry.parse_task_page(page)
    assert "TTV-Data Entry from images" in parsed["job_label"]
    assert parsed["earn"].startswith("You will earn")
    assert "data-mode" in parsed["widget_html"]


def test_map_record_ids_and_save_field_images(tmp_path):
    records = [
        {"external_id": "e1", "id": "i1", "image_b64": base64.b64encode(b"fake-jpeg").decode()},
        {"external_id": "e2", "id": "i2", "image_b64": "!!not-base64!!"},
    ]
    assert data_entry.map_record_ids(records) == {"e1": "i1", "e2": "i2"}
    saved = data_entry.save_field_images(records, tmp_path)
    assert saved["count"] == 1
    assert saved["errors"]


# ---- real fixtures (skipped when the collected HTML folder is absent) --------------------

@pytest.mark.skipif(not (FIXTURES / "Data Entry, Youtube and Linkedin html files").is_dir(),
                    reason="collected fixture HTML files not present")
def test_real_linkedin_fixtures_parse():
    folder = FIXTURES / "Data Entry, Youtube and Linkedin html files" / "Linkedin templates"
    jobs, errors = job_csv.jobs_from_directory(folder)
    assert not errors, errors
    assert len(jobs) >= 2
    for job in jobs:
        assert job["title"]
        assert job["company"]
        assert job["job_id"]
        assert len(job["description"]) > 100


@pytest.mark.skipif(not (FIXTURES / "Data Entry, Youtube and Linkedin html files").is_dir(),
                    reason="collected fixture HTML files not present")
def test_real_microworkers_fixtures_parse():
    folder = FIXTURES / "Data Entry, Youtube and Linkedin html files" / "Microworkers Data Entry"
    files = sorted(folder.glob("*.html"))
    assert files
    for path in files:
        page = data_entry.parse_task_page(path.read_text(encoding="utf-8", errors="replace"))
        widget = data_entry.parse_widget(page["widget_html"])
        assert widget["mode"] in {"field", "page"}
        assert len(widget["pairs"]) >= 5, path.name
