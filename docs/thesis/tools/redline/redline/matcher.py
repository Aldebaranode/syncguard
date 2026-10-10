"""Paragraph plain text and anchor matching.

Plain text of a w:p is the concatenation of its w:t text, including runs inside w:hyperlink and
excluding w:delText, with whitespace collapsed. A w:t without xml:space="preserve" loses its edge
whitespace (that is what Word renders). A w:br, w:cr or w:tab inside a run counts as one space,
so a manual line break reads like the space the drafts write for it. Paragraphs nested inside
another paragraph (text boxes) are neither indexed nor counted in the outer paragraph's text.
"""

import os
import re
from dataclasses import dataclass

from lxml import etree

from redline.wordml import XML_SPACE, has_ancestor, owning_paragraph, qn

MIN_PREFIX_ANCHOR = 40
CANDIDATE_LIMIT = 3
CANDIDATE_MIN_SHARED_START = 10
WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class IndexedParagraph:
    """A searchable paragraph with its plain text and 1-based position in the document."""

    element: etree._Element
    text: str
    number: int
    in_table: bool


class AnchorError(ValueError):
    """An anchor that does not pick exactly one paragraph; names the candidate paragraphs.

    kind is "no_match" (zero matches), "ambiguous" (several matches, no Occurrence) or
    "occurrence" (Occurrence larger than the number of matches).
    """

    def __init__(self, kind: str, reason: str, candidates: list[IndexedParagraph]):
        """Keep the failure kind, the reason text and the candidate paragraphs for the report."""
        super().__init__(reason)
        self.kind = kind
        self.reason = reason
        self.candidates = candidates


def normalize(text: str) -> str:
    """Collapse every whitespace run to one space and trim the ends."""
    return WHITESPACE.sub(" ", text).strip()


def paragraph_text(paragraph: etree._Element) -> str:
    """Return the plain text of one w:p as defined in the module docstring."""
    pieces: list[str] = []
    for element in paragraph.iter(qn("t"), qn("tab"), qn("br"), qn("cr")):
        if owning_paragraph(element) is not paragraph:
            continue
        if element.tag == qn("t"):
            pieces.append(_rendered_text(element))
        elif element.getparent().tag == qn("r") and not has_ancestor(element, "del", stop=paragraph):
            pieces.append(" ")
    return normalize("".join(pieces))


def index_paragraphs(body: etree._Element) -> list[IndexedParagraph]:
    """List every searchable paragraph of the body (table cells included) with its text."""
    indexed: list[IndexedParagraph] = []
    for paragraph in body.iter(qn("p")):
        if owning_paragraph(paragraph) is not None:
            continue
        in_table = any(ancestor.tag == qn("tc") for ancestor in paragraph.iterancestors())
        indexed.append(IndexedParagraph(paragraph, paragraph_text(paragraph), len(indexed) + 1, in_table))
    return indexed


def anchor_matches(text: str, anchor: str) -> bool:
    """True when the anchor equals the text, or is a prefix of it and at least 40 characters long."""
    if text == anchor:
        return True
    return len(anchor) >= MIN_PREFIX_ANCHOR and text.startswith(anchor)


def match_anchor(anchor: str, paragraphs: list[IndexedParagraph], occurrence: int | None = None) -> IndexedParagraph:
    """Return the one paragraph the anchor (and optional Occurrence) selects, or raise AnchorError."""
    anchor = normalize(anchor)
    matches = [item for item in paragraphs if anchor_matches(item.text, anchor)]
    if not matches:
        raise _no_match_error(anchor, paragraphs)
    if occurrence is None:
        if len(matches) == 1:
            return matches[0]
        raise AnchorError("ambiguous", f"anchor matches {len(matches)} paragraphs; add Occurrence to pick one", matches)
    if occurrence > len(matches):
        noun = "paragraph" if len(matches) == 1 else "paragraphs"
        reason = f"Occurrence {occurrence} requested but the anchor matches {len(matches)} {noun}"
        raise AnchorError("occurrence", reason, matches)
    return matches[occurrence - 1]


def match_prefix(prefix: str, paragraphs: list[IndexedParagraph]) -> IndexedParagraph:
    """Return the one paragraph whose text starts with prefix (any length), or raise AnchorError."""
    wanted = normalize(prefix)
    matches = [item for item in paragraphs if item.text.startswith(wanted)]
    if not matches:
        raise AnchorError("no_match", "no paragraph starts with this text", [])
    if len(matches) > 1:
        raise AnchorError("ambiguous", f"matches {len(matches)} paragraphs; give a longer prefix", matches)
    return matches[0]


def _no_match_error(anchor: str, paragraphs: list[IndexedParagraph]) -> AnchorError:
    """Build the zero-match error, with hints for table-row anchors and too-short prefixes."""
    reasons = ["no paragraph matches the anchor"]
    if " | " in anchor:
        reasons.append(
            "the anchor looks like a table row ('cell | cell'); write it as 'ROW: <first cell>' or 'TABLE: <first row>'"
        )
    prefixed = [item for item in paragraphs if item.text.startswith(anchor)]
    if prefixed:
        reasons.append(
            f"the anchor is shorter than {MIN_PREFIX_ANCHOR} characters, so it must equal the whole paragraph text"
        )
        return AnchorError("no_match", "; ".join(reasons), prefixed[:CANDIDATE_LIMIT])
    return AnchorError("no_match", "; ".join(reasons), _closest(anchor, paragraphs))


def _closest(anchor: str, paragraphs: list[IndexedParagraph]) -> list[IndexedParagraph]:
    """Return up to three paragraphs that share the longest start with the anchor."""
    scored = []
    for item in paragraphs:
        shared = len(os.path.commonprefix([anchor, item.text]))
        if shared >= CANDIDATE_MIN_SHARED_START:
            scored.append((-shared, item.number, item))
    scored.sort(key=lambda entry: (entry[0], entry[1]))
    return [item for _, _, item in scored[:CANDIDATE_LIMIT]]


def _rendered_text(text_element: etree._Element) -> str:
    """Return a w:t's text as Word renders it (edge whitespace dropped unless preserved)."""
    text = text_element.text or ""
    if text_element.get(XML_SPACE) == "preserve":
        return text
    return text.strip(" \t\r\n")
