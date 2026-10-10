"""Put a wide table on a landscape page of its own between two tracked section breaks (finding T-5).

A table is named by the text its caption starts with, read as accepted text (inserted text kept,
deleted text dropped). The block that turns is the caption plus the run of body elements right
after it that are tables (a fully deleted one too), paragraphs whose whole text is one
{{TABLE:name}} token, or table notes (paragraphs starting with "Catatan:", which stay on the page
of their table); the run stops at any other element and must hold at least one table or token.

An empty paragraph inserted before the caption ends the portrait part with a copy of the w:sectPr
of the section that holds the block; one inserted after the block ends the landscape section with
the same copy turned landscape: page width and height swapped and w:orient="landscape", margins
unchanged, as in the manuscript's own landscape sections. The holding section's w:sectPr is not
changed, so the text after the block goes on in portrait on a new page, and rejecting the two
inserted paragraphs gives back the single section.

A section that is not portrait, restarts its page numbers or has a different first page is not
split, since every part of a split section would restart the numbers or begin with a first page.
"""

from copy import deepcopy
from dataclasses import dataclass, field

from lxml import etree

from redline.fill import TABLE_TOKEN
from redline.matcher import AnchorError, IndexedParagraph, index_paragraphs, match_prefix, paragraph_text
from redline.structure import column_width_or_none, holding_section
from redline.tracked import RevisionMarks, build_inserted_paragraph
from redline.wordml import make_element, qn, remove_children

CAPTION_EXCERPT = 80
NOTE_PREFIX = "Catatan:"
TURNED = "turned to landscape"
SKIPPED = "skipped"
# Spacing 0, an exact 20 twip line and a 1 pt mark keep an empty paragraph from pushing a page of its own.
BREAK_LINE_TWIPS = "20"
BREAK_MARK_HALF_POINTS = "2"


@dataclass(frozen=True)
class BlockOutcome:
    """One prefix: the caption excerpt, the elements of its block, the text width before and after in twips, and the outcome."""

    prefix: str
    caption: str
    elements: tuple[str, ...]
    width_before: int | None
    width_after: int | None
    outcome: str


@dataclass
class LandscapeResult:
    """One outcome per prefix in the order given, and the problems that kept a prefix from turning."""

    blocks: list[BlockOutcome] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def turned(self) -> int:
        """Blocks now on a landscape page."""
        return sum(1 for block in self.blocks if block.outcome == TURNED)


def turn_landscape(body: etree._Element, prefixes: list[str], marks: RevisionMarks) -> LandscapeResult:
    """Put the block of each prefix on a landscape page; one entry point for the flow."""
    result = LandscapeResult()
    for prefix in prefixes:
        result.blocks.append(_turn_block(body, prefix, marks, result.problems))
    return result


def _turn_block(body: etree._Element, prefix: str, marks: RevisionMarks, problems: list[str]) -> BlockOutcome:
    """Turn the block whose caption the prefix names, or add the reason it cannot turn to problems."""
    try:
        caption = match_prefix(prefix, _body_paragraphs(body)).element
    except AnchorError as error:
        problems.append(f"{prefix}: {error.reason}")
        return BlockOutcome(prefix, "", (), None, None, SKIPPED)
    members = _block_members(caption)
    excerpt = paragraph_text(caption)[:CAPTION_EXCERPT]
    elements = tuple(_element_label(member) for member in members)
    width_before = column_width_or_none(caption)
    problem = _block_problem(caption, members)
    if problem is not None:
        problems.append(f"{prefix}: {problem}")
        return BlockOutcome(prefix, excerpt, elements, width_before, width_before, SKIPPED)
    section = holding_section(caption)
    caption.addprevious(_section_break_paragraph(_section_copy(section), marks))
    members[-1].addnext(_section_break_paragraph(_landscape_copy(section), marks))
    return BlockOutcome(prefix, excerpt, elements, width_before, column_width_or_none(caption), TURNED)


def _body_paragraphs(body: etree._Element) -> list[IndexedParagraph]:
    """The paragraphs directly in the body with their accepted text; table cells are left out."""
    return [item for item in index_paragraphs(body) if item.element.getparent() is body]


def _block_members(caption: etree._Element) -> list[etree._Element]:
    """The tables, {{TABLE:name}} paragraphs and table notes that directly follow the caption, in order."""
    members = []
    element = caption.getnext()
    while element is not None and (_is_table_or_token(element) or _is_table_note(element)):
        members.append(element)
        element = element.getnext()
    return members


