"""Tests for loading and saving a .docx package without disturbing untouched bytes."""

import zipfile

import pytest

from docx_builder import W_NS, build_docx, para
from redline.package import PackageError, WordPackage


def _entries(path) -> list[tuple[str, bytes]]:
    """Return (name, bytes) for every zip entry, in archive order."""
    with zipfile.ZipFile(path) as archive:
        return [(info.filename, archive.read(info.filename)) for info in archive.infolist()]


def test_roundtrip_without_changes_is_byte_identical(tmp_path):
    """Saving an unchanged package reproduces every entry byte for byte, in order."""
    source = build_docx(tmp_path / "in.docx", para("Satu") + para("Dua ← tiga"))
    WordPackage.load(source).save(tmp_path / "out.docx")
    assert _entries(tmp_path / "out.docx") == _entries(source)


def test_saving_after_an_edit_changes_only_document_xml(tmp_path):
    """Other parts stay identical and the untouched paragraph keeps its exact bytes."""
    untouched = para("Tetap sama", rpr="<w:i/>")
    source = build_docx(tmp_path / "in.docx", untouched + para("Akan diubah"))
    package = WordPackage.load(source)
    texts = package.body.findall(f".//{{{W_NS}}}t")
    texts[1].text = "Sudah diubah"
    package.save(tmp_path / "out.docx")

    before = dict(_entries(source))
    after = dict(_entries(tmp_path / "out.docx"))
    changed = [name for name in before if before[name] != after[name]]
    assert changed == ["word/document.xml"]
    assert b"Sudah diubah" in after["word/document.xml"]
    assert before["word/document.xml"].split(b"<w:body>")[1].split(b"</w:p>")[0] == (
        after["word/document.xml"].split(b"<w:body>")[1].split(b"</w:p>")[0]
    )


def test_styles_part_is_readable(tmp_path):
    """Other XML parts can be read for lookups (styles.xml here)."""
    package = WordPackage.load(build_docx(tmp_path / "in.docx", para("x")))
    assert package.read_part("word/styles.xml").tag == f"{{{W_NS}}}styles"


def test_package_without_document_part_is_rejected(tmp_path):
    """A zip that is not a Word document is refused with a clear message."""
    bogus = tmp_path / "bogus.docx"
    with zipfile.ZipFile(bogus, "w") as archive:
        archive.writestr("hello.txt", "hi")
    with pytest.raises(PackageError, match="word/document.xml"):
        WordPackage.load(bogus)


def test_set_part_replaces_one_entry_and_keeps_the_others_identical(tmp_path):
    """A replaced part changes; every other entry keeps its bytes and order."""
    source = build_docx(tmp_path / "in.docx", para("x"))
    package = WordPackage.load(source)
    package.set_part("word/styles.xml", b"<w:styles/>")
    package.save(tmp_path / "out.docx")
    before, after = dict(_entries(source)), dict(_entries(tmp_path / "out.docx"))
    assert [name for name in before if before[name] != after[name]] == ["word/styles.xml"]
    assert after["word/styles.xml"] == b"<w:styles/>"
    assert [name for name, _ in _entries(tmp_path / "out.docx")] == [name for name, _ in _entries(source)]


def test_set_part_adds_a_new_part_at_the_end(tmp_path):
    """A part that the package lacks is appended and can be read back."""
    source = build_docx(tmp_path / "in.docx", para("x"))
    package = WordPackage.load(source)
    assert package.has_part("word/media/a.png") is False
    package.set_part("word/media/a.png", b"PNG")
    assert package.has_part("word/media/a.png") is True
    assert package.part_bytes("word/media/a.png") == b"PNG"
    package.save(tmp_path / "out.docx")
    assert _entries(tmp_path / "out.docx")[-1] == ("word/media/a.png", b"PNG")


def test_read_part_sees_a_replaced_part(tmp_path):
    """read_part parses the replaced bytes, not the original ones."""
    package = WordPackage.load(build_docx(tmp_path / "in.docx", para("x")))
    package.set_part("word/styles.xml", f'<w:styles xmlns:w="{W_NS}"><w:style/></w:styles>'.encode())
    assert len(package.read_part("word/styles.xml")) == 1


def test_missing_part_raises_a_package_error(tmp_path):
    """part_bytes names the missing part."""
    package = WordPackage.load(build_docx(tmp_path / "in.docx", para("x")))
    with pytest.raises(PackageError, match="word/numbering.xml"):
        package.part_bytes("word/numbering.xml")
