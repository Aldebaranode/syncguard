"""Tests for the first-use italics pass over INTISARI and the chapters."""

from lxml import etree

from docx_builder import DEFAULT_STYLES, R_NS, W_NS, para
from redline.italics import italicise_document, load_terms
from redline.matcher import paragraph_text
from redline.structure import paragraph_style_names
from redline.tracked import RevisionMarks

NSMAP = {"w": W_NS}
DATE = "2026-10-05T08:00:00Z"
AUTHOR = "Syahid Nurrohim"


def _document(body: str) -> etree._Element:
    """A w:document holding body XML (no section properties needed by this pass)."""
    return etree.fromstring(f'<w:document xmlns:w="{W_NS}" xmlns:r="{R_NS}"><w:body>{body}</w:body></w:document>')


def _run(terms: list[str], body: str, author: str = AUTHOR):
    """Run the pass on a body and return the result and the document."""
    document = _document(body)
    names = paragraph_style_names(etree.fromstring(DEFAULT_STYLES.encode("utf-8")))
    marks = RevisionMarks.for_document(document, author, DATE)
    return italicise_document(document, names, terms, marks), document


FRAME_BEFORE = (
    para("SKRIPSI failover")
    + para("INTISARI")
    + "{intisari}"
    + para("ABSTRACT")
    + para("The failover system.")
    + para("BAB I PENDAHULUAN")
)
FRAME_AFTER = para("DAFTAR PUSTAKA") + para("[1] failover entry")


def _frame(intisari: str, chapter: str) -> str:
    """A manuscript-shaped body: cover, INTISARI, ABSTRACT, a chapter, then the reference list."""
    return FRAME_BEFORE.replace("{intisari}", intisari) + chapter + FRAME_AFTER


def _italic_texts(document: etree._Element) -> list[str]:
    """Text of every run whose own properties say italic and not off."""
    texts = []
    for run in document.iter(f"{{{W_NS}}}r"):
        italic = run.find("w:rPr/w:i", NSMAP)
        if italic is not None and italic.get(f"{{{W_NS}}}val", "1") not in ("0", "false"):
            texts.append("".join(run.xpath("w:t/text()", namespaces=NSMAP)))
    return texts


def test_load_terms_skips_blank_and_comment_lines_and_orders_longest_first(tmp_path):
    """One term per line; duplicates (ignoring case) collapse; the longest term comes first."""
    path = tmp_path / "terms.txt"
    path.write_text("# sumber\nfailover\n\nDouble-signing\nfailover\nsigning\n", encoding="utf-8")
    assert load_terms(path) == ["Double-signing", "failover", "signing"]


def test_first_use_is_italic_and_later_uses_stay_upright_in_each_scope():
    """INTISARI and the chapters count separately; the cover, ABSTRACT and the reference list are skipped."""
    body = _frame(para("Sistem failover aktif. Failover lagi."), para("Bab ini memakai failover. Lalu failover lagi."))
    result, document = _run(["failover"], body)
    assert _italic_texts(document) == ["failover", "failover"]
    paragraphs = [paragraph_text(p) for p in document.iter(f"{{{W_NS}}}p")]
    assert paragraphs[0] == "SKRIPSI failover"
    intisari = result.scopes[0]
    chapters = result.scopes[1]
    assert (intisari.name, chapters.name) == ("INTISARI", "BAB I sampai BAB V")
    assert intisari.terms["failover"].occurrences == 2 and intisari.terms["failover"].italicised == 1
    assert chapters.terms["failover"].occurrences == 2


def test_the_split_keeps_the_text_and_records_the_previous_formatting():
    """The matched text becomes its own run with rPrChange; the paragraph text is unchanged."""
    body = _frame(para("Tanpa."), para("Bab memakai failover aktif."))
    _, document = _run(["failover"], body)
    chapter = list(document.iter(f"{{{W_NS}}}p"))[6]
    assert paragraph_text(chapter) == "Bab memakai failover aktif."
    assert [r.xpath("string(w:t)", namespaces=NSMAP) for r in chapter.findall("w:r", NSMAP)] == ["Bab memakai ", "failover", " aktif."]
    changed = chapter.findall("w:r", NSMAP)[1].find("w:rPr", NSMAP)
    assert [etree.QName(child).localname for child in changed] == ["i", "iCs", "rPrChange"]
    assert changed.find("w:rPrChange", NSMAP).get(f"{{{W_NS}}}author") == AUTHOR


def test_longest_term_wins_and_matching_is_whole_word_and_case_insensitive():
    """double-signing is one use of that term, not a use of signing; failover_count and failovers do not match."""
    body = _frame(para("Tanpa."), para("Double-signing dan signing. failover_count, failovers, Failover."))
    result, document = _run(["double-signing", "signing", "failover"], body)
    assert _italic_texts(document) == ["Double-signing", "signing", "Failover"]
    uses = result.scopes[1].terms
    assert uses["double-signing"].occurrences == 1 and uses["signing"].occurrences == 1 and uses["failover"].occurrences == 1


def test_a_term_over_two_runs_is_italicised_in_both():
    """Runs that split a term are both toggled and the text stays."""
    runs = '<w:p><w:r><w:t xml:space="preserve">Ada double-</w:t></w:r><w:proofErr w:type="spellStart"/><w:r><w:t>signing ya.</w:t></w:r></w:p>'
    _, document = _run(["double-signing"], _frame(para("x"), runs))
    assert _italic_texts(document) == ["double-", "signing"]


