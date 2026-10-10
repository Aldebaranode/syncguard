"""Tests for building a tracked table from a Markdown table block."""

from lxml import etree

from docx_builder import R_NS, W_NS, para, table
from redline.markup import parse_text_blocks
from redline.tables import build_inserted_table
from redline.textwidth import estimate_text_width
from redline.tracked import RevisionMarks

NSMAP = {"w": W_NS, "r": R_NS}


def _body(inner: str) -> etree._Element:
    """Parse inner body XML into a w:body element."""
    return etree.fromstring(f'<w:body xmlns:w="{W_NS}" xmlns:r="{R_NS}">{inner}</w:body>')


def _new_table():
    """Build a 3-column tracked table against a fixture reference table."""
    reference = _body(para("x") + table([["A", "B"], ["1", "2"]], width=6000)).find(f"{{{W_NS}}}tbl")
    block = parse_text_blocks(["| No | Kelas | Status |", "|---|---|---|", "| 1 | *Crash* | Diuji |"])[0]
    marks = RevisionMarks("Syahid Nurrohim", "2026-10-05T08:00:00Z", 1)
    return build_inserted_table(block, reference, marks), marks


def _xpath(element, path):
    """Run an XPath with the w and r prefixes bound."""
    return element.xpath(path, namespaces=NSMAP)


def test_table_properties_come_from_the_reference_table():
    """Borders are copied; the grid shares the reference width over the new columns and the cells match it."""
    new, _ = _new_table()
    assert _xpath(new, "./w:tblPr/w:tblBorders/w:top/@w:val") == ["single"]
    widths = [int(width) for width in _xpath(new, "./w:tblGrid/w:gridCol/@w:w")]
    assert len(widths) == 3 and sum(widths) == 6000
    assert _xpath(new, ".//w:tc/w:tcPr/w:tcW/@w:w") == [str(width) for width in widths] * 2


def test_every_row_and_cell_is_a_tracked_insertion():
    """Each row has w:trPr/w:ins; each cell paragraph has a tracked mark and runs inside w:ins."""
    new, marks = _new_table()
    assert len(_xpath(new, "./w:tr/w:trPr/w:ins")) == 2
    assert len(_xpath(new, ".//w:tc/w:p/w:pPr/w:rPr/w:ins")) == 6
    assert _xpath(new, ".//w:tc/w:p/w:ins/w:r/w:t/text()") == ["No", "Kelas", "Status", "1", "Crash", "Diuji"]
    assert _xpath(new, ".//w:tc/w:p/w:r") == []
    assert marks.inserted_count == 2 + 6 + 6


def test_header_row_is_bold_and_body_keeps_its_own_formatting():
    """Header runs are bold; body runs are bold only when the markup says so."""
    new, _ = _new_table()
    rows = _xpath(new, "./w:tr")
    assert len(_xpath(rows[0], ".//w:ins/w:r/w:rPr/w:b")) == 3
    assert _xpath(rows[1], ".//w:ins/w:r/w:rPr/w:b") == []
    assert len(_xpath(rows[1], ".//w:ins/w:r/w:rPr/w:i")) == 1


def test_cell_paragraph_properties_follow_the_reference_cells():
    """Cell paragraphs copy the reference cell paragraph properties (spacing here)."""
    new, _ = _new_table()
    assert _xpath(new, ".//w:tc/w:p/w:pPr/w:spacing/@w:after") == ["0"] * 6


def test_header_row_repeats_on_every_page():
    """The header row carries w:tblHeader (before its w:ins) so Word repeats it across pages."""
    new, _ = _new_table()
    rows = _xpath(new, "./w:tr")
    assert [etree.QName(child).localname for child in rows[0].find(f"{{{W_NS}}}trPr")] == ["tblHeader", "ins"]
    assert _xpath(rows[1], "./w:trPr/w:tblHeader") == []


