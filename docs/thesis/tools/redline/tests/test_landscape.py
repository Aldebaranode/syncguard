"""Tests for putting a caption and its wide table on a landscape page between two tracked section breaks."""

from lxml import etree

from docx_builder import R_NS, W_NS, document_xml, para, table
from redline.breaks import remove_redundant_page_breaks
from redline.fill import fill_table_tokens
from redline.headers import mark_header_rows
from redline.landscape import turn_landscape
from redline.matcher import paragraph_text
from redline.structure import text_column_width
from redline.tracked import RevisionMarks

NSMAP = {"w": W_NS, "r": R_NS}
AUTHOR = "Syahid Nurrohim"
DATE = "2026-10-10T08:00:00Z"
APPLY_DATE = "2026-10-05T08:00:00Z"
# The body section of docx_builder: an A4 portrait page with the manuscript's margins, rounded.
PORTRAIT_WIDTH = 11909 - 2268 - 1701
LANDSCAPE_WIDTH = 16834 - 2268 - 1701
PORTRAIT_SIZE = '<w:pgSz w:w="11909" w:h="16834"/>'
LANDSCAPE_SIZE = '<w:pgSz w:w="16834" w:h="11909" w:orient="landscape"/>'
MARGINS = '<w:pgMar w:top="2268" w:right="1701" w:bottom="1701" w:left="2268" w:header="720" w:footer="720" w:gutter="0"/>'
CAPTION = "Tabel 4.5 Hasil Failover per Iterasi, Sesi S5"
PREFIX = "Tabel 4.5 Hasil Failover per Iterasi"
PAGE_BREAK = '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'
WIDE_TABLE = "| t_gagal | A | B | C | D |\n|---|---|---|---|---|\n| 08:00:12,282 | " + " | ".join(["failover.state_sync_interval"] * 4) + " |\n"


def _body(inner: str) -> etree._Element:
    """The w:body of a document holding inner XML, ended by docx_builder's portrait body section."""
    return etree.fromstring(document_xml(inner).encode("utf-8")).find("w:body", NSMAP)


def _turn(body: etree._Element, *prefixes: str):
    """Run the pass with a fixed author, date and first revision id."""
    return turn_landscape(body, list(prefixes), RevisionMarks(AUTHOR, DATE, 100))


def _token(name: str) -> str:
    """A {{TABLE:name}} paragraph as apply inserts it: a tracked paragraph mark and a tracked run."""
    text = "{{TABLE:" + name + "}}"
    revision = f'w:author="{AUTHOR}" w:date="{APPLY_DATE}"'
    return f'<w:p><w:pPr><w:rPr><w:ins w:id="5" {revision}/></w:rPr></w:pPr><w:ins w:id="6" {revision}><w:r><w:t>{text}</w:t></w:r></w:ins></w:p>'


def _tracked_rows(table_xml: str, revision: str) -> str:
    """The table with every row marked as a tracked insertion ('ins') or deletion ('del')."""
    return table_xml.replace("<w:tr>", f'<w:tr><w:trPr><w:{revision} w:id="7" w:author="{AUTHOR}" w:date="{APPLY_DATE}"/></w:trPr>')


def _section_end(children: str) -> str:
    """An empty paragraph whose w:sectPr, holding children, ends a section."""
    return f"<w:p><w:pPr><w:sectPr>{children}</w:sectPr></w:pPr></w:p>"


def _texts(body: etree._Element) -> list[str]:
    """Accepted text of every paragraph directly in the body, in order."""
    return [paragraph_text(paragraph) for paragraph in body.findall("w:p", NSMAP)]


def _section_breaks(body: etree._Element) -> list[etree._Element]:
    """The paragraphs directly in the body that carry a section break, in order."""
    return body.xpath("./w:p[w:pPr/w:sectPr]", namespaces=NSMAP)


def _attributes(element: etree._Element) -> dict[str, str]:
    """An element's attributes by local name."""
    return {etree.QName(name).localname: value for name, value in element.attrib.items()}


