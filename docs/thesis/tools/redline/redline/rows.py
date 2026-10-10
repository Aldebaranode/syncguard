"""Tracked changes on table rows: delete a row or a whole table, replace a row's cell texts,
and build a new tracked row shaped like an existing one.

A deleted row carries w:trPr/w:del and has every run of its cells wrapped in w:del. A new row
carries w:trPr/w:ins, and each of its cell paragraphs has a tracked mark and runs inside w:ins.
"""

from copy import deepcopy

from lxml import etree

from redline.markup import Row
from redline.tracked import (
    RevisionMarks,
    append_inserted_runs,
    build_inserted_paragraph,
    copied_paragraph_properties,
    delete_runs,
    first_text_run_properties,
)
from redline.wordml import local_name, make_element, qn, remove_children


class RowError(ValueError):
    """A row edit whose Text does not fit the anchored row."""


def delete_row(row: etree._Element, marks: RevisionMarks) -> None:
    """Record a whole row as deleted: its runs in w:del and w:del in its w:trPr."""
    for paragraph in row.iter(qn("p")):
        delete_runs(paragraph, marks)
    _add_row_revision(_row_properties(row), marks.deletion())


def delete_table(table: etree._Element, marks: RevisionMarks) -> None:
    """Record a whole table as deleted, row by row."""
    for row in table.findall(qn("tr")):
        delete_row(row, marks)


def replace_row_cells(row: etree._Element, cells: Row, marks: RevisionMarks) -> None:
    """Delete each cell's text and insert the new cell text, keeping each cell's run formatting."""
    existing = row.findall(qn("tc"))
    _check_cell_count(existing, cells)
    for cell, segments in zip(existing, cells):
        paragraphs = cell.findall(qn("p"))
        base = first_text_run_properties(paragraphs[0])
        insert_points = [delete_runs(paragraph, marks) for paragraph in paragraphs]
        append_inserted_runs(paragraphs[0], segments, base, marks, after=insert_points[0])


def build_inserted_row(anchor_row: etree._Element, cells: Row, marks: RevisionMarks) -> etree._Element:
    """Build a tracked row with the anchor row's row, cell and paragraph properties."""
    anchor_cells = anchor_row.findall(qn("tc"))
    _check_cell_count(anchor_cells, cells)
    row = make_element("tr")
    properties = anchor_row.find(qn("trPr"))
    properties = deepcopy(properties) if properties is not None else make_element("trPr")
    remove_children(properties, ("ins", "del", "trPrChange"))
    row.append(properties)
    _add_row_revision(properties, marks.insertion())
    for anchor_cell, segments in zip(anchor_cells, cells):
        cell = etree.SubElement(row, qn("tc"))
        cell_properties = anchor_cell.find(qn("tcPr"))
        if cell_properties is not None:
            cell_properties = deepcopy(cell_properties)
            remove_children(cell_properties, ("tcPrChange",))
            cell.append(cell_properties)
        anchor_paragraph = anchor_cell.find(qn("p"))
        paragraph_properties = copied_paragraph_properties(anchor_paragraph)
        base = first_text_run_properties(anchor_paragraph)
        cell.append(build_inserted_paragraph(paragraph_properties, segments, base, marks))
    return row


def _check_cell_count(existing: list[etree._Element], cells: Row) -> None:
    """The Text row must have exactly as many cells as the anchored row."""
    if len(existing) != len(cells):
        raise RowError(f"the row has {len(existing)} cells, the Text row has {len(cells)}")


def _row_properties(row: etree._Element) -> etree._Element:
    """Return the row's w:trPr, creating it after any w:tblPrEx when missing."""
    properties = row.find(qn("trPr"))
    if properties is not None:
        return properties
    properties = make_element("trPr")
    position = 1 if len(row) and local_name(row[0]) == "tblPrEx" else 0
    row.insert(position, properties)
    return properties


def _add_row_revision(properties: etree._Element, revision: etree._Element) -> None:
    """Place w:ins or w:del in w:trPr after the row properties and before w:trPrChange."""
    change = properties.find(qn("trPrChange"))
    if change is not None:
        change.addprevious(revision)
    else:
        properties.append(revision)