REFERENCE_WITH_INDENT = (
    '<w:tbl><w:tblPr><w:tblStyle w:val="a1"/><w:tblW w:w="6520" w:type="dxa"/><w:tblInd w:w="1408" w:type="dxa"/>'
    '<w:tblLayout w:type="fixed"/></w:tblPr><w:tblGrid><w:gridCol w:w="3260"/><w:gridCol w:w="3260"/></w:tblGrid>'
    '<w:tr><w:tc><w:tcPr><w:tcW w:w="3260" w:type="dxa"/><w:tcBorders><w:top w:val="single" w:sz="8" w:space="0" w:color="000000"/></w:tcBorders>'
    '<w:shd w:val="clear" w:fill="D9D9D9"/></w:tcPr><w:p><w:r><w:t>H</w:t></w:r></w:p></w:tc>'
    '<w:tc><w:tcPr><w:tcW w:w="3260" w:type="dxa"/></w:tcPr><w:p><w:r><w:t>H</w:t></w:r></w:p></w:tc></w:tr>'
    '<w:tr><w:tc><w:tcPr><w:tcW w:w="3260" w:type="dxa"/><w:tcBorders><w:bottom w:val="single" w:sz="4" w:space="0" w:color="000000"/></w:tcBorders></w:tcPr>'
    '<w:p><w:r><w:t>b</w:t></w:r></w:p></w:tc><w:tc><w:tcPr><w:tcW w:w="3260" w:type="dxa"/></w:tcPr><w:p><w:r><w:t>b</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
)
IDENTIFIER = "failover.state_sync_interval"
PROSE = "Disamakan dengan health.interval; pada kondisi normal salinan state di node pasif tertinggal paling lama satu interval"


def _built(lines: list[str], column_width: int | None):
    """Build a table from Markdown lines against the indented reference table."""
    reference = _body(REFERENCE_WITH_INDENT).find(f"{{{W_NS}}}tbl")
    marks = RevisionMarks("Syahid Nurrohim", "2026-10-05T08:00:00Z", 1)
    return build_inserted_table(parse_text_blocks(lines)[0], reference, marks, column_width)


def test_a_dotted_identifier_never_has_to_break_because_the_table_grows_to_the_text_column():
    """Tabel 3.1 case: equal quarters of the reference width split the identifier; content widths do not."""
    lines = ["| Parameter | Nilai | Dasar Penetapan | Bukti |", "|---|---|---|---|"]
    lines.append(f"| {IDENTIFIER} | 30 detik ke masa lalu dan ke masa depan | {PROSE} | Tabel 4.x pengujian keamanan negatif, kasus 2 dan 4 |")
    new = _built(lines, column_width=7940)
    widths = [int(width) for width in _xpath(new, "./w:tblGrid/w:gridCol/@w:w")]
    assert widths[0] >= estimate_text_width(IDENTIFIER, 12) + 230
    assert widths[0] > 6520 // 4
    assert sum(widths) == 7940
    assert _xpath(new, "./w:tblPr/w:tblW/@w:w") == ["7940"]
    assert _xpath(new, "./w:tblPr/w:tblInd") == []


def test_a_table_that_fits_keeps_the_reference_width_and_indent():
    """Short content stays inside the reference geometry."""
    new = _built(["| A | B |", "|---|---|", "| 1 | dua kata |"], column_width=7940)
    assert sum(int(width) for width in _xpath(new, "./w:tblGrid/w:gridCol/@w:w")) == 6520
    assert _xpath(new, "./w:tblPr/w:tblInd/@w:w") == ["1408"]


def test_without_a_known_column_width_the_reference_width_is_kept():
    """No section information means no growth; widths are still shared by content."""
    new = _built(["| A | B |", "|---|---|", f"| {IDENTIFIER} | {PROSE} |"], column_width=None)
    assert sum(int(width) for width in _xpath(new, "./w:tblGrid/w:gridCol/@w:w")) == 6520


def test_a_very_long_unbreakable_value_does_not_force_the_table_wider():
    """A 40 character address cannot fit any column and is allowed to wrap; it does not set the minimum."""
    address = "48DC218393FCEEF56A37D963B804FAB92C62CA9D"
    new = _built(["| Nama | Nilai |", "|---|---|", f"| a | {address} |"], column_width=7940)
    assert sum(int(width) for width in _xpath(new, "./w:tblGrid/w:gridCol/@w:w")) == 6520


