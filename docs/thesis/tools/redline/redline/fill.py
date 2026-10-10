"""Fill {{DATA:name}} tokens inside tracked insertions, and {{TABLE:name}} paragraphs with tables.

Values come from a JSON file, or from a directory holding an optional values.json plus one
<name>.md file per table token. Only DATA tokens inside a w:ins are filled, since the redline
edits wrote them; tokens in untracked text are counted and left alone. A token must sit inside one
w:t, which is how the apply command writes them.

A paragraph whose whole text is {{TABLE:name}} becomes the tracked table from <name>.md, built
like the tables of edits and formatted like the nearest table before it (tracked or not). A token
paragraph that an edit inserted is swapped for the table; one in original text is deleted as a
tracked change and the table follows it.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from redline.markup import MarkupError, TableBlock, parse_text_blocks
from redline.matcher import index_paragraphs
from redline.structure import column_width_or_none
from redline.tables import build_inserted_table, nearest_table_before
from redline.tracked import RevisionMarks, delete_runs, mark_paragraph_deleted
from redline.wordml import XML_SPACE, has_ancestor, qn

TOKEN = re.compile(r"\{\{DATA:([^{}]+)\}\}")
TABLE_TOKEN = re.compile(r"\{\{TABLE:([^{}]+)\}\}")
VALUES_FILE = "values.json"


class FillError(ValueError):
    """The values file cannot be used."""


@dataclass
class FillResult:
    """Per-name counts of filled tokens, tokens without values, and values without tokens."""

    filled: dict[str, int] = field(default_factory=dict)
    missing_values: dict[str, int] = field(default_factory=dict)
    unused_values: list[str] = field(default_factory=list)
    outside_insertions: dict[str, int] = field(default_factory=dict)


@dataclass
class TableFillResult:
    """Table tokens filled, tokens without a file, files without a token, and unusable files."""

    tables_filled: list[str] = field(default_factory=list)
    tables_without_file: list[str] = field(default_factory=list)
    files_without_token: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    review: dict[str, str] = field(default_factory=dict)


def load_values_source(path: Path) -> tuple[dict[str, str], dict[str, Path]]:
    """Read DATA values and table files from a JSON file or from a values directory."""
    path = Path(path)
    if not path.is_dir():
        return load_values(path), {}
    values_file = path / VALUES_FILE
    values = load_values(values_file) if values_file.is_file() else {}
    tables = {table_file.stem: table_file for table_file in sorted(path.glob("*.md"))}
    return values, tables


def load_table_file(path: Path) -> TableBlock:
    """Read a <name>.md file that must hold exactly one GitHub Markdown table."""
    lines = [line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    message = f"{path} must hold exactly one Markdown table and nothing else"
    if not lines or not all(line.startswith("|") for line in lines):
        raise FillError(message)
    try:
        blocks = parse_text_blocks(lines)
    except MarkupError as error:
        raise FillError(f"{message}: {error}") from error
    if len(blocks) != 1:
        raise FillError(message)
    return blocks[0]


def table_token_names(document: etree._Element) -> list[str]:
    """Names of the {{TABLE:name}} paragraphs in the body, in document order."""
    names = []
    for item in index_paragraphs(document.find(qn("body"))):
        token = TABLE_TOKEN.fullmatch(item.text)
        if token is not None:
            names.append(token.group(1))
    return names


def fill_table_tokens(document: etree._Element, table_files: dict[str, Path], marks: RevisionMarks) -> TableFillResult:
    """Replace every {{TABLE:name}} paragraph that has a <name>.md file with the tracked table."""
    result = TableFillResult()
    body = document.find(qn("body"))
    seen: list[str] = []
    for item in index_paragraphs(body):
        token = TABLE_TOKEN.fullmatch(item.text)
        if token is None:
            continue
        name = token.group(1)
        seen.append(name)
        if name not in table_files:
            if name not in result.tables_without_file:
                result.tables_without_file.append(name)
            continue
        try:
            block = load_table_file(table_files[name])
        except FillError as error:
            result.errors[name] = str(error)
            continue
        reference = nearest_table_before(body, item.element, _any_table)
        if reference is None:
            result.errors[name] = "the document has no table to copy table properties from"
            continue
        notes: list[str] = []
        table = build_inserted_table(block, reference, marks, column_width_or_none(item.element), notes)
        if notes:
            result.review[name] = notes[0]
        _put_table_in_place_of(item.element, table, marks)
        result.tables_filled.append(name)
    result.files_without_token = [name for name in table_files if name not in seen]
    return result


def load_values(path: Path) -> dict[str, str]:
    """Read a JSON object of token name to value; numbers are kept as written."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise FillError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise FillError(f"{path} must hold one JSON object of token name to value")
    values: dict[str, str] = {}
    for name, value in data.items():
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise FillError(f"value of '{name}' must be a string or a number")
        values[name] = str(value)
    return values


def fill_tokens(document: etree._Element, values: dict[str, str]) -> FillResult:
    """Replace tokens inside w:ins text with their values and count what was and was not filled."""
    result = FillResult()
    for text in document.iter(qn("t")):
        content = text.text or ""
        if not TOKEN.search(content):
            continue
        if not has_ancestor(text, "ins"):
            for name in TOKEN.findall(content):
                _count(result.outside_insertions, name)
            continue
        text.text = TOKEN.sub(lambda token: _replacement(token, values, result), content)
        if text.text != text.text.strip():
            text.set(XML_SPACE, "preserve")
    result.unused_values = [name for name in values if name not in result.filled]
    return result


def _replacement(token: re.Match, values: dict[str, str], result: FillResult) -> str:
    """Return the value for one token match, or the token itself when there is no value."""
    name = token.group(1)
    if name in values:
        _count(result.filled, name)
        return values[name]
    _count(result.missing_values, name)
    return token.group(0)


def _count(counter: dict[str, int], name: str) -> None:
    """Add one to a per-name counter."""
    counter[name] = counter.get(name, 0) + 1


def _any_table(table: etree._Element) -> bool:
    """Every table qualifies: its w:tblPr is a valid formatting source whether its rows are tracked or not."""
    return True


def _is_inserted_paragraph(paragraph: etree._Element) -> bool:
    """True when the paragraph mark and every run of the paragraph are tracked insertions."""
    if paragraph.find(f"{qn('pPr')}/{qn('rPr')}/{qn('ins')}") is None:
        return False
    return all(has_ancestor(run, "ins", stop=paragraph) for run in paragraph.iter(qn("r")))


def _put_table_in_place_of(paragraph: etree._Element, table: etree._Element, marks: RevisionMarks) -> None:
    """Swap an inserted token paragraph for the table, or delete an original one and add the table after it."""
    if _is_inserted_paragraph(paragraph):
        paragraph.addprevious(table)
        paragraph.getparent().remove(paragraph)
        return
    delete_runs(paragraph, marks)
    mark_paragraph_deleted(paragraph, marks)
    paragraph.addnext(table)
