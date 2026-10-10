"""Tests for the apply, check and fill flows."""

import json
import zipfile

import pytest

from docx_builder import build_docx, para
from redline.commands import CommandError, run_apply, run_check, run_fill

DRAFT = (
    "### E-1 (closes: X-1)\nOperation: replace\nAnchor: Kalimat lama.\nText:\nKalimat baru {{DATA:S4.n}}.\nRationale: r\n\n"
    "### E-2 (closes: X-1)\nOperation: delete\nAnchor: Tidak ada di dokumen.\nRationale: r\n"
)
FAKE_VALIDATE = "import sys\nprint('All validations PASSED!')\nsys.exit(0)\n"


def _inputs(tmp_path):
    """A fixture document and a draft with one good and one failing edit."""
    docx = build_docx(tmp_path / "in.docx", para("Kalimat lama."))
    draft = tmp_path / "A.md"
    draft.write_text(DRAFT, encoding="utf-8")
    return docx, draft


def _document_xml(path) -> bytes:
    """Raw bytes of word/document.xml in a package."""
    with zipfile.ZipFile(path) as archive:
        return archive.read("word/document.xml")


def test_apply_writes_the_revised_document_and_reports_each_edit(tmp_path, monkeypatch):
    """apply saves OUT, runs validate.py, and reports applied and failed edits."""
    docx, draft = _inputs(tmp_path)
    script = tmp_path / "validate.py"
    script.write_text(FAKE_VALIDATE, encoding="utf-8")
    monkeypatch.setenv("REDLINE_VALIDATE_SCRIPT", str(script))
    result = run_apply(docx, [draft], "Syahid Nurrohim", tmp_path / "out.docx", date="2026-10-05T08:00:00Z")
    assert b'w:author="Syahid Nurrohim"' in _document_xml(tmp_path / "out.docx")
    assert result.ok is False
    assert "| E-1 | A.md:1 | replace | applied |" in result.report
    assert "| E-2 | A.md:8 | delete | error |" in result.report
    assert "Result: PASSED (exit 0)" in result.report


def test_apply_refuses_to_overwrite_its_input(tmp_path):
    """The output path must differ from the input document."""
    docx, draft = _inputs(tmp_path)
    with pytest.raises(CommandError, match="refusing to overwrite the input document"):
        run_apply(docx, [draft], "A", docx, validate=False)


def test_check_matches_without_writing_anything(tmp_path):
    """check parses and matches only; the input is untouched and no file is created."""
    docx, draft = _inputs(tmp_path)
    before = docx.read_bytes()
    files_before = sorted(tmp_path.iterdir())
    result = run_check(docx, [draft])
    assert docx.read_bytes() == before
    assert sorted(tmp_path.iterdir()) == files_before
    assert result.ok is False
    assert result.report.startswith("# Redline check report")
    assert "| E-2 | A.md:8 | no paragraph matches the anchor | none |" in result.report


def test_check_lists_a_numbering_prefix_that_matches_nothing_with_the_anchor_failures(tmp_path):
    """check matches the Numbering prefix like an anchor and reports a failure in the same table."""
    docx = build_docx(tmp_path / "in.docx", para("Jangkar."))
    draft = tmp_path / "A.md"
    draft.write_text(
        "### E-1 (closes: X-1)\nOperation: insert_after\nAnchor: Jangkar.\nNumbering: continue Tidak ada.\nText:\nBaru.\nRationale: r\n",
        encoding="utf-8",
    )
    result = run_check(docx, [draft])
    assert result.ok is False
    failures = result.report.split("## Anchor failures")[1].split("## Other errors")[0]
    assert "| E-1 | A.md:1 | Numbering prefix 'Tidak ada.': no paragraph matches the anchor | none |" in failures


def test_apply_writes_the_w_num_of_a_new_list_into_numbering_xml(tmp_path):
    """'Numbering: new' adds a w:num; the saved package carries the changed numbering part."""
    numbering = (
        '<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:abstractNum w:abstractNumId="0"/><w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num></w:numbering>'
    )
    item = '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>'
    docx = build_docx(tmp_path / "in.docx", para("Awal satu.", ppr=item) + para("Jangkar."), numbering_xml=numbering)
    draft = tmp_path / "A.md"
    draft.write_text(
        "### E-1 (closes: X-1)\nOperation: insert_after\nAnchor: Jangkar.\nNumbering: new Awal satu.\nText:\nBaru.\nRationale: r\n",
        encoding="utf-8",
    )
    result = run_apply(docx, [draft], "Syahid Nurrohim", tmp_path / "out.docx", validate=False)
    assert result.ok is True
    with zipfile.ZipFile(tmp_path / "out.docx") as archive:
        assert b'w:numId="2"' in archive.read("word/numbering.xml")
    assert b'<w:numId w:val="2"/>' in _document_xml(tmp_path / "out.docx")


