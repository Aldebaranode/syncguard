"""Parse draft edit lists (the block format of docs/thesis/drafts/README.md) into Edit records.

A block starts at a heading '### E-<id> (closes: <ids>)' and ends at the next Markdown heading,
a thematic break ('---'), or the end of the file. Anything outside a block (intro text, reference lists, coverage notes)
is ignored. A malformed block is reported with its id and line number and is never applied.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

OPERATIONS = ("replace", "insert_after", "insert_before", "delete", "replace_image")
NUMBERING_OPERATIONS = ("replace", "insert_after", "insert_before")
NUMBERING_CONTINUE = "continue"
NUMBERING_NEW = "new"
NUMBERING_NONE = "none"

HEADING_LINE = re.compile(r"^#{1,6}\s")
THEMATIC_BREAK = re.compile(r"^ {0,3}([-*_])( *\1){2,} *$")
EDIT_HEADING_START = re.compile(r"^###\s+(E-\S*)")
EDIT_HEADING = re.compile(r"^###\s+(E-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)\s+\(closes:\s*(.+?)\)\s*$")
FIELD_LINE = re.compile(r"^(Operation|Anchor source|Anchor|Occurrence|Style|Numbering|Table):\s*(.*)$")
ROW_ANCHOR = "ROW:"
TABLE_ANCHOR = "TABLE:"


@dataclass(frozen=True)
class Numbering:
    """The Numbering key: continue (join the list of the paragraph prefix names), new (a new list formatted like it) or none."""

    mode: str
    prefix: str | None = None


@dataclass(frozen=True)
class Edit:
    """One parsed edit block."""

    edit_id: str
    closes: str
    operation: str
    anchor: str
    occurrence: int | None
    style: str | None
    text_lines: tuple[str, ...]
    rationale: str
    source: str
    line: int
    table: str | None = None
    anchor_source: str | None = None
    numbering: Numbering | None = None


@dataclass(frozen=True)
class ParseProblem:
    """A malformed block: which edit, where, and what is wrong."""

    edit_id: str | None
    source: str
    line: int
    message: str


@dataclass
class ParseResult:
    """Edits that parsed cleanly plus the problems found in the others."""

    edits: list[Edit] = field(default_factory=list)
    problems: list[ParseProblem] = field(default_factory=list)


@dataclass
class _RawBlock:
    """The lines of one edit block before validation."""

    edit_id: str
    closes: str
    line: int
    body: list[tuple[int, str]]


@dataclass
class _BlockFields:
    """Field values collected from one block body, with the line each came from."""

    values: dict[str, tuple[str, int]] = field(default_factory=dict)
    has_text: bool = False
    text_lines: list[str] = field(default_factory=list)
    rationale: str | None = None


def parse_edits(markdown: str, source: str) -> ParseResult:
    """Parse every edit block in one draft's Markdown text."""
    result = ParseResult()
    for block in _split_blocks(markdown.splitlines(), source, result.problems):
        edit = _parse_block(block, source, result.problems)
        if edit is not None:
            result.edits.append(edit)
    return result


def load_edit_files(paths: list[Path]) -> ParseResult:
    """Parse several draft files in order and reject edit ids that repeat across them."""
    combined = ParseResult()
    first_seen: dict[str, Edit] = {}
    for path in paths:
        parsed = parse_edits(Path(path).read_text(encoding="utf-8"), source=str(path))
        combined.problems.extend(parsed.problems)
        for edit in parsed.edits:
            earlier = first_seen.get(edit.edit_id)
            if earlier is None:
                first_seen[edit.edit_id] = edit
                combined.edits.append(edit)
                continue
            message = f"duplicate edit id (first defined in {earlier.source}:{earlier.line})"
            combined.problems.append(ParseProblem(edit.edit_id, edit.source, edit.line, message))
    return combined


def _split_blocks(lines: list[str], source: str, problems: list[ParseProblem]) -> list[_RawBlock]:
    """Group lines into edit blocks; a block with a malformed heading is reported and skipped."""
    blocks: list[_RawBlock] = []
    current: _RawBlock | None = None
    for number, line in enumerate(lines, start=1):
        if THEMATIC_BREAK.match(line):
            current = None
            continue
        if HEADING_LINE.match(line):
            current = None
            start = EDIT_HEADING_START.match(line)
            if start is None:
                continue
            heading = EDIT_HEADING.match(line)
            if heading is None:
                message = "heading must look like '### E-<id> (closes: <finding ids>)'"
                problems.append(ParseProblem(start.group(1), source, number, message))
                continue
            current = _RawBlock(heading.group(1), heading.group(2).strip(), number, [])
            blocks.append(current)
        elif current is not None:
            current.body.append((number, line))
    return blocks


LineReporter = Callable[[int, str], None]
BlockReporter = Callable[[str], None]


