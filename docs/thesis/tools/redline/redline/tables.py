"""Build a tracked w:tbl from a Markdown table block.

Table properties (borders, width, layout) are copied from a reference table that the caller
chooses (apply.py passes the table being replaced, or the nearest original table before the
insertion point). Column widths follow the content: every column is at least as wide as its
longest piece that Word cannot wrap (an identifier such as failover.state_sync_interval is one
piece), and the rest of the width goes to the columns with more text. The table keeps the
reference table's width and indent while that is enough; when the unwrappable pieces need more,
it grows to the text column and loses the indent (a reference wider than the text column, such
as a landscape table copied into a portrait section, is reset the same way); when even that is too narrow the side cell margins
drop to 60 twips, then the cell font steps down to 11 and 10 pt (never lower; a table that still
does not fit is reported for review), so that Word's fixed layout never has to split a word. Cell borders and shading are copied from the reference cells. Every row
carries w:trPr/w:ins, every cell paragraph has a tracked mark and its runs inside w:ins, and the
header row is bold and repeats on every page (w:tblHeader). Cell paragraphs copy the paragraph properties of the reference table's cells
(header cells from its first row, body cells from its second row when it has one).
"""

import re
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, replace

from lxml import etree

from redline.markup import Segment, TableBlock
from redline.textwidth import breakable_pieces, estimate_text_width
from redline.tracked import RevisionMarks, build_inserted_paragraph, copied_paragraph_properties
from redline.wordml import insert_in_order, make_element, qn, remove_children

# The tool's cells carry no run size, so Normal's 12 pt applies; table cell margins are 115 twips on each side in style a1.
CELL_FONT_PT = 12
SMALLER_FONT_STEPS = (11, 10)
DEFAULT_MARGINS = 230
# Left and right cell margins of 60 twips each, used before the font is reduced.
NARROW_MARGINS = 120
NARROW_MARGIN_EACH = 60
MINIMUM_FONT_PT = 10
# {{DATA:name}} is replaced by a short value later; this stands in for the value when sizing a column.
DATA_VALUE_STAND_IN = "0" * 10
DATA_TOKEN = re.compile(r"\{\{DATA:[^{}]+\}\}")
# Pieces longer than this (a hex address, a path) cannot fit any column and are allowed to wrap.
MAX_UNBREAKABLE_CHARS = 32
TABLE_PROPERTY_ORDER = (
    "tblStyle", "tblpPr", "tblOverlap", "bidiVisual", "tblStyleRowBandSize", "tblStyleColBandSize", "tblW", "jc",
    "tblCellSpacing", "tblInd", "tblBorders", "shd", "tblLayout", "tblCellMar", "tblLook", "tblCaption", "tblDescription", "tblPrChange",
)
CELL_PROPERTY_ORDER = ("tcW", "gridSpan", "vMerge", "tcBorders", "shd", "noWrap", "tcMar", "textDirection", "tcFitText", "vAlign", "hideMark")
COPIED_CELL_PROPERTIES = ("tcBorders", "shd", "vAlign")


def build_inserted_table(
    block: TableBlock,
    reference: etree._Element,
    marks: RevisionMarks,
    column_width: int | None = None,
    notes: list[str] | None = None,
) -> etree._Element:
    """Build the tracked table for block, formatted like the reference table.

    column_width is the text column width in twips where the table goes (None when unknown); the
    table may grow to it when its content cannot fit the reference table's width. A table whose
    unwrappable pieces do not fit even at the 10 pt floor adds a review note to notes.
    """
    column_count = len(block.rows[0])
    fit = _fit(block, reference, column_width)
    minimums, preferences = _column_needs(block, fit.font_pt, fit.margins)
    widths = _share_width(minimums, preferences, fit.total)
    run_properties = _cell_run_properties(fit.font_pt)
    if not fit.fits and notes is not None:
        notes.append(
            f"table needs review: its unbreakable pieces need {fit.needed} twips but the text column is {fit.total}; "
            f"kept at {fit.font_pt} pt, so a long identifier may wrap inside a cell"
        )
    header_properties, body_properties = _reference_cell_properties(reference)
    header_cells, body_cells = _reference_cell_formats(reference, column_count)

    new_table = make_element("tbl")
    table_properties = deepcopy(reference.find(qn("tblPr")))
    if table_properties is None:
        table_properties = make_element("tblPr")
    remove_children(table_properties, ("tblPrChange",))
    _set_table_width(table_properties, fit.total, drop_indent=fit.reset)
    _set_cell_margins(table_properties, fit.margins)
    new_table.append(table_properties)
    grid = etree.SubElement(new_table, qn("tblGrid"))
    for width in widths:
        grid.append(make_element("gridCol", {"w": str(width)}))

    for row_number, cells in enumerate(block.rows):
        is_header = row_number == 0
        row = etree.SubElement(new_table, qn("tr"))
        row_properties = etree.SubElement(row, qn("trPr"))
        if is_header:
            row_properties.append(make_element("tblHeader"))
        row_properties.append(marks.insertion())
        for column, segments in enumerate(cells):
            if is_header:
                segments = tuple(replace(segment, bold=True) for segment in segments)
            properties = deepcopy(header_properties if is_header else body_properties)
            formats = (header_cells if is_header else body_cells)[column]
            row.append(_cell(segments, properties, str(widths[column]), formats, run_properties, marks))
    return new_table