def test_cell_borders_and_shading_follow_the_reference_cells():
    """Header cells copy the reference header cell's borders and shading; body cells copy the second row's."""
    new = _built(["| A | B |", "|---|---|", "| 1 | 2 |"], column_width=None)
    header, body = _xpath(new, "./w:tr")
    assert _xpath(header, "./w:tc[1]/w:tcPr/w:tcBorders/w:top/@w:val") == ["single"]
    assert _xpath(header, "./w:tc[1]/w:tcPr/w:shd/@w:fill") == ["D9D9D9"]
    assert [etree.QName(child).localname for child in header.find("w:tc/w:tcPr", NSMAP)] == ["tcW", "tcBorders", "shd"]
    assert _xpath(body, "./w:tc[1]/w:tcPr/w:tcBorders/w:bottom/@w:sz") == ["4"]
    assert _xpath(body, "./w:tc[2]/w:tcPr/w:tcBorders") == []


THREE_IDENTIFIERS = ["| failover.state_sync_interval | cometbft.validator_address | failover.warmup_period |", "|---|---|---|", "| validator.stop_timeout | failover.grace_period | failover.retry_attempts |"]


def test_the_cell_margins_shrink_before_the_font_and_the_font_stops_at_ten_points():
    """Three 28 character identifiers per row: 12 pt at 115 twip margins needs more than the column; 60 twip margins and 10 pt fit."""
    new = _built(THREE_IDENTIFIERS, column_width=7940)
    sizes = set(_xpath(new, ".//w:ins/w:r/w:rPr/w:sz/@w:val")) | set(_xpath(new, ".//w:ins/w:r/w:rPr/w:szCs/@w:val"))
    assert sizes == {"20"}
    assert _xpath(new, "./w:tblPr/w:tblCellMar/w:left/@w:w") == ["60"]
    assert _xpath(new, "./w:tblPr/w:tblCellMar/w:right/@w:w") == ["60"]
    widths = [int(width) for width in _xpath(new, "./w:tblGrid/w:gridCol/@w:w")]
    assert sum(widths) == 7940
    assert widths[0] >= estimate_text_width("failover.state_sync_interval", 10) + 120


def test_narrow_margins_alone_are_used_when_they_are_enough_at_twelve_points():
    """A table that misses the column by less than the margin saving keeps 12 pt and gets 60 twip margins."""
    for length in range(12, 40):
        piece = "a" + "b" * length
        lines = ["| A | B | C |", "|---|---|---|", f"| failover.state_sync_interval | failover.state_sync_interval | {piece} |"]
        mins_wide = 2 * (estimate_text_width("failover.state_sync_interval", 12) + 230) + estimate_text_width(piece, 12) + 230
        if mins_wide > 7940 and mins_wide - 330 <= 7940 and len(piece) <= 32:
            new = _built(lines, column_width=7940)
            assert _xpath(new, ".//w:ins/w:r/w:rPr/w:sz") == []
            assert _xpath(new, "./w:tblPr/w:tblCellMar/w:left/@w:w") == ["60"]
            return
    raise AssertionError("no piece length produced the narrow margin case")


def test_a_table_that_does_not_fit_at_ten_points_keeps_ten_points_and_is_reported():
    """Four 28 character identifiers per row cannot fit 7940 twips even at 10 pt; the table stays at 10 pt and a note says so."""
    row = "| " + " | ".join(["failover.state_sync_interval"] * 4) + " |"
    notes: list[str] = []
    reference = _body(REFERENCE_WITH_INDENT).find(f"{{{W_NS}}}tbl")
    marks = RevisionMarks("Syahid Nurrohim", "2026-10-05T08:00:00Z", 1)
    new = build_inserted_table(parse_text_blocks(["| A | B | C | D |", "|---|---|---|---|", row])[0], reference, marks, 7940, notes)
    assert set(_xpath(new, ".//w:ins/w:r/w:rPr/w:sz/@w:val")) == {"20"}
    assert sum(int(width) for width in _xpath(new, "./w:tblGrid/w:gridCol/@w:w")) == 7940
    assert len(notes) == 1 and "10 pt" in notes[0] and "review" in notes[0]