def test_fill_replaces_tokens_in_the_applied_document(tmp_path):
    """fill writes OUT with values in place of tokens and reports the counts."""
    docx, draft = _inputs(tmp_path)
    applied = tmp_path / "applied.docx"
    run_apply(docx, [draft], "A", applied, validate=False)
    values = tmp_path / "values.json"
    values.write_text(json.dumps({"S4.n": "10", "extra": "1"}), encoding="utf-8")
    result = run_fill(applied, values, tmp_path / "filled.docx")
    assert b"Kalimat baru 10." in _document_xml(tmp_path / "filled.docx")
    assert "| S4.n | 1 |" in result.report
    assert "- extra" in result.report


def test_fill_with_a_values_directory_inserts_table_tokens(tmp_path):
    """A values directory fills DATA tokens from values.json and TABLE tokens from <name>.md files."""
    from docx_builder import table
    docx = build_docx(tmp_path / "in.docx", table([["A", "B"]]) + para("{{TABLE:hasil}}"))
    values = tmp_path / "values"
    values.mkdir()
    (values / "hasil.md").write_text("| No | Nilai |\n|---|---|\n| 1 | 2 |\n", encoding="utf-8")
    result = run_fill(docx, values, tmp_path / "filled.docx", author="Syahid Nurrohim")
    assert b"<w:tblHeader/>" in _document_xml(tmp_path / "filled.docx")
    assert "## Table tokens" in result.report
    assert "| hasil | filled |" in result.report


def test_fill_needs_an_author_to_insert_tables(tmp_path):
    """Table insertion is a tracked change, so it needs --author."""
    from docx_builder import table
    docx = build_docx(tmp_path / "in.docx", table([["A", "B"]]) + para("{{TABLE:hasil}}"))
    values = tmp_path / "values"
    values.mkdir()
    (values / "hasil.md").write_text("| No | Nilai |\n|---|---|\n| 1 | 2 |\n", encoding="utf-8")
    with pytest.raises(CommandError, match="--author"):
        run_fill(docx, values, tmp_path / "filled.docx")


def test_registry_command_writes_json_and_reports_decisions(tmp_path):
    """registry builds references.json from draft files and reports merges and errors."""
    from test_registry import _drafts
    from redline.commands import run_registry

    paths = []
    for name, text in _drafts().items():
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    out = tmp_path / "data" / "references.json"
    result = run_registry(paths, out)
    assert json.loads(out.read_text(encoding="utf-8"))["records"][0]["id"] == "dong2023"
    assert result.ok is False
    assert "## Merges" in result.report
    assert "nist80038d merged into nist-gcm" in result.report
    assert "## Errors" in result.report
    assert "[REF:tidak-ada]" in result.report


def test_cite_command_renumbers_rebuilds_the_list_and_reports(tmp_path):
    """cite writes OUT and a report with the mapping, counts, unresolved markers and composition."""
    from test_cite import FULL, REGISTRY
    from redline.commands import run_cite
    from redline.registry import write_registry

    docx = build_docx(tmp_path / "in.docx", para("Tanpa catatan [99].") + FULL)
    registry = tmp_path / "references.json"
    write_registry(REGISTRY.records, registry)
    result = run_cite(docx, registry, tmp_path / "cited.docx", "Syahid Nurrohim")
    assert b"SUMBER TEKNIS" in _document_xml(tmp_path / "cited.docx")
    assert result.ok is False
    assert "Reference list heading: REFERENSI (2 old entries deleted)" in result.report
    assert "| [1] | r3 | [3], [1, 3], [3]-[4] |" in result.report
    assert "| [T1] | tech1 | [REF:tech1] |" in result.report
    assert "| [99] | Tanpa catatan [99]. |" in result.report
    assert "- unused" in result.report
    assert "| 4 | 2 | 50.0% | 2 | 50.0% |" in result.report


def _figure_values(tmp_path, stem="a"):
    """A values directory holding one small PNG."""
    from docx_builder import png_bytes

    values = tmp_path / "values"
    values.mkdir()
    (values / f"{stem}.png").write_bytes(png_bytes(40, 20, 96))
    return values


