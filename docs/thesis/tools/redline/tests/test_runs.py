"""Tests for reading a paragraph as text pieces, splitting runs, and toggling italics with tracking."""

from lxml import etree

from docx_builder import R_NS, W_NS
from redline.runs import SEPARATOR, is_italic, isolate_text, set_italic, text_pieces
from redline.tracked import RevisionMarks

NSMAP = {"w": W_NS}
DATE = "2026-10-05T08:00:00Z"


def _p(inner: str) -> etree._Element:
    """Parse one w:p from inner XML."""
    return etree.fromstring(f'<w:p xmlns:w="{W_NS}" xmlns:r="{R_NS}">{inner}</w:p>')


def _run(text: str, rpr: str = "") -> str:
    """XML of one run with text."""
    return f"<w:r>{f'<w:rPr>{rpr}</w:rPr>' if rpr else ''}<w:t xml:space='preserve'>{text}</w:t></w:r>"


def _marks() -> RevisionMarks:
    """Marks for a fixed author."""
    return RevisionMarks("Syahid Nurrohim", DATE, 100)


def _joined(paragraph: etree._Element) -> str:
    """The w:t text of the paragraph in order."""
    return "".join(paragraph.xpath(".//w:t/text()", namespaces=NSMAP))


def test_pieces_cover_plain_runs_and_runs_inside_our_insertions():
    """Runs directly in the paragraph and inside w:ins are pieces with running offsets."""
    paragraph = _p(_run("satu ") + "<w:ins w:id='1' w:author='A'>" + _run("dua") + "</w:ins>")
    pieces = text_pieces(paragraph)
    assert [(piece.text, piece.start) for piece in pieces] == [("satu ", 0), ("dua", 5)]


def test_deleted_runs_are_invisible_and_proof_marks_are_transparent():
    """w:del content and proofErr do not break the text; the two sides join."""
    paragraph = _p(_run("dou") + "<w:proofErr w:type='spellStart'/><w:del w:id='1' w:author='A'><w:r><w:delText>xx</w:delText></w:r></w:del>" + _run("ble"))
    assert "".join(piece.text for piece in text_pieces(paragraph)) == "double"


def test_hyperlinks_fields_monospace_and_odd_runs_become_separators():
    """Content that must not be touched shows up as a separator piece (None) between text pieces."""
    paragraph = _p(
        _run("a ")
        + "<w:hyperlink><w:r><w:t>[1]</w:t></w:r></w:hyperlink>"
        + _run("b ")
        + "<w:r><w:fldChar w:fldCharType='begin'/></w:r><w:r><w:instrText>PAGE</w:instrText></w:r>"
        + "<w:r><w:fldChar w:fldCharType='separate'/></w:r>" + _run("7")
        + "<w:r><w:fldChar w:fldCharType='end'/></w:r>"
        + _run("c ", rpr="<w:rFonts w:ascii='Courier New' w:hAnsi='Courier New'/>")
        + "<w:r><w:tab/></w:r>" + _run("d")
    )
    kinds = [piece.text if piece else SEPARATOR for piece in text_pieces(paragraph, with_separators=True)]
    assert kinds == ["a ", SEPARATOR, "b "] + [SEPARATOR] * 7 + ["d"]


def test_text_pieces_without_separators_lists_only_text():
    """The default listing has the editable pieces only."""
    paragraph = _p(_run("a ") + "<w:hyperlink><w:r><w:t>[1]</w:t></w:r></w:hyperlink>" + _run("b"))
    assert [piece.text for piece in text_pieces(paragraph)] == ["a ", "b"]


def test_isolate_splits_a_run_into_three_with_equal_formatting():
    """Splitting keeps the text, copies rPr, and returns the middle run."""
    paragraph = _p(_run("sebelum failover sesudah", rpr="<w:color w:val='000000'/>"))
    run = paragraph[0]
    middle = isolate_text(run, 8, 16)
    assert _joined(paragraph) == "sebelum failover sesudah"
    assert [r.xpath("string(w:t)", namespaces=NSMAP) for r in paragraph] == ["sebelum ", "failover", " sesudah"]
    assert middle is paragraph[1]
    assert all(r.xpath("w:rPr/w:color/@w:val", namespaces=NSMAP) == ["000000"] for r in paragraph)
    assert all(r.find("w:t", NSMAP).get("{http://www.w3.org/XML/1998/namespace}space") == "preserve" for r in paragraph)