def test_a_table_that_fits_adds_no_note():
    """Nothing to review when the pieces fit."""
    notes: list[str] = []
    reference = _body(REFERENCE_WITH_INDENT).find(f"{{{W_NS}}}tbl")
    marks = RevisionMarks("Syahid Nurrohim", "2026-10-05T08:00:00Z", 1)
    build_inserted_table(parse_text_blocks(["| A | B |", "|---|---|", "| 1 | 2 |"])[0], reference, marks, 7940, notes)
    assert notes == []


def test_the_font_is_left_alone_when_twelve_points_fit():
    """Cells carry no size when the table fits at the document's own size."""
    new = _built(["| A | B |", "|---|---|", "| 1 | 2 |"], column_width=7940)
    assert _xpath(new, ".//w:ins/w:r/w:rPr/w:sz") == []


def test_data_tokens_count_as_short_values_not_as_their_placeholder_text():
    """{{DATA:name}} is replaced by a value later, so its placeholder length must not set a column minimum."""
    new = _built(["| A | B | C | D |", "|---|---|---|---|", "| {{DATA:partition.symptom}} | {{DATA:overhead.session}} | {{DATA:hang.session}} | 1 |"], column_width=7940)
    assert sum(int(width) for width in _xpath(new, "./w:tblGrid/w:gridCol/@w:w")) == 6520
    assert _xpath(new, ".//w:ins/w:r/w:rPr/w:sz") == []


def test_a_reference_table_wider_than_the_text_column_is_capped_to_the_column():
    """The abbreviation table copies a landscape reference (12868 wide) into a portrait section (7940 wide)."""
    reference = _body(REFERENCE_WITH_INDENT.replace('w:w="6520"', 'w:w="12868"').replace('w:gridCol w:w="3260"', 'w:gridCol w:w="6434"')).find(f"{{{W_NS}}}tbl")
    marks = RevisionMarks("Syahid Nurrohim", "2026-10-05T08:00:00Z", 1)
    new = build_inserted_table(parse_text_blocks(["| A | B |", "|---|---|", "| 1 | 2 |"])[0], reference, marks, 7940)
    assert sum(int(width) for width in _xpath(new, "./w:tblGrid/w:gridCol/@w:w")) == 7940
    assert _xpath(new, "./w:tblPr/w:tblW/@w:w") == ["7940"]
    assert _xpath(new, "./w:tblPr/w:tblInd") == []


def test_an_indented_reference_that_overflows_the_column_is_reset_to_the_column():
    """Reference width plus indent beyond the column also resets the geometry."""
    reference = _body(REFERENCE_WITH_INDENT.replace('w:w="1408"', 'w:w="2000"')).find(f"{{{W_NS}}}tbl")
    marks = RevisionMarks("Syahid Nurrohim", "2026-10-05T08:00:00Z", 1)
    new = build_inserted_table(parse_text_blocks(["| A | B |", "|---|---|", "| 1 | 2 |"])[0], reference, marks, 7940)
    assert _xpath(new, "./w:tblPr/w:tblW/@w:w") == ["7940"] and _xpath(new, "./w:tblPr/w:tblInd") == []


def test_the_needed_width_defaults_to_the_ten_point_floor_with_narrow_margins():
    """needed_width sums the column minimums; by default at 10 pt with 60 twip margins on each side, the last size fill tries."""
    from redline.tables import needed_width

    block = parse_text_blocks(["| A | B |", "|---|---|", "| failover.state_sync_interval | 1 |"])[0]
    first = max(estimate_text_width("A", 10, bold=True), estimate_text_width("failover.state_sync_interval", 10)) + 120
    second = max(estimate_text_width("B", 10, bold=True), estimate_text_width("1", 10)) + 120
    assert needed_width(block) == first + second
    assert needed_width(block, 12, 230) > needed_width(block)
