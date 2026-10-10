"""Repeat the first row of every multi-row table on each page (finding T-6) as tracked changes.

For an original table the w:tblHeader marker goes into the first row's w:trPr and a w:trPrChange
records the properties the row had before. A table inserted by a tracked change (its rows carry
w:trPr/w:ins) gets the marker directly, since the whole row is already a tracked insertion.

Left alone, and reported: one-row tables, rows that already repeat, tables whose first row is a
tracked deletion, and continuation tables. A continuation table is one that follows another
visible table with only empty paragraphs between and has the same number of columns; it is the
rest of a table that a section break split (Tabel 2.1), so its first row is data, not a header.
"""

from copy import deepcopy
from dataclasses import dataclass, field

from lxml import etree

from redline.matcher import paragraph_text
from redline.tracked import RevisionMarks, track_property_change
from redline.wordml import local_name, make_element, qn

TRACKED_AFTER_BASE = ("ins", "del", "trPrChange")
FIRST_CELL_EXCERPT = 40


@dataclass(frozen=True)
class TableOutcome:
    """What happened to one table: its 1-based position, size, first cell, and the action taken."""

    number: int
    rows: int
    first_cell: str
    action: str


@dataclass
class HeadersResult:
    """One outcome per top-level table, and how many got a header row."""

    tables: list[TableOutcome] = field(default_factory=list)

    @property
    def marked(self) -> int:
        """Tables that now repeat their first row."""
        return sum(1 for outcome in self.tables if outcome.action.startswith("marked"))


def mark_header_rows(body: etree._Element, marks: RevisionMarks, skip_first_cells: tuple[str, ...] = ()) -> HeadersResult:
    """Give the first row of each qualifying table a header marker; one entry point for the flow."""
    result = HeadersResult()
    tables = body.findall(qn("tbl"))
    for number, table in enumerate(tables, start=1):
        rows = table.findall(qn("tr"))
        first_cell = _first_cell_text(rows[0]) if rows else ""
        action = _decide(table, rows, first_cell, tables, skip_first_cells)
        if action == "mark":
            action = _mark_first_row(rows[0], marks)
        result.tables.append(TableOutcome(number, len(rows), first_cell[:FIRST_CELL_EXCERPT], action))
    return result


def _decide(table, rows, first_cell: str, tables: list, skip_first_cells: tuple[str, ...]) -> str:
    """The reason a table is left alone, or 'mark' when its first row should repeat."""
    if len(rows) < 2:
        return "single row"
    properties = rows[0].find(qn("trPr"))
    if _has_child(properties, "tblHeader"):
        return "already repeating"
    if _has_child(properties, "del"):
        return "deleted table"
    if any(first_cell.startswith(prefix) for prefix in skip_first_cells):
        return "skipped by request"
    previous = _continued_table(table, tables)
    if previous is not None:
        return f"continuation of table {tables.index(previous) + 1}"
    return "mark"


def _mark_first_row(row: etree._Element, marks: RevisionMarks) -> str:
    """Add tblHeader to the row, tracked unless the row is itself a tracked insertion."""
    properties = row.find(qn("trPr"))
    if properties is not None and properties.find(qn("trPrChange")) is not None:
        return "has an earlier row property change"
    previous = deepcopy(properties) if properties is not None else None
    if properties is None:
        properties = make_element("trPr")
        row.insert(1 if row.find(qn("tblPrEx")) is not None else 0, properties)
    _insert_before_tracked_elements(properties, make_element("tblHeader"))
    if properties.find(qn("ins")) is not None:
        return "marked directly"
    track_property_change(properties, "trPrChange", previous, marks)
    return "marked"


def _insert_before_tracked_elements(properties: etree._Element, marker: etree._Element) -> None:
    """Put marker among the base row properties, ahead of ins, del and trPrChange."""
    for index, child in enumerate(properties):
        if local_name(child) in TRACKED_AFTER_BASE:
            properties.insert(index, marker)
            return
    properties.append(marker)


def _continued_table(table: etree._Element, tables: list[etree._Element]) -> etree._Element | None:
    """The visible table right before this one (empty paragraphs between) with the same column count."""
    sibling = table.getprevious()
    while sibling is not None and sibling.tag == qn("p") and not paragraph_text(sibling):
        sibling = sibling.getprevious()
    if sibling is None or sibling.tag != qn("tbl") or sibling not in tables:
        return None
    rows = sibling.findall(qn("tr"))
    if not rows or _has_child(rows[0].find(qn("trPr")), "del"):
        return None
    if len(sibling.findall(f"{qn('tblGrid')}/{qn('gridCol')}")) != len(table.findall(f"{qn('tblGrid')}/{qn('gridCol')}")):
        return None
    return sibling


def _has_child(properties: etree._Element | None, name: str) -> bool:
    """True when the properties element exists and has a child with this local name."""
    return properties is not None and properties.find(qn(name)) is not None


def _first_cell_text(row: etree._Element) -> str:
    """Accepted text of the row's first cell."""
    cell = row.find(qn("tc"))
    if cell is None:
        return ""
    return " ".join(paragraph_text(paragraph) for paragraph in cell.iter(qn("p"))).strip()