def _collect_fields(block: _RawBlock, report: LineReporter) -> _BlockFields:
    """Read header fields, the Text lines and the Rationale line of one block body."""
    fields = _BlockFields()
    state = "fields"
    for number, line in block.body:
        stripped = line.strip()
        if state == "after":
            if stripped:
                report(number, f"unexpected line after Rationale '{stripped}'")
            continue
        if line.startswith("Rationale:"):
            fields.rationale = line[len("Rationale:"):].strip()
            state = "after"
            continue
        if state == "text":
            if stripped:
                fields.text_lines.append(stripped)
            continue
        if not stripped:
            continue
        if stripped == "Text:":
            fields.has_text = True
            state = "text"
            continue
        field_match = FIELD_LINE.match(line)
        if field_match is None:
            report(number, f"unexpected line '{stripped}'")
            continue
        name, value = field_match.group(1), field_match.group(2).strip()
        if name in fields.values:
            report(number, f"duplicate field {name}")
            continue
        fields.values[name] = (value, number)
    return fields


def _parse_occurrence(fields: _BlockFields, report: LineReporter) -> int | None:
    """Return the Occurrence value, reporting anything that is not a positive integer."""
    if "Occurrence" not in fields.values:
        return None
    raw, number = fields.values["Occurrence"]
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    report(number, f"Occurrence must be a positive integer, got '{raw}'")
    return None


def _check_operation_rules(operation: str, fields: _BlockFields, report_block: BlockReporter) -> None:
    """Apply the per-operation rules on Text (the Style rule is judged at apply time)."""
    if operation == "delete" and fields.has_text:
        report_block("delete must not have Text")
    if operation != "delete" and not fields.text_lines:
        report_block(f"{operation} needs Text")
    if operation == "replace_image" and len(fields.text_lines) > 1:
        report_block("replace_image takes exactly one Text line (the {{FIGURE:name}} token)")


def _parse_numbering(fields: _BlockFields, operation: str, anchor: str, report: LineReporter) -> Numbering | None:
    """Return the Numbering value, reporting it when malformed or on an operation or anchor without lists."""
    if "Numbering" not in fields.values:
        return None
    raw, number = fields.values["Numbering"]
    if operation in OPERATIONS and operation not in NUMBERING_OPERATIONS:
        report(number, "Numbering is only defined for replace, insert_after and insert_before")
        return None
    if anchor.startswith((ROW_ANCHOR, TABLE_ANCHOR)):
        report(number, "Numbering is not defined for ROW: and TABLE: anchors")
        return None
    if raw == NUMBERING_NONE:
        return Numbering(NUMBERING_NONE)
    mode, _, prefix = raw.partition(" ")
    if mode in (NUMBERING_CONTINUE, NUMBERING_NEW) and prefix.strip():
        return Numbering(mode, prefix.strip())
    report(number, f"Numbering must be 'continue <prefix>', 'new <prefix>' or 'none', got '{raw}'")
    return None


def _check_table_anchor(anchor: str, fields: _BlockFields, report_block: BlockReporter) -> None:
    """ROW: and TABLE: anchors need a value; Table: narrows ROW: anchors only."""
    for prefix in (ROW_ANCHOR, TABLE_ANCHOR):
        if anchor.startswith(prefix) and not anchor[len(prefix):].strip():
            report_block("ROW: and TABLE: anchors need the text of a cell or row")
    if "Table" in fields.values and not anchor.startswith(ROW_ANCHOR):
        report_block("Table is only defined for ROW: anchors")


def _parse_block(block: _RawBlock, source: str, problems: list[ParseProblem]) -> Edit | None:
    """Validate one block and turn it into an Edit, or record its problems and return None."""
    found: list[ParseProblem] = []

    def report(line: int, message: str) -> None:
        """Record a problem at a specific line of this block."""
        found.append(ParseProblem(block.edit_id, source, line, message))

    def report_block(message: str) -> None:
        """Record a problem about the block as a whole, at its heading line."""
        report(block.line, message)

    fields = _collect_fields(block, report)
    operation = fields.values.get("Operation", ("", 0))[0]
    if not operation:
        report_block("missing Operation")
    elif operation not in OPERATIONS:
        report_block(f"unknown Operation '{operation}'")
    anchor = fields.values.get("Anchor", ("", 0))[0]
    if not anchor:
        report_block("missing Anchor")
    _check_table_anchor(anchor, fields, report_block)
    occurrence = _parse_occurrence(fields, report)
    numbering = _parse_numbering(fields, operation, anchor, report)
    if operation in OPERATIONS:
        _check_operation_rules(operation, fields, report_block)
    if fields.rationale is None:
        report_block("missing Rationale")

    problems.extend(found)
    if found:
        return None
    style = fields.values.get("Style", (None, 0))[0]
    table = fields.values.get("Table", (None, 0))[0]
    anchor_source = fields.values.get("Anchor source", (None, 0))[0]
    return Edit(
        edit_id=block.edit_id,
        closes=block.closes,
        operation=operation,
        anchor=anchor,
        occurrence=occurrence,
        style=style,
        text_lines=tuple(fields.text_lines),
        rationale=fields.rationale or "",
        source=source,
        line=block.line,
        table=table,
        anchor_source=anchor_source,
        numbering=numbering,
    )
