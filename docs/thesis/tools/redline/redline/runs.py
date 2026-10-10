"""Read a paragraph as editable text pieces, split runs at text offsets, and toggle italics with tracking.

A text piece is a run that holds plain text and nothing else (w:rPr, one w:t and at most a
rendered page break marker). Everything the italics pass must not touch is not a piece: runs in
hyperlinks, fields, monospace runs, runs with tabs, breaks or drawings. Those can optionally
appear as separators so that a term never matches across them. Deleted text is invisible, which
matches the accepted view, and proofing or bookmark markers are transparent.
"""

from copy import deepcopy
from dataclasses import dataclass

from lxml import etree

from redline.constants import OFF_VALUES
from redline.tracked import RevisionMarks, track_property_change
from redline.wordml import RUN_PROPERTY_ORDER, XML_SPACE, insert_in_order, local_name, make_element, qn, remove_children

SEPARATOR = "\x00"
TRANSPARENT = (
    "proofErr", "bookmarkStart", "bookmarkEnd", "commentRangeStart", "commentRangeEnd", "permStart", "permEnd", "del",
)
PLAIN_RUN_CHILDREN = ("rPr", "t", "lastRenderedPageBreak")
MONOSPACE_FONT_HINTS = ("courier", "consolas", "menlo", "monaco", "mono", "lucida console", "fira code", "source code")


@dataclass(frozen=True)
class Piece:
    """A plain text run of a paragraph and the offset where its text starts in the joined text."""

    run: etree._Element
    text: str
    start: int


def text_pieces(paragraph: etree._Element, with_separators: bool = False) -> list[Piece | None]:
    """The paragraph's plain text runs in order; None marks content the pass must not touch when asked for."""
    entries: list[etree._Element | None] = []
    field_depth = 0
    for child in paragraph:
        name = local_name(child)
        if name == "pPr" or name in TRANSPARENT:
            continue
        if name == "r":
            candidates = [child]
        elif name == "ins":
            candidates = [run for run in child if local_name(run) == "r"]
        else:
            entries.append(None)
            continue
        for run in candidates:
            field_depth += _field_change(run)
            entries.append(run if field_depth == 0 and _is_plain_text_run(run) else None)
    pieces: list[Piece | None] = []
    offset = 0
    for entry in entries:
        if entry is None:
            if with_separators:
                pieces.append(None)
            offset += 1
            continue
        text = entry.find(qn("t")).text or ""
        pieces.append(Piece(entry, text, offset))
        offset += len(text)
    return pieces


def isolate_text(run: etree._Element, start: int, end: int) -> etree._Element:
    """Split a plain text run so that [start, end) of its text is a run of its own; return that run.

    The leading text stays in the original element, so offsets to the left remain valid. The
    new runs copy the run's w:rPr; a rendered page break marker stays in the first piece.
    """
    text_element = run.find(qn("t"))
    text = text_element.text or ""
    if start == 0 and end == len(text):
        return run
    if start == 0:
        text_element.text = text[:end]
        _keep_spaces(text_element)
        run.addnext(_trailing_copy(run, text[end:]))
        return run
    text_element.text = text[:start]
    _keep_spaces(text_element)
    middle = _trailing_copy(run, text[start:end])
    run.addnext(middle)
    if end < len(text):
        middle.addnext(_trailing_copy(run, text[end:]))
    return middle


def is_italic(run: etree._Element) -> bool:
    """True when the run has a direct italic property that is not switched off."""
    italic = run.find(f"{qn('rPr')}/{qn('i')}")
    return italic is not None and italic.get(qn("val"), "1") not in OFF_VALUES


def set_italic(run: etree._Element, wanted: bool, marks: RevisionMarks, track: bool) -> str:
    """Make the run italic or upright; return 'unchanged', 'changed' or 'skipped'.

    With track, the previous w:rPr is stored in a w:rPrChange (used for original runs). Without
    it the run is edited directly (used inside our own insertion). A run that already carries a
    tracked format change is skipped, because a w:rPr holds only one.
    """
    if is_italic(run) == wanted:
        return "unchanged"
    properties = run.find(qn("rPr"))
    if track and properties is not None and properties.find(qn("rPrChange")) is not None:
        return "skipped"
    previous = deepcopy(properties) if properties is not None else None
    if properties is None:
        properties = make_element("rPr")
        run.insert(0, properties)
    remove_children(properties, ("i", "iCs"))
    if wanted:
        insert_in_order(properties, make_element("i"), RUN_PROPERTY_ORDER)
        insert_in_order(properties, make_element("iCs"), RUN_PROPERTY_ORDER)
    if track:
        track_property_change(properties, "rPrChange", previous, marks)
    return "changed"


def _field_change(run: etree._Element) -> int:
    """+1 for a run that begins a complex field, -1 for one that ends it, otherwise 0."""
    marker = run.find(qn("fldChar"))
    if marker is None:
        return 0
    kind = marker.get(qn("fldCharType"))
    return 1 if kind == "begin" else -1 if kind == "end" else 0


def _is_plain_text_run(run: etree._Element) -> bool:
    """True for a run of one w:t (plus rPr and a page break marker) that is not monospace."""
    children = [local_name(child) for child in run]
    if children.count("t") != 1 or any(name not in PLAIN_RUN_CHILDREN for name in children):
        return False
    return not _is_monospace(run)


def _is_monospace(run: etree._Element) -> bool:
    """True when the run's font is a monospace one (program code)."""
    fonts = run.find(f"{qn('rPr')}/{qn('rFonts')}")
    if fonts is None:
        return False
    names = [fonts.get(qn(attribute), "") for attribute in ("ascii", "hAnsi", "cs")]
    return any(hint in name.casefold() for name in names for hint in MONOSPACE_FONT_HINTS)


def _trailing_copy(run: etree._Element, text: str) -> etree._Element:
    """A copy of the run that holds text and no page break marker (the marker stays with the first piece)."""
    copy = deepcopy(run)
    remove_children(copy, ("lastRenderedPageBreak",))
    element = copy.find(qn("t"))
    element.text = text
    _keep_spaces(element)
    return copy


def _keep_spaces(text_element: etree._Element) -> None:
    """Set xml:space=preserve so that edge spaces survive."""
    text_element.set(XML_SPACE, "preserve")
