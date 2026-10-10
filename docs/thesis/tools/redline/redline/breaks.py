"""Remove a hard page break that sits right before a next-page section break, as a tracked change.

A paragraph whose only content is a page-break run, directly followed by a paragraph that carries
a section break of type nextPage (or no type, which means nextPage), produces an empty page: the
page break ends the page and the section break ends the next one. The page-break run is wrapped
in w:del; the paragraph and the section break stay, and the section break still starts the next
page. Page breaks anywhere else are left alone.
"""

from dataclasses import dataclass, field

from lxml import etree

from redline.matcher import paragraph_text
from redline.tracked import RevisionMarks
from redline.wordml import local_name, qn

PAGE_BREAK_RUN_CHILDREN = ("rPr", "br", "lastRenderedPageBreak")
TRANSPARENT = ("pPr", "proofErr", "bookmarkStart", "bookmarkEnd")
TABLE_LABEL = "(table)"


@dataclass(frozen=True)
class BreakRemoval:
    """One removed page break and the nearest text on each side."""

    before: str
    after: str


@dataclass
class BreaksResult:
    """The removals made."""

    removals: list[BreakRemoval] = field(default_factory=list)


def remove_redundant_page_breaks(body: etree._Element, marks: RevisionMarks) -> BreaksResult:
    """Delete, tracked, the page-break run of every page-break-only paragraph before a next-page section break."""
    result = BreaksResult()
    for paragraph in body.findall(qn("p")):
        run = _page_break_run(paragraph)
        following = paragraph.getnext()
        if run is None or following is None or not _is_next_page_section_break(following):
            continue
        deletion = marks.deletion()
        run.addprevious(deletion)
        deletion.append(run)
        result.removals.append(BreakRemoval(_neighbour_text(paragraph, "getprevious"), _neighbour_text(following, "getnext")))
    return result


def _page_break_run(paragraph: etree._Element) -> etree._Element | None:
    """The single page-break run when it is all the paragraph holds, else None."""
    runs = []
    for child in paragraph:
        name = local_name(child)
        if name in TRANSPARENT:
            continue
        if name != "r":
            return None
        names = [local_name(part) for part in child]
        is_page_break = child.find(qn("br")) is not None and child.find(qn("br")).get(qn("type")) == "page"
        if not is_page_break or any(part not in PAGE_BREAK_RUN_CHILDREN for part in names):
            return None
        runs.append(child)
    return runs[0] if len(runs) == 1 else None


def _is_next_page_section_break(element: etree._Element) -> bool:
    """True for a paragraph carrying a w:sectPr whose type is nextPage or absent."""
    if element.tag != qn("p"):
        return False
    section = element.find(f"{qn('pPr')}/{qn('sectPr')}")
    if section is None:
        return False
    section_type = section.find(qn("type"))
    return section_type is None or section_type.get(qn("val"), "nextPage") == "nextPage"


def _neighbour_text(start: etree._Element, direction: str) -> str:
    """Text of the nearest non-empty paragraph in the direction (a table is labelled), skipping empty paragraphs."""
    sibling = getattr(start, direction)()
    while sibling is not None:
        if sibling.tag == qn("tbl"):
            return TABLE_LABEL
        if sibling.tag == qn("p") and paragraph_text(sibling):
            return paragraph_text(sibling)
        sibling = getattr(sibling, direction)()
    return ""
