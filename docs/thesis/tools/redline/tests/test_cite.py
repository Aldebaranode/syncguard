"""Tests for citation renumbering and the rebuilt reference list."""

from lxml import etree

from docx_builder import R_NS, W_NS, para
from redline.cite import cite_document, format_labels
from redline.matcher import paragraph_text
from redline.registry import Record, Registry
from redline.tracked import RevisionMarks

NSMAP = {"w": W_NS, "r": R_NS}
INSERTED = (
    '<w:p><w:pPr><w:rPr><w:ins w:id="1" w:author="A" w:date="2026-10-05T08:00:00Z"/></w:rPr></w:pPr>'
    '<w:ins w:id="2" w:author="A" w:date="2026-10-05T08:00:00Z"><w:r><w:t xml:space="preserve">Sisipan [REF:tech1] dan [1, 3].</w:t></w:r></w:ins></w:p>'
)
HYPERLINKED = (
    '<w:p><w:r><w:t xml:space="preserve">Lihat </w:t></w:r><w:hyperlink r:id="rId9"><w:r><w:t>[1]</w:t></w:r></w:hyperlink>'
    "<w:r><w:t>.</w:t></w:r></w:p>"
)
DELETED = '<w:p><w:r><w:t xml:space="preserve">Tetap </w:t></w:r><w:del w:id="3" w:author="A"><w:r><w:delText>[9]</w:delText></w:r></w:del></w:p>'
REFERENCE_LIST = (
    '<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:rPr><w:b/></w:rPr><w:t>REFERENSI</w:t></w:r></w:p>'
    '<w:p><w:pPr><w:ind w:left="640" w:hanging="640"/></w:pPr><w:r><w:t>[1]</w:t></w:r><w:r><w:tab/><w:t>Entri lama satu.</w:t></w:r></w:p>'
    '<w:p><w:pPr><w:ind w:left="640" w:hanging="640"/></w:pPr><w:r><w:t>[3]</w:t></w:r><w:r><w:tab/><w:t>Entri lama tiga.</w:t></w:r></w:p>'
)


def _record(record_id: str, aliases: list[str], record_type: str = "peer_reviewed", year: int | None = 2023, technical: bool = False) -> Record:
    """A registry record with an italic journal name in its entry."""
    return Record(record_id, f"A. Penulis, \"Judul {record_id},\" *Jurnal {record_id}*, {year}.", record_type, year, technical, aliases)


REGISTRY = Registry(
    [
        _record("r1", ["1"], year=2019),
        _record("r3", ["3"]),
        _record("r4", ["4"], record_type="standard", year=2022),
        _record("keyB", ["keyB"], record_type="web", year=None),
        _record("tech1", ["tech1"], record_type="repository", year=None, technical=True),
        _record("unused", ["7"]),
    ]
)


def _document(body: str) -> etree._Element:
    """A w:document with the given body XML."""
    return etree.fromstring(f'<w:document xmlns:w="{W_NS}" xmlns:r="{R_NS}"><w:body>{body}</w:body></w:document>')


def _cite(body: str):
    """Run cite on a fixture body; return the document, the result and the marks."""
    document = _document(body)
    marks = RevisionMarks("Syahid Nurrohim", "2026-10-05T08:00:00Z", 100)
    return document, cite_document(document, REGISTRY, marks), marks


def _paragraphs(document: etree._Element) -> list[etree._Element]:
    """Top-level body paragraphs."""
    return document.find(f"{{{W_NS}}}body").findall(f"{{{W_NS}}}p")


FULL = para("Teks pertama [3] dan [REF:keyB].") + HYPERLINKED + INSERTED + para("Rentang [3]-[4].") + DELETED + REFERENCE_LIST


def test_markers_are_numbered_by_first_appearance_and_technical_ones_get_t_labels():
    """Main records count 1, 2, 3 in reading order; technical records count T1, T2; the list itself is skipped."""
    _, result, _ = _cite(FULL)
    assert result.labels == {"r3": "[1]", "keyB": "[2]", "r1": "[3]", "tech1": "[T1]", "r4": "[4]"}
    assert result.never_cited == ["unused"]
    assert result.heading == "REFERENSI"


def test_visible_text_after_rewriting_carries_the_new_numbers():
    """Every marker reads its new label in the accepted text; deleted text is left alone."""
    document, _, _ = _cite(FULL)
    texts = [paragraph_text(p) for p in _paragraphs(document)[:5]]
    assert texts == ["Teks pertama [1] dan [2].", "Lihat [3].", "Sisipan [T1] dan [1], [3].", "Rentang [1], [4].", "Tetap"]


