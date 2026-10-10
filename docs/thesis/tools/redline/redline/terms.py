"""Build the foreign term list for the italics pass from evidence, not taste.

Evidence has two sources: every word or phrase that is italic in BAB III of the original docx
(the examiner found BAB III consistent), and every *term* in the Text of the drafts. Product and
proper names (a list kept in data/term_exclusions.txt, each with its reason) and code identifiers
are dropped. The report names the sources of every kept term and the reason for every dropped
one, so the author can prune the list before the italics pass uses it.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from redline.edits import parse_edits
from redline.markup import MarkupError, ParagraphBlock, parse_text_blocks
from redline.matcher import index_paragraphs
from redline.runs import is_italic
from redline.structure import is_chapter_heading_text
from redline.wordml import has_ancestor, owning_paragraph, qn

DOCX_SOURCE = "BAB III (docx)"
BAB_III = re.compile(r"^BAB\s+III(?![IVX])")
EDGE_PUNCTUATION = " .,;:!?()[]\"'“”‘’"
# A leading dot or slash belongs to a code fragment (.real, /status), so it is not edge punctuation on the left.
LEFT_EDGE_PUNCTUATION = EDGE_PUNCTUATION.replace(".", "")
CODE_PATTERNS = (
    re.compile(r"^[/.\-]"),
    re.compile(r"_"),
    re.compile(r"[A-Za-z0-9]\.[A-Za-z]"),
    re.compile(r"[(){}`=\\]"),
)
DEFAULT_ADDITIONS_PATH = Path(__file__).resolve().parent.parent / "data" / "term_additions.txt"
DEFAULT_EXCLUSIONS_PATH = Path(__file__).resolve().parent.parent / "data" / "term_exclusions.txt"


@dataclass
class TermEvidence:
    """One term (by lower-case key), the spellings seen, and where each source saw it."""

    term: str
    sources: Counter = field(default_factory=Counter)
    spellings: Counter = field(default_factory=Counter)


@dataclass
class Selection:
    """Kept terms in alphabetical order, and dropped terms with the reason."""

    kept: list[TermEvidence] = field(default_factory=list)
    dropped: list[tuple[TermEvidence, str]] = field(default_factory=list)


def collect_term_evidence(body: etree._Element, drafts: dict[str, str]) -> dict[str, TermEvidence]:
    """Gather italic phrases of BAB III and starred terms of the drafts, keyed by lower-case term."""
    evidence: dict[str, TermEvidence] = {}
    for phrase in _bab_iii_italics(body):
        _add(evidence, phrase, DOCX_SOURCE)
    for name, text in drafts.items():
        for phrase in _starred_terms(text, name):
            _add(evidence, phrase, f"draft {Path(name).name.split('-')[0]}")
    for item in evidence.values():
        item.term = _preferred_spelling(item.spellings)
    return evidence


def load_additions(path: Path) -> dict[str, str]:
    """Read 'term | reason' lines (blank lines and # comments skipped) of terms the author wants on the list."""
    additions = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            term, _, reason = line.partition(" | ")
            additions[term.strip()] = reason.strip() or "added"
    return additions


def add_additions(evidence: dict[str, TermEvidence], additions: dict[str, str]) -> None:
    """Record each addition as one sighting from the source 'addition: <reason>'."""
    for term, reason in additions.items():
        _add(evidence, term, f"addition: {reason}")
    for item in evidence.values():
        item.term = _preferred_spelling(item.spellings)


def is_code_identifier(term: str) -> bool:
    """True for items that are code (a dotted or underscored name, a path, a call, a flag)."""
    return any(pattern.search(term) for pattern in CODE_PATTERNS)


def load_exclusions(path: Path) -> dict[str, str]:
    """Read 'term | reason' lines (blank lines and # comments skipped) into lower-case term to reason."""
    exclusions = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        term, _, reason = line.partition(" | ")
        exclusions[" ".join(term.casefold().split())] = reason.strip() or "excluded"
    return exclusions


def select_terms(evidence: dict[str, TermEvidence], exclusions: dict[str, str]) -> Selection:
    """Split the evidence into kept terms and dropped ones (excluded names first, then code identifiers)."""
    selection = Selection()
    for key in sorted(evidence):
        item = evidence[key]
        if key in exclusions:
            selection.dropped.append((item, exclusions[key]))
        elif is_code_identifier(item.term):
            selection.dropped.append((item, "code identifier"))
        else:
            selection.kept.append(item)
    return selection


def render_terms_report(selection: Selection, output: str) -> str:
    """Markdown report: kept terms with their sources, and dropped items with the reason."""
    lines = ["# Foreign term list", "", f"- Output: `{output}`", f"- Kept: {len(selection.kept)}; dropped: {len(selection.dropped)}"]
    lines += ["", "## Kept terms", "", "| Term | Sources |", "|---|---|"]
    lines += [f"| {item.term} | {_sources_text(item.sources)} |" for item in selection.kept]
    lines += ["", "## Dropped", "", "| Item | Reason |", "|---|---|"]
    lines += [f"| {item.term} | {reason} |" for item, reason in selection.dropped]
    return "\n".join(lines) + "\n"


def _sources_text(sources: Counter) -> str:
    """'BAB III (docx) 1, draft C 2': the docx first, then drafts in letter order."""
    ordered = sorted(sources, key=lambda name: (name != DOCX_SOURCE, name))
    return ", ".join(f"{name} {sources[name]}" for name in ordered)


def _add(evidence: dict[str, TermEvidence], phrase: str, source: str) -> None:
    """Record one sighting of a phrase."""
    cleaned = _clean(phrase)
    if not cleaned:
        return
    item = evidence.setdefault(" ".join(cleaned.casefold().split()), TermEvidence(cleaned))
    item.sources[source] += 1
    item.spellings[cleaned] += 1


def _clean(phrase: str) -> str:
    """Single spaces, no quotes or sentence punctuation at the edges (a leading dot or slash is kept)."""
    return " ".join(phrase.split()).lstrip(LEFT_EDGE_PUNCTUATION).rstrip(EDGE_PUNCTUATION)


def _preferred_spelling(spellings: Counter) -> str:
    """A spelling that starts in lower case when one exists (matching ignores case), else the most common."""
    lowercase = [spelling for spelling in spellings if spelling[:1].islower()]
    pool = lowercase or list(spellings)
    return max(pool, key=lambda spelling: spellings[spelling])


def _bab_iii_italics(body: etree._Element) -> list[str]:
    """Italic phrases in BAB III: the paragraphs from its heading up to the next chapter heading."""
    paragraphs = index_paragraphs(body)
    start = next((index for index, item in enumerate(paragraphs) if not item.in_table and BAB_III.match(item.text) and is_chapter_heading_text(item.text)), None)
    if start is None:
        return []
    end = next((index for index, item in enumerate(paragraphs) if index > start and not item.in_table and is_chapter_heading_text(item.text)), len(paragraphs))
    phrases: list[str] = []
    for item in paragraphs[start + 1 : end]:
        phrases.extend(_italic_phrases(item.element))
    return phrases


def _italic_phrases(paragraph: etree._Element) -> list[str]:
    """Maximal stretches of italic text in a paragraph; a whitespace-only plain run does not end a stretch."""
    phrases: list[str] = []
    buffer = ""
    for run in paragraph.iter(qn("r")):
        if owning_paragraph(run) is not paragraph or has_ancestor(run, "del", stop=paragraph):
            continue
        text = "".join(piece.text or "" for piece in run.findall(qn("t")))
        if not text:
            continue
        if is_italic(run) or (buffer and not text.strip()):
            buffer += text
            continue
        if buffer:
            phrases.append(buffer)
            buffer = ""
    if buffer:
        phrases.append(buffer)
    return phrases


def _starred_terms(draft_text: str, name: str) -> list[str]:
    """Italic segments of every edit's Text, paragraphs and table cells alike."""
    phrases = []
    for edit in parse_edits(draft_text, name).edits:
        try:
            blocks = parse_text_blocks(edit.text_lines)
        except MarkupError:
            continue
        for block in blocks:
            segment_lists = [block.segments] if isinstance(block, ParagraphBlock) else [cell for row in block.rows for cell in row]
            phrases.extend(segment.text for segments in segment_lists for segment in segments if segment.italic)
    return phrases
