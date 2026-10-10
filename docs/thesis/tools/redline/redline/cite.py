"""Renumber citations in reading order and rebuild the reference list, as tracked changes.

The document is read in body order (table cells included), skipping the reference list section.
Markers are found in the visible (accepted) text: [n], [n, m], [n]-[m] ranges and [REF:key],
including markers inside w:hyperlink runs and inside tracked insertions. Each marker resolves
through the registry; main records are numbered by first appearance, technical records get
[T1], [T2], ... by first appearance.

A marker whose text changes is rewritten:
  - in original text, as a tracked deletion of the old marker runs plus a tracked insertion of
    the new marker right after them, in the same container (so a Zotero w:hyperlink survives);
  - inside a tracked insertion, in place, because the text is already a tracked insertion.
Markers in a paragraph are rewritten right to left, and splitting a run keeps the text before the
marker in the original w:t, so the offsets of markers further left stay valid.

The reference list section (found by its heading) is replaced: every old entry becomes a tracked
deletion, the new numbered IEEE list follows as tracked insertions, then a tracked "SUMBER TEKNIS"
heading with the [T1].. entries.
"""

import re
from copy import deepcopy
from dataclasses import dataclass, field

from lxml import etree

from redline.constants import REFERENCE_HEADINGS, TECHNICAL_HEADING
from redline.markup import Segment, parse_inline
from redline.matcher import IndexedParagraph, index_paragraphs, normalize
from redline.registry import Record, Registry
from redline.tracked import (
    RevisionMarks,
    build_inserted_paragraph,
    copied_paragraph_properties,
    delete_runs,
    first_text_run_properties,
    make_runs,
    mark_paragraph_deleted,
    turn_into_deleted_text,
)
from redline.wordml import XML_SPACE, has_ancestor, local_name, make_element, owning_paragraph, qn

MARKER = re.compile(
    r"\[(?P<first>\d+)\]\s*[-–]\s*\[(?P<last>\d+)\]"
    r"|\[REF:(?P<key>[^\]\s]+)\]"
    r"|\[(?P<numbers>\d+(?:\s*,\s*\d+)*)\]"
)
RECENT_YEARS = range(2021, 2027)
EXCERPT_LIMIT = 60

Piece = tuple[etree._Element, int, int]


@dataclass
class Composition:
    """Size of the main list and how many of its records are peer-reviewed or dated 2021 to 2026."""

    total: int = 0
    peer_reviewed: int = 0
    recent: int = 0


@dataclass
class CiteResult:
    """Labels per record and everything the cite report needs."""

    heading: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    old_markers: dict[str, list[str]] = field(default_factory=dict)
    first_seen: dict[str, str] = field(default_factory=dict)
    markers_found: int = 0
    rewritten_tracked: int = 0
    rewritten_in_insertions: int = 0
    unchanged: int = 0
    unresolved: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    never_cited: list[str] = field(default_factory=list)
    old_entries_deleted: int = 0
    main_list: list[Record] = field(default_factory=list)
    technical_list: list[Record] = field(default_factory=list)
    composition: Composition = field(default_factory=Composition)


@dataclass
class _Marker:
    """One citation marker found in a paragraph, with the w:t pieces that hold it."""

    paragraph: IndexedParagraph
    start: int
    text: str
    pieces: list[Piece | None]
    records: list[Record] | None


def cite_document(document: etree._Element, registry: Registry, marks: RevisionMarks) -> CiteResult:
    """Renumber every citation marker and rebuild the reference list; one entry point for the flow."""
    result = CiteResult()
    body = document.find(qn("body"))
    paragraphs = index_paragraphs(body)
    heading, section = _reference_section(paragraphs)
    skipped = {id(item.element) for item in section}
    if heading is not None:
        result.heading = heading.text
        skipped.add(id(heading.element))
    else:
        result.errors.append(f"no reference list heading found (looked for: {', '.join(REFERENCE_HEADINGS)})")
    markers = [marker for item in paragraphs if id(item.element) not in skipped for marker in _find_markers(item, registry, result)]
    _assign_labels(markers, registry, result)
    _rewrite_markers(markers, result, marks)
    if heading is not None:
        _rebuild_reference_list(heading, section, result, marks)
    _compose(result)
    return result


def format_labels(labels: list[str]) -> str:
    """IEEE-style marker text: sorted numbers, runs of three or more collapsed, T labels last."""
    numbers = sorted({int(label[1:-1]) for label in labels if not label.startswith("[T")})
    technical = sorted({label for label in labels if label.startswith("[T")}, key=lambda label: int(label[2:-1]))
    parts = []
    index = 0
    while index < len(numbers):
        end = index
        while end + 1 < len(numbers) and numbers[end + 1] == numbers[end] + 1:
            end += 1
        if end - index >= 2:
            parts.append(f"[{numbers[index]}]–[{numbers[end]}]")
        else:
            parts.extend(f"[{number}]" for number in numbers[index : end + 1])
        index = end + 1
    return ", ".join(parts + technical)


