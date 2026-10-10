"""Fill {{FIGURE:stem}} paragraphs with the picture <stem>.png as a tracked insertion.

A paragraph whose whole accepted text is the token gets an inline drawing. A token inside our own
w:ins (the usual case: an inserted paragraph, or the Text of a replace_image edit) is swapped for
the drawing in place, since the text it stands for is already a tracked insertion. A token in
original text is deleted as a tracked change and the drawing follows in a w:ins run.

The picture is as wide as the section's text column minus the paragraph's indents, or its natural
width at its own resolution when that is smaller; the aspect ratio is kept. The PNG becomes a new
part under word/media, with a relationship in word/_rels/document.xml.rels and a png Default in
[Content_Types].xml.
"""

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

from lxml import etree

from redline.matcher import index_paragraphs
from redline.package import WordPackage
from redline.structure import text_area_height, text_column_width
from redline.tracked import RevisionMarks, delete_runs
from redline.wordml import W_NS, has_ancestor, owning_paragraph, qn

FIGURE_TOKEN = re.compile(r"\{\{FIGURE:([^{}]+)\}\}")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
EMU_PER_INCH = 914400
EMU_PER_TWIP = 635
DEFAULT_DPI = 96
# Room kept below a figure on its page for the caption (two lines of 12 pt text plus spacing), in twips.
CAPTION_ALLOWANCE = 1134
METRES_PER_INCH = 0.0254
RELATIONSHIPS_PART = "word/_rels/document.xml.rels"
CONTENT_TYPES_PART = "[Content_Types].xml"
IMAGE_RELATIONSHIP = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
PACKAGE_RELATIONSHIPS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
PIC_NS = "http://schemas.openxmlformats.org/drawingml/2006/picture"


class FigureError(ValueError):
    """A figure file or token that cannot be used."""


@dataclass(frozen=True)
class PngInfo:
    """Pixel size and resolution (dots per inch) of a PNG."""

    width_px: int
    height_px: int
    dpi: int


@dataclass
class FigureFillResult:
    """Figure tokens filled, tokens without a file, files without a token, and unusable files."""

    figures_filled: list[str] = field(default_factory=list)
    tokens_without_file: list[str] = field(default_factory=list)
    files_without_token: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


def read_png_info(data: bytes) -> PngInfo:
    """Read the pixel size from IHDR and the resolution from pHYs (96 dpi when the file has none)."""
    if not data.startswith(PNG_SIGNATURE):
        raise FigureError("the file is not a PNG")
    width = height = 0
    dpi = DEFAULT_DPI
    position = len(PNG_SIGNATURE)
    while position + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[position : position + 8])
        body = data[position + 8 : position + 8 + length]
        if kind == b"IHDR":
            width, height = struct.unpack(">II", body[:8])
        elif kind == b"pHYs":
            per_metre, _, unit = struct.unpack(">IIB", body[:9])
            if unit == 1 and per_metre > 0:
                dpi = max(1, round(per_metre * METRES_PER_INCH))
        elif kind == b"IDAT":
            break
        position += 12 + length
    if not width or not height:
        raise FigureError("the PNG has no readable header")
    return PngInfo(width, height, dpi)


def figure_extent(info: PngInfo, max_width_emu: int, max_height_emu: int | None = None) -> tuple[int, int]:
    """Width and height in EMU: the natural size at the file's dpi, shrunk to fit the width and the height."""
    natural = round(info.width_px / info.dpi * EMU_PER_INCH)
    width = min(natural, max_width_emu)
    height = round(width * info.height_px / info.width_px)
    if max_height_emu is not None and height > max_height_emu:
        height = max_height_emu
        width = round(height * info.width_px / info.height_px)
    return width, height


def figure_files(path: Path) -> dict[str, Path]:
    """Map stem to path for every <stem>.png in a values directory (a plain values file has none)."""
    path = Path(path)
    if not path.is_dir():
        return {}
    return {figure.stem: figure for figure in sorted(path.glob("*.png"))}


def figure_token_names(document: etree._Element) -> list[str]:
    """Stems of the {{FIGURE:stem}} paragraphs in the body, in document order."""
    names = []
    for item in index_paragraphs(document.find(qn("body"))):
        token = FIGURE_TOKEN.fullmatch(item.text)
        if token is not None:
            names.append(token.group(1))
    return names


def fill_figure_tokens(package: WordPackage, figure_paths: dict[str, Path], marks: RevisionMarks) -> FigureFillResult:
    """Put the picture of every {{FIGURE:stem}} paragraph that has a <stem>.png in its place."""
    result = FigureFillResult()
    registry = _ImageRegistry(package)
    drawing_ids = _DrawingIds(package.document)
    seen: list[str] = []
    for item in index_paragraphs(package.body):
        token = FIGURE_TOKEN.fullmatch(item.text)
        if token is None:
            continue
        stem = token.group(1)
        seen.append(stem)
        if stem not in figure_paths:
            if stem not in result.tokens_without_file:
                result.tokens_without_file.append(stem)
            continue
        try:
            data = Path(figure_paths[stem]).read_bytes()
            info = read_png_info(data)
            relationship_id = registry.register(stem, data)
            width, height = figure_extent(info, _available_width(item.element) * EMU_PER_TWIP, _available_height(item.element) * EMU_PER_TWIP)
            drawing = _drawing_run(relationship_id, width, height, drawing_ids.next(), stem)
            _put_drawing_in_place_of_token(item.element, drawing, marks)
        except FigureError as error:
            result.errors[stem] = str(error)
            continue
        result.figures_filled.append(stem)
    registry.commit()
    result.files_without_token = [stem for stem in figure_paths if stem not in seen]
    return result


