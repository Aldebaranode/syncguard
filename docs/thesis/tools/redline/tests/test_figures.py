"""Tests for figure tokens: PNG sizing, the inline drawing, and filling {{FIGURE:stem}} paragraphs."""

import zipfile

import pytest
from lxml import etree

from docx_builder import R_NS, W_NS, build_docx, para, png_bytes
from redline.figures import (
    CAPTION_ALLOWANCE,
    EMU_PER_INCH,
    EMU_PER_TWIP,
    FigureError,
    fill_figure_tokens,
    figure_extent,
    figure_files,
    read_png_info,
)
from redline.package import WordPackage
from redline.tracked import RevisionMarks

DATE = "2026-10-05T08:00:00Z"
AUTHOR = "Syahid Nurrohim"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
NSMAP = {"w": W_NS, "r": R_NS, "wp": WP_NS, "rel": "http://schemas.openxmlformats.org/package/2006/relationships"}
COLUMN = 11909 - 2268 - 1701


def _marks() -> RevisionMarks:
    """Revision marks for the fill."""
    return RevisionMarks(AUTHOR, DATE, 100)


def _package(tmp_path, body: str) -> WordPackage:
    """Load a fixture package with the given body."""
    return WordPackage.load(build_docx(tmp_path / "in.docx", body))


def _files(tmp_path, **sizes) -> dict:
    """Write one PNG per keyword (stem=(width, height, dpi)) and return the stem to path map."""
    paths = {}
    for stem, (width, height, dpi) in sizes.items():
        path = tmp_path / f"{stem}.png"
        path.write_bytes(png_bytes(width, height, dpi))
        paths[stem] = path
    return paths


INSERTED_TOKEN = (
    '<w:p><w:pPr><w:rPr><w:ins w:id="5" w:author="A" w:date="2026-10-05T08:00:00Z"/></w:rPr></w:pPr>'
    '<w:ins w:id="6" w:author="A" w:date="2026-10-05T08:00:00Z"><w:r><w:t>{{FIGURE:%s}}</w:t></w:r></w:ins></w:p>'
)


def test_png_info_reads_pixels_and_dpi():
    """Width, height and the pHYs resolution are read; a missing pHYs means 96 dpi."""
    info = read_png_info(png_bytes(600, 300, 300))
    assert (info.width_px, info.height_px) == (600, 300)
    assert info.dpi == pytest.approx(300, abs=0.5)
    assert read_png_info(png_bytes(10, 20)).dpi == 96


def test_png_info_refuses_other_data():
    """Bytes that are not a PNG raise a FigureError."""
    with pytest.raises(FigureError, match="not a PNG"):
        read_png_info(b"GIF89a....")


def test_extent_is_capped_to_the_column_and_keeps_the_aspect_ratio():
    """A wide image shrinks to the column width; the height follows the ratio."""
    info = read_png_info(png_bytes(3000, 1500, 300))
    width, height = figure_extent(info, COLUMN * EMU_PER_TWIP)
    assert width == COLUMN * EMU_PER_TWIP
    assert height == width // 2


def test_extent_is_also_capped_to_the_available_height():
    """A tall image shrinks until its height fits; the width follows the ratio."""
    info = read_png_info(png_bytes(1000, 4000, 300))
    width, height = figure_extent(info, 10 * EMU_PER_INCH, 5 * EMU_PER_INCH)
    assert height == 5 * EMU_PER_INCH
    assert width == round(5 * EMU_PER_INCH * 1000 / 4000)


def test_fill_leaves_room_for_the_caption_below_a_tall_figure(tmp_path):
    """A figure taller than the text area minus the caption allowance is scaled to fit."""
    package = _package(tmp_path, INSERTED_TOKEN % "tall")
    fill_figure_tokens(package, _files(tmp_path, tall=(1000, 9000, 300)), _marks())
    [extent] = package.body.xpath(".//wp:inline/wp:extent", namespaces=NSMAP)
    assert int(extent.get("cy")) == (16834 - 2268 - 1701 - CAPTION_ALLOWANCE) * EMU_PER_TWIP


def test_extent_keeps_the_natural_width_of_a_small_image():
    """An image narrower than the column keeps its size at its own dpi."""
    info = read_png_info(png_bytes(600, 300, 300))
    assert figure_extent(info, COLUMN * EMU_PER_TWIP) == (2 * EMU_PER_INCH, EMU_PER_INCH)


def test_fill_replaces_an_inserted_token_by_a_drawing_and_registers_the_image(tmp_path):
    """The token run in our w:ins becomes a drawing run; part, relationship and content type are added."""
    package = _package(tmp_path, INSERTED_TOKEN % "S4.fig")
    files = _files(tmp_path, **{"S4.fig": (3000, 1500, 300)})
    result = fill_figure_tokens(package, files, _marks())
    assert result.figures_filled == ["S4.fig"]
    ins = package.body.find(f"{{{W_NS}}}p/{{{W_NS}}}ins")
    assert ins.xpath(".//w:t", namespaces=NSMAP) == []
    [extent] = ins.xpath(".//wp:inline/wp:extent", namespaces=NSMAP)
    assert int(extent.get("cx")) == COLUMN * EMU_PER_TWIP
    [blip] = ins.xpath(".//*[local-name()='blip']")
    relationship_id = blip.get(f"{{{R_NS}}}embed")
    relationships = package.read_part("word/_rels/document.xml.rels")
    [target] = relationships.xpath(f"rel:Relationship[@Id='{relationship_id}']/@Target", namespaces=NSMAP)
    assert package.part_bytes(f"word/{target}") == files["S4.fig"].read_bytes()
    assert b'Extension="png"' in package.part_bytes("[Content_Types].xml")
    package.save(tmp_path / "out.docx")
    assert f"word/{target}" in zipfile.ZipFile(tmp_path / "out.docx").namelist()


