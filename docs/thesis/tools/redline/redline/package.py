"""Load a .docx, expose word/document.xml as an lxml tree, and save it back.

Only word/document.xml is re-serialized. Every other zip entry is written back with its original
bytes and ZipInfo (name, order, timestamp, compression). The document part keeps its original XML
declaration bytes and is serialized as UTF-8 without character references, so paragraphs the
tool does not touch come out byte-identical.
"""

import zipfile
from pathlib import Path

from lxml import etree

from redline.wordml import qn

DOCUMENT_PART = "word/document.xml"
ADDED_PART_DATE = (2026, 1, 1, 0, 0, 0)


class PackageError(ValueError):
    """The file is not a Word package the tool can work on."""


class WordPackage:
    """An opened .docx: the parsed document part plus the raw bytes of every entry."""

    def __init__(self, entries: list[tuple[zipfile.ZipInfo, bytes]], document: etree._Element, declaration: bytes):
        """Hold the zip entries, the parsed document root and the document's declaration prefix."""
        self._entries = entries
        self._added: dict[str, bytes] = {}
        self.document = document
        self._declaration = declaration

    @classmethod
    def load(cls, path: Path) -> "WordPackage":
        """Read every entry of the package at path and parse word/document.xml."""
        with zipfile.ZipFile(path) as archive:
            entries = [(info, archive.read(info.filename)) for info in archive.infolist()]
        raw = {info.filename: data for info, data in entries}.get(DOCUMENT_PART)
        if raw is None:
            raise PackageError(f"{path} has no {DOCUMENT_PART}; it is not a Word document")
        return cls(entries, etree.fromstring(raw), _declaration_prefix(raw))

    @property
    def body(self) -> etree._Element:
        """The w:body element of the document."""
        return self.document.find(qn("body"))

    def has_part(self, name: str) -> bool:
        """True when the package holds a part with this name."""
        return any(info.filename == name for info, _ in self._entries) or name in self._added

    def part_bytes(self, name: str) -> bytes:
        """The current bytes of a part (a replaced or added part wins over the original)."""
        if name in self._added:
            return self._added[name]
        for info, data in self._entries:
            if info.filename == name:
                return data
        raise PackageError(f"package has no part {name}")

    def read_part(self, name: str) -> etree._Element:
        """Parse another XML part (read-only use, such as styles.xml)."""
        return etree.fromstring(self.part_bytes(name))

    def set_part(self, name: str, data: bytes) -> None:
        """Replace the bytes of an existing part in place, or append a new part at the end."""
        for index, (info, _) in enumerate(self._entries):
            if info.filename == name:
                self._entries[index] = (info, data)
                return
        self._added[name] = data

    def set_xml_part(self, name: str, root: etree._Element) -> None:
        """Serialize an XML tree as the part's new bytes, with a standalone UTF-8 declaration."""
        self.set_part(name, etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True))

    def document_bytes(self) -> bytes:
        """Serialize the document part with its original declaration bytes."""
        return self._declaration + etree.tostring(self.document, encoding="UTF-8")

    def save(self, path: Path) -> None:
        """Write the package to path, replacing only the document part's bytes."""
        with zipfile.ZipFile(path, "w") as archive:
            for info, data in self._entries:
                if info.filename == DOCUMENT_PART:
                    data = self.document_bytes()
                archive.writestr(info, data)
            for name, data in self._added.items():
                archive.writestr(zipfile.ZipInfo(name, date_time=ADDED_PART_DATE), data, zipfile.ZIP_DEFLATED)


def _declaration_prefix(raw: bytes) -> bytes:
    """Return the XML declaration and the line break after it, exactly as stored."""
    if not raw.startswith(b"<?xml"):
        return b""
    end = raw.index(b"?>") + 2
    rest = raw[end:]
    line_break_length = len(rest) - len(rest.lstrip(b"\r\n"))
    return raw[: end + line_break_length]
