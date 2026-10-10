"""Tests for paragraph plain text and anchor matching."""

import pytest
from lxml import etree

from docx_builder import R_NS, W_NS, para, table
from redline.matcher import AnchorError, index_paragraphs, match_anchor, paragraph_text

LONG = "Penelitian ini merancang dan mengimplementasikan sistem failover otomatis bernama syncguard"


def _body(inner: str) -> etree._Element:
    """Parse inner body XML into a w:body element."""
    return etree.fromstring(f'<w:body xmlns:w="{W_NS}" xmlns:r="{R_NS}">{inner}</w:body>')


def _first_paragraph(inner: str) -> etree._Element:
    """Return the first w:p of a body built from inner XML."""
    return _body(inner).find(f"{{{W_NS}}}p")


def test_text_joins_runs_including_hyperlinks_and_skips_deleted_text():
    """Runs inside w:hyperlink count; w:delText does not; whitespace collapses."""
    p = _first_paragraph(
        '<w:p><w:r><w:t xml:space="preserve">Validator  wajib </w:t></w:r>'
        '<w:hyperlink r:id="rId9"><w:r><w:t>[15]</w:t></w:r></w:hyperlink>'
        '<w:del w:id="1" w:author="a"><w:r><w:delText>hapus</w:delText></w:r></w:del>'
        '<w:r><w:t xml:space="preserve">.  </w:t></w:r></w:p>'
    )
    assert paragraph_text(p) == "Validator wajib [15]."


def test_line_breaks_and_tabs_count_as_spaces_but_tab_stops_do_not():
    """w:br and w:tab inside runs are whitespace; tab-stop definitions in w:pPr are not text."""
    p = _first_paragraph(
        '<w:p><w:pPr><w:tabs><w:tab w:val="left" w:pos="720"/></w:tabs></w:pPr>'
        "<w:r><w:t>BAB III</w:t></w:r><w:r><w:br/><w:t>METODE</w:t></w:r>"
        "<w:r><w:tab/><w:t>PENELITIAN</w:t></w:r></w:p>"
    )
    assert paragraph_text(p) == "BAB III METODE PENELITIAN"


def test_edge_whitespace_without_preserve_is_not_rendered():
    """Word drops edge whitespace of a w:t that lacks xml:space='preserve'."""
    p = _first_paragraph("<w:p><w:r><w:t>Hello </w:t></w:r><w:r><w:t>world</w:t></w:r></w:p>")
    assert paragraph_text(p) == "Helloworld"


def test_index_covers_table_cells_and_skips_text_boxes():
    """Table cell paragraphs are searchable; a paragraph nested inside another (text box) is not."""
    body = _body(
        para("Sebelum")
        + table([["Komponen", "Fungsi"]])
        + '<w:p><w:r><w:t>Luar</w:t></w:r><w:r><w:pict><w:txbxContent>'
        "<w:p><w:r><w:t>Dalam kotak</w:t></w:r></w:p></w:txbxContent></w:pict></w:r></w:p>"
    )
    indexed = index_paragraphs(body)
    assert [(item.number, item.text, item.in_table) for item in indexed] == [
        (1, "Sebelum", False),
        (2, "Komponen", True),
        (3, "Fungsi", True),
        (4, "Luar", False),
    ]


def test_exact_match_and_long_prefix_match():
    """A short anchor must equal the text; an anchor of 40+ characters may be a prefix."""
    indexed = index_paragraphs(_body(para("Tabel 2.1 Keaslian Penelitian") + para(LONG + " untuk node validator.")))
    assert match_anchor("Tabel 2.1 Keaslian Penelitian", indexed).number == 1
    assert match_anchor(LONG, indexed).number == 2


def test_short_prefix_does_not_match_and_says_why():
    """An anchor under 40 characters that only starts a paragraph is a zero-match error with a hint."""
    indexed = index_paragraphs(_body(para("BAB III METODE PENELITIAN: Menguraikan metodologi.")))
    with pytest.raises(AnchorError) as raised:
        match_anchor("BAB III METODE PENELITIAN", indexed)
    assert raised.value.kind == "no_match"
    assert "no paragraph matches" in raised.value.reason
    assert "shorter than 40 characters" in raised.value.reason
    assert [candidate.number for candidate in raised.value.candidates] == [1]


