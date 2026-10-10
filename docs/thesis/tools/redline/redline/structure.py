"""Document structure helpers shared by the passes: headings, captions, and the section that holds an element with its text column width.

Headings in the manuscript are not always styled (a Heading style is often replaced by direct
formatting), so a paragraph also counts as a heading by its text: a chapter title, a numbered
section title, or a short all-bold line that does not end in a full stop.
"""

import re

from lxml import etree

from redline.constants import OFF_VALUES
from redline.matcher import paragraph_text
from redline.wordml import owning_paragraph, qn

CAPTION_TEXT = re.compile(r"^(Gambar|Tabel|Kode Program|Algoritma)\s+\d")
CHAPTER_TEXT = re.compile(r"^BAB\s+[IVX]+")
NUMBERED_TEXT = re.compile(r"^\d+(\.\d+)+\.?\s+\S")
HEADING_STYLE_NAMES = ("title", "subtitle")
HEADING_MAX_LENGTH = 120
BOLD_HEADING_MAX_LENGTH = 100
MAX_EMPTY_BETWEEN = 1


def paragraph_style_names(styles_root: etree._Element) -> dict[str, str]:
    """Map each paragraph style id to its lower-case display name."""
    names = {}
    for style in styles_root.iter(qn("style")):
        name = style.find(qn("name"))
        if style.get(qn("type")) == "paragraph" and name is not None:
            names[style.get(qn("styleId"))] = name.get(qn("val"), "").casefold()
    return names


def is_caption_text(text: str) -> bool:
    """True for a figure, table, listing or algorithm caption such as 'Gambar 3.1 ...'."""
    return CAPTION_TEXT.match(text) is not None


def is_chapter_heading_text(text: str) -> bool:
    """True for a chapter title such as 'BAB III METODE PENELITIAN' (a sentence about a chapter is not one)."""
    return CHAPTER_TEXT.match(text) is not None and len(text) <= HEADING_MAX_LENGTH and not text.endswith(".")


def is_heading(paragraph: etree._Element, text: str, style_names: dict[str, str]) -> bool:
    """True when the paragraph is a heading by style, by chapter or section numbering, or by all-bold short text."""
    if not text:
        return False
    style = paragraph.find(f"{qn('pPr')}/{qn('pStyle')}")
    style_name = style_names.get(style.get(qn("val")), "") if style is not None else ""
    if style_name.startswith("heading") or style_name in HEADING_STYLE_NAMES:
        return True
    if is_chapter_heading_text(text):
        return True
    if NUMBERED_TEXT.match(text) and len(text) <= HEADING_MAX_LENGTH and not text.endswith("."):
        return True
    return len(text) <= BOLD_HEADING_MAX_LENGTH and not text.endswith(".") and _all_text_bold(paragraph)


def text_column_width(element: etree._Element) -> int:
    """Text column width in twips of the section that holds element: page width minus left and right margins."""
    size, margins = _page_geometry(element)
    return int(size.get(qn("w"))) - int(margins.get(qn("left"))) - int(margins.get(qn("right")))


def text_area_height(element: etree._Element) -> int:
    """Text area height in twips of the section that holds element: page height minus top and bottom margins."""
    size, margins = _page_geometry(element)
    return int(size.get(qn("h"))) - int(margins.get(qn("top"))) - int(margins.get(qn("bottom")))


def holding_section(element: etree._Element) -> etree._Element:
    """The w:sectPr that ends the section holding element (any element inside the body)."""
    body = element.getparent()
    top_level = element
    while body is not None and body.tag != qn("body"):
        top_level, body = body, body.getparent()
    if body is None:
        raise ValueError("element is not inside a w:body")
    return _section_of(top_level, body)


def _page_geometry(element: etree._Element) -> tuple[etree._Element, etree._Element]:
    """The w:pgSz and w:pgMar of the section holding element."""
    section = holding_section(element)
    size = section.find(qn("pgSz"))
    margins = section.find(qn("pgMar"))
    if size is None or margins is None:
        raise ValueError("the section has no page size or margins")
    return size, margins


def column_width_or_none(element: etree._Element) -> int | None:
    """text_column_width, or None when the section does not say (a bare fixture, a partial tree)."""
    try:
        return text_column_width(element)
    except ValueError:
        return None


def _section_of(top_level: etree._Element, body: etree._Element) -> etree._Element:
    """The w:sectPr that ends the section holding a body child: the next section break, else the body's own."""
    sibling = top_level
    while sibling is not None:
        found = sibling.find(f"{qn('pPr')}/{qn('sectPr')}")
        if found is not None:
            return found
        sibling = sibling.getnext()
    final = body.find(qn("sectPr"))
    if final is None:
        raise ValueError("the document body has no final section properties")
    return final


def _all_text_bold(paragraph: etree._Element) -> bool:
    """True when the paragraph has text and every run that carries text is bold."""
    seen_text = False
    for run in paragraph.iter(qn("r")):
        if not any(text.text for text in run.iter(qn("t"))):
            continue
        seen_text = True
        bold = run.find(f"{qn('rPr')}/{qn('b')}")
        if bold is None or bold.get(qn("val"), "1") in OFF_VALUES:
            return False
    return seen_text


def body_font_name(styles_root: etree._Element) -> str | None:
    """The body font: the Normal style's own font if it names one, else the document default font."""
    for style in styles_root.iter(qn("style")):
        if style.get(qn("type")) == "paragraph" and style.get(qn("default")) in ("1", "true", "on"):
            fonts = style.find(f"{qn('rPr')}/{qn('rFonts')}")
            if fonts is not None and fonts.get(qn("ascii")):
                return fonts.get(qn("ascii"))
    default = styles_root.find(f"{qn('docDefaults')}/{qn('rPrDefault')}/{qn('rPr')}/{qn('rFonts')}")
    return default.get(qn("ascii")) if default is not None else None


def has_image(paragraph: etree._Element) -> bool:
    """True when a run of the paragraph (deleted or not) holds a drawing or a VML picture."""
    for run in paragraph.iter(qn("r")):
        if owning_paragraph(run) is paragraph and (run.find(qn("drawing")) is not None or run.find(qn("pict")) is not None):
            return True
    return False


def find_image_paragraph(caption: etree._Element) -> etree._Element | None:
    """The image paragraph directly above a caption: the nearest earlier paragraph with a picture.

    One empty paragraph may sit between the two. Any text, a table, or a second empty paragraph
    in between means the caption has no picture of its own and None is returned.
    """
    empty_seen = 0
    sibling = caption.getprevious()
    while sibling is not None and sibling.tag == qn("p"):
        if has_image(sibling):
            return sibling
        if paragraph_text(sibling):
            return None
        empty_seen += 1
        if empty_seen > MAX_EMPTY_BETWEEN:
            return None
        sibling = sibling.getprevious()
    return None