def test_a_caption_and_its_token_paragraph_turn_between_two_inserted_section_breaks():
    """One empty section-break paragraph goes right before the caption and one right after the token."""
    body = _body(para("Teks sebelum.") + para(CAPTION) + _token("failover_iterations") + para("Teks sesudah."))
    result = _turn(body, PREFIX)
    paragraphs = body.findall("w:p", NSMAP)
    assert _texts(body) == ["Teks sebelum.", "", CAPTION, "{{TABLE:failover_iterations}}", "", "Teks sesudah."]
    assert _section_breaks(body) == [paragraphs[1], paragraphs[4]]
    [block] = result.blocks
    assert (block.prefix, block.caption, block.elements, block.outcome) == (PREFIX, CAPTION, ("token failover_iterations",), "turned to landscape")
    assert result.problems == [] and result.turned == 1


def test_a_fully_deleted_table_after_the_token_turns_with_the_block():
    """Tabel 4.8, 4.11 and 4.13: the token, then the original table with every row deleted; the landscape break follows that table."""
    deleted = _tracked_rows(table([["Iterasi", "TTR"], ["1", "9"]]), "del")
    body = _body(para(CAPTION) + _token("failback_iterations") + deleted + para("Teks sesudah."))
    result = _turn(body, PREFIX)
    landscape_end = _section_breaks(body)[1]
    assert result.blocks[0].elements == ("token failback_iterations", "deleted table")
    assert landscape_end.getprevious().tag == f"{{{W_NS}}}tbl"
    assert paragraph_text(landscape_end.getnext()) == "Teks sesudah."


def test_an_inserted_table_before_the_token_turns_with_the_block():
    """Tabel 4.3: an inserted table, then the token; both belong to the block."""
    inserted = _tracked_rows(table([["Sesi", "Iterasi"], ["S5", "10"]]), "ins")
    body = _body(para(CAPTION) + inserted + _token("failover_iterations") + para("Teks sesudah."))
    result = _turn(body, PREFIX)
    assert result.blocks[0].elements == ("table", "token failover_iterations")
    assert [etree.QName(child).localname for child in body] == ["p", "p", "tbl", "p", "p", "p", "sectPr"]
    assert _section_breaks(body)[1].getprevious() is body.findall("w:p", NSMAP)[2]


def test_a_prefix_that_matches_no_paragraph_is_a_problem_and_changes_nothing():
    """The prefix gets a skipped row and a problem; the document stays as it was."""
    body = _body(para(CAPTION) + _token("failover_iterations"))
    before = etree.tostring(body)
    result = _turn(body, "Tabel 9.9")
    assert result.problems == ["Tabel 9.9: no paragraph starts with this text"]
    assert (result.blocks[0].caption, result.blocks[0].outcome) == ("", "skipped")
    assert etree.tostring(body) == before


def test_a_prefix_that_matches_two_body_paragraphs_is_a_problem_but_table_cells_do_not_count():
    """Only paragraphs directly in the body can be the caption; two of them make the prefix ambiguous."""
    twice = _body(para(CAPTION) + _token("a") + para(CAPTION.replace("S5", "S6")) + _token("b"))
    result = _turn(twice, PREFIX)
    assert result.problems == [f"{PREFIX}: matches 2 paragraphs; give a longer prefix"]
    assert _section_breaks(twice) == []
    in_a_cell = _body(table([[CAPTION]]) + para("Teks.") + para(CAPTION) + _token("a"))
    assert _turn(in_a_cell, PREFIX).problems == []


