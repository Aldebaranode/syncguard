"""First-use italics for foreign terms (finding T-4): italic at first use, upright afterwards.

Two scopes are counted separately, in document order over the accepted view (deleted text is
invisible, inserted text counts): the INTISARI section, and the body from the first chapter
heading to the reference list. Excluded: the cover and the ABSTRACT section (before them), headings,
captions, table cells, program code (monospace runs and the Kode Program style), runs inside
hyperlinks or fields, and the reference list with whatever follows it.

A term is a whole word or phrase, matched without regard to case, longest term first, so that
"double-signing" is one use of that term and not a use of "signing". Original runs are split at
the term boundaries without changing any text, and the italic toggle is recorded as a w:rPrChange
that holds the previous w:rPr. Runs inside an insertion of the same author are changed directly.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from redline.constants import REFERENCE_HEADINGS
from redline.matcher import IndexedParagraph, index_paragraphs
from redline.runs import SEPARATOR, Piece, isolate_text, set_italic, text_pieces
from redline.structure import is_caption_text, is_chapter_heading_text, is_heading
from redline.tracked import RevisionMarks
from redline.wordml import qn

INTISARI_SCOPE = "INTISARI"
BODY_SCOPE = "BAB I sampai BAB V"
CODE_STYLE_NAMES = ("kode program", "code", "source code")
EXCERPT_CONTEXT = 30
# {{DATA:..}}, {{TABLE:..}} and {{FIGURE:..}} spans are filled later and must stay whole in one w:t.
PLACEHOLDER = re.compile(r"\{\{[^{}]*\}\}")


@dataclass
class TermUse:
    """What happened to one term in one scope."""

    occurrences: int = 0
    first_excerpt: str = ""
    italicised: int = 0
    upright: int = 0
    unchanged: int = 0
    skipped: int = 0


@dataclass
class ScopeResult:
    """The counted paragraphs and per-term outcomes of one scope."""

    name: str
    paragraphs: int = 0
    terms: dict[str, TermUse] = field(default_factory=dict)


@dataclass
class ItalicsResult:
    """Scopes in document order, terms found in neither scope, and notes about missing boundaries."""

    scopes: list[ScopeResult] = field(default_factory=list)
    unfound_terms: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def load_terms(path: Path) -> list[str]:
    """Read one term per line (blank lines and lines starting with # are skipped); longest first."""
    terms: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        term = line.strip()
        if term and not term.startswith("#"):
            terms.setdefault(_key(term), term)
    return sorted(terms.values(), key=lambda term: (-len(term), term.casefold()))


def italicise_document(
    document: etree._Element, style_names: dict[str, str], terms: list[str], marks: RevisionMarks
) -> ItalicsResult:
    """Apply the first-use rule to every term in both scopes; one entry point for the flow."""
    result = ItalicsResult()
    body = document.find(qn("body"))
    paragraphs = index_paragraphs(body)
    scoped = _scope_paragraphs(paragraphs, style_names, result)
    pattern = _term_pattern(terms)
    canonical = {_key(term): term for term in terms}
    for name, members in scoped:
        scope = ScopeResult(name, len(members))
        seen: set[str] = set()
        for item in members:
            _process_paragraph(item, pattern, canonical, seen, scope, marks)
        result.scopes.append(scope)
    found = {key for scope in result.scopes for key in scope.terms}
    result.unfound_terms = [term for term in terms if _key(term) not in found]
    return result


def _key(term: str) -> str:
    """A term's identity: lower case with single spaces."""
    return " ".join(term.casefold().split())


def _term_pattern(terms: list[str]) -> re.Pattern:
    """One whole-word, case-insensitive pattern for all terms, longest alternative first."""
    ordered = sorted(terms, key=lambda term: (-len(term), term.casefold()))
    alternatives = [r"\s+".join(re.escape(word) for word in term.split()) for term in ordered]
    # A dot or slash joined to a word makes an identifier (failover.retry_attempts, /status), not a use of its parts.
    return re.compile(r"(?<![\w/])(?<!\w\.)(?:" + "|".join(alternatives) + r")(?![\w/])(?!\.\w)", re.IGNORECASE)


def _scope_paragraphs(
    paragraphs: list[IndexedParagraph], style_names: dict[str, str], result: ItalicsResult
) -> list[tuple[str, list[IndexedParagraph]]]:
    """The countable paragraphs of the INTISARI scope and of the chapter scope, with boundary notes."""
    body_level = [(index, item) for index, item in enumerate(paragraphs) if not item.in_table]
    intisari = _first_index(body_level, lambda item: item.text.casefold() == "intisari")
    abstract = _first_index(body_level, lambda item: item.text.casefold() == "abstract", after=intisari)
    chapter_from = abstract if abstract is not None else intisari
    chapter = _first_index(body_level, lambda item: is_chapter_heading_text(item.text), after=chapter_from)
    references = _first_index(body_level, lambda item: item.text.upper() in REFERENCE_HEADINGS, after=chapter)
    scopes: list[tuple[str, list[IndexedParagraph]]] = []
    if intisari is None or abstract is None:
        result.notes.append("no INTISARI section followed by ABSTRACT was found, so that scope was skipped")
    else:
        scopes.append((INTISARI_SCOPE, _countable(paragraphs[intisari + 1 : abstract], style_names)))
    if chapter is None:
        result.notes.append("no chapter heading (BAB I) was found, so the chapter scope was skipped")
    else:
        end = references if references is not None else len(paragraphs)
        if references is None:
            result.notes.append("no reference list heading was found; the chapter scope runs to the end of the document")
        scopes.append((BODY_SCOPE, _countable(paragraphs[chapter + 1 : end], style_names)))
    return scopes


def _first_index(body_level, matches, after: int | None = None) -> int | None:
    """Index in the full paragraph list of the first body-level paragraph after `after` that matches."""
    for index, item in body_level:
        if (after is None or index > after) and matches(item):
            return index
    return None


def _countable(paragraphs: list[IndexedParagraph], style_names: dict[str, str]) -> list[IndexedParagraph]:
    """Drop table cells, headings, captions, program code and empty paragraphs."""
    return [item for item in paragraphs if _is_countable(item, style_names)]


def _is_countable(item: IndexedParagraph, style_names: dict[str, str]) -> bool:
    """True for ordinary prose paragraphs."""
    if item.in_table or not item.text or is_caption_text(item.text) or is_heading(item.element, item.text, style_names):
        return False
    style = item.element.find(f"{qn('pPr')}/{qn('pStyle')}")
    style_name = style_names.get(style.get(qn("val")), "") if style is not None else ""
    return style_name not in CODE_STYLE_NAMES


def _process_paragraph(
    item: IndexedParagraph, pattern: re.Pattern, canonical: dict[str, str], seen: set[str], scope: ScopeResult, marks: RevisionMarks
) -> None:
    """Decide first or later use for each match left to right, then apply the changes right to left."""
    pieces = text_pieces(item.element, with_separators=True)
    joined = PLACEHOLDER.sub(lambda span: SEPARATOR * len(span.group(0)), "".join(piece.text if piece else SEPARATOR for piece in pieces))
    decisions = []
    for match in pattern.finditer(joined):
        key = _key(match.group(0))
        is_first = key not in seen
        seen.add(key)
        use = scope.terms.setdefault(key, TermUse())
        use.occurrences += 1
        if is_first:
            use.first_excerpt = _excerpt(joined, match)
        decisions.append((match, use, is_first))
    for match, use, wanted in reversed(decisions):
        outcome = _apply_to_match(pieces, match.start(), match.end(), wanted, marks)
        _count(use, outcome, wanted)


def _apply_to_match(pieces: list[Piece | None], start: int, end: int, wanted: bool, marks: RevisionMarks) -> str:
    """Isolate and toggle every piece slice of one match; 'skipped' beats 'changed' beats 'unchanged'."""
    outcomes = []
    for piece in pieces:
        if piece is None or piece.start + len(piece.text) <= start or piece.start >= end:
            continue
        local_start = max(start, piece.start) - piece.start
        local_end = min(end, piece.start + len(piece.text)) - piece.start
        run = isolate_text(piece.run, local_start, local_end)
        outcomes.append(set_italic(run, wanted, marks, track=not _in_own_insertion(run, marks.author)))
    for status in ("skipped", "changed"):
        if status in outcomes:
            return status
    return "unchanged"


def _in_own_insertion(run: etree._Element, author: str) -> bool:
    """True when the run sits directly in a w:ins written by this author."""
    parent = run.getparent()
    return parent.tag == qn("ins") and parent.get(qn("author")) == author


def _count(use: TermUse, outcome: str, wanted: bool) -> None:
    """Add one match outcome to the term's counters."""
    if outcome == "skipped":
        use.skipped += 1
    elif outcome == "unchanged":
        use.unchanged += 1
    elif wanted:
        use.italicised += 1
    else:
        use.upright += 1


def _excerpt(text: str, match: re.Match) -> str:
    """The match with some context on both sides, separators shown as spaces."""
    left = max(0, match.start() - EXCERPT_CONTEXT)
    right = min(len(text), match.end() + EXCERPT_CONTEXT)
    return text[left:right].replace(SEPARATOR, " ").strip()