def _reference_section(paragraphs: list[IndexedParagraph]) -> tuple[IndexedParagraph | None, list[IndexedParagraph]]:
    """The last reference list heading and the body paragraphs that follow it up to the next heading."""
    candidates = [item for item in paragraphs if not item.in_table and item.text.upper() in REFERENCE_HEADINGS]
    if not candidates:
        return None, []
    heading = candidates[-1]
    by_element = {id(item.element): item for item in paragraphs}
    section = []
    sibling = heading.element.getnext()
    while sibling is not None and sibling.tag == qn("p") and not _is_heading_style(sibling):
        section.append(by_element[id(sibling)])
        sibling = sibling.getnext()
    return heading, section


def _is_heading_style(paragraph: etree._Element) -> bool:
    """True when the paragraph uses a Heading style."""
    style = paragraph.find(f"{qn('pPr')}/{qn('pStyle')}")
    return style is not None and style.get(qn("val"), "").startswith("Heading")


def _visible_text(paragraph: etree._Element) -> tuple[str, list[tuple[etree._Element, int] | None]]:
    """The paragraph's accepted text, with each character's w:t and offset (None for tabs and breaks)."""
    characters: list[str] = []
    positions: list[tuple[etree._Element, int] | None] = []
    for element in paragraph.iter(qn("t"), qn("tab"), qn("br"), qn("cr")):
        if owning_paragraph(element) is not paragraph:
            continue
        if element.tag == qn("t"):
            for offset, character in enumerate(element.text or ""):
                characters.append(character)
                positions.append((element, offset))
        elif element.getparent().tag == qn("r") and not has_ancestor(element, "del", stop=paragraph):
            characters.append(" ")
            positions.append(None)
    return "".join(characters), positions


def _find_markers(item: IndexedParagraph, registry: Registry, result: CiteResult) -> list[_Marker]:
    """Find and resolve the markers of one paragraph."""
    text, positions = _visible_text(item.element)
    markers = []
    for match in MARKER.finditer(text):
        result.markers_found += 1
        tokens = _tokens(match)
        records = [registry.resolve(token) for token in tokens]
        if any(record is None for record in records):
            result.unresolved.append((match.group(0), _excerpt(item.text)))
            records = None
        pieces = _pieces(positions[match.start() : match.end()])
        markers.append(_Marker(item, match.start(), match.group(0), pieces, records))
    return markers


def _tokens(match: re.Match) -> list[str]:
    """The registry aliases one marker names: a key, a number range, or a number list."""
    if match.group("key"):
        return [match.group("key")]
    if match.group("first"):
        first, last = int(match.group("first")), int(match.group("last"))
        return [str(number) for number in range(first, last + 1)]
    return [number.strip() for number in match.group("numbers").split(",")]


def _pieces(positions: list[tuple[etree._Element, int] | None]) -> list[Piece | None]:
    """Group a marker's character positions into (w:t, start, end) pieces; None marks a tab or break."""
    pieces: list[Piece | None] = []
    for position in positions:
        if position is None:
            pieces.append(None)
            continue
        element, offset = position
        last = pieces[-1] if pieces else None
        if last is not None and last[0] is element and last[2] == offset:
            pieces[-1] = (element, last[1], offset + 1)
        else:
            pieces.append((element, offset, offset + 1))
    return pieces


def _assign_labels(markers: list[_Marker], registry: Registry, result: CiteResult) -> None:
    """Number main records and label technical records by first appearance."""
    main_count = 0
    technical_count = 0
    for marker in markers:
        for record in marker.records or []:
            result.old_markers.setdefault(record.record_id, [])
            if marker.text not in result.old_markers[record.record_id]:
                result.old_markers[record.record_id].append(marker.text)
            if record.record_id in result.labels:
                continue
            if record.technical:
                technical_count += 1
                result.labels[record.record_id] = f"[T{technical_count}]"
                result.technical_list.append(record)
            else:
                main_count += 1
                result.labels[record.record_id] = f"[{main_count}]"
                result.main_list.append(record)
            result.first_seen[record.record_id] = _excerpt(marker.paragraph.text)
    result.never_cited = [record.record_id for record in registry.records if record.record_id not in result.labels]


def _rewrite_markers(markers: list[_Marker], result: CiteResult, marks: RevisionMarks) -> None:
    """Rewrite every resolved marker whose text changes, right to left within each paragraph."""
    for marker in sorted(markers, key=lambda marker: (marker.paragraph.number, marker.start), reverse=True):
        if marker.records is None:
            continue
        new_text = format_labels([result.labels[record.record_id] for record in marker.records])
        if new_text == marker.text:
            result.unchanged += 1
            continue
        if any(piece is None for piece in marker.pieces):
            result.errors.append(f"marker {marker.text} spans a tab or line break; not rewritten ({_excerpt(marker.paragraph.text)})")
            continue
        inserted = [has_ancestor(piece[0], "ins", stop=marker.paragraph.element) for piece in marker.pieces]
        if all(inserted):
            _rewrite_in_place(marker.pieces, new_text)
            result.rewritten_in_insertions += 1
        elif not any(inserted):
            _rewrite_tracked(marker.pieces, new_text, marks)
            result.rewritten_tracked += 1
        else:
            result.errors.append(f"marker {marker.text} mixes inserted and original text; not rewritten ({_excerpt(marker.paragraph.text)})")