def test_a_caption_not_followed_by_a_table_or_token_is_a_problem():
    """A block needs at least one table or {{TABLE:name}} paragraph right after its caption; a table note alone is not enough."""
    body = _body(para(CAPTION) + para("Teks biasa.") + _token("failover_iterations"))
    result = _turn(body, PREFIX)
    assert result.problems == [PREFIX + ": the caption is not followed by a table or a {{TABLE:name}} paragraph"]
    assert (result.blocks[0].caption, result.blocks[0].elements, result.blocks[0].outcome) == (CAPTION, (), "skipped")
    assert _section_breaks(body) == []
    only_a_note = _body(para(CAPTION) + para("Catatan: tabel menyusul.") + para("Teks biasa.") + _token("failover_iterations"))
    result = _turn(only_a_note, PREFIX)
    assert result.problems == [PREFIX + ": the caption is not followed by a table or a {{TABLE:name}} paragraph"]
    assert (result.blocks[0].elements, result.blocks[0].outcome) == (("note",), "skipped")
    assert _section_breaks(only_a_note) == []


def test_a_table_note_and_the_deleted_table_after_it_turn_with_the_block():
    """Tabel 4.8 after apply: caption, token, a Catatan: note, then the original table with every row deleted; all four go on the landscape page."""
    caption = "Tabel 4.8 Hasil Failback per Iterasi, Sesi S5"
    deleted = _tracked_rows(table([["Iterasi", "TTR"], ["1", "9"]]), "del")
    note = "Catatan: TTR dihitung dari t_gagal sampai tanda tangan pertama node cadangan."
    body = _body(para(caption) + _token("failback") + para(note) + deleted + para("Teks sesudah."))
    caption_paragraph, token_paragraph, note_paragraph, after_paragraph = body.findall("w:p", NSMAP)
    deleted_table = body.find("w:tbl", NSMAP)
    result = _turn(body, "Tabel 4.8 Hasil Failback per Iterasi")
    assert result.blocks[0].elements == ("token failback", "note", "deleted table")
    assert _section_breaks(body)[1].getprevious() is deleted_table
    block = (caption_paragraph, token_paragraph, note_paragraph, deleted_table)
    assert [text_column_width(element) for element in block] == [LANDSCAPE_WIDTH] * 4
    assert text_column_width(after_paragraph) == PORTRAIT_WIDTH


def test_a_table_note_after_the_deleted_table_ends_the_block():
    """Tabel 4.8 in the applied manuscript: caption, token, the original table with every row deleted, then its Catatan: note."""
    caption = "Tabel 4.8 Hasil Failback per Iterasi, Sesi S5"
    deleted = _tracked_rows(table([["Iterasi", "TTR"], ["1", "9"]]), "del")
    note = "Catatan: stempel notifikasi diambil dari log syncguard."
    body = _body(para(caption) + _token("failback") + deleted + para(note) + para("Teks sesudah."))
    caption_paragraph, token_paragraph, note_paragraph, after_paragraph = body.findall("w:p", NSMAP)
    deleted_table = body.find("w:tbl", NSMAP)
    result = _turn(body, "Tabel 4.8 Hasil Failback per Iterasi")
    assert result.blocks[0].elements == ("token failback", "deleted table", "note")
    assert _section_breaks(body)[1].getprevious() is note_paragraph
    block = (caption_paragraph, token_paragraph, deleted_table, note_paragraph)
    assert [text_column_width(element) for element in block] == [LANDSCAPE_WIDTH] * 4
    assert text_column_width(after_paragraph) == PORTRAIT_WIDTH


def test_a_table_note_directly_after_the_token_stays_on_the_landscape_page():
    """A note that ends the run is the last element of the block, so the landscape section ends right after it."""
    note = "Catatan: waktu ditulis dalam WIB dengan ketelitian milidetik."
    body = _body(para(CAPTION) + _token("failover_iterations") + para(note) + para("Teks sesudah."))
    result = _turn(body, PREFIX)
    assert result.blocks[0].elements == ("token failover_iterations", "note")
    assert _texts(body) == ["", CAPTION, "{{TABLE:failover_iterations}}", note, "", "Teks sesudah."]
    note_paragraph = body.findall("w:p", NSMAP)[3]
    assert text_column_width(note_paragraph) == LANDSCAPE_WIDTH


