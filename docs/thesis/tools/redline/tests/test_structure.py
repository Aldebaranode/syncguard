"""Tests for the document structure helpers: headings, captions and the text column width."""

from lxml import etree

from docx_builder import R_NS, W_NS
from redline.structure import (
    is_caption_text,
    is_chapter_heading_text,
    is_heading,
    paragraph_style_names,
    text_area_height,
    text_column_width,
)

STYLES = (
    f'<w:styles xmlns:w="{W_NS}">'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/></w:style>'
    "</w:styles>"
)
SECTION = '<w:sectPr><w:pgSz w:w="11909" w:h="16834"/><w:pgMar w:top="1" w:right="1701" w:bottom="1" w:left="2268" w:header="1" w:footer="1" w:gutter="0"/></w:sectPr>'
LANDSCAPE = '<w:sectPr><w:pgSz w:w="16834" w:h="11909" w:orient="landscape"/><w:pgMar w:top="1" w:right="1700" w:bottom="1" w:left="2267" w:header="1" w:footer="1" w:gutter="0"/></w:sectPr>'


def _body(inner: str) -> etree._Element:
    """A w:body holding inner XML."""
    return etree.fromstring(f'<w:body xmlns:w="{W_NS}" xmlns:r="{R_NS}">{inner}</w:body>')


def _paragraph(ppr: str, runs: str) -> etree._Element:
    """One w:p with paragraph properties and runs."""
    return etree.fromstring(f'<w:p xmlns:w="{W_NS}"><w:pPr>{ppr}</w:pPr>{runs}</w:p>')


def _names() -> dict[str, str]:
    """Style id to lower-case name for the fixture styles."""
    return paragraph_style_names(etree.fromstring(STYLES))


def test_style_names_map_ids_to_lower_case_names():
    """Heading3 maps to 'heading 3'."""
    assert _names() == {"Normal": "normal", "Heading3": "heading 3", "Title": "title"}


def test_heading_styles_make_headings():
    """Heading and Title styles are headings; Normal is not."""
    names = _names()
    assert is_heading(_paragraph('<w:pStyle w:val="Heading3"/>', "<w:r><w:t>x</w:t></w:r>"), "Isi", names)
    assert is_heading(_paragraph('<w:pStyle w:val="Title"/>', "<w:r><w:t>x</w:t></w:r>"), "Isi", names)
    assert not is_heading(_paragraph("", "<w:r><w:t>Isi biasa saja.</w:t></w:r>"), "Isi biasa saja.", names)


def test_chapter_and_numbered_text_make_headings_without_a_style():
    """'BAB III', '3.3.2 Perancangan' and an all-bold short line read as headings."""
    names = _names()
    assert is_heading(_paragraph("", "<w:r><w:t>x</w:t></w:r>"), "BAB III METODE PENELITIAN", names)
    assert is_heading(_paragraph("", "<w:r><w:t>x</w:t></w:r>"), "3.3.2 Tahap Perancangan", names)
    bold = _paragraph("", "<w:r><w:rPr><w:b/></w:rPr><w:t>Lingkup Jaringan</w:t></w:r>")
    assert is_heading(bold, "Lingkup Jaringan", names)
    long_bold = _paragraph("", "<w:r><w:rPr><w:b/></w:rPr><w:t>Kalimat panjang berakhir titik.</w:t></w:r>")
    assert not is_heading(long_bold, "Kalimat panjang berakhir titik.", names)


def test_a_numbered_sentence_is_not_a_heading():
    """A number followed by a sentence that ends with a full stop is body text."""
    assert not is_heading(_paragraph("", "<w:r><w:t>x</w:t></w:r>"), "2.5 kali lipat lebih cepat daripada dugaan awal.", _names())


def test_caption_and_chapter_text_patterns():
    """Captions start with Gambar, Tabel, Kode Program or Algoritma plus a number."""
    assert is_caption_text("Gambar 3.1 Flowchart")
    assert is_caption_text("Kode Program 4.2 Log")
    assert not is_caption_text("Gambar ini menunjukkan")
    assert is_chapter_heading_text("BAB I PENDAHULUAN")
    assert is_chapter_heading_text("BAB IIIMETODE PENELITIAN")
    assert is_chapter_heading_text("BAB II TINJAUAN PUSTAKA")
    assert not is_chapter_heading_text("Bab ini membahas")


