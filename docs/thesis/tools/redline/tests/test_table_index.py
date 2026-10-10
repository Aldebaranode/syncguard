"""Tests for locating tables and table rows by cell text."""

import pytest
from lxml import etree

from docx_builder import R_NS, W_NS, para, table
from redline.matcher import AnchorError, index_paragraphs
from redline.table_index import index_tables, match_row, match_table

BODY = (
    para("Tabel 3.1")
    + table([["Parameter", "Nilai Default", "Keterangan"], ["health.interval", "5 detik", "x"]])
    + para("Tabel 4.2")
    + table([["Parameter", "Nilai", "Keterangan"], ["health.interval", "10 detik", "y"]])
    + table([["Komponen", "Fungsi"], ["Docker", "kontainer"]])
)


def _tables():
    """Index the fixture tables."""
    body = etree.fromstring(f'<w:body xmlns:w="{W_NS}" xmlns:r="{R_NS}">{BODY}</w:body>')
    return index_tables(body, index_paragraphs(body))


def test_table_is_found_by_first_cell_or_by_whole_first_row():
    """A value without ' | ' compares the first cell; with ' | ' it compares the whole first row."""
    tables = _tables()
    assert match_table("Komponen", tables) is tables[2]
    assert match_table("Parameter | Nilai Default | Keterangan", tables) is tables[0]
    assert match_table("Parameter | Nilai | Keterangan", tables) is tables[1]


def test_ambiguous_table_names_its_candidates():
    """A first cell shared by two tables needs the whole first row or Occurrence."""
    tables = _tables()
    with pytest.raises(AnchorError) as raised:
        match_table("Parameter", tables)
    assert raised.value.kind == "ambiguous"
    assert "matches 2 tables" in raised.value.reason
    assert [candidate.text for candidate in raised.value.candidates] == ["Parameter", "Parameter"]
    assert match_table("Parameter", tables, occurrence=2) is tables[1]


def test_row_is_found_by_first_cell_within_the_named_table():
    """Table: narrows the row search to tables whose first row matches it."""
    tables = _tables()
    row = match_row("health.interval", tables, table_value="Parameter | Nilai | Keterangan")
    assert row.text == "health.interval | 10 detik | y"
    assert row.table is tables[1]


def test_row_without_table_value_can_be_ambiguous_or_use_the_whole_row():
    """Two rows with the same first cell are ambiguous; the whole row text picks one."""
    tables = _tables()
    with pytest.raises(AnchorError, match="matches 2 table rows") as raised:
        match_row("health.interval", tables)
    assert raised.value.kind == "ambiguous"
    assert match_row("health.interval | 5 detik | x", tables).table is tables[0]


def test_missing_row_or_table_is_a_no_match():
    """Unknown rows, and a Table: value that names no table, are zero-match errors."""
    tables = _tables()
    with pytest.raises(AnchorError, match="no table row matches") as raised:
        match_row("tidak ada", tables)
    assert raised.value.kind == "no_match"
    with pytest.raises(AnchorError, match="no table has a first row matching 'Alat'"):
        match_row("Docker", tables, table_value="Alat")