def test_a_block_in_a_section_that_is_not_portrait_is_a_problem():
    """A section that is already landscape, or that gives no page size, is left alone."""
    landscape = _body(para(CAPTION) + _token("a") + _section_end(LANDSCAPE_SIZE + MARGINS) + para("Teks."))
    before = etree.tostring(landscape)
    result = _turn(landscape, PREFIX)
    assert result.problems == [f"{PREFIX}: the section that holds the block is not portrait"]
    assert (result.blocks[0].width_before, result.blocks[0].width_after) == (LANDSCAPE_WIDTH, LANDSCAPE_WIDTH)
    assert etree.tostring(landscape) == before
    without_size = _body(para(CAPTION) + _token("a") + _section_end(MARGINS))
    assert _turn(without_size, PREFIX).problems == [f"{PREFIX}: the section that holds the block is not portrait"]


def test_a_block_in_a_section_that_restarts_page_numbers_is_a_problem():
    """Splitting a section with w:pgNumType w:start would restart the numbers in every part; a number format alone is fine."""
    restarting = _body(para(CAPTION) + _token("a") + _section_end(PORTRAIT_SIZE + MARGINS + '<w:pgNumType w:start="1"/>'))
    result = _turn(restarting, PREFIX)
    assert result.problems == [f"{PREFIX}: the section that holds the block restarts its page numbers (w:pgNumType w:start); splitting it is not supported"]
    assert len(_section_breaks(restarting)) == 1
    formatted = _body(para(CAPTION) + _token("a") + _section_end(PORTRAIT_SIZE + MARGINS + '<w:pgNumType w:fmt="lowerRoman"/>'))
    assert _turn(formatted, PREFIX).problems == []


def test_a_block_in_a_section_with_a_different_first_page_is_a_problem():
    """Splitting a section with w:titlePg would give every part its own first-page header."""
    body = _body(para(CAPTION) + _token("a") + _section_end(PORTRAIT_SIZE + MARGINS + "<w:titlePg/>"))
    result = _turn(body, PREFIX)
    assert result.problems == [f"{PREFIX}: the section that holds the block has a different first page (w:titlePg); splitting it is not supported"]


def test_a_section_break_inside_the_block_is_a_problem():
    """A token paragraph that itself ends a section would put the landscape break in the next section."""
    section = f"<w:sectPr>{PORTRAIT_SIZE}{MARGINS}</w:sectPr></w:pPr>"
    body = _body(para(CAPTION) + _token("a").replace("</w:pPr>", section, 1) + para("Teks sesudah."))
    before = etree.tostring(body)
    result = _turn(body, PREFIX)
    assert result.problems == [f"{PREFIX}: a section break sits inside the block"]
    assert etree.tostring(body) == before


def test_a_body_without_section_properties_is_a_problem():
    """Without a w:sectPr there is nothing to copy, so the block cannot turn."""
    body = etree.fromstring(f'<w:body xmlns:w="{W_NS}">{para(CAPTION)}{_token("a")}</w:body>')
    result = _turn(body, PREFIX)
    assert result.problems == [f"{PREFIX}: the document body has no final section properties"]
    assert (result.blocks[0].width_before, result.blocks[0].outcome) == (None, "skipped")