def test_text_column_width_comes_from_the_section_that_ends_the_paragraph_run():
    """The width is page width minus margins of the next section break at or after the element."""
    body = _body(f'<w:p><w:r/></w:p><w:p><w:pPr>{LANDSCAPE}</w:pPr></w:p><w:p><w:r/></w:p>{SECTION}')
    first, second, third = body.findall(f"{{{W_NS}}}p")
    assert text_column_width(first) == 16834 - 2267 - 1700
    assert text_column_width(third) == 11909 - 2268 - 1701


def test_text_column_width_of_an_element_in_a_table_uses_the_enclosing_section():
    """A paragraph inside a table cell resolves through its top-level table."""
    body = _body(f"<w:tbl><w:tr><w:tc><w:p/></w:tc></w:tr></w:tbl>{SECTION}")
    assert text_column_width(next(body.iter(f"{{{W_NS}}}p"))) == 11909 - 2268 - 1701


def test_column_width_is_none_when_the_section_has_no_margins():
    """A body without pgMar has no known column width, which callers treat as unknown."""
    from redline.structure import column_width_or_none

    body = _body('<w:p/><w:sectPr><w:pgSz w:w="11909" w:h="16834"/></w:sectPr>')
    assert column_width_or_none(body.find(f"{{{W_NS}}}p")) is None
    assert column_width_or_none(_body(f"<w:p/>{SECTION}").find(f"{{{W_NS}}}p")) == 11909 - 2268 - 1701


def test_text_area_height_is_page_height_minus_top_and_bottom_margins():
    """The first paragraph is in the landscape section (page height 11909), the third in the portrait one (16834)."""
    body = _body(f'<w:p><w:r/></w:p><w:p><w:pPr>{LANDSCAPE}</w:pPr></w:p><w:p><w:r/></w:p>{SECTION}')
    first, _, third = body.findall(f"{{{W_NS}}}p")
    assert text_area_height(first) == 11909 - 1 - 1
    assert text_area_height(third) == 16834 - 1 - 1


def test_body_font_is_read_from_the_document_defaults_then_the_normal_style():
    """docDefaults rFonts wins unless the Normal style names its own font."""
    from redline.structure import body_font_name

    defaults = etree.fromstring(f'<w:styles xmlns:w="{W_NS}"><w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Times New Roman"/></w:rPr></w:rPrDefault></w:docDefaults></w:styles>')
    assert body_font_name(defaults) == "Times New Roman"
    normal = etree.fromstring(
        f'<w:styles xmlns:w="{W_NS}"><w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Times New Roman"/></w:rPr></w:rPrDefault></w:docDefaults>'
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:rPr><w:rFonts w:ascii="Calibri"/></w:rPr></w:style></w:styles>'
    )
    assert body_font_name(normal) == "Calibri"
    assert body_font_name(etree.fromstring(f'<w:styles xmlns:w="{W_NS}"/>')) is None


def test_the_holding_section_is_the_next_section_break_or_the_body_section():
    """The w:sectPr that ends the section of an element: the first one at or after its top-level element, else the body's own."""
    from redline.structure import holding_section

    body = _body(f'<w:p><w:r/></w:p><w:p><w:pPr>{LANDSCAPE}</w:pPr></w:p><w:tbl><w:tr><w:tc><w:p/></w:tc></w:tr></w:tbl>{SECTION}')
    first, second = body.findall(f"{{{W_NS}}}p")
    landscape_section, final_section = body.iter(f"{{{W_NS}}}sectPr")
    cell_paragraph = next(body.find(f"{{{W_NS}}}tbl").iter(f"{{{W_NS}}}p"))
    assert holding_section(first) is landscape_section
    assert holding_section(second) is landscape_section
    assert holding_section(cell_paragraph) is final_section