def nearest_table_before(
    body: etree._Element, anchor: etree._Element, is_reference: Callable[[etree._Element], bool]
) -> etree._Element | None:
    """The last qualifying table before anchor in document order, else the first qualifying table."""
    nearest = None
    for element in body.iter():
        if element is anchor:
            break
        if element.tag == qn("tbl") and is_reference(element):
            nearest = element
    if nearest is not None:
        return nearest
    return next((table for table in body.iter(qn("tbl")) if is_reference(table)), None)


def _cell(
    segments: tuple[Segment, ...],
    properties: etree._Element | None,
    width: str,
    formats: list[etree._Element],
    run_properties: etree._Element | None,
    marks: RevisionMarks,
) -> etree._Element:
    """Build one w:tc with its width, the reference cell's borders and shading, and one tracked paragraph."""
    cell = make_element("tc")
    cell_properties = etree.SubElement(cell, qn("tcPr"))
    cell_properties.append(make_element("tcW", {"w": width, "type": "dxa"}))
    for property_element in formats:
        insert_in_order(cell_properties, deepcopy(property_element), CELL_PROPERTY_ORDER)
    cell.append(build_inserted_paragraph(properties, segments, run_properties, marks))
    return cell


def _cell_text_width(segments: tuple[Segment, ...], bold: bool, font_pt: int) -> int:
    """Estimated width of a cell's text on one line, without margins."""
    return sum(estimate_text_width(_for_sizing(segment.text), font_pt, bold or segment.bold) for segment in segments)


def _for_sizing(text: str) -> str:
    """The text as it will read after fill: every {{DATA:name}} token becomes a short stand-in value."""
    return DATA_TOKEN.sub(DATA_VALUE_STAND_IN, text)


def _column_needs(block: TableBlock, font_pt: int, margins: int) -> tuple[list[int], list[int]]:
    """Per column: the least width that keeps every unwrappable piece whole, and the width of the longest cell."""
    minimums = [margins] * len(block.rows[0])
    preferences = [margins] * len(block.rows[0])
    for row_number, cells in enumerate(block.rows):
        for column, segments in enumerate(cells):
            bold = row_number == 0
            text = _for_sizing("".join(segment.text for segment in segments))
            pieces = [piece for piece in breakable_pieces(text) if len(piece) <= MAX_UNBREAKABLE_CHARS]
            longest = max((estimate_text_width(piece, font_pt, bold) for piece in pieces), default=0)
            minimums[column] = max(minimums[column], longest + margins)
            preferences[column] = max(preferences[column], _cell_text_width(segments, bold, font_pt) + margins)
    return minimums, preferences


def needed_width(block: TableBlock, font_pt: int = MINIMUM_FONT_PT, margins: int = NARROW_MARGINS) -> int:
    """The least table width in twips that keeps every unwrappable piece whole at font_pt with these cell margins.

    The defaults are fill's floor, 10 pt with 60 twip margins on each side: a table wider than this
    at the floor fits no text column.
    """
    return sum(_column_needs(block, font_pt, margins)[0])


@dataclass(frozen=True)
class _Fit:
    """The chosen font size, cell margins (both sides together), table width, whether the geometry was reset, and whether it fits."""

    font_pt: int
    margins: int
    total: int
    reset: bool
    fits: bool
    needed: int = 0


def _fit(block: TableBlock, reference: etree._Element, column_width: int | None) -> _Fit:
    """Choose font, margins and width so that unwrappable pieces stay whole.

    Without a known text column the reference width, 12 pt and the style margins are used.
    Otherwise, in order: the reference's own width and indent at 12 pt; the text column at 12 pt;
    the text column at 12 pt with 60 twip side margins; then 11 and 10 pt with 60 twip margins.
    The font never goes below 10 pt: when nothing fits, 10 pt is kept and fits is False so that
    the caller can report the table for review.
    """
    reference_width = _grid_width(reference)
    if column_width is None:
        return _Fit(CELL_FONT_PT, DEFAULT_MARGINS, reference_width, False, True)
    fits_column = reference_width + _table_indent(reference) <= column_width
    if fits_column and needed_width(block, CELL_FONT_PT, DEFAULT_MARGINS) <= reference_width:
        return _Fit(CELL_FONT_PT, DEFAULT_MARGINS, reference_width, False, True)
    attempts = [(CELL_FONT_PT, DEFAULT_MARGINS), (CELL_FONT_PT, NARROW_MARGINS)]
    attempts += [(font_pt, NARROW_MARGINS) for font_pt in SMALLER_FONT_STEPS]
    needed = 0
    for font_pt, margins in attempts:
        needed = needed_width(block, font_pt, margins)
        if needed <= column_width:
            return _Fit(font_pt, margins, column_width, True, True, needed)
    return _Fit(MINIMUM_FONT_PT, NARROW_MARGINS, column_width, True, False, needed)