def _rewrite_in_place(pieces: list[Piece], new_text: str) -> None:
    """Swap the marker text inside a tracked insertion: new text in the first piece, the rest removed."""
    for index, (element, start, end) in reversed(list(enumerate(pieces))):
        replacement = new_text if index == 0 else ""
        text = element.text or ""
        element.text = text[:start] + replacement + text[end:]
        element.set(XML_SPACE, "preserve")


def _rewrite_tracked(pieces: list[Piece], new_text: str, marks: RevisionMarks) -> None:
    """Delete the marker runs (tracked) and insert the new marker after the last one, in the same container."""
    properties = pieces[0][0].getparent().find(qn("rPr"))
    properties = deepcopy(properties) if properties is not None else None
    last_deletion = None
    for element, start, end in reversed(pieces):
        run = _isolate(element, start, end)
        deletion = marks.deletion()
        run.addprevious(deletion)
        deletion.append(run)
        turn_into_deleted_text(run)
        if last_deletion is None:
            last_deletion = deletion
    insertion = marks.insertion()
    for run in make_runs((Segment(new_text),), properties):
        insertion.append(run)
    last_deletion.addnext(insertion)


def _isolate(text_element: etree._Element, start: int, end: int) -> etree._Element:
    """Move text_element[start:end] into a run of its own, right after the text before it.

    The text before start stays in text_element, so earlier offsets keep pointing at the same
    characters. Text after end, and the run's later children, move to a third run.
    """
    run = text_element.getparent()
    text = text_element.text or ""
    content = [child for child in run if local_name(child) != "rPr"]
    later_children = content[content.index(text_element) + 1 :]
    middle = _sibling_run(run, text[start:end])
    run.addnext(middle)
    if text[end:] or later_children:
        after = _sibling_run(run, text[end:])
        for child in later_children:
            after.append(child)
        middle.addnext(after)
    if start > 0:
        text_element.text = text[:start]
        text_element.set(XML_SPACE, "preserve")
    else:
        run.remove(text_element)
        if not [child for child in run if local_name(child) != "rPr"]:
            run.getparent().remove(run)
    return middle


def _sibling_run(run: etree._Element, text: str) -> etree._Element:
    """A new run with the same w:rPr as run and the given text (no w:t when text is empty)."""
    new_run = make_element("r")
    properties = run.find(qn("rPr"))
    if properties is not None:
        new_run.append(deepcopy(properties))
    if text:
        text_element = etree.SubElement(new_run, qn("t"))
        text_element.text = text
        text_element.set(XML_SPACE, "preserve")
    return new_run


def _rebuild_reference_list(heading: IndexedParagraph, section: list[IndexedParagraph], result: CiteResult, marks: RevisionMarks) -> None:
    """Delete the old entries and insert the new main list, then the SUMBER TEKNIS section."""
    template_source = next((item.element for item in section if item.text), None)
    entry_properties = copied_paragraph_properties(template_source) if template_source is not None else None
    for item in section:
        delete_runs(item.element, marks)
        mark_paragraph_deleted(item.element, marks)
    result.old_entries_deleted = len(section)
    cursor = section[-1].element if section else heading.element
    new_paragraphs = [_entry(result.labels[record.record_id], record.ieee, entry_properties, marks) for record in result.main_list]
    if result.technical_list:
        heading_properties = copied_paragraph_properties(heading.element)
        heading_run = first_text_run_properties(heading.element)
        new_paragraphs.append(build_inserted_paragraph(heading_properties, (Segment(TECHNICAL_HEADING),), heading_run, marks))
        new_paragraphs += [_entry(result.labels[record.record_id], record.ieee, entry_properties, marks) for record in result.technical_list]
    for paragraph in new_paragraphs:
        cursor.addnext(paragraph)
        cursor = paragraph


def _entry(label: str, ieee: str, properties: etree._Element | None, marks: RevisionMarks) -> etree._Element:
    """One tracked list entry: the label, a tab, then the IEEE text with its italics."""
    paragraph = build_inserted_paragraph(deepcopy(properties) if properties is not None else None, (Segment(label),), None, marks)
    insertion = paragraph.find(qn("ins"))
    tab_run = make_element("r")
    tab_run.append(make_element("tab"))
    insertion.append(tab_run)
    for run in make_runs(parse_inline(ieee), None):
        insertion.append(run)
    return paragraph


def _compose(result: CiteResult) -> None:
    """Count the main list's size, peer-reviewed records and records dated 2021 to 2026."""
    result.composition = Composition(
        total=len(result.main_list),
        peer_reviewed=sum(1 for record in result.main_list if record.record_type == "peer_reviewed"),
        recent=sum(1 for record in result.main_list if record.year in RECENT_YEARS),
    )


def _excerpt(text: str) -> str:
    """The start of a paragraph's text, for reports."""
    text = normalize(text)
    return text if len(text) <= EXCERPT_LIMIT else text[:EXCERPT_LIMIT] + "..."