def test_fill_places_figure_pictures_and_reports_them(tmp_path):
    """A values directory with <stem>.png fills {{FIGURE:stem}} paragraphs and lists the result."""
    docx = build_docx(tmp_path / "in.docx", para("{{FIGURE:a}}") + para("{{FIGURE:b}}"))
    result = run_fill(docx, _figure_values(tmp_path), tmp_path / "filled.docx", author="Syahid Nurrohim")
    with zipfile.ZipFile(tmp_path / "filled.docx") as archive:
        assert "word/media/a.png" in archive.namelist()
        assert b"<w:drawing>" in archive.read("word/document.xml")
    assert result.ok is False
    assert "## Figure tokens" in result.report
    assert "| a | filled |" in result.report
    assert "| b | no file |" in result.report


def test_fill_reports_a_figure_file_without_a_token(tmp_path):
    """A PNG with no token is listed, and the run still succeeds."""
    docx = build_docx(tmp_path / "in.docx", para("Tanpa token."))
    result = run_fill(docx, _figure_values(tmp_path, "unused"), tmp_path / "filled.docx")
    assert result.ok is True
    assert "- unused" in result.report.split("Figure files without a token:")[1]


def test_fill_needs_an_author_to_insert_figures(tmp_path):
    """A picture is a tracked insertion, so it needs --author."""
    docx = build_docx(tmp_path / "in.docx", para("{{FIGURE:a}}"))
    with pytest.raises(CommandError, match="--author"):
        run_fill(docx, _figure_values(tmp_path), tmp_path / "filled.docx")


FRAME = (
    para("SKRIPSI") + para("INTISARI") + para("Sistem failover aktif.") + para("ABSTRACT") + para("The system.")
    + para("BAB I PENDAHULUAN") + para("Bab memakai failover dan double-signing. Lalu failover lagi.") + para("DAFTAR PUSTAKA") + para("[1] entri")
)


def test_italics_command_writes_tracked_formatting_and_reports_each_scope(tmp_path):
    """italics saves OUT with rPrChange and reports per scope and term; unfound terms are listed."""
    from redline.commands import run_italics

    docx = build_docx(tmp_path / "in.docx", FRAME)
    terms = tmp_path / "terms.txt"
    terms.write_text("double-signing\nfailover\nslashing\n", encoding="utf-8")
    result = run_italics(docx, terms, tmp_path / "out.docx", "Syahid Nurrohim")
    assert b"<w:rPrChange" in _document_xml(tmp_path / "out.docx")
    assert result.ok is True
    assert "# Redline italics report" in result.report
    assert "## INTISARI" in result.report and "## BAB I sampai BAB V" in result.report
    assert "| failover | 2 | 1 | 0 | 1 | 0 |" in result.report.split("## BAB I sampai BAB V")[1]
    assert "- slashing" in result.report.split("Terms found in neither scope")[1]
    assert "Tracked formatting changes: 3" in result.report


def test_italics_command_fails_when_a_scope_boundary_is_missing(tmp_path):
    """No INTISARI section means a note and a failing verdict."""
    from redline.commands import run_italics

    docx = build_docx(tmp_path / "in.docx", para("Sistem failover."))
    terms = tmp_path / "terms.txt"
    terms.write_text("failover\n", encoding="utf-8")
    result = run_italics(docx, terms, tmp_path / "out.docx", "A")
    assert result.ok is False and "INTISARI" in result.report


def test_headers_command_marks_tables_and_reports_each(tmp_path):
    """headers saves OUT with tblHeader and trPrChange and lists every table's action."""
    from docx_builder import table
    from redline.commands import run_headers

    docx = build_docx(tmp_path / "in.docx", table([["A", "B"], ["1", "2"]]) + para("Teks.") + table([["C"]]))
    result = run_headers(docx, tmp_path / "out.docx", "Syahid Nurrohim")
    xml = _document_xml(tmp_path / "out.docx")
    assert b"<w:tblHeader/>" in xml and b"<w:trPrChange" in xml
    assert "| 1 | 2 | A | marked |" in result.report
    assert "| 2 | 1 | C | single row |" in result.report
    assert "Tables given a repeating header row: 1" in result.report
    assert result.ok is True


