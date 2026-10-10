"""Tests for removing a hard page break that sits right before a next-page section break."""

from lxml import etree

from docx_builder import R_NS, W_NS, para
from redline.breaks import remove_redundant_page_breaks
from redline.tracked import RevisionMarks

NSMAP = {"w": W_NS}
DATE = "2026-10-05T08:00:00Z"
BREAK = '<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:br w:type="page"/></w:r></w:p>'
NEXT_PAGE = '<w:p><w:pPr><w:sectPr><w:type w:val="nextPage"/><w:pgSz w:w="16834" w:h="11909"/></w:sectPr></w:pPr></w:p>'
NO_TYPE = '<w:p><w:pPr><w:sectPr><w:pgSz w:w="16834" w:h="11909"/></w:sectPr></w:pPr></w:p>'
CONTINUOUS = '<w:p><w:pPr><w:sectPr><w:type w:val="continuous"/></w:sectPr></w:pPr></w:p>'


def _run(body: str):
    """Run the pass on a body; return the result and the body."""
    root = etree.fromstring(f'<w:body xmlns:w="{W_NS}" xmlns:r="{R_NS}">{body}</w:body>')
    return remove_redundant_page_breaks(root, RevisionMarks("Syahid Nurrohim", DATE, 100)), root


def test_a_page_break_before_a_next_page_section_break_is_deleted_as_a_tracked_change():
    """The break run is wrapped in w:del; the paragraph and the section break stay."""
    result, body = _run(para("Tabel 2.1 baris akhir") + BREAK + NEXT_PAGE + para("Setelah"))
    assert len(result.removals) == 1
    assert body.xpath("count(./w:p[2]/w:del/w:r/w:br[@w:type='page'])", namespaces=NSMAP) == 1
    assert body.xpath("count(./w:p[2]/w:r)", namespaces=NSMAP) == 0
    assert body.xpath("./w:p[2]/w:del/@w:author", namespaces=NSMAP) == ["Syahid Nurrohim"]
    assert body.xpath("count(./w:p[3]/w:pPr/w:sectPr)", namespaces=NSMAP) == 1


def test_a_section_break_without_a_type_counts_as_next_page():
    """OOXML defaults the section type to nextPage."""
    result, _ = _run(BREAK + NO_TYPE)
    assert len(result.removals) == 1


def test_the_removal_reports_the_text_around_it():
    """The previous non-empty paragraph and the next non-empty paragraph are named."""
    result, _ = _run(para("Sebelum itu") + BREAK + NEXT_PAGE + para("Sesudah itu"))
    assert (result.removals[0].before, result.removals[0].after) == ("Sebelum itu", "Sesudah itu")


def test_a_page_break_before_an_ordinary_paragraph_stays():
    """Only the pattern break plus section break is touched."""
    result, body = _run(para("A") + BREAK + para("BAB berikutnya"))
    assert result.removals == [] and body.xpath("count(//w:del)", namespaces=NSMAP) == 0


def test_a_continuous_section_break_and_a_break_with_text_stay():
    """A continuous section break starts no page, and a paragraph with text besides the break is not only a break."""
    with_text = '<w:p><w:r><w:t>teks</w:t></w:r><w:r><w:br w:type="page"/></w:r></w:p>'
    for body in (BREAK + CONTINUOUS, with_text + NEXT_PAGE):
        result, _ = _run(body)
        assert result.removals == []


def test_the_pass_is_idempotent():
    """A break that is already a tracked deletion is not deleted again."""
    result, body = _run(BREAK + NEXT_PAGE)
    again = remove_redundant_page_breaks(body, RevisionMarks("Syahid Nurrohim", DATE, 200))
    assert len(result.removals) == 1 and again.removals == []
