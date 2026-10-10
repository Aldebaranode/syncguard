"""WordprocessingML names and small element helpers shared by the other modules."""

from lxml import etree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

# Child order of w:rPr (EG_RPrBase), preceded by the revision markers that only a paragraph
# mark's w:rPr may hold (CT_ParaRPr). Used to insert new children at a schema-valid position.
RUN_PROPERTY_ORDER = (
    "ins", "del", "moveFrom", "moveTo",
    "rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike", "dstrike",
    "outline", "shadow", "emboss", "imprint", "noProof", "snapToGrid", "vanish", "webHidden",
    "color", "spacing", "w", "kern", "position", "sz", "szCs", "highlight", "u", "effect",
    "bdr", "shd", "fitText", "vertAlign", "rtl", "cs", "em", "lang", "eastAsianLayout",
    "specVanish", "oMath", "rPrChange",
)
# Child order of w:pPr: CT_PPrBase, then the paragraph mark, section break and change of CT_PPr.
PARAGRAPH_PROPERTY_ORDER = (
    "pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr", "widowControl", "numPr",
    "suppressLineNumbers", "pBdr", "shd", "tabs", "suppressAutoHyphens", "kinsoku", "wordWrap",
    "overflowPunct", "topLinePunct", "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd",
    "snapToGrid", "spacing", "ind", "contextualSpacing", "mirrorIndents", "suppressOverlap", "jc",
    "textDirection", "textAlignment", "textboxTightWrap", "outlineLvl", "divId", "cnfStyle",
    "rPr", "sectPr", "pPrChange",
)


def qn(tag: str) -> str:
    """Return the Clark name of a w: element or attribute, e.g. qn('p') for w:p."""
    return f"{{{W_NS}}}{tag}"


def local_name(element: etree._Element) -> str:
    """Return an element's tag without its namespace."""
    return etree.QName(element).localname


def make_element(tag: str, attributes: dict[str, str] | None = None) -> etree._Element:
    """Create a detached w: element with w: attributes."""
    element = etree.Element(qn(tag), nsmap={"w": W_NS})
    for name, value in (attributes or {}).items():
        element.set(qn(name), value)
    return element


def owning_paragraph(element: etree._Element) -> etree._Element | None:
    """Return the nearest w:p ancestor of an element, or None."""
    for ancestor in element.iterancestors(qn("p")):
        return ancestor
    return None


def insert_in_order(parent: etree._Element, child: etree._Element, order: tuple[str, ...]) -> etree._Element:
    """Insert child before the first sibling that the schema order places after it."""
    rank = order.index(local_name(child))
    for index, existing in enumerate(parent):
        existing_name = local_name(existing)
        if existing_name in order and order.index(existing_name) > rank:
            parent.insert(index, child)
            return child
    parent.append(child)
    return child


def remove_children(parent: etree._Element, names: tuple[str, ...]) -> None:
    """Remove every direct child whose local name is in names."""
    for child in list(parent):
        if local_name(child) in names:
            parent.remove(child)


def has_ancestor(element: etree._Element, tag: str, stop: etree._Element | None = None) -> bool:
    """True when an ancestor of element (below stop, if given) has the w: tag."""
    for ancestor in element.iterancestors():
        if ancestor is stop:
            return False
        if ancestor.tag == qn(tag):
            return True
    return False
