"""Build tiny .docx fixtures for tests: a minimal package with a body the test supplies."""

import zipfile
from pathlib import Path

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    "</Types>"
)

PACKAGE_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    "</Relationships>"
)

DOCUMENT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
    'Target="styles.xml"/>'
    '<Relationship Id="rId9" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
    'Target="https://www.zotero.org/google-docs/?abc123" TargetMode="External"/>'
    "</Relationships>"
)

DEFAULT_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
    f'<w:styles xmlns:w="{W_NS}">'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/></w:style>'
    '<w:style w:type="paragraph" w:customStyle="1" w:styleId="KodeProgram">'
    '<w:name w:val="Kode Program"/></w:style>'
    '<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/></w:style>'
    "</w:styles>"
)


def document_xml(body_xml: str) -> str:
    """Wrap inner body XML in a w:document element with the w and r namespaces declared."""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        f'<w:document xmlns:w="{W_NS}" xmlns:r="{R_NS}"><w:body>{body_xml}'
        '<w:sectPr><w:pgSz w:w="11909" w:h="16834"/>'
        '<w:pgMar w:top="2268" w:right="1701" w:bottom="1701" w:left="2268" w:header="720" w:footer="720" w:gutter="0"/></w:sectPr></w:body></w:document>'
    )


def build_docx(path: Path, body_xml: str, styles_xml: str = DEFAULT_STYLES, numbering_xml: str | None = None) -> Path:
    """Write a minimal .docx at path whose body holds body_xml, plus word/numbering.xml when given; return the path."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", CONTENT_TYPES)
        package.writestr("_rels/.rels", PACKAGE_RELS)
        package.writestr("word/document.xml", document_xml(body_xml))
        package.writestr("word/styles.xml", styles_xml)
        package.writestr("word/_rels/document.xml.rels", DOCUMENT_RELS)
        if numbering_xml is not None:
            package.writestr("word/numbering.xml", numbering_xml)
    return path


def para(text: str, ppr: str = "", rpr: str = "") -> str:
    """Return the XML of one paragraph holding text in a single run."""
    ppr_xml = f"<w:pPr>{ppr}</w:pPr>" if ppr else ""
    rpr_xml = f"<w:rPr>{rpr}</w:rPr>" if rpr else ""
    return f'<w:p>{ppr_xml}<w:r>{rpr_xml}<w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def table(rows: list[list[str]], width: int = 9000) -> str:
    """Return the XML of a bordered table whose cells each hold one paragraph of text."""
    column = width // len(rows[0])
    grid = "".join(f'<w:gridCol w:w="{column}"/>' for _ in rows[0])
    row_xml = ""
    for row in rows:
        cells = "".join(
            f'<w:tc><w:tcPr><w:tcW w:w="{column}" w:type="dxa"/></w:tcPr>'
            f'<w:p><w:pPr><w:spacing w:after="0"/></w:pPr><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>'
            for cell in row
        )
        row_xml += f"<w:tr>{cells}</w:tr>"
    return (
        f'<w:tbl><w:tblPr><w:tblW w:w="{width}" w:type="dxa"/><w:tblBorders>'
        '<w:top w:val="single" w:sz="8" w:space="0" w:color="000000"/>'
        '<w:insideH w:val="single" w:sz="8" w:space="0" w:color="000000"/>'
        f"</w:tblBorders></w:tblPr><w:tblGrid>{grid}</w:tblGrid>{row_xml}</w:tbl>"
    )


def png_bytes(width: int, height: int, dpi: int | None = None) -> bytes:
    """A valid solid-grey PNG of the given pixel size; dpi (when given) is written as a pHYs chunk."""
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        """One PNG chunk: length, type, data and CRC."""
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
    physical = b""
    if dpi is not None:
        per_metre = round(dpi / 0.0254)
        physical = chunk(b"pHYs", struct.pack(">IIB", per_metre, per_metre, 1))
    rows = b"".join(b"\x00" + b"\x80" * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + header + physical + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
