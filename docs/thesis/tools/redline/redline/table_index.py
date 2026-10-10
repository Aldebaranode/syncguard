"""Locate tables and table rows of the original document by cell text.

Anchor forms (see the report, fix round 1):
  Anchor: ROW: <value>    a row, optionally narrowed by  Table: <value>
  Anchor: TABLE: <value>  a whole table, identified by its first row
A value without ' | ' is compared with the first cell's plain text; a value with ' | ' is
compared with the whole row, its cells' plain text joined by ' | '. Comparison is exact after
whitespace collapses. Cell text is the plain text of the cell's paragraphs joined by a space.
"""

from collections.abc import Callable
from dataclasses import dataclass

from lxml import etree

from redline.matcher import AnchorError, IndexedParagraph, normalize, paragraph_text
from redline.wordml import qn

CELL_SEPARATOR = " | "


@dataclass(frozen=True)
class IndexedRow:
    """A table row with its first-cell text, whole-row text and its first paragraph (for reports)."""

    element: etree._Element
    table: "IndexedTable"
    first_cell: str
    text: str
    lead: IndexedParagraph


@dataclass(eq=False)
class IndexedTable:
    """A table of the original document with its rows in order."""

    element: etree._Element
    rows: list[IndexedRow]


def index_tables(body: etree._Element, paragraphs: list[IndexedParagraph]) -> list[IndexedTable]:
    """Index every table of the body (nested ones included) and its rows."""
    by_element = {id(item.element): item for item in paragraphs}
    tables: list[IndexedTable] = []
    for table_element in body.iter(qn("tbl")):
        indexed = IndexedTable(table_element, [])
        for row in table_element.findall(qn("tr")):
            cells = [_cell_text(cell) for cell in row.findall(qn("tc"))]
            lead_paragraph = next(row.iter(qn("p")), None)
            if not cells or lead_paragraph is None or id(lead_paragraph) not in by_element:
                continue
            indexed.rows.append(IndexedRow(row, indexed, cells[0], CELL_SEPARATOR.join(cells), by_element[id(lead_paragraph)]))
        if indexed.rows:
            tables.append(indexed)
    return tables


def row_matches(row: IndexedRow, value: str) -> bool:
    """True when the value equals the row's first cell, or the whole row when it holds ' | '."""
    value = normalize(value)
    if CELL_SEPARATOR in value:
        return row.text == value
    return row.first_cell == value


def match_table(value: str, tables: list[IndexedTable], occurrence: int | None = None) -> IndexedTable:
    """Return the one table whose first row matches value, or raise AnchorError."""
    matches = [table for table in tables if row_matches(table.rows[0], value)]
    if not matches:
        raise AnchorError("no_match", f"no table has a first row matching '{normalize(value)}'", [])
    hint = "give the whole first row (cells joined by ' | ') or Occurrence"
    return _pick(matches, occurrence, "tables", hint, lambda table: table.rows[0].lead)


def match_row(
    value: str, tables: list[IndexedTable], table_value: str | None = None, occurrence: int | None = None
) -> IndexedRow:
    """Return the one row matching value (within tables matching table_value), or raise AnchorError."""
    if table_value is not None:
        tables = [table for table in tables if row_matches(table.rows[0], table_value)]
        if not tables:
            raise AnchorError("no_match", f"no table has a first row matching '{normalize(table_value)}'", [])
    matches = [row for table in tables for row in table.rows if row_matches(row, value)]
    if not matches:
        raise AnchorError("no_match", "no table row matches the anchor", [])
    return _pick(matches, occurrence, "table rows", "add Table: or Occurrence to pick one", lambda row: row.lead)


def _pick[T](
    matches: list[T], occurrence: int | None, noun: str, hint: str, lead: Callable[[T], IndexedParagraph]
) -> T:
    """Apply the one-match and Occurrence rules shared by table and row anchors."""
    candidates = [lead(match) for match in matches]
    if occurrence is None:
        if len(matches) == 1:
            return matches[0]
        raise AnchorError("ambiguous", f"anchor matches {len(matches)} {noun}; {hint}", candidates)
    if occurrence > len(matches):
        raise AnchorError("occurrence", f"Occurrence {occurrence} requested but the anchor matches {len(matches)} {noun}", candidates)
    return matches[occurrence - 1]


def _cell_text(cell: etree._Element) -> str:
    """Plain text of a cell: its own paragraphs' text joined by a space."""
    return normalize(" ".join(paragraph_text(paragraph) for paragraph in cell.findall(qn("p"))))