def _table_indent(table: etree._Element) -> int:
    """The table's left indent in twips (0 when it has none)."""
    indent = table.find(f"{qn('tblPr')}/{qn('tblInd')}")
    return int(indent.get(qn("w"), "0")) if indent is not None else 0


def _cell_run_properties(font_pt: int) -> etree._Element | None:
    """Run properties that set the cell font size, or None when the document's own size applies."""
    if font_pt == CELL_FONT_PT:
        return None
    properties = make_element("rPr")
    properties.append(make_element("sz", {"val": str(font_pt * 2)}))
    properties.append(make_element("szCs", {"val": str(font_pt * 2)}))
    return properties


def _share_width(minimums: list[int], preferences: list[int], total: int) -> list[int]:
    """Column widths that sum to total: the minimums first, the rest in proportion to how much more text a column has."""
    if sum(minimums) >= total:
        widths = [minimum * total // sum(minimums) for minimum in minimums]
    else:
        extra = total - sum(minimums)
        wants = [max(preference - minimum, 0) for minimum, preference in zip(minimums, preferences)]
        if sum(wants) == 0:
            wants = [1] * len(minimums)
        widths = [minimum + extra * want // sum(wants) for minimum, want in zip(minimums, wants)]
    widths[-1] += total - sum(widths)
    return widths


def _set_table_width(table_properties: etree._Element, total: int, drop_indent: bool) -> None:
    """Write the new total into w:tblW (when the reference has one) and drop w:tblInd when the table grew."""
    width = table_properties.find(qn("tblW"))
    if width is not None:
        width.set(qn("w"), str(total))
        width.set(qn("type"), "dxa")
    if drop_indent:
        remove_children(table_properties, ("tblInd",))


def _set_cell_margins(table_properties: etree._Element, margins: int) -> None:
    """Give the table 60 twip left and right cell margins when the narrow margins were chosen."""
    if margins != NARROW_MARGINS:
        return
    remove_children(table_properties, ("tblCellMar",))
    cell_margins = make_element("tblCellMar")
    cell_margins.append(make_element("left", {"w": str(NARROW_MARGIN_EACH), "type": "dxa"}))
    cell_margins.append(make_element("right", {"w": str(NARROW_MARGIN_EACH), "type": "dxa"}))
    insert_in_order(table_properties, cell_margins, TABLE_PROPERTY_ORDER)


def _reference_cell_formats(reference: etree._Element, column_count: int) -> tuple[list[list[etree._Element]], list[list[etree._Element]]]:
    """Borders, shading and vertical alignment of the reference's first-row and second-row cells, per new column."""
    rows = reference.findall(qn("tr"))
    header_row = rows[0] if rows else None
    body_row = rows[1] if len(rows) > 1 else header_row
    return _row_cell_formats(header_row, column_count), _row_cell_formats(body_row, column_count)


def _row_cell_formats(row: etree._Element | None, column_count: int) -> list[list[etree._Element]]:
    """For each column the copied cell properties of the reference cell in that column (the last cell when it has fewer)."""
    cells = row.findall(qn("tc")) if row is not None else []
    formats = []
    for column in range(column_count):
        if not cells:
            formats.append([])
            continue
        properties = cells[min(column, len(cells) - 1)].find(qn("tcPr"))
        copied = [child for child in properties if etree.QName(child).localname in COPIED_CELL_PROPERTIES] if properties is not None else []
        formats.append(copied)
    return formats


def _grid_width(reference: etree._Element) -> int:
    """Total width of the reference table's grid columns, in twentieths of a point."""
    widths = [int(column.get(qn("w"), "0")) for column in reference.iter(qn("gridCol"))]
    return sum(widths)


def _reference_cell_properties(reference: etree._Element) -> tuple[etree._Element | None, etree._Element | None]:
    """Paragraph properties of the reference table's first-row and second-row cells."""
    rows = reference.findall(qn("tr"))
    header_paragraph = next(rows[0].iter(qn("p")), None) if rows else None
    body_paragraph = next(rows[1].iter(qn("p")), None) if len(rows) > 1 else header_paragraph
    header = copied_paragraph_properties(header_paragraph) if header_paragraph is not None else None
    body = copied_paragraph_properties(body_paragraph) if body_paragraph is not None else None
    return header, body