def test_markers_in_original_text_become_tracked_deletions_and_insertions_inside_their_hyperlink():
    """Original markers are rewritten as w:del plus w:ins; a Zotero hyperlink keeps both inside it."""
    document, result, _ = _cite(FULL)
    first, linked = _paragraphs(document)[:2]
    assert first.xpath("./w:del/w:r/w:delText/text()", namespaces=NSMAP) == ["[3]", "[REF:keyB]"]
    assert first.xpath("./w:ins/w:r/w:t/text()", namespaces=NSMAP) == ["[1]", "[2]"]
    assert linked.xpath("./w:hyperlink/@r:id", namespaces=NSMAP) == ["rId9"]
    assert linked.xpath("./w:hyperlink/w:del/w:r/w:delText/text()", namespaces=NSMAP) == ["[1]"]
    assert linked.xpath("./w:hyperlink/w:ins/w:r/w:t/text()", namespaces=NSMAP) == ["[3]"]
    assert (result.rewritten_tracked, result.rewritten_in_insertions, result.unchanged) == (4, 2, 0)


def test_markers_inside_tracked_insertions_are_rewritten_in_place():
    """Text inserted by the edits is still one insertion: the marker text changes inside it."""
    document, _, _ = _cite(FULL)
    inserted = _paragraphs(document)[2]
    assert inserted.xpath(".//w:del", namespaces=NSMAP) == []
    assert inserted.xpath("./w:ins/w:r/w:t/text()", namespaces=NSMAP) == ["Sisipan [T1] dan [1], [3]."]


def test_a_marker_split_over_two_runs_is_rewritten_as_one_change():
    """'[' and '3]' in separate runs are both deleted and one new marker is inserted."""
    body = '<w:p><w:r><w:t xml:space="preserve">Lihat [</w:t></w:r><w:r><w:t xml:space="preserve">3] juga</w:t></w:r></w:p>' + REFERENCE_LIST
    document, _, _ = _cite(body)
    first = _paragraphs(document)[0]
    assert paragraph_text(first) == "Lihat [1] juga"
    assert first.xpath(".//w:delText/text()", namespaces=NSMAP) == ["[", "3]"]
    assert first.xpath(".//w:ins/w:r/w:t/text()", namespaces=NSMAP) == ["[1]"]


def test_unresolved_markers_are_errors_and_stay_unchanged():
    """A number no record carries is reported and not rewritten."""
    document, result, _ = _cite(para("Angka [99] dan [3].") + REFERENCE_LIST)
    assert [marker for marker, _ in result.unresolved] == ["[99]"]
    assert paragraph_text(_paragraphs(document)[0]) == "Angka [99] dan [1]."


def test_reference_list_is_replaced_and_technical_sources_get_their_own_section():
    """Old entries are deleted (runs and marks); the new list follows in order, then SUMBER TEKNIS."""
    document, result, _ = _cite(FULL)
    paragraphs = _paragraphs(document)
    heading_index = [paragraph_text(p) for p in paragraphs].index("REFERENSI")
    old_entries = paragraphs[heading_index + 1 : heading_index + 3]
    assert all(p.xpath("./w:pPr/w:rPr/w:del", namespaces=NSMAP) for p in old_entries)
    new_texts = [paragraph_text(p) for p in paragraphs[heading_index + 3 :]]
    assert new_texts == [
        '[1] A. Penulis, "Judul r3," Jurnal r3, 2023.',
        '[2] A. Penulis, "Judul keyB," Jurnal keyB, None.',
        '[3] A. Penulis, "Judul r1," Jurnal r1, 2019.',
        '[4] A. Penulis, "Judul r4," Jurnal r4, 2022.',
        "SUMBER TEKNIS",
        '[T1] A. Penulis, "Judul tech1," Jurnal tech1, None.',
    ]
    first_new = paragraphs[heading_index + 3]
    assert first_new.xpath("./w:pPr/w:ind/@w:hanging", namespaces=NSMAP) == ["640"]
    assert first_new.xpath("./w:ins/w:r/w:rPr/w:i", namespaces=NSMAP) != []
    technical_heading = paragraphs[heading_index + 7]
    assert technical_heading.xpath("./w:pPr/w:jc/@w:val", namespaces=NSMAP) == ["center"]
    assert technical_heading.xpath("./w:ins/w:r/w:rPr/w:b", namespaces=NSMAP) != []


def test_composition_of_the_main_list():
    """Total, peer-reviewed and 2021 to 2026 counts cover the numbered list only."""
    _, result, _ = _cite(FULL)
    assert (result.composition.total, result.composition.peer_reviewed, result.composition.recent) == (4, 2, 2)


def test_missing_reference_heading_is_an_error():
    """Without a reference list heading the list cannot be rebuilt; markers are still renumbered."""
    _, result, _ = _cite(para("Teks [3]."))
    assert result.heading is None
    assert "no reference list heading" in result.errors[0]


def test_label_formatting_sorts_and_collapses_runs_of_three():
    """IEEE style: '[1], [3]'; three or more consecutive numbers become '[a]–[c]'; T labels last."""
    assert format_labels(["[3]", "[1]"]) == "[1], [3]"
    assert format_labels(["[4]", "[2]", "[3]", "[7]", "[T2]"]) == "[2]–[4], [7], [T2]"