class _ImageRegistry:
    """Adds image parts, document relationships and the png content type, once per stem."""

    def __init__(self, package: WordPackage):
        """Parse the relationships and content types parts; nothing is written until commit."""
        self._package = package
        self._relationships = package.read_part(RELATIONSHIPS_PART)
        self._content_types = package.read_part(CONTENT_TYPES_PART)
        self._by_stem: dict[str, str] = {}
        self._changed = False

    def register(self, stem: str, data: bytes) -> str:
        """Store the PNG as a media part and return its relationship id (the same id for a repeated stem)."""
        if stem in self._by_stem:
            return self._by_stem[stem]
        part_name = self._free_part_name(stem)
        self._package.set_part(part_name, data)
        relationship_id = f"rId{self._highest_relationship_number() + 1}"
        relationship = etree.SubElement(self._relationships, f"{{{PACKAGE_RELATIONSHIPS_NS}}}Relationship")
        relationship.set("Id", relationship_id)
        relationship.set("Type", IMAGE_RELATIONSHIP)
        relationship.set("Target", part_name.removeprefix("word/"))
        self._by_stem[stem] = relationship_id
        self._changed = True
        return relationship_id

    def commit(self) -> None:
        """Write the relationships and, when png has no Default yet, the content types back."""
        if not self._changed:
            return
        self._package.set_xml_part(RELATIONSHIPS_PART, self._relationships)
        extensions = [default.get("Extension", "").casefold() for default in self._content_types]
        if "png" not in extensions:
            default = etree.Element(f"{{{CONTENT_TYPES_NS}}}Default")
            default.set("Extension", "png")
            default.set("ContentType", "image/png")
            self._content_types.insert(0, default)
            self._package.set_xml_part(CONTENT_TYPES_PART, self._content_types)

    def _free_part_name(self, stem: str) -> str:
        """word/media/<safe stem>.png, numbered when that name is taken."""
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", stem)
        name = f"word/media/{safe}.png"
        number = 2
        while self._package.has_part(name):
            name = f"word/media/{safe}-{number}.png"
            number += 1
        return name

    def _highest_relationship_number(self) -> int:
        """The highest number among rId<n> relationship ids."""
        numbers = [int(match.group(1)) for rel in self._relationships if (match := re.fullmatch(r"rId(\d+)", rel.get("Id", "")))]
        return max(numbers, default=0)


class _DrawingIds:
    """Hands out wp:docPr ids above every id already used in the document."""

    def __init__(self, document: etree._Element):
        """Start after the highest existing docPr id."""
        existing = [int(element.get("id")) for element in document.iter(f"{{{WP_NS}}}docPr") if (element.get("id") or "").isdigit()]
        self._last = max(existing, default=0)

    def next(self) -> int:
        """The next free id."""
        self._last += 1
        return self._last


def _available_width(paragraph: etree._Element) -> int:
    """Text column width in twips minus the paragraph's left and right indents."""
    indent = paragraph.find(f"{qn('pPr')}/{qn('ind')}")
    taken = 0
    if indent is not None:
        for name in ("left", "right"):
            taken += int(indent.get(qn(name), "0"))
    return text_column_width(paragraph) - taken


def _available_height(paragraph: etree._Element) -> int:
    """Text area height in twips minus the room kept for the caption below the figure."""
    return text_area_height(paragraph) - CAPTION_ALLOWANCE


def _put_drawing_in_place_of_token(paragraph: etree._Element, drawing: etree._Element, marks: RevisionMarks) -> None:
    """Swap the token run for the drawing run inside our w:ins, or delete the token and insert after it."""
    token_runs = [
        run
        for run in paragraph.iter(qn("r"))
        if owning_paragraph(run) is paragraph and not has_ancestor(run, "del", stop=paragraph) and run.find(qn("t")) is not None
    ]
    if len(token_runs) != 1:
        raise FigureError("the token is split over several runs")
    run = token_runs[0]
    if run.getparent().tag == qn("ins"):
        run.addprevious(drawing)
        run.getparent().remove(run)
        return
    insert_point = delete_runs(paragraph, marks)
    insertion = marks.insertion()
    insertion.append(drawing)
    if insert_point is None:
        paragraph.append(insertion)
    else:
        insert_point.addnext(insertion)


def _drawing_run(relationship_id: str, width: int, height: int, drawing_id: int, stem: str) -> etree._Element:
    """A w:r holding an inline picture that points at the relationship."""
    label = escape(stem, {'"': "&quot;"})
    xml = (
        f'<w:r xmlns:w="{W_NS}" xmlns:wp="{WP_NS}" xmlns:a="{A_NS}" xmlns:pic="{PIC_NS}" xmlns:r="{R_NS}">'
        "<w:rPr><w:noProof/></w:rPr><w:drawing>"
        '<wp:inline distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{width}" cy="{height}"/><wp:effectExtent l="0" t="0" r="0" b="0"/>'
        f'<wp:docPr id="{drawing_id}" name="{label}.png" descr="{label}"/>'
        '<wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>'
        f'<a:graphic><a:graphicData uri="{PIC_NS}"><pic:pic>'
        f'<pic:nvPicPr><pic:cNvPr id="{drawing_id}" name="{label}.png"/><pic:cNvPicPr/></pic:nvPicPr>'
        f'<pic:blipFill><a:blip r:embed="{relationship_id}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{width}" cy="{height}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
        "</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>"
    )
    return etree.fromstring(xml)