def test_the_inserted_paragraphs_are_tracked_empty_lines_that_carry_the_section_copies():
    """Each break paragraph has a tracked mark by the author, no text or style, zero spacing, a 20 twip exact line and a 2 half-point mark.

    The first holds a copy of the holding section, the second the same copy turned landscape (width and
    height swapped, margins unchanged); both drop w:sectPrChange and keep the header reference.
    """
    change = f'<w:sectPrChange w:id="3" w:author="Penguji" w:date="{APPLY_DATE}"><w:sectPr/></w:sectPrChange>'
    holding = '<w:headerReference w:type="default" r:id="rId9"/>' + PORTRAIT_SIZE + MARGINS + change
    body = _body(para(CAPTION) + _token("failover_iterations") + _section_end(holding) + para("Teks."))
    _turn(body, PREFIX)
    portrait_end, landscape_end, holding_end = _section_breaks(body)
    for paragraph in (portrait_end, landscape_end):
        properties = paragraph.find("w:pPr", NSMAP)
        mark = properties.find("w:rPr", NSMAP)
        revision = _attributes(mark.find("w:ins", NSMAP))
        section = properties.find("w:sectPr", NSMAP)
        assert [etree.QName(child).localname for child in properties] == ["spacing", "rPr", "sectPr"]
        assert _attributes(properties.find("w:spacing", NSMAP)) == {"before": "0", "after": "0", "line": "20", "lineRule": "exact"}
        assert [etree.QName(child).localname for child in mark] == ["ins", "sz", "szCs"]
        assert (revision["author"], revision["date"]) == (AUTHOR, DATE)
        assert mark.xpath("./w:sz/@w:val | ./w:szCs/@w:val", namespaces=NSMAP) == ["2", "2"]
        assert paragraph.xpath("./w:r | ./w:ins", namespaces=NSMAP) == []
        assert section.find("w:sectPrChange", NSMAP) is None
        assert section.xpath("./w:headerReference/@r:id", namespaces=NSMAP) == ["rId9"]
        assert _attributes(section.find("w:pgMar", NSMAP)) == _attributes(holding_end.find("w:pPr/w:sectPr/w:pgMar", NSMAP))
    assert _attributes(portrait_end.find("w:pPr/w:sectPr/w:pgSz", NSMAP)) == {"w": "11909", "h": "16834"}
    assert _attributes(landscape_end.find("w:pPr/w:sectPr/w:pgSz", NSMAP)) == {"w": "16834", "h": "11909", "orient": "landscape"}
    assert holding_end.find("w:pPr/w:sectPr/w:sectPrChange", NSMAP) is not None


def test_rejecting_the_two_inserted_paragraphs_gives_back_the_original_body():
    """The holding section is not changed, so taking out the inserted break paragraphs restores the single section."""
    body = _body(para("Teks sebelum.") + para(CAPTION) + _token("failover_iterations") + para("Teks sesudah."))
    before = etree.tostring(body)
    _turn(body, PREFIX)
    for paragraph in _section_breaks(body):
        if paragraph.find("w:pPr/w:rPr/w:ins", NSMAP) is not None:
            body.remove(paragraph)
    assert etree.tostring(body) == before


def test_the_token_paragraph_gets_the_landscape_text_width_and_the_text_around_it_stays_portrait():
    """fill sizes a table for the section its token sits in, which is now the landscape one."""
    body = _body(para("Teks sebelum.") + para(CAPTION) + _token("failover_iterations") + para("Teks sesudah."))
    before_text, caption, token, after_text = body.findall("w:p", NSMAP)
    assert text_column_width(token) == PORTRAIT_WIDTH
    result = _turn(body, PREFIX)
    assert (text_column_width(caption), text_column_width(token)) == (LANDSCAPE_WIDTH, LANDSCAPE_WIDTH)
    assert (text_column_width(before_text), text_column_width(after_text)) == (PORTRAIT_WIDTH, PORTRAIT_WIDTH)
    assert (result.blocks[0].width_before, result.blocks[0].width_after) == (PORTRAIT_WIDTH, LANDSCAPE_WIDTH)


def test_two_blocks_of_one_section_both_turn_and_report_in_the_order_given():
    """Each prefix gets its own row; the block turned second still splits the same portrait section."""
    second = CAPTION.replace("4.5", "4.8")
    body = _body(para(CAPTION) + _token("a") + para("Teks di antara.") + para(second) + _token("b") + para("Teks sesudah."))
    result = _turn(body, "Tabel 4.8", "Tabel 4.5")
    assert [(block.prefix, block.outcome) for block in result.blocks] == [("Tabel 4.8", "turned to landscape"), ("Tabel 4.5", "turned to landscape")]
    assert _texts(body) == ["", CAPTION, "{{TABLE:a}}", "", "Teks di antara.", "", second, "{{TABLE:b}}", "", "Teks sesudah."]
    portrait, landscape = PORTRAIT_WIDTH, LANDSCAPE_WIDTH
    widths = [text_column_width(paragraph) for paragraph in body.findall("w:p", NSMAP)]
    assert widths == [portrait, landscape, landscape, landscape, portrait, portrait, landscape, landscape, landscape, portrait]


