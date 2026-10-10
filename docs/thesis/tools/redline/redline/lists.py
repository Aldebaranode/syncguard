"""List numbering helpers, and restarting numbered lists at 1 (finding IV-19).

The helpers read a paragraph's list numbering, build a w:numPr, and add a w:num that starts a
list's format again at 1; the apply pass uses them for the Numbering key.

restart_lists names each list by the text its first item starts with. Its items are that
paragraph and the later paragraphs that use the same numId, up to the next heading or the first
item of the next named list. The list gets a new w:num that points at the same abstract
numbering and overrides the start of every level the list uses to 1. Each item then points at
the new numId; an original paragraph records its previous w:pPr in a w:pPrChange, a paragraph
inserted by the same author is changed directly. The new w:num elements go into
word/numbering.xml, which is not tracked.
"""

from copy import deepcopy
from dataclasses import dataclass, field

from lxml import etree

from redline.matcher import AnchorError, IndexedParagraph, index_paragraphs, match_prefix
from redline.structure import is_heading
from redline.tracked import RevisionMarks, can_track_paragraph_change, open_paragraph_change
from redline.wordml import make_element, qn


@dataclass
class ListRestart:
    """One restarted list: the first item's text, the numbering ids, and how many paragraphs moved."""

    prefix: str
    first_text: str
    old_num_id: str
    new_num_id: str
    paragraphs: int


@dataclass
class RestartResult:
    """Restarted lists and one message per prefix that could not be used."""

    restarts: list[ListRestart] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class _Group:
    """A list found in the document and not yet changed."""

    prefix: str
    first: IndexedParagraph
    old_num_id: str
    members: list[IndexedParagraph]


def restart_lists(
    document: etree._Element,
    numbering: etree._Element,
    style_names: dict[str, str],
    prefixes: list[str],
    marks: RevisionMarks,
) -> RestartResult:
    """Restart the list that starts at each prefix; one entry point for the flow."""
    result = RestartResult()
    paragraphs = index_paragraphs(document.find(qn("body")))
    starts = _resolve_starts(paragraphs, prefixes, numbering, result)
    groups = _collect_groups(paragraphs, starts, style_names)
    for group in groups:
        levels = sorted({paragraph_level(member.element) for member in group.members}, key=int)
        new_id = add_restarted_num(numbering, group.old_num_id, levels)
        moved = 0
        for member in group.members:
            if _point_at(member.element, new_id, marks):
                moved += 1
            else:
                result.errors.append(
                    f"{group.prefix}: the item '{member.text}' already has a tracked paragraph change and stays on the old list"
                )
        result.restarts.append(ListRestart(group.prefix, group.first.text, group.old_num_id, new_id, moved))
    return result


def _resolve_starts(
    paragraphs: list[IndexedParagraph], prefixes: list[str], numbering: etree._Element, result: RestartResult
) -> list[tuple[str, IndexedParagraph]]:
    """The first-item paragraph of each prefix in document order; problems go to result.errors."""
    starts = []
    for prefix in prefixes:
        try:
            first = match_prefix(prefix, paragraphs)
        except AnchorError as error:
            result.errors.append(f"{prefix}: {error.reason}")
            continue
        num_id = paragraph_num_id(first.element)
        if num_id is None:
            result.errors.append(f"{prefix}: the paragraph is not a numbered list item")
        elif find_num(numbering, num_id) is None:
            result.errors.append(f"{prefix}: numId {num_id} has no w:num in numbering.xml")
        else:
            starts.append((prefix, first))
    starts.sort(key=lambda start: start[1].number)
    return starts


def _collect_groups(
    paragraphs: list[IndexedParagraph], starts: list[tuple[str, IndexedParagraph]], style_names: dict[str, str]
) -> list[_Group]:
    """Read-only pass: each list's members, before any numId is changed."""
    start_numbers = {item.number for _, item in starts}
    groups = []
    for prefix, first in starts:
        old_id = paragraph_num_id(first.element)
        members = [first]
        for item in paragraphs[first.number :]:
            if item.number in start_numbers or is_heading(item.element, item.text, style_names):
                break
            if paragraph_num_id(item.element) == old_id:
                members.append(item)
        groups.append(_Group(prefix, first, old_id, members))
    return groups


def paragraph_num_id(paragraph: etree._Element) -> str | None:
    """The paragraph's direct numId, or None when it is not numbered."""
    element = paragraph.find(f"{qn('pPr')}/{qn('numPr')}/{qn('numId')}")
    return element.get(qn("val")) if element is not None else None


def paragraph_level(paragraph: etree._Element) -> str:
    """The paragraph's list level (0 when none is written)."""
    element = paragraph.find(f"{qn('pPr')}/{qn('numPr')}/{qn('ilvl')}")
    return element.get(qn("val")) if element is not None else "0"


def find_num(numbering: etree._Element, num_id: str) -> etree._Element | None:
    """The w:num element with this id."""
    for num in numbering.findall(qn("num")):
        if num.get(qn("numId")) == num_id:
            return num
    return None


def _highest_num_id(numbering: etree._Element) -> int:
    """The highest numeric numId in the numbering part."""
    return max((int(num.get(qn("numId"))) for num in numbering.findall(qn("num")) if num.get(qn("numId"), "").isdigit()), default=0)


def add_restarted_num(numbering: etree._Element, old_num_id: str, levels: list[str]) -> str:
    """Append a w:num formatted like old_num_id's whose given levels start at 1; return its numId.

    The new w:num points at the same abstract numbering and copies the old one's overrides of
    the other levels.
    """
    old = find_num(numbering, old_num_id)
    new_id = str(_highest_num_id(numbering) + 1)
    new = make_element("num", {"numId": new_id})
    new.append(make_element("abstractNumId", {"val": old.find(qn("abstractNumId")).get(qn("val"))}))
    overrides = {level.get(qn("ilvl")): deepcopy(level) for level in old.findall(qn("lvlOverride")) if level.get(qn("ilvl")) not in levels}
    for level in levels:
        override = make_element("lvlOverride", {"ilvl": level})
        override.append(make_element("startOverride", {"val": "1"}))
        overrides[level] = override
    for level in sorted(overrides, key=int):
        new.append(overrides[level])
    last = numbering.findall(qn("num"))[-1]
    last.addnext(new)
    return new_id


def numbering_properties(num_id: str, level: str) -> etree._Element:
    """A w:numPr that puts a paragraph at this level of list num_id."""
    properties = make_element("numPr")
    properties.append(make_element("ilvl", {"val": level}))
    properties.append(make_element("numId", {"val": num_id}))
    return properties


def _point_at(paragraph: etree._Element, new_id: str, marks: RevisionMarks) -> bool:
    """Switch the paragraph to the new numId, tracked unless it is our own insertion; False when it cannot be tracked."""
    if not can_track_paragraph_change(paragraph, marks.author):
        return False
    properties = open_paragraph_change(paragraph, marks)
    properties.find(f"{qn('numPr')}/{qn('numId')}").set(qn("val"), new_id)
    return True