def test_headings_captions_cells_hyperlinks_code_and_fields_are_skipped_and_do_not_count():
    """None of these count as the first use; the first body sentence still gets the italics."""
    chapter = (
        para("1.1 Tentang failover")
        + para("Gambar 3.1 Alur failover")
        + f"<w:tbl><w:tr><w:tc>{para('failover di sel')}</w:tc></w:tr></w:tbl>"
        + '<w:p><w:hyperlink><w:r><w:t>failover [1]</w:t></w:r></w:hyperlink></w:p>'
        + '<w:p><w:r><w:rPr><w:rFonts w:ascii="Courier New" w:hAnsi="Courier New"/></w:rPr><w:t>failover()</w:t></w:r></w:p>'
        + para("Kode Program 4.1 failover", ppr="")
        + para("Kalimat isi memuat failover.")
    )
    result, document = _run(["failover"], _frame(para("x"), chapter))
    assert _italic_texts(document) == ["failover"]
    assert result.scopes[1].terms["failover"].occurrences == 1


def test_runs_in_our_own_insertion_change_directly_without_a_tracked_property_change():
    """Text inserted by the same author is edited in place; other authors' text gets an rPrChange."""
    ours = f'<w:p><w:ins w:id="1" w:author="{AUTHOR}"><w:r><w:t>Isi failover baru.</w:t></w:r></w:ins></w:p>'
    theirs = '<w:p><w:ins w:id="2" w:author="Lain"><w:r><w:t>Isi slashing Lain.</w:t></w:r></w:ins></w:p>'
    _, document = _run(["failover", "slashing"], _frame(para("x"), ours + theirs))
    assert document.xpath("count(//w:ins[@w:author='%s']//w:rPrChange)" % AUTHOR, namespaces=NSMAP) == 0
    assert _italic_texts(document).count("failover") == 1
    assert document.xpath("count(//w:ins[@w:author='Lain']//w:rPrChange)", namespaces=NSMAP) == 1


def test_a_later_italic_use_is_made_upright_and_a_correct_first_use_is_left():
    """Italics at first use stay; italics at later uses are removed with a tracked change."""
    chapter = para("Satu failover.", rpr="<w:i/>") + para("Dua failover.", rpr="<w:i/>")
    result, document = _run(["failover"], _frame(para("x"), chapter))
    uses = result.scopes[1].terms["failover"]
    assert (uses.unchanged, uses.upright, uses.italicised) == (1, 1, 0)
    last = list(document.iter(f"{{{W_NS}}}p"))[7]
    runs = last.findall("w:r", NSMAP)
    assert [run.xpath("string(w:t)", namespaces=NSMAP) for run in runs] == ["Dua ", "failover", "."]
    assert runs[1].find("w:rPr/w:i", NSMAP) is None
    assert runs[1].find("w:rPr/w:rPrChange/w:rPr/w:i", NSMAP) is not None
    assert runs[0].find("w:rPr/w:i", NSMAP) is not None


def test_a_run_with_an_earlier_tracked_format_change_is_skipped_and_counted():
    """A second rPrChange would be invalid, so the use is reported as skipped."""
    chapter = '<w:p><w:r><w:rPr><w:rPrChange w:id="9" w:author="B"><w:rPr/></w:rPrChange></w:rPr><w:t>failover</w:t></w:r></w:p>'
    result, document = _run(["failover"], _frame(para("x"), chapter))
    assert result.scopes[1].terms["failover"].skipped == 1
    assert _italic_texts(document) == []


def test_terms_never_found_are_listed_and_the_first_use_excerpt_is_kept():
    """The result names terms absent from both scopes, and keeps context for the first use."""
    result, _ = _run(["failover", "slashing"], _frame(para("x"), para("Bab memakai failover aktif.")))
    assert result.unfound_terms == ["slashing"]
    assert "failover" in result.scopes[1].terms["failover"].first_excerpt


def test_missing_boundaries_are_reported_as_notes():
    """Without an INTISARI heading that scope is skipped with a note; without chapters likewise."""
    result, _ = _run(["failover"], para("Sistem failover.") + para("DAFTAR PUSTAKA"))
    assert result.scopes == []
    assert any("INTISARI" in note for note in result.notes) and any("BAB I" in note for note in result.notes)


def test_tokens_are_never_split_or_restyled_even_when_their_name_holds_a_term():
    """{{DATA:failover.n}} stays whole in one w:t and is no use of 'failover'; the real first use is italic."""
    chapter = para("Ada {{DATA:failover.n}} siklus, lalu {{TABLE:failover}} dan {{FIGURE:failover}}.") + para("Bab memakai failover.")
    result, document = _run(["failover"], _frame(para("x"), chapter))
    texts = document.xpath("//w:t/text()", namespaces=NSMAP)
    assert "Ada {{DATA:failover.n}} siklus, lalu {{TABLE:failover}} dan {{FIGURE:failover}}." in texts
    assert _italic_texts(document) == ["failover"]
    assert result.scopes[1].terms["failover"].occurrences == 1


def test_a_dotted_or_slashed_identifier_in_plain_text_is_not_a_use_of_its_parts():
    """failover.retry_attempts and /status/failover are identifiers, not the word failover."""
    chapter = para("Atur failover.retry_attempts dan /status/failover lalu failover.")
    _, document = _run(["failover", "status"], _frame(para("x"), chapter))
    assert _italic_texts(document) == ["failover"]


def test_the_body_scope_ends_at_the_first_reference_heading_not_a_repeat_in_the_appendix():
    """A second REFERENSI heading inside the SUMBER TEKNIS appendix does not extend the scope."""
    body = _frame(para("x"), para("Bab memakai failover.")).replace(
        FRAME_AFTER, para("REFERENSI") + para("[1] failover entri") + para("SUMBER TEKNIS") + para("REFERENSI") + para("Lampiran failover teknis.")
    )
    result, document = _run(["failover"], body)
    assert result.scopes[1].terms["failover"].occurrences == 1
    assert _italic_texts(document) == ["failover"]