def test_fill_fits_a_wide_table_at_the_landscape_width_where_it_did_not_fit_in_portrait(tmp_path):
    """A millisecond stamp and four 28 character identifiers per row.

    In portrait even 10 pt is too wide, so fill keeps 10 pt (w:sz 20) and asks for a review; in the
    landscape section the same table fits across the whole column at 11 pt (w:sz 22).
    """
    values = tmp_path / "failover_iterations.md"
    values.write_text(WIDE_TABLE, encoding="utf-8")
    inner = table([["A", "B"], ["1", "2"]], width=6000) + para(CAPTION) + _token("failover_iterations") + para("Teks sesudah.")
    portrait = _body(inner)
    portrait_fill = fill_table_tokens(portrait.getparent(), {"failover_iterations": values}, RevisionMarks(AUTHOR, DATE, 100))
    portrait_table = portrait.findall("w:tbl", NSMAP)[1]
    assert list(portrait_fill.review) == ["failover_iterations"]
    assert portrait_table.xpath("./w:tblPr/w:tblW/@w:w", namespaces=NSMAP) == [str(PORTRAIT_WIDTH)]
    assert set(portrait_table.xpath(".//w:r/w:rPr/w:sz/@w:val", namespaces=NSMAP)) == {"20"}
    landscape = _body(inner)
    _turn(landscape, PREFIX)
    landscape_fill = fill_table_tokens(landscape.getparent(), {"failover_iterations": values}, RevisionMarks(AUTHOR, DATE, 200))
    landscape_table = landscape.findall("w:tbl", NSMAP)[1]
    assert landscape_fill.tables_filled == ["failover_iterations"] and landscape_fill.review == {}
    assert landscape_table.xpath("./w:tblPr/w:tblW/@w:w", namespaces=NSMAP) == [str(LANDSCAPE_WIDTH)]
    assert set(landscape_table.xpath(".//w:r/w:rPr/w:sz/@w:val", namespaces=NSMAP)) == {"22"}
    assert text_column_width(landscape_table) == LANDSCAPE_WIDTH


def test_breaks_still_removes_a_page_break_right_before_the_inserted_section_break():
    """A page-break-only paragraph before the caption now sits before a next-page section break, so breaks deletes its break."""
    body = _body(para("Teks sebelum.") + PAGE_BREAK + para(CAPTION) + _token("failover_iterations") + para("Teks sesudah."))
    _turn(body, PREFIX)
    result = remove_redundant_page_breaks(body, RevisionMarks(AUTHOR, DATE, 200))
    assert [(removal.before, removal.after) for removal in result.removals] == [("Teks sebelum.", CAPTION)]
    assert body.xpath("count(./w:p[2]/w:del/w:r/w:br[@w:type='page'])", namespaces=NSMAP) == 1


def test_headers_still_marks_the_header_row_of_a_table_inside_the_landscape_section():
    """After the pass the table sits in the landscape section and still gets its repeating header row."""
    body = _body(para("Teks sebelum.") + para(CAPTION) + table([["Iterasi", "TTD"], ["1", "12,4"]]) + para("Teks sesudah."))
    _turn(body, PREFIX)
    result = mark_header_rows(body, RevisionMarks(AUTHOR, DATE, 200))
    the_table = body.find("w:tbl", NSMAP)
    assert text_column_width(the_table) == LANDSCAPE_WIDTH
    assert [outcome.action for outcome in result.tables] == ["marked"]
    assert the_table.find("w:tr/w:trPr/w:tblHeader", NSMAP) is not None