def test_restart_lists_command_updates_numbering_and_reports(tmp_path):
    """restart-lists writes a new w:num into numbering.xml and moves the items; errors fail the run."""
    from redline.commands import run_restart_lists

    numbering = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:abstractNum w:abstractNumId="0"/><w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num></w:numbering>'
    )
    item = '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>'
    docx = build_docx(tmp_path / "in.docx", para("Awal satu.", ppr=item) + para("Awal dua.", ppr=item))
    with zipfile.ZipFile(docx, "a") as archive:
        archive.writestr("word/numbering.xml", numbering)
    result = run_restart_lists(docx, tmp_path / "out.docx", "Syahid Nurrohim", ["Awal satu", "Tidak ada"])
    with zipfile.ZipFile(tmp_path / "out.docx") as archive:
        assert b'w:numId="2"' in archive.read("word/numbering.xml")
    assert b'<w:numId w:val="2"/>' in _document_xml(tmp_path / "out.docx")
    assert "| Awal satu | 1 | 2 | 2 |" in result.report
    assert "Tidak ada: no paragraph starts with this text" in result.report
    assert result.ok is False


def test_terms_command_writes_the_list_and_the_evidence_report(tmp_path):
    """terms writes one term per line and reports sources and dropped items."""
    from redline.commands import run_terms

    docx = build_docx(tmp_path / "in.docx", para("BAB III METODE") + '<w:p><w:r><w:rPr><w:i/></w:rPr><w:t>failover</w:t></w:r></w:p>')
    draft = tmp_path / "C-bab3.md"
    draft.write_text("### E-1 (closes: X-1)\nOperation: delete\nAnchor: A\nRationale: r\n\n### E-2 (closes: X-1)\nOperation: replace\nAnchor: B\nText:\nIni *health.interval* dan *standby*.\nRationale: r\n", encoding="utf-8")
    exclusions = tmp_path / "exclusions.txt"
    exclusions.write_text("", encoding="utf-8")
    out = tmp_path / "terms.txt"
    result = run_terms(docx, [draft], out, exclusions)
    assert out.read_text(encoding="utf-8") == "failover\nstandby\n"
    assert "| standby | draft C 1 |" in result.report
    assert "| health.interval | code identifier |" in result.report


def test_breaks_command_deletes_the_redundant_page_break_and_reports_the_neighbours(tmp_path):
    """breaks saves OUT with the break run in a w:del and names the text before and after."""
    from redline.commands import run_breaks

    body = para("Tabel 2.1 akhir") + '<w:p><w:r><w:br w:type="page"/></w:r></w:p>' + '<w:p><w:pPr><w:sectPr><w:pgSz w:w="16834" w:h="11909"/></w:sectPr></w:pPr></w:p>' + para("Sesudah")
    docx = build_docx(tmp_path / "in.docx", body)
    result = run_breaks(docx, tmp_path / "out.docx", "Syahid Nurrohim")
    assert b'<w:del ' in _document_xml(tmp_path / "out.docx")
    assert "Page breaks removed: 1" in result.report
    assert "| Tabel 2.1 akhir | Sesudah |" in result.report
    assert result.ok is True


def test_fill_after_italics_still_fills_a_token_whose_name_holds_a_term(tmp_path):
    """Pipeline order fill, italics, fill: the placeholder survives the italics pass whole."""
    from redline.commands import run_italics

    ours = '<w:p><w:ins w:id="1" w:author="Syahid Nurrohim"><w:r><w:t>Isi failover {{DATA:failover.n}} siklus.</w:t></w:r></w:ins></w:p>'
    docx = build_docx(tmp_path / "in.docx", para("INTISARI") + para("x") + para("ABSTRACT") + para("y") + para("BAB I PENDAHULUAN") + ours + para("DAFTAR PUSTAKA"))
    terms = tmp_path / "terms.txt"
    terms.write_text("failover\n", encoding="utf-8")
    run_italics(docx, terms, tmp_path / "italic.docx", "Syahid Nurrohim")
    values = tmp_path / "values.json"
    values.write_text(json.dumps({"failover.n": "10"}), encoding="utf-8")
    result = run_fill(tmp_path / "italic.docx", values, tmp_path / "filled.docx")
    assert "| failover.n | 1 |" in result.report
    assert b"siklus" in _document_xml(tmp_path / "filled.docx")


