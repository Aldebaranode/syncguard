"""Tests for the tracked-change XML primitives."""

from lxml import etree

from docx_builder import R_NS, W_NS
from redline.markup import Segment
from redline.tracked import (
    RevisionMarks,
    append_inserted_runs,
    build_inserted_paragraph,
    copied_paragraph_properties,
    delete_runs,
    first_text_run_properties,
    mark_paragraph_deleted,
    styled_paragraph_properties,
    track_property_change,
)

DATE = "2026-10-05T08:00:00Z"
AUTHOR = "Syahid Nurrohim"
NSMAP = {"w": W_NS, "r": R_NS}


def _p(inner: str) -> etree._Element:
    """Parse one w:p from inner XML."""
    return etree.fromstring(f'<w:p xmlns:w="{W_NS}" xmlns:r="{R_NS}">{inner}</w:p>')


def _marks(first_id: int = 100) -> RevisionMarks:
    """Revision marks with a fixed author, date and first id."""
    return RevisionMarks(AUTHOR, DATE, first_id)


def _tags(element: etree._Element) -> list[str]:
    """Local names of an element's children."""
    return [etree.QName(child).localname for child in element]


def _xpath(element: etree._Element, path: str) -> list:
    """Run an XPath with the w and r prefixes bound."""
    return element.xpath(path, namespaces=NSMAP)


def test_delete_runs_wraps_text_runs_including_hyperlink_runs():
    """Every run with content goes into w:del; w:t becomes w:delText; hyperlinks keep their r:id."""
    p = _p(
        "<w:pPr><w:jc w:val='both'/></w:pPr>"
        "<w:r><w:t>Satu</w:t></w:r><w:r><w:rPr><w:i/></w:rPr><w:t xml:space='preserve'> dua</w:t></w:r>"
        "<w:hyperlink r:id='rId9'><w:r><w:t>[1]</w:t></w:r></w:hyperlink>"
        "<w:r><w:instrText>PAGE</w:instrText></w:r>"
    )
    delete_runs(p, _marks())
    assert _tags(p) == ["pPr", "del", "hyperlink", "del"]
    assert _xpath(p, ".//w:t") == []
    assert _xpath(p, ".//w:delText/text()") == ["Satu", " dua", "[1]"]
    assert _xpath(p, "./w:del[1]/w:r/w:delText/@xml:space") == ["preserve"]
    assert _xpath(p, "./w:hyperlink/@r:id") == ["rId9"]
    assert _xpath(p, "./w:hyperlink/w:del/w:r/w:delText/text()") == ["[1]"]
    assert _xpath(p, ".//w:delInstrText/text()") == ["PAGE"]
    first_del = p.find(f"{{{W_NS}}}del")
    assert first_del.get(f"{{{W_NS}}}author") == AUTHOR
    assert first_del.get(f"{{{W_NS}}}date") == DATE
    assert first_del.get(f"{{{W_NS}}}id") == "100"


def test_page_break_run_is_kept_and_new_runs_go_before_it():
    """A run holding only a page break is layout, not text: it stays, after the inserted text."""
    p = _p("<w:r><w:t>Keyword: lama</w:t></w:r><w:r><w:br w:type='page'/></w:r>")
    marks = _marks()
    insert_point = delete_runs(p, marks)
    append_inserted_runs(p, (Segment("Keyword: baru"),), None, marks, after=insert_point)
    assert _tags(p) == ["del", "ins", "r"]
    assert _xpath(p, "./w:r/w:br/@w:type") == ["page"]
    assert _xpath(p, "./w:ins/w:r/w:t/text()") == ["Keyword: baru"]


def test_inserted_runs_copy_base_properties_and_add_flags_in_schema_order():
    """Italic and bold go into the copied w:rPr at their schema positions, without duplicates."""
    p = _p("<w:r><w:rPr><w:rFonts w:ascii='Cardo'/><w:b/><w:color w:val='000000'/><w:sz w:val='20'/></w:rPr><w:t>x</w:t></w:r>")
    base = first_text_run_properties(p)
    marks = _marks()
    append_inserted_runs(p, (Segment("miring", italic=True), Segment(" tebal ", bold=True)), base, marks)
    runs = _xpath(p, "./w:ins/w:r")
    assert _tags(runs[0].find(f"{{{W_NS}}}rPr")) == ["rFonts", "b", "i", "iCs", "color", "sz"]
    assert _tags(runs[1].find(f"{{{W_NS}}}rPr")) == ["rFonts", "b", "bCs", "color", "sz"]
    assert _xpath(p, "./w:ins/w:r/w:t/@xml:space") == ["preserve", "preserve"]
    assert _xpath(p, "./w:ins/w:r/w:t/text()") == ["miring", " tebal "]
    assert marks.inserted_count == 1


def test_inserted_paragraph_copies_anchor_properties_and_tracks_its_mark():
    """pPr is copied without sectPr; the mark gets w:ins first in pPr/rPr; runs sit in one w:ins."""
    anchor = _p(
        "<w:pPr><w:pStyle w:val='Normal'/><w:numPr><w:ilvl w:val='0'/><w:numId w:val='2'/></w:numPr>"
        "<w:rPr><w:b/></w:rPr><w:sectPr/></w:pPr><w:r><w:t>Anchor</w:t></w:r>"
    )
    marks = _marks()
    new = build_inserted_paragraph(copied_paragraph_properties(anchor), (Segment("Baru"),), None, marks)
    assert _tags(new.find(f"{{{W_NS}}}pPr")) == ["pStyle", "numPr", "rPr"]
    assert _tags(new.find(f"{{{W_NS}}}pPr/{{{W_NS}}}rPr")) == ["ins", "b"]
    assert _tags(new) == ["pPr", "ins"]
    assert _xpath(new, "./w:ins/w:r/w:t/text()") == ["Baru"]
    assert marks.inserted_count == 2


