"""Tests for turning Text lines into paragraph and table blocks with inline formatting."""

import pytest

from redline.markup import MarkupError, ParagraphBlock, Segment, TableBlock, parse_inline, parse_row_lines, parse_text_blocks


def test_plain_line_is_one_plain_segment():
    """Text without markers stays one literal segment."""
    assert parse_inline("Story Network adalah jaringan.") == (Segment("Story Network adalah jaringan."),)


def test_italic_and_bold_markers_become_formatted_segments():
    """*x* is italic and **x** is bold; the markers themselves are dropped."""
    assert parse_inline("**Lapis pertama:** kunci *mock-key swap* dipakai.") == (
        Segment("Lapis pertama:", bold=True),
        Segment(" kunci "),
        Segment("mock-key swap", italic=True),
        Segment(" dipakai."),
    )


def test_tokens_and_citations_stay_literal():
    """[REF:key], [n] and {{DATA:name}} are plain text."""
    line = "Nilai {{DATA:S4.ttd.mean}} detik [REF:story-genesis], [15]."
    assert parse_inline(line) == (Segment(line),)


def test_stray_asterisks_stay_literal():
    """An asterisk with a space after it, or without a partner, is not a marker."""
    assert parse_inline("a * b * c dan 5*") == (Segment("a * b * c dan 5*"),)


def test_paragraph_lines_become_paragraph_blocks():
    """Each non-table line is one paragraph."""
    blocks = parse_text_blocks(["Baris satu.", "Baris *dua*."])
    assert blocks == [
        ParagraphBlock((Segment("Baris satu."),)),
        ParagraphBlock((Segment("Baris "), Segment("dua", italic=True), Segment("."))),
    ]


def test_markdown_table_becomes_table_block_between_paragraphs():
    """A header row, a separator and body rows form one table; cells keep inline formatting."""
    blocks = parse_text_blocks(
        [
            "Sebelum tabel.",
            "| Parameter | Nilai |",
            "|---|:---:|",
            "| health.interval | 5 detik |",
            "| *warm-up* | 90 detik \\| maks |",
            "Sesudah tabel.",
        ]
    )
    assert blocks[0] == ParagraphBlock((Segment("Sebelum tabel."),))
    assert blocks[1] == TableBlock(
        (
            ((Segment("Parameter"),), (Segment("Nilai"),)),
            ((Segment("health.interval"),), (Segment("5 detik"),)),
            ((Segment("warm-up", italic=True),), (Segment("90 detik | maks"),)),
        )
    )
    assert blocks[2] == ParagraphBlock((Segment("Sesudah tabel."),))


def test_table_row_with_wrong_cell_count_is_an_error():
    """Every row must have as many cells as the header; a mismatch is not guessed."""
    with pytest.raises(MarkupError, match="table row 2 has 3 cells, header has 2"):
        parse_text_blocks(["| a | b |", "|---|---|", "| 1 | 2 | 3 |"])


def test_pipe_lines_without_separator_are_an_error():
    """Lines starting with '|' must form a table with a separator row."""
    with pytest.raises(MarkupError, match="table needs a separator row"):
        parse_text_blocks(["| a | b |", "| 1 | 2 |"])


def test_row_lines_become_rows_of_formatted_cells():
    """Row edits take '| cell | cell |' lines, one row per line, without a separator."""
    assert parse_row_lines(["| health.interval | *5* detik |", "| min_peers | 3 |"]) == [
        ((Segment("health.interval"),), (Segment("5", italic=True), Segment(" detik"))),
        ((Segment("min_peers"),), (Segment("3"),)),
    ]


def test_row_lines_refuse_separators_and_plain_lines():
    """A separator row or a non-row line in a row edit is an error."""
    with pytest.raises(MarkupError, match="row edits take table rows only"):
        parse_row_lines(["| a | b |", "|---|---|"])
    with pytest.raises(MarkupError, match="row edits take table rows only"):
        parse_row_lines(["teks biasa"])


def test_table_cells_keep_the_cell_text_as_written():
    """The raw cells of a pipe table: markers stay, an escaped pipe is unescaped, the separator row is dropped."""
    from redline.markup import table_cells

    assert table_cells(["| Sesi | Catatan |", "|---|---|", "| S5 | *tetap* a \\| b |"]) == [["Sesi", "Catatan"], ["S5", "*tetap* a | b"]]
    with pytest.raises(MarkupError, match="table row 2 has 1 cells, header has 2"):
        table_cells(["| A | B |", "|---|---|", "| 1 |"])


def test_a_table_row_escapes_pipes_and_line_breaks_so_its_cells_read_back():
    """format_table_row writes one Markdown row that table_cells reads back as the same cells."""
    from redline.markup import format_table_row, table_cells

    row = format_table_row(["a | b", "baris\nkedua", 3])
    assert row == "| a \\| b | baris kedua | 3 |"
    assert table_cells([format_table_row(["A", "B", "C"]), "|---|---|---|", row])[1] == ["a | b", "baris kedua", "3"]
