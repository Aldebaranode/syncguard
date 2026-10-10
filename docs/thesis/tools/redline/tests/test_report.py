"""Tests for the Markdown reports."""

from lxml import etree

from redline.apply import EditOutcome, Problem, PropertyChange
from redline.fill import FillResult
from redline.matcher import IndexedParagraph
from redline.report import RunInfo, render_edit_report, render_fill_report
from redline.validation import ValidationResult

ELEMENT = etree.Element("p")


def _paragraph(number: int, text: str, in_table: bool = False) -> IndexedParagraph:
    """An indexed paragraph stand-in for report rendering."""
    return IndexedParagraph(ELEMENT, text, number, in_table)


def _outcomes() -> list[EditOutcome]:
    """One applied edit, one anchor failure with a table-row anchor, one style error, one inserted-text match."""
    applied = EditOutcome(
        "E-1-01", "/d/A.md", 13, "replace", matched=_paragraph(5, "Blockchain merupakan teknologi " * 4), inserted=1, deleted=1,
        warnings=["Style 'Heading 3' resolves to style id 'Heading3', but the paragraph has 'Normal'; paragraph properties kept"],
    )
    failed = EditOutcome(
        "E-II-21",
        "/d/B.md",
        166,
        "delete",
        problems=[Problem("match", "no paragraph matches the anchor; the anchor looks like a table row ('cell | cell')", (_paragraph(88, "Peneliti / Tahun", True),))],
    )
    styled = EditOutcome("E-II-30", "/d/B.md", 200, "insert_after", problems=[Problem("style", "paragraph style 'List' is not defined")], matched=_paragraph(90, "Jangkar"))
    chained = EditOutcome("E-1-09", "/d/A.md", 40, "insert_after", matched=_paragraph(0, "Paragraf sisipan"), matched_inserted_by="E-1-08", inserted=2)
    return [applied, chained, failed, styled]


def _info(command: str) -> RunInfo:
    """Run details for a report."""
    return RunInfo(command, "/m/in.docx", ["/d/A.md", "/d/B.md"], "Syahid Nurrohim", "2026-10-05T08:00:00Z", "/tmp/out.docx")


def test_summary_counts_per_file_and_total():
    """Each edit file gets a row with edits, applied, anchor failures, other errors and revisions."""
    report = render_edit_report(_info("apply"), _outcomes(), None)
    assert "| A.md | 2 | 2 | 0 | 0 | 1 | 3 | 1 |" in report
    assert "| B.md | 2 | 0 | 1 | 1 | 0 | 0 | 0 |" in report
    assert "| Total | 4 | 2 | 1 | 1 | 1 | 3 | 1 |" in report


def test_anchor_failures_name_the_edit_reason_and_candidates_with_pipes_escaped():
    """A failing anchor lists edit id, location, reason and candidate paragraphs."""
    report = render_edit_report(_info("check"), _outcomes(), None)
    assert "| E-II-21 | B.md:166 | no paragraph matches the anchor; the anchor looks like a table row ('cell \\| cell') | #88 (table): Peneliti / Tahun |" in report
    assert "| E-II-30 | B.md:200 | style | paragraph style 'List' is not defined |" in report


def test_edit_rows_show_status_excerpt_and_inserted_text_matches():
    """Applied edits show the matched paragraph; matches in inserted text say which edit inserted it."""
    report = render_edit_report(_info("apply"), _outcomes(), None)
    assert "| E-1-01 | A.md:13 | replace | applied | #5: Blockchain merupakan teknologi Blockchain merupakan teknologi Blockcha... | 1 | 1 | warning |" in report
    assert "| E-1-09 | A.md:40 | insert_after | applied | inserted by E-1-08: Paragraf sisipan | 2 | 0 | anchor found only in text inserted by E-1-08 |" in report


def test_check_report_says_ok_instead_of_applied_and_has_no_validation_section():
    """Check mode writes nothing, so it reports matches as ok."""
    report = render_edit_report(_info("check"), _outcomes(), None)
    assert report.startswith("# Redline check report")
    assert "| E-1-01 | A.md:13 | replace | ok |" in report
    assert "validate.py" not in report


def test_validation_section_reports_pass_fail_and_not_run():
    """The validate.py verdict and its output are included in apply reports."""
    passed = ValidationResult(True, True, 0, "python validate.py out --original in --author A", "All validations PASSED!")
    report = render_edit_report(_info("apply"), _outcomes(), passed)
    assert "## validate.py" in report
    assert "Result: PASSED (exit 0)" in report
    assert "All validations PASSED!" in report
    not_run = ValidationResult(False, False, None, "python validate.py", "validate.py not found at /x")
    assert "Result: NOT RUN" in render_edit_report(_info("apply"), _outcomes(), not_run)


def test_fill_report_lists_filled_missing_and_unused():
    """The fill report names tokens without values and values without tokens."""
    result = FillResult({"S4.n": 2}, {"soak.availability": 1}, ["unused.key"], {"S4.n": 1})
    report = render_fill_report("/m/in.docx", "/v.json", "/m/out.docx", result)
    assert "| S4.n | 2 |" in report
    assert "## Tokens without values\n\n| Token | Occurrences |\n|---|---|\n| soak.availability | 1 |" in report
    assert "## Values without tokens\n\n- unused.key" in report
    assert "## Tokens outside tracked insertions (not filled)\n\n| Token | Occurrences |\n|---|---|\n| S4.n | 1 |" in report


def test_reports_contain_no_em_dash():
    """Reports never contain the em-dash character."""
    report = render_edit_report(_info("apply"), _outcomes(), None)
    assert "\u2014" not in report


def test_a_replace_whose_paragraph_properties_changed_carries_a_ppr_changed_note():
    """The controller reviews every replace whose style or list numbering changed; the note names both."""
    heading = EditOutcome(
        "E-II-27", "/d/B.md", 210, "replace", matched=_paragraph(154, "Proof of Stake (PoS)"), inserted=1, deleted=1,
        property_change=PropertyChange("Normal", "12", "Heading3", None),
    )
    plain = EditOutcome("E-II-28", "/d/B.md", 220, "replace", matched=_paragraph(155, "Teks."), inserted=1, deleted=1)
    report = render_edit_report(_info("apply"), [heading, plain], None)
    assert "| E-II-27 | B.md:210 | replace | applied | #154: Proof of Stake (PoS) | 1 | 1 | pPr changed (style Normal to Heading3; numId 12 to none) |" in report
    assert "| E-II-28 | B.md:220 | replace | applied | #155: Teks. | 1 | 1 |  |" in report


def test_warnings_are_listed_per_edit():
    """Warnings get their own section with edit id and location."""
    report = render_edit_report(_info("apply"), _outcomes(), None)
    assert "## Warnings\n\n| Edit | Location | Warning |\n|---|---|---|\n| E-1-01 | A.md:13 | Style 'Heading 3' resolves" in report