def test_duplicate_match_is_an_error_unless_occurrence_picks_one():
    """Two matching paragraphs need Occurrence; Occurrence n picks the nth in document order."""
    indexed = index_paragraphs(_body(para("Tingkat Keberhasilan Switching") + para("x") + para("Tingkat Keberhasilan Switching")))
    with pytest.raises(AnchorError) as raised:
        match_anchor("Tingkat Keberhasilan Switching", indexed)
    assert raised.value.kind == "ambiguous"
    assert raised.value.reason == "anchor matches 2 paragraphs; add Occurrence to pick one"
    assert [candidate.number for candidate in raised.value.candidates] == [1, 3]
    assert match_anchor("Tingkat Keberhasilan Switching", indexed, occurrence=2).number == 3


def test_occurrence_beyond_the_matches_is_an_error():
    """Occurrence larger than the match count is not guessed."""
    indexed = index_paragraphs(_body(para("Satu baris")))
    with pytest.raises(AnchorError, match="Occurrence 2 requested but the anchor matches 1 paragraph") as raised:
        match_anchor("Satu baris", indexed, occurrence=2)
    assert raised.value.kind == "occurrence"


def test_zero_match_lists_closest_candidates():
    """With no match, the paragraphs sharing the longest start with the anchor are named."""
    indexed = index_paragraphs(_body(para("Sistem failover manual: Validator beralih") + para("Lain sama sekali")))
    with pytest.raises(AnchorError) as raised:
        match_anchor("Sistem failover otomatis: Validator", indexed)
    assert raised.value.reason == "no paragraph matches the anchor"
    assert [candidate.number for candidate in raised.value.candidates] == [1]


def test_table_row_anchor_gets_a_specific_reason():
    """'cell | cell' anchors are table rows; the spec matches paragraph text only."""
    indexed = index_paragraphs(_body(table([["Komponen", "Fungsi"]])))
    with pytest.raises(AnchorError) as raised:
        match_anchor("Komponen | Fungsi", indexed)
    assert "looks like a table row" in raised.value.reason
    assert "'ROW: <first cell>' or 'TABLE: <first row>'" in raised.value.reason


def test_anchor_matches_inside_table_cell_and_paragraph_with_hyperlink():
    """Cell paragraphs and paragraphs whose text runs through a hyperlink both match."""
    body = _body(
        table([["Peneliti / Tahun", "Lokasi"]])
        + '<w:p><w:hyperlink r:id="rId9"><w:r><w:t>[11]</w:t></w:r></w:hyperlink>'
        "<w:r><w:t>Blockchain</w:t></w:r></w:p>"
    )
    indexed = index_paragraphs(body)
    assert match_anchor("Peneliti / Tahun", indexed).in_table is True
    assert match_anchor("[11]Blockchain", indexed).number == 3


def test_a_prefix_selects_the_one_paragraph_whose_text_starts_with_it():
    """A prefix of any length picks a paragraph by its start; no match or several matches raise AnchorError."""
    from redline.matcher import match_prefix

    paragraphs = index_paragraphs(_body(para("Tabel 4.5 Hasil Failover") + para("Tabel 4.6 Hasil Failback") + para("Teks.")))
    assert match_prefix("Tabel 4.5", paragraphs).text == "Tabel 4.5 Hasil Failover"
    assert match_prefix("  Tabel   4.6 ", paragraphs).number == 2
    with pytest.raises(AnchorError, match="no paragraph starts with this text") as missing:
        match_prefix("Tabel 9.9", paragraphs)
    assert missing.value.kind == "no_match"
    with pytest.raises(AnchorError, match="matches 2 paragraphs; give a longer prefix") as several:
        match_prefix("Tabel 4", paragraphs)
    assert several.value.kind == "ambiguous"
    assert [item.number for item in several.value.candidates] == [1, 2]
