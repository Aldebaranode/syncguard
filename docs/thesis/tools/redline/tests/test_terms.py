"""Tests for building the foreign term list from evidence: italics in BAB III and *term* in the drafts."""

from lxml import etree

from docx_builder import R_NS, W_NS, para
from redline.terms import (
    collect_term_evidence,
    is_code_identifier,
    load_exclusions,
    render_terms_report,
    select_terms,
)

DRAFT = """### E-1 (closes: X-1)
Operation: replace
Anchor: A
Text:
Sistem *failover* aktif dengan *Hash-based Message Authentication Code* dan *CometBFT*.
| Kolom | Nilai |
|---|---|
| *health.interval* | *Failover* |
Rationale: r
"""


def _body(inner: str) -> etree._Element:
    """A w:body holding inner XML."""
    return etree.fromstring(f'<w:body xmlns:w="{W_NS}" xmlns:r="{R_NS}">{inner}</w:body>')


def _italic(text: str) -> str:
    """A paragraph with one italic run."""
    return f"<w:p><w:r><w:rPr><w:i/></w:rPr><w:t>{text}</w:t></w:r></w:p>"


CHAPTERS = (
    para("BAB II TINJAUAN PUSTAKA")
    + _italic("blockchain")
    + para("BAB III METODE PENELITIAN")
    + '<w:p><w:r><w:t xml:space="preserve">Kita memakai </w:t></w:r><w:r><w:rPr><w:i/></w:rPr><w:t>grace </w:t></w:r><w:r><w:rPr><w:i/></w:rPr><w:t>period</w:t></w:r><w:r><w:t>.</w:t></w:r></w:p>'
    + _italic("failover")
    + para("BAB IV HASIL DAN PEMBAHASAN")
    + _italic("deployment")
)


def test_italic_phrases_of_bab_iii_are_collected_with_runs_merged():
    """Adjacent italic runs form one phrase; italics outside BAB III are ignored."""
    evidence = collect_term_evidence(_body(CHAPTERS), {})
    assert sorted(evidence) == ["failover", "grace period"]
    assert evidence["grace period"].sources == {"BAB III (docx)": 1}


def test_starred_terms_of_the_drafts_are_collected_from_text_and_tables():
    """Italic segments of paragraph Text and of table cells count; the source names the draft letter."""
    evidence = collect_term_evidence(_body(CHAPTERS), {"C-bab3.md": DRAFT})
    assert evidence["failover"].sources == {"BAB III (docx)": 1, "draft C": 2}
    assert "hash-based message authentication code" in evidence
    assert evidence["failover"].term == "failover"


def test_the_lowercase_spelling_wins_when_both_appear():
    """Failover and failover collapse into one entry spelled as the lowercase form."""
    evidence = collect_term_evidence(_body(CHAPTERS), {"C-bab3.md": DRAFT})
    assert [key for key in evidence if key.startswith("failover")] == ["failover"]


def test_code_identifiers_are_recognised():
    """Dotted, underscored, slashed and file-like items are code, plain words and phrases are not."""
    for code in ("health.interval", "catching_up", "/net_info", ".real", "priv_validator_key.json", "run()", "--flag"):
        assert is_code_identifier(code), code
    for word in ("failover", "grace period", "double-signing", "Hash-based Message Authentication Code", "peer-to-peer"):
        assert not is_code_identifier(word), word


def test_selection_drops_code_and_excluded_proper_nouns_and_reports_why(tmp_path):
    """select_terms keeps foreign terms, and lists every dropped item with its reason."""
    exclusions = tmp_path / "exclusions.txt"
    exclusions.write_text("# header\nCometBFT | product name\nfalse | literal value\n", encoding="utf-8")
    evidence = collect_term_evidence(_body(CHAPTERS), {"C-bab3.md": DRAFT})
    selection = select_terms(evidence, load_exclusions(exclusions))
    assert [item.term for item in selection.kept] == ["failover", "grace period", "Hash-based Message Authentication Code"]
    dropped = {item.term: reason for item, reason in selection.dropped}
    assert dropped == {"CometBFT": "product name", "health.interval": "code identifier"}


def test_report_lists_each_kept_term_with_its_sources_and_the_dropped_items():
    """The Markdown report has a table of kept terms with sources, and a table of dropped items."""
    evidence = collect_term_evidence(_body(CHAPTERS), {"C-bab3.md": DRAFT})
    report = render_terms_report(select_terms(evidence, {}), "terms.txt")
    assert "| failover | BAB III (docx) 1, draft C 2 |" in report
    assert "| health.interval | code identifier |" in report
    assert "## Kept terms" in report and "## Dropped" in report


def test_an_italic_fragment_that_starts_with_a_dot_stays_a_code_identifier():
    """'.real' is a code fragment; cleaning must not strip the dot and turn it into the plain word 'real'."""
    body = _body(para("BAB III METODE") + _italic(".real") + _italic("failover."))
    evidence = collect_term_evidence(body, {})
    assert sorted(evidence) == [".real", "failover"]
    assert [item.term for item, _ in select_terms(evidence, {}).dropped] == [".real"]


def test_additions_join_the_evidence_with_their_own_source(tmp_path):
    """data/term_additions.txt lines 'term | reason' become kept terms whose source names the reason."""
    from redline.terms import add_additions, load_additions

    path = tmp_path / "additions.txt"
    path.write_text("# header\nsplit-brain | reviewer addition\nfailover | reviewer addition\n", encoding="utf-8")
    evidence = collect_term_evidence(_body(CHAPTERS), {})
    add_additions(evidence, load_additions(path))
    assert evidence["split-brain"].sources == {"addition: reviewer addition": 1}
    assert evidence["failover"].sources == {"BAB III (docx)": 1, "addition: reviewer addition": 1}
    assert [item.term for item in select_terms(evidence, {}).kept] == ["failover", "grace period", "split-brain"]
