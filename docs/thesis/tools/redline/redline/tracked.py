"""Tracked-change primitives: revision marks, deleted runs, inserted runs and paragraphs.

Every w:ins and w:del comes from one RevisionMarks object, so the whole run shares one id
counter (starting above every w:id already in the document), one author and one date.
"""

from copy import deepcopy

from lxml import etree

from redline.markup import Segment
from redline.wordml import (
    PARAGRAPH_PROPERTY_ORDER,
    RUN_PROPERTY_ORDER,
    XML_SPACE,
    has_ancestor,
    insert_in_order,
    local_name,
    make_element,
    owning_paragraph,
    qn,
    remove_children,
)

REVISION_MARKERS = ("ins", "del", "moveFrom", "moveTo")
# What a stored original may not contain, per change element: the markers, the paragraph mark
# and section break (pPr only) and any earlier change.
ORIGINAL_EXCLUDES = {
    "rPrChange": REVISION_MARKERS + ("rPrChange",),
    "pPrChange": ("rPr", "sectPr", "pPrChange"),
    "trPrChange": REVISION_MARKERS + ("trPrChange",),
}
LAYOUT_BREAK_TYPES = ("page", "column")


class RevisionMarks:
    """Creates w:ins and w:del elements with one shared id counter, author and date."""

    def __init__(self, author: str, date: str, first_id: int):
        """Start the id counter at first_id; count nothing yet."""
        self.author = author
        self.date = date
        self._next_id = first_id
        self.inserted_count = 0
        self.deleted_count = 0
        self.property_changed_count = 0

    @classmethod
    def for_document(cls, document: etree._Element, author: str, date: str) -> "RevisionMarks":
        """Start ids one above the highest numeric w:id already used in the document."""
        highest = 0
        for element in document.iter(tag=etree.Element):
            value = element.get(qn("id"))
            if value is not None and value.isdigit():
                highest = max(highest, int(value))
        return cls(author, date, highest + 1)

    def insertion(self) -> etree._Element:
        """Return a new w:ins element."""
        self.inserted_count += 1
        return self._revision("ins")

    def deletion(self) -> etree._Element:
        """Return a new w:del element."""
        self.deleted_count += 1
        return self._revision("del")

    def property_change(self, tag: str) -> etree._Element:
        """Return a new w:rPrChange, w:pPrChange or w:trPrChange element."""
        self.property_changed_count += 1
        return self._revision(tag)

    def discard(self, revision: etree._Element) -> None:
        """Uncount a w:ins or w:del that is being removed again within the same run."""
        if revision.tag == qn("ins"):
            self.inserted_count -= 1
        elif revision.tag == qn("del"):
            self.deleted_count -= 1

    def _revision(self, tag: str) -> etree._Element:
        """Create one revision element and advance the id counter."""
        element = make_element(tag, {"id": str(self._next_id), "author": self.author, "date": self.date})
        self._next_id += 1
        return element


def delete_runs(paragraph: etree._Element, marks: RevisionMarks) -> etree._Element | None:
    """Wrap every content run of the paragraph in w:del and return where inserted runs belong.

    Runs inside hyperlinks are wrapped in place (w:del inside w:hyperlink). Consecutive sibling
    runs share one w:del. Runs that hold only a page or column break are layout and stay as they
    are. The return value is the paragraph child holding the last deletion, so text inserted
    after it lands before any trailing page break.
    """
    groups: list[list[etree._Element]] = []
    for run in paragraph.iter(qn("r")):
        if owning_paragraph(run) is not paragraph or not _has_content(run) or has_ancestor(run, "del", stop=paragraph):
            continue
        if groups and run.getprevious() is groups[-1][-1]:
            groups[-1].append(run)
        else:
            groups.append([run])
    last_wrapper = None
    for group in groups:
        wrapper = marks.deletion()
        group[0].addprevious(wrapper)
        for run in group:
            wrapper.append(run)
            turn_into_deleted_text(run)
        last_wrapper = wrapper
    if last_wrapper is None:
        return None
    return _paragraph_child(last_wrapper, paragraph)