def test_fill_tracks_an_original_token_as_deleted_and_inserts_the_drawing(tmp_path):
    """A token in original text is deleted as a tracked change and the drawing is a tracked insertion."""
    package = _package(tmp_path, para("{{FIGURE:a}}"))
    result = fill_figure_tokens(package, _files(tmp_path, a=(100, 100, 96)), _marks())
    assert result.figures_filled == ["a"]
    paragraph = package.body.find(f"{{{W_NS}}}p")
    assert paragraph.xpath("./w:del/w:r/w:delText/text()", namespaces=NSMAP) == ["{{FIGURE:a}}"]
    assert len(paragraph.xpath("./w:ins/w:r/w:drawing", namespaces=NSMAP)) == 1


def test_fill_subtracts_paragraph_indents_from_the_width(tmp_path):
    """The available width is the column width minus the paragraph's left and right indents."""
    token = para("{{FIGURE:a}}", ppr='<w:ind w:left="709" w:right="100"/>')
    package = _package(tmp_path, token)
    fill_figure_tokens(package, _files(tmp_path, a=(3000, 1500, 300)), _marks())
    [extent] = package.body.xpath(".//wp:inline/wp:extent", namespaces=NSMAP)
    assert int(extent.get("cx")) == (COLUMN - 709 - 100) * EMU_PER_TWIP


def test_fill_gives_each_drawing_a_unique_id(tmp_path):
    """Two figures get different wp:docPr ids, above any id already in the document."""
    existing = '<w:p><w:r><w:drawing><wp:inline xmlns:wp="%s"><wp:docPr id="7" name="x"/></wp:inline></w:drawing></w:r></w:p>' % WP_NS
    package = _package(tmp_path, existing + INSERTED_TOKEN % "a" + INSERTED_TOKEN % "b")
    fill_figure_tokens(package, _files(tmp_path, a=(10, 10, 96), b=(10, 10, 96)), _marks())
    ids = package.body.xpath(".//wp:docPr/@id", namespaces=NSMAP)
    assert len(set(ids)) == 3 and ids[0] == "7" and all(int(i) > 7 for i in ids[1:])


def test_fill_reuses_one_image_part_for_a_repeated_token(tmp_path):
    """The same stem twice adds one part and one relationship."""
    package = _package(tmp_path, INSERTED_TOKEN % "a" + INSERTED_TOKEN % "a")
    fill_figure_tokens(package, _files(tmp_path, a=(10, 10, 96)), _marks())
    blips = package.body.xpath(".//*[local-name()='blip']/@r:embed", namespaces=NSMAP)
    assert len(blips) == 2 and blips[0] == blips[1]


def test_fill_reports_tokens_without_file_and_files_without_token(tmp_path):
    """Both mismatches are listed; nothing else changes."""
    package = _package(tmp_path, INSERTED_TOKEN % "missing")
    result = fill_figure_tokens(package, _files(tmp_path, extra=(10, 10, 96)), _marks())
    assert result.tokens_without_file == ["missing"]
    assert result.files_without_token == ["extra"]
    assert result.figures_filled == []
    assert package.body.xpath(".//w:t/text()", namespaces=NSMAP) == ["{{FIGURE:missing}}"]


def test_fill_reports_an_unreadable_file_as_an_error(tmp_path):
    """A file that is not a PNG is an error for that token and leaves the token in place."""
    package = _package(tmp_path, INSERTED_TOKEN % "bad")
    (tmp_path / "bad.png").write_bytes(b"not a png")
    result = fill_figure_tokens(package, {"bad": tmp_path / "bad.png"}, _marks())
    assert "not a PNG" in result.errors["bad"]
    assert package.body.xpath(".//w:t/text()", namespaces=NSMAP) == ["{{FIGURE:bad}}"]


def test_a_token_inside_running_text_is_not_a_figure_paragraph(tmp_path):
    """Only a paragraph whose whole text is the token is filled."""
    package = _package(tmp_path, para("Lihat {{FIGURE:a}} berikut."))
    result = fill_figure_tokens(package, _files(tmp_path, a=(10, 10, 96)), _marks())
    assert result.figures_filled == [] and result.tokens_without_file == []
    assert result.files_without_token == ["a"]


def test_figure_files_lists_pngs_of_a_values_directory(tmp_path):
    """Every <stem>.png in the directory maps to its path; a plain file maps to nothing."""
    (tmp_path / "S4.fig.png").write_bytes(png_bytes(1, 1))
    (tmp_path / "hasil.md").write_text("x", encoding="utf-8")
    assert figure_files(tmp_path) == {"S4.fig": tmp_path / "S4.fig.png"}
    assert figure_files(tmp_path / "values.json") == {}
