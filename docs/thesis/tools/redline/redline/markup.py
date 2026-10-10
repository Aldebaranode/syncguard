"""Turn the Text lines of an edit into paragraph and table blocks with inline formatting.

Inline rules: '**text**' is bold, '*text*' is italic (the marker must touch non-space text on
both inner sides, as in Markdown). Everything else is literal, including [REF:key], [n] and
{{DATA:name}} tokens. A run of lines starting with '|' is a GitHub Markdown table whose second
line is the separator row; the first row is the header.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

INLINE_MARKER = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|\*(?=[^\s*])(.+?)(?<=[^\s*])\*")
CELL_SPLIT = re.compile(r"(?<!\\)\|")
SEPARATOR_CELL = re.compile(r"^:?-+:?$")


class MarkupError(ValueError):
    """Text that cannot be turned into blocks without guessing."""


@dataclass(frozen=True)
class Segment:
    """A stretch of text with one formatting."""

    text: str
    bold: bool = False
    italic: bool = False


@dataclass(frozen=True)
class ParagraphBlock:
    """One paragraph: a sequence of formatted segments."""

    segments: tuple[Segment, ...]


@dataclass(frozen=True)
class TableBlock:
    """A table: rows of cells of segments; rows[0] is the header row."""

    rows: tuple[tuple[tuple[Segment, ...], ...], ...]


Block = ParagraphBlock | TableBlock
Row = tuple[tuple[Segment, ...], ...]


def parse_inline(text: str) -> tuple[Segment, ...]:
    """Split one line into plain, italic and bold segments."""
    segments: list[Segment] = []
    position = 0
    for marker in INLINE_MARKER.finditer(text):
        if marker.start() > position:
            segments.append(Segment(text[position:marker.start()]))
        if marker.group(1) is not None:
            segments.append(Segment(marker.group(1), bold=True))
        else:
            segments.append(Segment(marker.group(2), italic=True))
        position = marker.end()
    if position < len(text):
        segments.append(Segment(text[position:]))
    return tuple(segments)


def parse_text_blocks(lines: list[str] | tuple[str, ...]) -> list[Block]:
    """Group Text lines into paragraph blocks and table blocks, in order."""
    blocks: list[Block] = []
    table_lines: list[str] = []
    for line in lines:
        if line.startswith("|"):
            table_lines.append(line)
            continue
        if table_lines:
            blocks.append(_parse_table(table_lines))
            table_lines = []
        blocks.append(ParagraphBlock(parse_inline(line)))
    if table_lines:
        blocks.append(_parse_table(table_lines))
    return blocks


def parse_row_lines(lines: list[str] | tuple[str, ...]) -> list[Row]:
    """Parse the Text of a row edit: one '| cell | cell |' line per row, no separator row."""
    rows: list[Row] = []
    for line in lines:
        cells = _split_cells(line) if line.startswith("|") else []
        if not cells or all(SEPARATOR_CELL.match(cell) for cell in cells):
            raise MarkupError(f"row edits take table rows only ('| cell | cell |' lines without a separator), got '{line}'")
        rows.append(tuple(parse_inline(cell) for cell in cells))
    return rows


def _split_cells(line: str) -> list[str]:
    """Split one table line on unescaped pipes and drop the outer empty parts."""
    parts = CELL_SPLIT.split(line.strip())
    if parts and parts[0].strip() == "":
        parts = parts[1:]
    if parts and parts[-1].strip() == "":
        parts = parts[:-1]
    return [part.strip().replace("\\|", "|") for part in parts]


def table_cells(lines: list[str] | tuple[str, ...]) -> list[list[str]]:
    """The cell texts of consecutive pipe lines as written (no inline parsing): the header row, then the body rows.

    The second line must be the separator row, which is checked and left out, and every row must
    have as many cells as the header.
    """
    if len(lines) < 2:
        raise MarkupError("table needs a separator row as its second line")
    header = _split_cells(lines[0])
    separator = _split_cells(lines[1])
    if not separator or not all(SEPARATOR_CELL.match(cell) for cell in separator):
        raise MarkupError("table needs a separator row as its second line")
    if len(separator) != len(header):
        raise MarkupError(f"table separator has {len(separator)} cells, header has {len(header)}")
    rows = [header]
    for row_number, line in enumerate(lines[2:], start=2):
        cells = _split_cells(line)
        if len(cells) != len(header):
            raise MarkupError(f"table row {row_number} has {len(cells)} cells, header has {len(header)}")
        rows.append(cells)
    return rows


def format_table_row(cells: Iterable[object]) -> str:
    """One Markdown table row, with pipes in cells escaped and line breaks turned into spaces."""
    escaped = [str(cell).replace("|", "\\|").replace("\n", " ") for cell in cells]
    return "| " + " | ".join(escaped) + " |"


def _parse_table(lines: list[str]) -> TableBlock:
    """Parse consecutive pipe lines into a table of formatted segments."""
    return TableBlock(tuple(tuple(parse_inline(cell) for cell in row) for row in table_cells(lines)))