def append_inserted_runs(
    paragraph: etree._Element,
    segments: tuple[Segment, ...],
    base_properties: etree._Element | None,
    marks: RevisionMarks,
    after: etree._Element | None = None,
) -> etree._Element | None:
    """Add the segments as runs inside one w:ins, after the given child or at the paragraph end."""
    runs = make_runs(segments, base_properties)
    if not runs:
        return None
    wrapper = marks.insertion()
    for run in runs:
        wrapper.append(run)
    if after is None:
        paragraph.append(wrapper)
    else:
        after.addnext(wrapper)
    return wrapper


def make_runs(segments: tuple[Segment, ...], base_properties: etree._Element | None) -> list[etree._Element]:
    """Build one w:r per non-empty segment, copying base_properties and adding bold or italic."""
    runs = []
    for segment in segments:
        if not segment.text:
            continue
        properties = deepcopy(base_properties) if base_properties is not None else make_element("rPr")
        if segment.bold:
            _set_toggle(properties, "b", "bCs")
        if segment.italic:
            _set_toggle(properties, "i", "iCs")
        run = make_element("r")
        if len(properties):
            run.append(properties)
        text = etree.SubElement(run, qn("t"))
        text.text = segment.text
        text.set(XML_SPACE, "preserve")
        runs.append(run)
    return runs


def build_inserted_paragraph(
    properties: etree._Element | None,
    segments: tuple[Segment, ...],
    base_properties: etree._Element | None,
    marks: RevisionMarks,
) -> etree._Element:
    """Build a new w:p whose mark is a tracked insertion and whose runs sit in one w:ins."""
    paragraph = make_element("p")
    if properties is None:
        properties = make_element("pPr")
    paragraph.append(properties)
    _mark_properties(properties).insert(0, marks.insertion())
    append_inserted_runs(paragraph, segments, base_properties, marks)
    return paragraph


def mark_paragraph_deleted(paragraph: etree._Element, marks: RevisionMarks) -> None:
    """Record the paragraph mark as deleted: w:del first in w:pPr/w:rPr (after an existing w:ins)."""
    mark = _mark_properties(_paragraph_properties(paragraph))
    position = 1 if len(mark) and local_name(mark[0]) == "ins" else 0
    mark.insert(position, marks.deletion())


def can_track_paragraph_change(paragraph: etree._Element, author: str) -> bool:
    """True when a change of the paragraph's w:pPr can be recorded: author inserted it, or it holds no w:pPrChange yet."""
    if _is_own_insertion(paragraph, author):
        return True
    return paragraph.find(f"{qn('pPr')}/{qn('pPrChange')}") is None


def open_paragraph_change(paragraph: etree._Element, marks: RevisionMarks) -> etree._Element:
    """Return the paragraph's w:pPr for the caller to change, with its current state already recorded.

    A paragraph the author inserted is new text, so it changes directly. Any other paragraph first
    stores its current w:pPr in a w:pPrChange. A w:pPr holds one change, so callers ask
    can_track_paragraph_change before they change anything.
    """
    properties = _paragraph_properties(paragraph)
    if _is_own_insertion(paragraph, marks.author):
        return properties
    # Recorded before the caller changes anything, so the current w:pPr is the original.
    if not track_property_change(properties, "pPrChange", properties, marks):
        raise ValueError("the paragraph already holds a w:pPrChange; ask can_track_paragraph_change first")
    return properties


def copied_paragraph_properties(paragraph: etree._Element) -> etree._Element | None:
    """Copy a paragraph's w:pPr without its section break, property change or revision markers."""
    properties = paragraph.find(qn("pPr"))
    if properties is None:
        return None
    copy = deepcopy(properties)
    remove_children(copy, ("sectPr", "pPrChange"))
    mark = copy.find(qn("rPr"))
    if mark is not None:
        remove_children(mark, REVISION_MARKERS + ("rPrChange",))
    return copy


def styled_paragraph_properties(style_id: str) -> etree._Element:
    """Return a w:pPr that holds only the given paragraph style."""
    properties = make_element("pPr")
    properties.append(make_element("pStyle", {"val": style_id}))
    return properties


def set_paragraph_property(properties: etree._Element, name: str, value: etree._Element | None) -> None:
    """Replace the w:pPr child called name with value at its schema position; None only removes it."""
    remove_children(properties, (name,))
    if value is not None:
        insert_in_order(properties, value, PARAGRAPH_PROPERTY_ORDER)