def test_styled_paragraph_properties_hold_only_the_style():
    """With a Style, the new paragraph's pPr is just the pStyle (plus the tracked mark)."""
    new = build_inserted_paragraph(styled_paragraph_properties("Heading3"), (Segment("2.2.6 Judul"),), None, _marks())
    assert _xpath(new, "./w:pPr/w:pStyle/@w:val") == ["Heading3"]
    assert _tags(new.find(f"{{{W_NS}}}pPr")) == ["pStyle", "rPr"]


def test_copied_properties_drop_revision_markers_of_the_anchor():
    """An anchor whose mark an earlier edit deleted does not pass that marker on."""
    anchor = _p("<w:pPr><w:rPr><w:del w:id='5' w:author='a'/><w:i/></w:rPr></w:pPr>")
    copied = copied_paragraph_properties(anchor)
    assert _tags(copied.find(f"{{{W_NS}}}rPr")) == ["i"]


def test_paragraph_mark_deletion_goes_first_in_rpr():
    """w:del is the first child of pPr/rPr; a missing pPr or rPr is created."""
    with_rpr = _p("<w:pPr><w:jc w:val='both'/><w:rPr><w:b/></w:rPr></w:pPr><w:r><w:t>x</w:t></w:r>")
    mark_paragraph_deleted(with_rpr, _marks())
    assert _tags(with_rpr.find(f"{{{W_NS}}}pPr/{{{W_NS}}}rPr")) == ["del", "b"]
    bare = _p("<w:r><w:t>x</w:t></w:r>")
    mark_paragraph_deleted(bare, _marks())
    assert _tags(bare) == ["pPr", "r"]
    assert _tags(bare.find(f"{{{W_NS}}}pPr/{{{W_NS}}}rPr")) == ["del"]


def test_ids_start_after_the_highest_existing_id_and_never_repeat():
    """One counter for the document, above every w:id already present (bookmarks, comments)."""
    document = etree.fromstring(
        f'<w:document xmlns:w="{W_NS}"><w:body><w:p><w:bookmarkStart w:id="41" w:name="b"/>'
        '<w:bookmarkEnd w:id="41"/></w:p></w:body></w:document>'
    )
    marks = RevisionMarks.for_document(document, AUTHOR, DATE)
    ids = [marks.insertion().get(f"{{{W_NS}}}id"), marks.deletion().get(f"{{{W_NS}}}id"), marks.insertion().get(f"{{{W_NS}}}id")]
    assert ids == ["42", "43", "44"]
    assert (marks.inserted_count, marks.deleted_count) == (2, 1)


def _properties(tag: str, inner: str) -> etree._Element:
    """Parse one w:rPr, w:pPr or w:trPr element."""
    return etree.fromstring(f'<w:{tag} xmlns:w="{W_NS}">{inner}</w:{tag}>')


def test_run_property_change_records_the_previous_properties_last():
    """rPrChange holds a copy of the old rPr, carries id, author and date, and is the last child."""
    marks = _marks()
    properties = _properties("rPr", "<w:b/><w:i/>")
    previous = _properties("rPr", "<w:b/>")
    assert track_property_change(properties, "rPrChange", previous, marks) is True
    assert _tags(properties) == ["b", "i", "rPrChange"]
    change = properties[-1]
    assert [change.get(f"{{{W_NS}}}id"), change.get(f"{{{W_NS}}}author"), change.get(f"{{{W_NS}}}date")] == ["100", AUTHOR, DATE]
    assert _tags(change) == ["rPr"] and _tags(change[0]) == ["b"]
    assert marks.property_changed_count == 1


def test_property_change_of_nothing_holds_an_empty_original():
    """A run that had no rPr gets an rPrChange with an empty w:rPr."""
    properties = _properties("rPr", "<w:i/>")
    track_property_change(properties, "rPrChange", None, _marks())
    assert _tags(properties[-1]) == ["rPr"] and len(properties[-1][0]) == 0


def test_property_change_strips_what_the_original_may_not_hold():
    """The stored original has no revision markers, paragraph mark, section or earlier change."""
    properties = _properties("pPr", "<w:numPr/>")
    previous = _properties("pPr", '<w:jc w:val="both"/><w:rPr><w:b/></w:rPr><w:pPrChange w:id="1"/>')
    track_property_change(properties, "pPrChange", previous, _marks())
    assert _tags(properties[-1][0]) == ["jc"]


def test_property_change_is_refused_when_one_is_already_recorded():
    """A properties element holds at most one change; the second request changes nothing."""
    properties = _properties("rPr", "<w:i/>")
    assert track_property_change(properties, "rPrChange", None, _marks()) is True
    assert track_property_change(properties, "rPrChange", None, _marks()) is False
    assert _tags(properties) == ["i", "rPrChange"]