def _is_table_or_token(element: etree._Element) -> bool:
    """True for a table (deleted or not) or a paragraph whose whole accepted text is one {{TABLE:name}} token."""
    return element.tag == qn("tbl") or _token_name(element) is not None


def _is_table_note(element: etree._Element) -> bool:
    """True for a paragraph whose accepted text starts with 'Catatan:'."""
    return element.tag == qn("p") and paragraph_text(element).startswith(NOTE_PREFIX)


def _token_name(element: etree._Element) -> str | None:
    """The name of a paragraph whose whole accepted text is one {{TABLE:name}} token, else None."""
    if element.tag != qn("p"):
        return None
    token = TABLE_TOKEN.fullmatch(paragraph_text(element))
    return token.group(1) if token is not None else None


def _element_label(member: etree._Element) -> str:
    """How the report names a block element: table, deleted table, token with its name, or note."""
    if member.tag == qn("tbl"):
        return "deleted table" if _is_deleted_table(member) else "table"
    if _is_table_note(member):
        return "note"
    return f"token {_token_name(member)}"


def _is_deleted_table(table: etree._Element) -> bool:
    """True when every row of the table is a tracked deletion."""
    rows = table.findall(qn("tr"))
    return bool(rows) and all(row.find(f"{qn('trPr')}/{qn('del')}") is not None for row in rows)


def _block_problem(caption: etree._Element, members: list[etree._Element]) -> str | None:
    """The reason the block cannot turn, or None when it can."""
    if not any(_is_table_or_token(member) for member in members):
        return "the caption is not followed by a table or a {{TABLE:name}} paragraph"
    if any(_ends_a_section(element) for element in [caption, *members]):
        return "a section break sits inside the block"
    try:
        section = holding_section(caption)
    except ValueError as error:
        return str(error)
    if not _is_portrait(section):
        return "the section that holds the block is not portrait"
    page_numbers = section.find(qn("pgNumType"))
    if page_numbers is not None and page_numbers.get(qn("start")) is not None:
        return "the section that holds the block restarts its page numbers (w:pgNumType w:start); splitting it is not supported"
    if section.find(qn("titlePg")) is not None:
        return "the section that holds the block has a different first page (w:titlePg); splitting it is not supported"
    return None


def _ends_a_section(element: etree._Element) -> bool:
    """True when the element is a paragraph that carries a section break."""
    return element.find(f"{qn('pPr')}/{qn('sectPr')}") is not None


def _is_portrait(section: etree._Element) -> bool:
    """True when the section's page is taller than it is wide and not marked landscape."""
    size = section.find(qn("pgSz"))
    if size is None or size.get(qn("w")) is None or size.get(qn("h")) is None:
        return False
    return size.get(qn("orient")) != "landscape" and int(size.get(qn("w"))) < int(size.get(qn("h")))


def _section_copy(section: etree._Element) -> etree._Element:
    """A deep copy of the section properties without their tracked change; header and footer references stay."""
    copy = deepcopy(section)
    remove_children(copy, ("sectPrChange",))
    return copy


def _landscape_copy(section: etree._Element) -> etree._Element:
    """A copy of the section properties turned landscape: page width and height swapped, margins unchanged."""
    copy = _section_copy(section)
    size = copy.find(qn("pgSz"))
    width, height = size.get(qn("w")), size.get(qn("h"))
    size.set(qn("w"), height)
    size.set(qn("h"), width)
    size.set(qn("orient"), "landscape")
    return copy


def _section_break_paragraph(section: etree._Element, marks: RevisionMarks) -> etree._Element:
    """An inserted empty paragraph that ends a section: tracked mark, no style, and too low to fill a page of its own."""
    properties = make_element("pPr")
    properties.append(make_element("spacing", {"before": "0", "after": "0", "line": BREAK_LINE_TWIPS, "lineRule": "exact"}))
    mark = make_element("rPr")
    mark.append(make_element("sz", {"val": BREAK_MARK_HALF_POINTS}))
    mark.append(make_element("szCs", {"val": BREAK_MARK_HALF_POINTS}))
    properties.append(mark)
    properties.append(section)
    return build_inserted_paragraph(properties, (), None, marks)