def figure_paragraph_properties(style_id: str) -> etree._Element:
    """A w:pPr for a figure: the style, kept with the caption below, single spaced and centred.

    The manuscript's own image paragraphs are centred with single line spacing; keepNext is added
    so that a figure and its caption (which sits below it) never separate across pages.
    """
    properties = styled_paragraph_properties(style_id)
    properties.append(make_element("keepNext"))
    properties.append(make_element("spacing", {"line": "240", "lineRule": "auto"}))
    properties.append(make_element("jc", {"val": "center"}))
    return properties


def first_text_run_properties(paragraph: etree._Element) -> etree._Element | None:
    """Copy the w:rPr of the paragraph's first run that carries text (None when it has none)."""
    for run in paragraph.iter(qn("r")):
        if owning_paragraph(run) is not paragraph or run.find(qn("t")) is None:
            continue
        properties = run.find(qn("rPr"))
        if properties is None:
            return None
        copy = deepcopy(properties)
        remove_children(copy, ("rPrChange",))
        return copy
    return None


def track_property_change(
    properties: etree._Element, change_tag: str, original: etree._Element | None, marks: RevisionMarks
) -> bool:
    """Append a w:rPrChange, w:pPrChange or w:trPrChange to properties, holding a copy of original.

    original is the properties element as it was before the edit (None when there was none). The
    change goes last, which is where the schema puts it. A properties element holds one change,
    so when it already has one nothing is added and False is returned.
    """
    if properties.find(qn(change_tag)) is not None:
        return False
    holder_name = change_tag[: -len("Change")]
    stored = deepcopy(original) if original is not None else make_element(holder_name)
    remove_children(stored, ORIGINAL_EXCLUDES[change_tag])
    change = marks.property_change(change_tag)
    change.append(stored)
    properties.append(change)
    return True


def _has_content(run: etree._Element) -> bool:
    """True when the run holds anything besides properties and layout-only page or column breaks."""
    for child in run:
        name = local_name(child)
        if name in ("rPr", "lastRenderedPageBreak"):
            continue
        if name == "br" and child.get(qn("type")) in LAYOUT_BREAK_TYPES:
            continue
        return True
    return False


def turn_into_deleted_text(run: etree._Element) -> None:
    """Rename w:t to w:delText and w:instrText to w:delInstrText inside a deleted run."""
    for text in list(run.iter(qn("t"))):
        text.tag = qn("delText")
    for instruction in list(run.iter(qn("instrText"))):
        instruction.tag = qn("delInstrText")


def _paragraph_child(element: etree._Element, paragraph: etree._Element) -> etree._Element:
    """Return the direct child of the paragraph that contains element."""
    while element.getparent() is not paragraph:
        element = element.getparent()
    return element


def _paragraph_properties(paragraph: etree._Element) -> etree._Element:
    """The paragraph's w:pPr, created as its first child when it has none."""
    properties = paragraph.find(qn("pPr"))
    if properties is None:
        properties = make_element("pPr")
        paragraph.insert(0, properties)
    return properties


def _is_own_insertion(paragraph: etree._Element, author: str) -> bool:
    """True when the paragraph mark is a tracked insertion by author."""
    mark = paragraph.find(f"{qn('pPr')}/{qn('rPr')}/{qn('ins')}")
    return mark is not None and mark.get(qn("author")) == author


def _mark_properties(properties: etree._Element) -> etree._Element:
    """Return the paragraph mark's w:rPr inside w:pPr, creating it before sectPr or pPrChange."""
    mark = properties.find(qn("rPr"))
    if mark is not None:
        return mark
    mark = make_element("rPr")
    for index, child in enumerate(properties):
        if local_name(child) in ("sectPr", "pPrChange"):
            properties.insert(index, mark)
            return mark
    properties.append(mark)
    return mark


def _set_toggle(properties: etree._Element, name: str, complex_script_name: str) -> None:
    """Turn a toggle property (bold or italic) on, replacing any existing setting."""
    remove_children(properties, (name, complex_script_name))
    insert_in_order(properties, make_element(name), RUN_PROPERTY_ORDER)
    insert_in_order(properties, make_element(complex_script_name), RUN_PROPERTY_ORDER)
