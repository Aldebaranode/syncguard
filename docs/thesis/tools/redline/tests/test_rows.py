"""Tests for tracked row deletion, row replacement and row insertion."""

import pytest
from lxml import etree

from docx_builder import R_NS, W_NS
from redline.markup import parse_row_lines
from redline.rows import RowError, build_inserted_row, delete_row, delete_table, replace_row_cells
from redline.tracked import RevisionMarks

NSMAP = {"w": W_NS, "r": R_NS}
TABLE = (
    f'<w:tbl xmlns:w="{W_NS}" xmlns:r="{R_NS}"><w:tblPr/><w:tblGrid><w:gridCol w:w="2000"/><w:gridCol w:w="3000"/></w:tblGrid>'
    '<w:tr><w:trPr><w:jc w:val="center"/></w:trPr>'
    '<w:tc><w:tcPr><w:tcW w:w="2000" w:type="dxa"/></w:tcPr><w:p><w:pPr><w:jc w:val="both"/></w:pPr>'
    '<w:r><w:rPr><w:b/></w:rPr><w:t>Parameter</w:t></w:r></w:p></w:tc>'
    '<w:tc><w:tcPr><w:tcW w:w="3000" w:type="dxa"/></w:tcPr><w:p><w:r><w:t>Nilai</w:t></w:r></w:p></w:tc></w:tr>'
    '<w:tr><w:tc><w:tcPr><w:tcW w:w="2000" w:type="dxa"/></w:tcPr><w:p><w:r><w:t>health.interval</w:t></w:r></w:p></w:tc>'
    '<w:tc><w:tcPr><w:tcW w:w="3000" w:type="dxa"/></w:tcPr><w:p><w:r><w:t>5 detik</w:t></w:r></w:p></w:tc></w:tr>'
    "</w:tbl>"
)


def _table() -> etree._Element:
    """Parse the fixture table."""
    return etree.fromstring(TABLE)


def _marks() -> RevisionMarks:
    """Revision marks with a fixed author and date."""
    return RevisionMarks("Syahid Nurrohim", "2026-10-05T08:00:00Z", 1)


def _xpath(element, path):
    """Run an XPath with the w and r prefixes bound."""
    return element.xpath(path, namespaces=NSMAP)


def test_deleted_row_marks_trpr_and_wraps_every_run():
    """w:trPr gets w:del after its other properties; cell runs become w:delText inside w:del."""
    table = _table()
    marks = _marks()
    delete_row(table.findall(f"{{{W_NS}}}tr")[0], marks)
    first = table.findall(f"{{{W_NS}}}tr")[0]
    assert [etree.QName(child).localname for child in first.find(f"{{{W_NS}}}trPr")] == ["jc", "del"]
    assert _xpath(first, ".//w:tc/w:p/w:del/w:r/w:delText/text()") == ["Parameter", "Nilai"]
    assert _xpath(first, ".//w:t") == []
    assert (marks.inserted_count, marks.deleted_count) == (0, 3)


def test_deleted_row_without_trpr_gets_one_before_its_cells():
    """A row with no w:trPr gets one as its first child."""
    table = _table()
    second = table.findall(f"{{{W_NS}}}tr")[1]
    delete_row(second, _marks())
    assert etree.QName(second[0]).localname == "trPr"
    assert _xpath(second, "./w:trPr/w:del") != []


def test_deleting_a_table_deletes_every_row():
    """A table deletion is a tracked deletion of each of its rows."""
    table = _table()
    delete_table(table, _marks())
    assert len(_xpath(table, "./w:tr/w:trPr/w:del")) == 2


def test_replaced_row_cells_keep_their_formatting_and_get_new_text():
    """Each cell's old runs are deleted and the new text is inserted with that cell's run formatting."""
    table = _table()
    first = table.findall(f"{{{W_NS}}}tr")[0]
    [cells] = parse_row_lines(["| Parameter baru | *Nilai* |"])
    replace_row_cells(first, cells, _marks())
    assert _xpath(first, ".//w:tc[1]/w:p/w:ins/w:r/w:t/text()") == ["Parameter baru"]
    assert _xpath(first, ".//w:tc[1]/w:p/w:ins/w:r/w:rPr/w:b") != []
    assert _xpath(first, ".//w:tc[2]/w:p/w:ins/w:r/w:rPr/w:i") != []
    assert _xpath(first, ".//w:tc/w:p/w:del/w:r/w:delText/text()") == ["Parameter", "Nilai"]


def test_row_cell_count_must_match():
    """A Text row with another cell count than the anchored row is refused."""
    first = _table().findall(f"{{{W_NS}}}tr")[0]
    [cells] = parse_row_lines(["| hanya satu |"])
    with pytest.raises(RowError, match="the row has 2 cells, the Text row has 1"):
        replace_row_cells(first, cells, _marks())


def test_inserted_row_copies_the_anchor_row_layout_and_is_tracked():
    """The new row copies trPr, tcPr and cell paragraph properties, and every part is tracked."""
    anchor = _table().findall(f"{{{W_NS}}}tr")[0]
    [cells] = parse_row_lines(["| failover.warmup_period | 90 detik |"])
    new = build_inserted_row(anchor, cells, _marks())
    assert [etree.QName(child).localname for child in new.find(f"{{{W_NS}}}trPr")] == ["jc", "ins"]
    assert _xpath(new, "./w:tc/w:tcPr/w:tcW/@w:w") == ["2000", "3000"]
    assert _xpath(new, "./w:tc[1]/w:p/w:pPr/w:jc/@w:val") == ["both"]
    assert len(_xpath(new, "./w:tc/w:p/w:pPr/w:rPr/w:ins")) == 2
    assert _xpath(new, "./w:tc/w:p/w:ins/w:r/w:t/text()") == ["failover.warmup_period", "90 detik"]
