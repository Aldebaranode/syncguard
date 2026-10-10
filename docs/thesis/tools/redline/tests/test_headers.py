"""Tests for repeating the first row of multi-row tables as a header row."""

from lxml import etree

from docx_builder import R_NS, W_NS, para, table
from redline.headers import mark_header_rows
from redline.tracked import RevisionMarks

NSMAP = {"w": W_NS}
DATE = "2026-10-05T08:00:00Z"
AUTHOR = "Syahid Nurrohim"


def _body(inner: str) -> etree._Element:
    """A w:body holding inner XML."""
    return etree.fromstring(f'<w:body xmlns:w="{W_NS}" xmlns:r="{R_NS}">{inner}</w:body>')


def _marks() -> RevisionMarks:
    """Marks for a fixed author."""
    return RevisionMarks(AUTHOR, DATE, 100)


def _first_row_properties(body: etree._Element, number: int = 0) -> etree._Element | None:
    """The w:trPr of the first row of the number-th table."""
    return body.findall("w:tbl", NSMAP)[number].find("w:tr/w:trPr", NSMAP)


def test_an_original_table_gets_tblheader_and_a_tracked_row_property_change():
    """tblHeader is added to the first row; trPrChange holds the previous (empty) properties."""
    body = _body(table([["A", "B"], ["1", "2"]]))
    result = mark_header_rows(body, _marks())
    properties = _first_row_properties(body)
    assert [etree.QName(child).localname for child in properties] == ["tblHeader", "trPrChange"]
    assert len(properties.find("w:trPrChange/w:trPr", NSMAP)) == 0
    assert [outcome.action for outcome in result.tables] == ["marked"]
    assert result.marked == 1


def test_existing_row_properties_are_kept_before_the_new_marker_and_stored_in_the_change():
    """A row with trPr gets tblHeader before ins/del/change elements; the old trPr is stored."""
    body = _body(table([["A", "B"], ["1", "2"]]).replace("<w:tr><w:tc>", '<w:tr><w:trPr><w:cantSplit/></w:trPr><w:tc>', 1))
    mark_header_rows(body, _marks())
    properties = _first_row_properties(body)
    assert [etree.QName(child).localname for child in properties] == ["cantSplit", "tblHeader", "trPrChange"]
    assert [etree.QName(child).localname for child in properties.find("w:trPrChange/w:trPr", NSMAP)] == ["cantSplit"]


def test_a_table_inserted_by_us_gets_the_marker_directly():
    """Rows that are tracked insertions need no property change."""
    inserted = table([["A", "B"], ["1", "2"]]).replace("<w:tr>", f'<w:tr><w:trPr><w:ins w:id="5" w:author="{AUTHOR}" w:date="{DATE}"/></w:trPr>')
    body = _body(inserted)
    result = mark_header_rows(body, _marks())
    properties = _first_row_properties(body)
    assert [etree.QName(child).localname for child in properties] == ["tblHeader", "ins"]
    assert result.tables[0].action == "marked directly"


def test_single_row_tables_and_tables_that_already_repeat_are_left_alone():
    """One-row tables and first rows that already have tblHeader are not touched."""
    already = table([["A", "B"], ["1", "2"]]).replace("<w:tr>", "<w:tr><w:trPr><w:tblHeader/></w:trPr>", 1)
    body = _body(table([["A", "B"]]) + para("") + already)
    result = mark_header_rows(body, _marks())
    assert [outcome.action for outcome in result.tables] == ["single row", "already repeating"]
    assert result.marked == 0


def test_a_table_after_a_deleted_table_is_not_a_continuation_and_the_deleted_one_is_skipped():
    """A table whose first row is a tracked deletion is skipped; its replacement is its own table."""
    deleted = table([["A", "B"], ["1", "2"]]).replace("<w:tr>", f'<w:tr><w:trPr><w:del w:id="3" w:author="{AUTHOR}" w:date="{DATE}"/></w:trPr>')
    body = _body(deleted + table([["X", "Y"], ["3", "4"]]))
    result = mark_header_rows(body, _marks())
    assert [outcome.action for outcome in result.tables] == ["deleted table", "marked"]


def test_a_continuation_table_of_the_same_width_is_skipped():
    """Only empty paragraphs between two tables of equal column count means the second continues the first."""
    body = _body(table([["A", "B"], ["1", "2"]]) + para("") + para("") + table([["3", "4"], ["5", "6"]]))
    result = mark_header_rows(body, _marks())
    assert [outcome.action for outcome in result.tables] == ["marked", "continuation of table 1"]
    assert _first_row_properties(body, 1) is None


def test_text_between_tables_makes_them_separate_tables():
    """A paragraph with text between two tables of equal width means the second has its own header."""
    body = _body(table([["A", "B"], ["1", "2"]]) + para("Keterangan.") + table([["C", "D"], ["3", "4"]]))
    result = mark_header_rows(body, _marks())
    assert [outcome.action for outcome in result.tables] == ["marked", "marked"]


def test_tables_can_be_skipped_by_their_first_cell():
    """A first-cell prefix on the skip list leaves that table alone."""
    body = _body(table([["Kolom", "B"], ["1", "2"]]))
    result = mark_header_rows(body, _marks(), skip_first_cells=("Kol",))
    assert result.tables[0].action == "skipped by request" and _first_row_properties(body) is None


def test_a_row_with_an_earlier_tracked_change_is_not_marked():
    """A second trPrChange would be invalid; the table is reported and left alone."""
    changed = table([["A", "B"], ["1", "2"]]).replace("<w:tr>", '<w:tr><w:trPr><w:trPrChange w:id="1" w:author="B"><w:trPr/></w:trPrChange></w:trPr>', 1)
    body = _body(changed)
    result = mark_header_rows(body, _marks())
    assert result.tables[0].action == "has an earlier row property change"