def test_isolate_a_whole_run_changes_nothing():
    """A slice that is the whole run returns the run itself."""
    paragraph = _p(_run("failover"))
    assert isolate_text(paragraph[0], 0, 8) is paragraph[0]
    assert len(paragraph) == 1


def test_isolate_keeps_the_page_break_marker_in_the_first_piece():
    """lastRenderedPageBreak stays with the leading text."""
    paragraph = _p("<w:r><w:lastRenderedPageBreak/><w:t>ab cd</w:t></w:r>")
    isolate_text(paragraph[0], 3, 5)
    assert [len(r.findall("w:lastRenderedPageBreak", NSMAP)) for r in paragraph] == [1, 0]


def test_italic_state_reads_the_direct_property():
    """w:i counts unless its value turns it off."""
    assert is_italic(_p(_run("x", rpr="<w:i/>"))[0]) is True
    assert is_italic(_p(_run("x", rpr="<w:i w:val='0'/>"))[0]) is False
    assert is_italic(_p(_run("x"))[0]) is False


def test_set_italic_on_an_original_run_records_the_previous_properties():
    """Turning italics on adds i and iCs in schema order and an rPrChange holding the old rPr."""
    paragraph = _p(_run("x", rpr="<w:b/><w:color w:val='000000'/>"))
    marks = _marks()
    assert set_italic(paragraph[0], True, marks, track=True) == "changed"
    properties = paragraph[0].find("w:rPr", NSMAP)
    assert [etree.QName(child).localname for child in properties] == ["b", "i", "iCs", "color", "rPrChange"]
    assert [etree.QName(child).localname for child in properties[-1][0]] == ["b", "color"]
    assert marks.property_changed_count == 1


def test_set_italic_on_a_run_without_properties_creates_them():
    """A bare run gets an rPr first, and the stored original is empty."""
    paragraph = _p(_run("x"))
    set_italic(paragraph[0], True, _marks(), track=True)
    assert paragraph[0][0].tag == f"{{{W_NS}}}rPr"
    assert len(paragraph[0].find("w:rPr/w:rPrChange/w:rPr", NSMAP)) == 0


def test_set_italic_off_removes_both_toggles_and_tracks_it():
    """Turning italics off removes i and iCs; the change keeps them in the original."""
    paragraph = _p(_run("x", rpr="<w:i/><w:iCs/>"))
    set_italic(paragraph[0], False, _marks(), track=True)
    properties = paragraph[0].find("w:rPr", NSMAP)
    assert [etree.QName(child).localname for child in properties] == ["rPrChange"]
    assert [etree.QName(child).localname for child in properties[0][0]] == ["i", "iCs"]


def test_set_italic_without_tracking_changes_the_run_directly():
    """Runs inside our own insertion are changed without an rPrChange."""
    paragraph = _p(_run("x", rpr="<w:b/>"))
    marks = _marks()
    set_italic(paragraph[0], True, marks, track=False)
    assert paragraph[0].find("w:rPr/w:rPrChange", NSMAP) is None
    assert marks.property_changed_count == 0


def test_set_italic_leaves_a_run_alone_when_it_already_has_a_tracked_format_change():
    """A second rPrChange would be invalid, so the run is reported as skipped."""
    paragraph = _p(_run("x", rpr="<w:b/><w:rPrChange w:id='9' w:author='B'><w:rPr/></w:rPrChange>"))
    assert set_italic(paragraph[0], True, _marks(), track=True) == "skipped"
    assert is_italic(paragraph[0]) is False


def test_set_italic_reports_unchanged_when_the_state_already_matches():
    """Nothing is touched when the run is already as wanted."""
    paragraph = _p(_run("x", rpr="<w:i/>"))
    assert set_italic(paragraph[0], True, _marks(), track=True) == "unchanged"
    assert set_italic(_p(_run("x"))[0], False, _marks(), track=True) == "unchanged"