def test_landscape_command_writes_the_section_breaks_and_reports_each_prefix(tmp_path):
    """landscape saves OUT with two tracked section breaks per turned block, one report row per prefix and the problems."""
    from redline.commands import run_landscape

    token = "<w:p><w:r><w:t>{{TABLE:failover_iterations}}</w:t></w:r></w:p>"
    docx = build_docx(tmp_path / "in.docx", para("Tabel 4.5 Hasil Failover per Iterasi, Sesi S5") + token + para("Sesudah."))
    with pytest.raises(CommandError, match="refusing to overwrite the input document"):
        run_landscape(docx, docx, "Syahid Nurrohim", ["Tabel 4.5"])
    result = run_landscape(docx, tmp_path / "out.docx", "Syahid Nurrohim", ["Tabel 4.5 Hasil Failover", "Tabel 9.9"])
    xml = _document_xml(tmp_path / "out.docx")
    assert xml.count(b"<w:sectPr>") == 3 and xml.count(b'w:orient="landscape"') == 1
    assert "Blocks turned to landscape: 1" in result.report
    caption = "Tabel 4.5 Hasil Failover per Iterasi, Sesi S5"
    assert f"| Tabel 4.5 Hasil Failover | {caption} | token failover_iterations | 7940 | 12865 | turned to landscape |" in result.report
    assert "| Tabel 9.9 |  |  | n/a | n/a | skipped |" in result.report
    assert "- Tabel 9.9: no paragraph starts with this text" in result.report.split("## Problems")[1]
    assert result.ok is False


def test_landscape_report_lists_table_notes_in_the_block_column(tmp_path):
    """Tabel 4.8: token, the deleted original table, then its Catatan: note; the Block column names all three and the run is ok."""
    from docx_builder import table
    from redline.commands import run_landscape

    caption = "Tabel 4.8 Hasil Failback per Iterasi, Sesi S5"
    deleted = table([["Iterasi", "TTR"], ["1", "9"]]).replace("<w:tr>", '<w:tr><w:trPr><w:del w:id="7" w:author="Syahid Nurrohim"/></w:trPr>')
    body = para(caption) + para("{{TABLE:failback}}") + deleted + para("Catatan: stempel notifikasi diambil dari log.") + para("Sesudah.")
    docx = build_docx(tmp_path / "in.docx", body)
    result = run_landscape(docx, tmp_path / "out.docx", "Syahid Nurrohim", ["Tabel 4.8 Hasil Failback"])
    assert f"| Tabel 4.8 Hasil Failback | {caption} | token failback, deleted table, note | 7940 | 12865 | turned to landscape |" in result.report
    assert result.ok is True


def test_prep_command_builds_the_fill_folder_and_reports_every_section(tmp_path):
    """prep writes OUT and a report with the values, tables, figures, landscape command and problems; a problem fails the run."""
    from test_prep import FAILOVER, HARNESS, _repository
    from redline.commands import run_prep
    from redline.fill import load_table_file
    from redline.prep import centimetres
    from redline.tables import needed_width

    tables = [
        {"token": "failover_iterations", "source": "tabel-failover.md", "caption": "Tabel 4.5 Hasil Failover per Iterasi", "columns": ["Sesi", "Iterasi", "TTD (s)"]},
        {"token": "session_map", "source": "tabel-peta-sesi.md", "caption": "Tabel 4.3 Peta Sesi Pengujian", "manual": "merged by hand"},
        {"token": "drill", "source": "tabel-drill.md", "caption": "Tabel 4.11 Hasil Uji Pemulihan Manual"},
    ]
    layout = {"tables": tables, "text_width_twips": {"portrait": 1000, "landscape": 12867}}
    layout_path, root = _repository(tmp_path, {"tabel-failover.md": FAILOVER}, HARNESS, layout)
    draft = tmp_path / "D-bab4.md"
    draft.write_text("## Data tokens\n\n- `{{DATA:soak.availability}}`: ketersediaan.\n", encoding="utf-8")
    out = tmp_path / "values"
    with pytest.raises(CommandError, match="is not empty"):
        run_prep(layout_path, root, tmp_path, None, [])
    result = run_prep(layout_path, root, out, None, [draft])
    report = result.report
    needed = needed_width(load_table_file(out / "failover_iterations.md"))
    assert result.ok is False
    assert "Values written to values.json: 3" in report
    assert "- `soak.availability`: no session has soak.json" in report.split("Uncomputed by the harness (1):")[1]
    assert "- `soak.availability` (notes sections only; D-bab4.md): no session has soak.json" in report
    assert "### failover_iterations: Tabel 4.5 Hasil Failover per Iterasi" in report
    assert "- Dropped columns: Alasan tidak valid (not listed in columns); Keterangan (not listed in columns)" in report
    assert f"- Needed width at 10 pt: {needed} twips ({centimetres(needed)} cm)" in report
    assert "- Orientation: landscape" in report and "- Transposed: no" in report
    assert "- session_map (Tabel 4.3 Peta Sesi Pengujian): merged by hand" in report
    assert "--author NAME --at 'Tabel 4.5 Hasil Failover per Iterasi'" in report.split("## Landscape command")[1]
    assert "- table drill: tabel-drill.md is not in the results folder" in report.split("## Problems")[1]
