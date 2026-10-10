"""Tests for applying parsed edits to a document as tracked changes."""

from dataclasses import replace

from lxml import etree

from docx_builder import R_NS, W_NS, build_docx, para, table
from redline.apply import apply_edits, run_edits
from redline.edits import Edit, Numbering
from redline.matcher import paragraph_text
from redline.package import WordPackage

DATE = "2026-10-05T08:00:00Z"
AUTHOR = "Syahid Nurrohim"
NSMAP = {"w": W_NS, "r": R_NS}
LONG = "Penelitian ini merancang dan mengimplementasikan sistem failover otomatis bernama syncguard."


def _edit(edit_id: str, operation: str, anchor: str, *text: str, style=None, occurrence=None, numbering=None) -> Edit:
    """Build an Edit record the way the parser would."""
    return Edit(edit_id, "X-1", operation, anchor, occurrence, style, tuple(text), "r", "draft.md", 1, numbering=numbering)


def _package(tmp_path, body: str, numbering: str | None = None) -> WordPackage:
    """Load a fixture package with the given body and, when given, word/numbering.xml."""
    return WordPackage.load(build_docx(tmp_path / "in.docx", body, numbering_xml=numbering))


def _texts(package: WordPackage) -> list[str]:
    """Plain text of every top-level body paragraph after the edits."""
    return [paragraph_text(p) for p in package.body.findall(f"{{{W_NS}}}p")]


def _xpath(element, path):
    """Run an XPath with the w and r prefixes bound."""
    return element.xpath(path, namespaces=NSMAP)


def _tags(element) -> list[str]:
    """Local names of an element's children."""
    return [etree.QName(child).localname for child in element]


def _item(text: str, num_id: int, level: int = 0) -> str:
    """A list paragraph shaped like the manuscript's: widowControl, numPr, spacing and jc."""
    numbering = f"<w:numPr><w:ilvl w:val='{level}'/><w:numId w:val='{num_id}'/></w:numPr>"
    return para(text, ppr=f"<w:widowControl w:val='0'/>{numbering}<w:spacing w:after='0'/><w:jc w:val='both'/>")


def _num_ids(package: WordPackage) -> list[str]:
    """The numId of every top-level body paragraph, '-' for a paragraph without list numbering."""
    num_ids = []
    for paragraph in package.body.findall(f"{{{W_NS}}}p"):
        found = _xpath(paragraph, "./w:pPr/w:numPr/w:numId/@w:val")
        num_ids.append(found[0] if found else "-")
    return num_ids


def test_replace_deletes_old_runs_and_inserts_text_with_copied_formatting(tmp_path):
    """The old text becomes w:delText; the new text is w:ins runs with the first run's rPr."""
    package = _package(tmp_path, para("Kalimat lama yang diganti.", rpr="<w:color w:val='000000'/>"))
    [outcome] = apply_edits(package, [_edit("E-1", "replace", "Kalimat lama yang diganti.", "Kalimat *baru*.")], AUTHOR, DATE)
    assert outcome.status == "applied"
    assert (outcome.inserted, outcome.deleted) == (1, 1)
    assert outcome.matched.text == "Kalimat lama yang diganti."
    assert _texts(package) == ["Kalimat baru."]
    assert _xpath(package.body, "./w:p/w:del/w:r/w:delText/text()") == ["Kalimat lama yang diganti."]
    assert _xpath(package.body, "./w:p/w:ins/w:r/w:rPr/w:color/@w:val") == ["000000", "000000", "000000"]


def test_replace_with_several_lines_adds_paragraphs_with_the_same_properties(tmp_path):
    """Lines after the first become tracked paragraphs right after, copying the paragraph's pPr."""
    package = _package(tmp_path, para("Butir lama.", ppr="<w:numPr><w:ilvl w:val='1'/><w:numId w:val='16'/></w:numPr>") + para("Sesudah."))
    apply_edits(package, [_edit("E-1", "replace", "Butir lama.", "Butir baru.", "Butir tambahan.")], AUTHOR, DATE)
    assert _texts(package) == ["Butir baru.", "Butir tambahan.", "Sesudah."]
    added = package.body.findall(f"{{{W_NS}}}p")[1]
    assert _xpath(added, "./w:pPr/w:numPr/w:numId/@w:val") == ["16"]
    assert _xpath(added, "./w:pPr/w:rPr/w:ins") != []


def test_inserts_around_one_anchor_keep_the_listed_order(tmp_path):
    """insert_before edits stack before the anchor and insert_after edits after it, in edit order."""
    package = _package(tmp_path, para("Jangkar.") + para("Akhir."))
    edits = [
        _edit("E-1", "insert_before", "Jangkar.", "Sebelum satu."),
        _edit("E-2", "insert_before", "Jangkar.", "Sebelum dua."),
        _edit("E-3", "insert_after", "Jangkar.", "Sesudah satu."),
        _edit("E-4", "insert_after", "Jangkar.", "Sesudah dua."),
    ]
    outcomes = apply_edits(package, edits, AUTHOR, DATE)
    assert [outcome.status for outcome in outcomes] == ["applied"] * 4
    assert _texts(package) == ["Sebelum satu.", "Sebelum dua.", "Jangkar.", "Sesudah satu.", "Sesudah dua.", "Akhir."]


def test_insert_with_style_uses_the_style_id(tmp_path):
    """Style names are resolved to style ids for the new paragraph."""
    package = _package(tmp_path, para("Jangkar.", ppr="<w:jc w:val='both'/>"))
    apply_edits(package, [_edit("E-1", "insert_after", "Jangkar.", "2.2.6 Metrik", style="Heading 3")], AUTHOR, DATE)
    new = package.body.findall(f"{{{W_NS}}}p")[1]
    assert _xpath(new, "./w:pPr/w:pStyle/@w:val") == ["Heading3"]
    assert _xpath(new, "./w:pPr/w:jc") == []


def test_style_neither_defined_nor_aliased_falls_back_to_the_anchor_with_a_warning(tmp_path):
    """An unknown style is never an error: the anchor's properties are copied and a warning is kept."""
    package = _package(tmp_path, para("Jangkar.", ppr="<w:jc w:val='center'/>"))
    [outcome] = apply_edits(package, [_edit("E-1", "insert_after", "Jangkar.", "Teks", style="Judul Lampiran")], AUTHOR, DATE)
    assert outcome.status == "applied"
    assert "'Judul Lampiran' is not defined" in outcome.warnings[0]
    new = package.body.findall(f"{{{W_NS}}}p")[1]
    assert _xpath(new, "./w:pPr/w:jc/@w:val") == ["center"]


def test_style_matching_the_anchor_style_copies_its_properties_without_numbering(tmp_path):
    """Same style as the anchor: its direct formatting is copied; numbering only for list items."""
    list_item = "<w:numPr><w:ilvl w:val='0'/><w:numId w:val='14'/></w:numPr><w:jc w:val='both'/>"
    package = _package(tmp_path, para("Butir daftar.", ppr=list_item))
    [outcome] = apply_edits(package, [_edit("E-1", "insert_before", "Butir daftar.", "Paragraf biasa.", style="Normal")], AUTHOR, DATE)
    assert outcome.warnings == []
    new = package.body.findall(f"{{{W_NS}}}p")[0]
    assert _xpath(new, "./w:pPr/w:jc/@w:val") == ["both"]
    assert _xpath(new, "./w:pPr/w:numPr") == []


def test_list_alias_keeps_the_numbering_of_a_list_item_anchor(tmp_path):
    """'List' resolves to Normal through the alias map and keeps the anchor's list numbering."""
    list_item = "<w:numPr><w:ilvl w:val='1'/><w:numId w:val='16'/></w:numPr>"
    package = _package(tmp_path, para("Butir satu.", ppr=list_item))
    [outcome] = apply_edits(package, [_edit("E-1", "insert_after", "Butir satu.", "Butir dua.", style="List")], AUTHOR, DATE)
    assert (outcome.status, outcome.warnings) == ("applied", [])
    new = package.body.findall(f"{{{W_NS}}}p")[1]
    assert _xpath(new, "./w:pPr/w:numPr/w:numId/@w:val") == ["16"]


def test_list_alias_next_to_a_non_list_paragraph_warns(tmp_path):
    """A list item asked for next to a heading cannot take numbering from it; that is a warning."""
    package = _package(tmp_path, para("3.3.5 Tahap Pengujian", ppr="<w:pStyle w:val='Heading3'/>"))
    [outcome] = apply_edits(package, [_edit("E-1", "insert_before", "3.3.5 Tahap Pengujian", "Butir.", style="List")], AUTHOR, DATE)
    assert outcome.status == "applied"
    assert "asks for a list item" in outcome.warnings[0]
    new = package.body.findall(f"{{{W_NS}}}p")[0]
    assert _xpath(new, "./w:pPr/w:pStyle/@w:val") == ["Normal"]


def test_style_on_replace_matching_the_paragraph_is_ignored(tmp_path):
    """A replace whose Style resolves to the paragraph's own list style changes nothing, writes nothing and warns nothing."""
    package = _package(tmp_path, para("Butir lama.", ppr="<w:numPr><w:ilvl w:val='0'/><w:numId w:val='2'/></w:numPr>"))
    [outcome] = apply_edits(package, [_edit("E-1", "replace", "Butir lama.", "Butir baru.", style="List")], AUTHOR, DATE)
    assert (outcome.status, outcome.warnings) == ("applied", [])
    assert outcome.property_change is None
    assert _xpath(package.body, ".//w:pPrChange") == []


def test_style_on_replace_that_differs_sets_the_style_drops_the_numbering_and_tracks_both(tmp_path):
    """II-7: a list item replaced with Style Heading 3 becomes a plain heading; the w:pPrChange keeps the list item."""
    package = _package(tmp_path, _item("Proof of Stake (PoS)", 12))
    edit = _edit("E-1", "replace", "Proof of Stake (PoS)", "2.2.2 Proof of Stake (PoS)", style="Heading 3")
    [outcome] = apply_edits(package, [edit], AUTHOR, DATE)
    assert (outcome.status, outcome.warnings) == ("applied", [])
    first = package.body.findall(f"{{{W_NS}}}p")[0]
    assert _tags(first.find(f"{{{W_NS}}}pPr")) == ["pStyle", "widowControl", "spacing", "jc", "pPrChange"]
    assert _xpath(first, "./w:pPr/w:pStyle/@w:val") == ["Heading3"]
    assert _xpath(first, "./w:pPr/w:pPrChange/w:pPr/w:pStyle") == []
    assert _xpath(first, "./w:pPr/w:pPrChange/w:pPr/w:numPr/w:numId/@w:val") == ["12"]
    change = outcome.property_change
    assert (change.old_style, change.old_num_id, change.new_style, change.new_num_id) == ("Normal", "12", "Heading3", None)


def test_style_normal_on_replace_of_a_list_item_drops_only_its_numbering(tmp_path):
    """II-7: Style Normal is already the paragraph's style, so only w:numPr goes, as a tracked change."""
    package = _package(tmp_path, _item("[11][12][13]Blockchain Blockchain adalah struktur data.", 12))
    edit = _edit("E-1", "replace", "[11][12][13]Blockchain Blockchain adalah struktur data.", "Blockchain adalah struktur data.", style="Normal")
    [outcome] = apply_edits(package, [edit], AUTHOR, DATE)
    assert (outcome.status, outcome.warnings) == ("applied", [])
    first = package.body.findall(f"{{{W_NS}}}p")[0]
    assert _tags(first.find(f"{{{W_NS}}}pPr")) == ["widowControl", "spacing", "jc", "pPrChange"]
    assert _xpath(first, "./w:pPr/w:pPrChange/w:pPr/w:numPr/w:numId/@w:val") == ["12"]
    change = outcome.property_change
    assert (change.old_style, change.old_num_id, change.new_style, change.new_num_id) == ("Normal", "12", "Normal", None)


def test_a_numbering_key_decides_the_list_of_a_replace_with_a_non_list_style(tmp_path):
    """With Numbering given, a Style that is not a list alias sets the style but leaves the list to the key."""
    package = _package(tmp_path, _item("Butir satu.", 16) + _item("Butir dua.", 12))
    edit = _edit("E-1", "replace", "Butir dua.", "Butir baru.", style="Heading 3", numbering=Numbering("continue", "Butir satu."))
    [outcome] = apply_edits(package, [edit], AUTHOR, DATE)
    assert (outcome.status, outcome.warnings) == ("applied", [])
    second = package.body.findall(f"{{{W_NS}}}p")[1]
    assert _xpath(second, "./w:pPr/w:pStyle/@w:val") == ["Heading3"]
    assert _xpath(second, "./w:pPr/w:numPr/w:numId/@w:val") == ["16"]
    assert len(_xpath(second, "./w:pPr/w:pPrChange")) == 1


def test_unknown_style_on_replace_changes_nothing_and_keeps_the_warning(tmp_path):
    """A Style neither defined nor aliased is ignored on replace, with the same warning as before."""
    package = _package(tmp_path, _item("Butir lama.", 12))
    [outcome] = apply_edits(package, [_edit("E-1", "replace", "Butir lama.", "Butir baru.", style="Judul Lampiran")], AUTHOR, DATE)
    assert outcome.status == "applied"
    assert outcome.warnings[0].endswith("; Style ignored, paragraph properties kept")
    assert outcome.property_change is None
    assert _xpath(package.body, ".//w:pPrChange") == []


def test_style_on_a_replace_of_an_inserted_paragraph_changes_it_without_a_property_change(tmp_path):
    """The paragraph is this run's insertion, so its new style needs no w:pPrChange."""
    package = _package(tmp_path, para("Jangkar."))
    edits = [_edit("E-1", "insert_after", "Jangkar.", "Sisipan."), _edit("E-2", "replace", "Sisipan.", "2.2.6 Metrik", style="Heading 3")]
    outcomes = apply_edits(package, edits, AUTHOR, DATE)
    assert [(outcome.status, outcome.warnings) for outcome in outcomes] == [("applied", []), ("applied", [])]
    assert _xpath(package.body, "./w:p[2]/w:pPr/w:pStyle/@w:val") == ["Heading3"]
    assert _xpath(package.body, ".//w:pPrChange") == []


def test_style_on_delete_changes_nothing_and_warns_when_it_differs(tmp_path):
    """A deleted paragraph keeps its properties; a Style that differs from its style is reported."""
    package = _package(tmp_path, _item("Hapus aku.", 12))
    [outcome] = apply_edits(package, [_edit("E-1", "delete", "Hapus aku.", style="Heading 3")], AUTHOR, DATE)
    assert outcome.status == "applied"
    assert "paragraph properties kept" in outcome.warnings[0]
    assert _xpath(package.body, ".//w:pPrChange") == []


def test_delete_marks_runs_and_the_paragraph_mark(tmp_path):
    """delete wraps the runs in w:del and records the paragraph mark as deleted."""
    package = _package(tmp_path, para("Hapus aku.") + para("Tetap."))
    [outcome] = apply_edits(package, [_edit("E-1", "delete", "Hapus aku.")], AUTHOR, DATE)
    assert (outcome.inserted, outcome.deleted) == (0, 2)
    first = package.body.findall(f"{{{W_NS}}}p")[0]
    assert _xpath(first, "./w:pPr/w:rPr/w:del") != []
    assert _xpath(first, "./w:del/w:r/w:delText/text()") == ["Hapus aku."]


def test_anchors_match_original_text_even_after_a_replace(tmp_path):
    """A later edit can anchor on a paragraph's original text after an earlier replace."""
    package = _package(tmp_path, para("Teks asli."))
    edits = [_edit("E-1", "replace", "Teks asli.", "Teks baru."), _edit("E-2", "insert_after", "Teks asli.", "Tambahan.")]
    outcomes = apply_edits(package, edits, AUTHOR, DATE)
    assert [outcome.status for outcome in outcomes] == ["applied", "applied"]
    assert _texts(package) == ["Teks baru.", "Tambahan."]


def test_changing_one_paragraph_twice_is_a_conflict(tmp_path):
    """A second replace or delete of the same paragraph is refused."""
    package = _package(tmp_path, para("Sekali saja."))
    edits = [_edit("E-1", "replace", "Sekali saja.", "Satu."), _edit("E-2", "delete", "Sekali saja.")]
    outcomes = apply_edits(package, edits, AUTHOR, DATE)
    assert outcomes[1].status == "error"
    assert outcomes[1].problems[0].kind == "conflict"
    assert "already changed by E-1" in outcomes[1].problems[0].message


def test_anchor_found_only_in_inserted_text_is_matched_and_flagged(tmp_path):
    """When the original has no match, paragraphs inserted earlier are searched and named."""
    package = _package(tmp_path, para("Jangkar."))
    edits = [_edit("E-1", "insert_after", "Jangkar.", "Paragraf sisipan."), _edit("E-2", "insert_after", "Paragraf sisipan.", "Lanjutan.")]
    outcomes = apply_edits(package, edits, AUTHOR, DATE)
    assert outcomes[1].status == "applied"
    assert outcomes[1].matched_inserted_by == "E-1"
    assert _texts(package) == ["Jangkar.", "Paragraf sisipan.", "Lanjutan."]


def test_a_failing_edit_does_not_stop_the_next_one(tmp_path):
    """Errors are reported per edit; the remaining edits still apply."""
    package = _package(tmp_path, para("Ada."))
    edits = [_edit("E-1", "delete", "Tidak ada."), _edit("E-2", "replace", "Ada.", "Masih ada.")]
    outcomes = apply_edits(package, edits, AUTHOR, DATE)
    assert [outcome.status for outcome in outcomes] == ["error", "applied"]
    assert outcomes[0].problems[0].kind == "match"


def test_table_text_is_inserted_as_a_tracked_table(tmp_path):
    """A Markdown table in Text becomes a w:tbl after the anchor, formatted like the first table."""
    package = _package(tmp_path, table([["A", "B"]]) + para("Tabel 3.2 Model Kegagalan"))
    edit = _edit("E-1", "insert_after", "Tabel 3.2 Model Kegagalan", "| No | Kelas |", "|---|---|", "| 1 | Crash |")
    [outcome] = apply_edits(package, [edit], AUTHOR, DATE)
    assert outcome.status == "applied"
    children = [etree.QName(child).localname for child in package.body]
    assert children == ["tbl", "p", "tbl", "sectPr"]
    assert _xpath(package.body, "./w:tbl[2]/w:tr/w:trPr/w:ins") != []


def test_replace_whose_text_starts_with_a_table_deletes_the_paragraph_and_puts_the_table_there(tmp_path):
    """The anchored paragraph is deleted (runs and mark); the table and further lines follow it."""
    package = _package(tmp_path, table([["A", "B"]]) + para("Jangkar.") + para("Akhir."))
    edit = _edit("E-1", "replace", "Jangkar.", "| No | Kelas |", "|---|---|", "| 1 | Crash |", "Catatan sesudah tabel.")
    [outcome] = apply_edits(package, [edit], AUTHOR, DATE)
    assert outcome.status == "applied"
    children = [etree.QName(child).localname for child in package.body]
    assert children == ["tbl", "p", "tbl", "p", "p", "sectPr"]
    deleted = package.body.findall(f"{{{W_NS}}}p")[0]
    assert _xpath(deleted, "./w:pPr/w:rPr/w:del") != []
    assert _texts(package) == ["", "Catatan sesudah tabel.", "Akhir."]


def test_new_tables_copy_the_nearest_preceding_table(tmp_path):
    """A new table takes its properties from the closest original table before it, else the first."""
    body = para("Awal.") + table([["A", "B"]], width=7500) + para("Tengah.") + table([["C", "D"]], width=6000) + para("Akhir.")
    package = _package(tmp_path, body)
    edits = [
        _edit("E-1", "insert_after", "Akhir.", "| X | Y |", "|---|---|"),
        _edit("E-2", "insert_after", "Awal.", "| X | Y |", "|---|---|"),
    ]
    apply_edits(package, edits, AUTHOR, DATE)
    widths = _xpath(package.body, "./w:tbl/w:tblPr/w:tblW/@w:w")
    assert widths == ["7500", "7500", "6000", "6000"]


def test_reported_counts_equal_the_revision_elements_in_the_document(tmp_path):
    """The per-edit insert and delete counts add up to the w:ins and w:del elements written."""
    package = _package(tmp_path, para(LONG) + para("Dua.") + para("Tiga."))
    edits = [
        _edit("E-1", "replace", LONG[:60], "Baru *satu*.", "Baru dua."),
        _edit("E-2", "delete", "Dua."),
        _edit("E-3", "insert_before", "Tiga.", "Sisipan."),
    ]
    outcomes = apply_edits(package, edits, AUTHOR, DATE)
    assert sum(outcome.inserted for outcome in outcomes) == len(_xpath(package.body, ".//w:ins"))
    assert sum(outcome.deleted for outcome in outcomes) == len(_xpath(package.body, ".//w:del"))


def test_run_edits_reports_malformed_blocks_next_to_applied_ones(tmp_path):
    """Parse problems become error outcomes in file order; good blocks still apply."""
    draft = tmp_path / "draft.md"
    draft.write_text(
        "### E-1 (closes: X-1)\nOperation: shuffle\nAnchor: Ada.\nRationale: r\n\n"
        "### E-2 (closes: X-1)\nOperation: replace\nAnchor: Ada.\nText:\nBaru.\nRationale: r\n",
        encoding="utf-8",
    )
    package = _package(tmp_path, para("Ada."))
    outcomes = run_edits(package, [draft], AUTHOR, DATE)
    assert [(outcome.edit_id, outcome.status) for outcome in outcomes] == [("E-1", "error"), ("E-2", "applied")]
    assert outcomes[0].problems[0].kind == "parse"
    assert "unknown Operation 'shuffle'" in outcomes[0].problems[0].message


TABLES = (
    para("Tabel 3.1 Parameter")
    + table([["Parameter", "Nilai Default", "Keterangan"], ["health.interval", "5 detik", "x"]], width=6000)
    + para("Tabel 4.2 Konfigurasi")
    + table([["Parameter", "Nilai", "Keterangan"], ["health.interval", "10 detik", "y"]], width=7200)
)


def test_row_delete_marks_the_row_deleted(tmp_path):
    """ROW: delete is a tracked deletion of that row only."""
    package = _package(tmp_path, TABLES)
    edit = _edit("E-1", "delete", "ROW: health.interval")
    edit = replace(edit, table="Parameter | Nilai | Keterangan")
    [outcome] = apply_edits(package, [edit], AUTHOR, DATE)
    assert outcome.status == "applied"
    assert outcome.matched.text == "health.interval"
    second_table = package.body.findall(f"{{{W_NS}}}tbl")[1]
    assert len(_xpath(second_table, "./w:tr/w:trPr/w:del")) == 1
    assert _xpath(second_table, "./w:tr[2]/w:tc/w:p/w:del/w:r/w:delText/text()") == ["health.interval", "10 detik", "y"]


def test_row_replace_and_row_inserts(tmp_path):
    """ROW: replace rewrites the cells; insert_after and insert_before add tracked rows in order."""
    package = _package(tmp_path, TABLES)
    edits = [
        _edit("E-1", "replace", "ROW: health.interval | 5 detik | x", "| health.interval | 5 detik | dasar baru |"),
        _edit("E-2", "insert_after", "ROW: health.interval | 5 detik | x", "| health.timeout | 5 detik | t |", "| min_peers | 3 | p |"),
        _edit("E-3", "insert_before", "ROW: Parameter | Nilai Default | Keterangan", "| Judul | Atas | Tabel |"),
    ]
    outcomes = apply_edits(package, edits, AUTHOR, DATE)
    assert [outcome.status for outcome in outcomes] == ["applied"] * 3
    first_table = package.body.findall(f"{{{W_NS}}}tbl")[0]
    rows = [" | ".join(_xpath(row, "./w:tc/w:p/w:ins/w:r/w:t/text()")) for row in first_table.findall(f"{{{W_NS}}}tr")]
    assert rows == ["Judul | Atas | Tabel", "", "health.interval | 5 detik | dasar baru", "health.timeout | 5 detik | t", "min_peers | 3 | p"]
    assert len(_xpath(first_table, "./w:tr/w:trPr/w:ins")) == 3


def test_row_text_with_the_wrong_cell_count_is_an_error(tmp_path):
    """The Text row must match the anchored row's cell count."""
    package = _package(tmp_path, TABLES)
    edit = _edit("E-1", "insert_after", "ROW: health.interval | 5 detik | x", "| satu | dua |")
    [outcome] = apply_edits(package, [edit], AUTHOR, DATE)
    assert [problem.kind for problem in outcome.problems] == ["table"]
    assert "the row has 3 cells, the Text row has 2" in outcome.problems[0].message


def test_table_delete_and_table_replace(tmp_path):
    """TABLE: delete deletes every row; TABLE: replace adds the new table after the old one, formatted like it."""
    package = _package(tmp_path, TABLES + para("Akhir."))
    edits = [
        _edit("E-1", "delete", "TABLE: Parameter | Nilai | Keterangan"),
        _edit("E-2", "replace", "TABLE: Parameter | Nilai Default | Keterangan", "| Parameter | Nilai | Dasar | Bukti |", "|---|---|---|---|", "| a | b | c | d |", style="Table"),
    ]
    outcomes = apply_edits(package, edits, AUTHOR, DATE)
    assert [outcome.status for outcome in outcomes] == ["applied", "applied"]
    assert outcomes[1].warnings == ["Style is not used for ROW: and TABLE: edits"]
    tables = package.body.findall(f"{{{W_NS}}}tbl")
    assert len(tables) == 3
    assert len(_xpath(tables[0], "./w:tr/w:trPr/w:del")) == 2
    assert _xpath(tables[1], "./w:tblPr/w:tblW/@w:w") == ["6000"]
    widths = [int(width) for width in _xpath(tables[1], "./w:tblGrid/w:gridCol/@w:w")]
    assert len(widths) == 4 and sum(widths) == 6000
    assert len(_xpath(tables[2], "./w:tr/w:trPr/w:del")) == 2


def test_ambiguous_table_anchor_and_insert_on_a_table_anchor_are_errors(tmp_path):
    """A first cell shared by two tables is ambiguous; inserts are not defined for TABLE: anchors."""
    package = _package(tmp_path, TABLES)
    edits = [
        _edit("E-1", "delete", "TABLE: Parameter"),
        _edit("E-2", "insert_after", "TABLE: Parameter | Nilai | Keterangan", "Teks."),
    ]
    outcomes = apply_edits(package, edits, AUTHOR, DATE)
    assert outcomes[0].problems[0].kind == "match"
    assert "matches 2 tables" in outcomes[0].problems[0].message
    assert outcomes[1].problems[0].kind == "rule"


def test_table_text_in_a_document_without_tables_is_an_error(tmp_path):
    """New tables copy an existing table's properties; with no table at all the edit is refused."""
    package = _package(tmp_path, para("Jangkar."))
    [outcome] = apply_edits(package, [_edit("E-1", "insert_after", "Jangkar.", "| A | B |", "|---|---|")], AUTHOR, DATE)
    assert [problem.kind for problem in outcome.problems] == ["table"]
    assert "no existing table" in outcome.problems[0].message


def test_replace_of_text_an_earlier_replace_inserted_swaps_that_insertion(tmp_path):
    """A second replace anchored on the first replace's new text replaces the inserted runs; the original stays deleted."""
    package = _package(tmp_path, para("Teks asli."))
    edits = [_edit("E-1", "replace", "Teks asli.", "Teks baru."), _edit("E-2", "replace", "Teks baru.", "Teks *terbaru*.")]
    outcomes = apply_edits(package, edits, AUTHOR, DATE)
    assert [outcome.status for outcome in outcomes] == ["applied", "applied"]
    assert outcomes[1].matched_inserted_by == "E-1"
    assert _texts(package) == ["Teks terbaru."]
    paragraph = package.body.findall(f"{{{W_NS}}}p")[0]
    assert _xpath(paragraph, "./w:del/w:r/w:delText/text()") == ["Teks asli."]
    assert len(_xpath(paragraph, "./w:ins")) == 1
    assert sum(outcome.inserted for outcome in outcomes) == len(_xpath(package.body, ".//w:ins"))
    assert sum(outcome.deleted for outcome in outcomes) == len(_xpath(package.body, ".//w:del"))


def test_insert_after_text_an_earlier_replace_inserted(tmp_path):
    """Replacement text is searchable for later anchors, like new paragraphs."""
    package = _package(tmp_path, para("Teks asli."))
    edits = [_edit("E-1", "replace", "Teks asli.", "Teks baru."), _edit("E-2", "insert_after", "Teks baru.", "Sesudahnya.")]
    outcomes = apply_edits(package, edits, AUTHOR, DATE)
    assert outcomes[1].matched_inserted_by == "E-1"
    assert _texts(package) == ["Teks baru.", "Sesudahnya."]


def test_replace_of_an_inserted_paragraph_keeps_its_inserted_mark(tmp_path):
    """Replacing a paragraph an insert created swaps its runs; the paragraph mark stays a tracked insertion."""
    package = _package(tmp_path, para("Jangkar."))
    edits = [_edit("E-1", "insert_after", "Jangkar.", "Sisipan."), _edit("E-2", "replace", "Sisipan.", "Sisipan baru.")]
    outcomes = apply_edits(package, edits, AUTHOR, DATE)
    assert [outcome.status for outcome in outcomes] == ["applied", "applied"]
    assert _texts(package) == ["Jangkar.", "Sisipan baru."]
    new = package.body.findall(f"{{{W_NS}}}p")[1]
    assert _xpath(new, "./w:pPr/w:rPr/w:ins") != []
    assert _xpath(new, ".//w:del") == []


def test_delete_of_inserted_text_is_still_refused(tmp_path):
    """Deleting text inserted earlier in the same run is not defined."""
    package = _package(tmp_path, para("Jangkar."))
    edits = [_edit("E-1", "insert_after", "Jangkar.", "Sisipan."), _edit("E-2", "delete", "Sisipan.")]
    outcomes = apply_edits(package, edits, AUTHOR, DATE)
    assert outcomes[1].problems[0].kind == "conflict"


WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
IMAGE_PARAGRAPH = (
    '<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:rPr><w:noProof/></w:rPr><w:drawing>'
    f'<wp:inline xmlns:wp="{WP}"><wp:extent cx="1" cy="1"/><wp:docPr id="3" name="old.png"/></wp:inline></w:drawing></w:r></w:p>'
)
CAPTION = "Gambar 4.2 Produksi blok selama failover otomatis"


def test_replace_image_deletes_the_picture_above_the_caption_and_inserts_the_token(tmp_path):
    """The drawing run sits in w:del; the token is a tracked insertion in the same paragraph; the caption stays."""
    package = _package(tmp_path, para("Pengantar.") + IMAGE_PARAGRAPH + para(CAPTION))
    [outcome] = apply_edits(package, [_edit("E-1", "replace_image", CAPTION, "{{FIGURE:S4.fig}}")], AUTHOR, DATE)
    assert outcome.status == "applied" and (outcome.inserted, outcome.deleted) == (1, 1)
    image = package.body.findall(f"{{{W_NS}}}p")[1]
    assert _xpath(image, "./w:del/w:r/w:drawing") != []
    assert _xpath(image, "./w:ins/w:r/w:t/text()") == ["{{FIGURE:S4.fig}}"]
    assert _xpath(image, "./w:pPr/w:jc/@w:val") == ["center"]
    assert _texts(package) == ["Pengantar.", "{{FIGURE:S4.fig}}", CAPTION]


def test_replace_image_accepts_one_empty_paragraph_between_picture_and_caption(tmp_path):
    """A single empty paragraph between the image paragraph and its caption is skipped."""
    package = _package(tmp_path, IMAGE_PARAGRAPH + para("") + para(CAPTION))
    [outcome] = apply_edits(package, [_edit("E-1", "replace_image", CAPTION, "{{FIGURE:a}}")], AUTHOR, DATE)
    assert outcome.status == "applied"
    assert _xpath(package.body, "./w:p/w:del/w:r/w:drawing") != []


def test_replace_image_fails_clearly_when_no_image_paragraph_is_above(tmp_path):
    """Two empty paragraphs, or text, between caption and picture means no image paragraph is found."""
    package = _package(tmp_path, IMAGE_PARAGRAPH + para("") + para("") + para(CAPTION))
    [outcome] = apply_edits(package, [_edit("E-1", "replace_image", CAPTION, "{{FIGURE:a}}")], AUTHOR, DATE)
    assert outcome.status == "error"
    assert outcome.problems[0].kind == "figure"
    assert "no image paragraph directly above the caption" in outcome.problems[0].message
    assert _xpath(package.body, "./w:p/w:del") == []


def test_replace_image_twice_on_one_picture_is_a_conflict(tmp_path):
    """The second replace_image of the same image paragraph is refused."""
    package = _package(tmp_path, IMAGE_PARAGRAPH + para(CAPTION))
    edits = [_edit("E-1", "replace_image", CAPTION, "{{FIGURE:a}}"), _edit("E-2", "replace_image", CAPTION, "{{FIGURE:b}}")]
    first, second = apply_edits(package, edits, AUTHOR, DATE)
    assert (first.status, second.status) == ("applied", "error")
    assert second.problems[0].message == "already changed by E-1"


def test_replace_image_warns_when_text_is_not_a_figure_token(tmp_path):
    """Text other than {{FIGURE:name}} is applied but warned about, since fill would not place a picture."""
    package = _package(tmp_path, IMAGE_PARAGRAPH + para(CAPTION))
    [outcome] = apply_edits(package, [_edit("E-1", "replace_image", CAPTION, "gambar baru")], AUTHOR, DATE)
    assert outcome.status == "applied"
    assert "not a {{FIGURE:name}} token" in outcome.warnings[0]


def test_style_figure_gives_an_inserted_paragraph_the_manuscript_figure_layout(tmp_path):
    """Style: Figure builds a Normal, centred, single-spaced paragraph kept with the caption below it."""
    package = _package(tmp_path, para("Jangkar.", ppr='<w:ind w:firstLine="720"/><w:jc w:val="both"/>'))
    [outcome] = apply_edits(package, [_edit("E-1", "insert_after", "Jangkar.", "{{FIGURE:S4.fig}}", style="Figure")], AUTHOR, DATE)
    assert outcome.status == "applied" and outcome.warnings == []
    new = package.body.findall(f"{{{W_NS}}}p")[1]
    properties = _xpath(new, "./w:pPr/*")
    assert [etree.QName(child).localname for child in properties] == ["pStyle", "keepNext", "spacing", "jc", "rPr"]
    assert _xpath(new, "./w:pPr/w:pStyle/@w:val") == ["Normal"]
    assert _xpath(new, "./w:pPr/w:jc/@w:val") == ["center"]
    assert _xpath(new, "./w:pPr/w:spacing/@w:line") == ["240"]
    assert _xpath(new, "./w:pPr/w:ind") == []


WIDE_ROW = "| failover.state_sync_interval | 30 detik ke masa lalu dan ke masa depan | Disamakan dengan health.interval pada kondisi normal | Tabel 4.x pengujian keamanan negatif |"


def test_a_built_table_grows_to_the_text_column_so_identifiers_stay_whole(tmp_path):
    """TABLE: replace of a narrow reference table with long dotted identifiers widens the new table to the text column."""
    package = _package(tmp_path, table([["Parameter", "Nilai", "Keterangan"], ["a", "b", "c"]], width=6000) + para("Akhir."))
    edits = [_edit("E-1", "replace", "TABLE: Parameter | Nilai | Keterangan", "| Parameter | Nilai | Dasar | Bukti |", "|---|---|---|---|", WIDE_ROW)]
    [outcome] = apply_edits(package, edits, AUTHOR, DATE)
    assert outcome.status == "applied"
    new = package.body.findall(f"{{{W_NS}}}tbl")[1]
    widths = [int(width) for width in _xpath(new, "./w:tblGrid/w:gridCol/@w:w")]
    assert sum(widths) == 11909 - 2268 - 1701
    assert widths[0] > 6000 // 4


def test_an_inserted_table_in_text_flow_is_sized_from_its_section(tmp_path):
    """A table inside an insert_after Text sizes itself from the anchor's section as well."""
    package = _package(tmp_path, table([["A", "B"], ["1", "2"]], width=6000) + para("Jangkar."))
    edits = [_edit("E-1", "insert_after", "Jangkar.", "| Parameter | Nilai | Dasar | Bukti |", "|---|---|---|---|", WIDE_ROW)]
    apply_edits(package, edits, AUTHOR, DATE)
    new = package.body.findall(f"{{{W_NS}}}tbl")[1]
    assert sum(int(width) for width in _xpath(new, "./w:tblGrid/w:gridCol/@w:w")) == 11909 - 2268 - 1701


def test_a_table_that_cannot_fit_is_reported_as_a_warning_of_its_edit(tmp_path):
    """The review note of the table builder becomes a warning on the edit that inserted the table."""
    row = "| " + " | ".join(["failover.state_sync_interval"] * 4) + " |"
    package = _package(tmp_path, table([["A", "B"], ["1", "2"]], width=6000) + para("Jangkar."))
    [outcome] = apply_edits(package, [_edit("E-1", "insert_after", "Jangkar.", "| A | B | C | D |", "|---|---|---|---|", row)], AUTHOR, DATE)
    assert outcome.status == "applied"
    assert len(outcome.warnings) == 1 and outcome.warnings[0].startswith("table needs review")


def test_a_body_font_other_than_times_new_roman_is_warned_about_for_each_built_table(tmp_path):
    """Widths are estimated with Times New Roman metrics, so another body font earns a warning."""
    from docx_builder import DEFAULT_STYLES

    styles = DEFAULT_STYLES.replace("<w:styles ", "<w:styles ", 1).replace(
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>',
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:rPr><w:rFonts w:ascii="Calibri"/></w:rPr></w:style>',
    )
    package = WordPackage.load(build_docx(tmp_path / "in.docx", table([["A", "B"], ["1", "2"]], width=6000) + para("Jangkar."), styles_xml=styles))
    [outcome] = apply_edits(package, [_edit("E-1", "insert_after", "Jangkar.", "| A | B |", "|---|---|", "| 1 | 2 |")], AUTHOR, DATE)
    assert outcome.warnings == ["table widths are estimated with Times New Roman metrics, but the body font is Calibri"]


NUMBERING = (
    f'<w:numbering xmlns:w="{W_NS}">'
    '<w:abstractNum w:abstractNumId="2"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl></w:abstractNum>'
    '<w:abstractNum w:abstractNumId="14"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="-"/></w:lvl></w:abstractNum>'
    '<w:num w:numId="2"><w:abstractNumId w:val="2"/></w:num>'
    '<w:num w:numId="9"><w:abstractNumId w:val="14"/></w:num>'
    "</w:numbering>"
)
RM_1 = "Bagaimana rancangan mekanisme deteksi kegagalan?"
TUJUAN_1 = "Merancang mekanisme deteksi kegagalan."
TUJUAN_2 = "Merancang pencegahan double-signing."


def test_numbering_continue_gives_every_inserted_paragraph_the_named_items_list(tmp_path):
    """'continue <prefix>' copies the named item's numId and level into each new paragraph; the List warning goes."""
    heading = para("3.3.5 Tahap Pengujian dan Analisis", ppr="<w:pStyle w:val='Heading3'/>")
    package = _package(tmp_path, _item("Butir satu.", 16, level=1) + heading)
    numbering = Numbering("continue", "Butir satu.")
    edit = _edit("E-1", "insert_before", "3.3.5 Tahap Pengujian dan Analisis", "Butir dua.", "Butir tiga.", style="List", numbering=numbering)
    [outcome] = apply_edits(package, [edit], AUTHOR, DATE)
    assert (outcome.status, outcome.warnings) == ("applied", [])
    new = package.body.findall(f"{{{W_NS}}}p")[1:3]
    assert [_xpath(p, "./w:pPr/w:numPr/w:ilvl/@w:val | ./w:pPr/w:numPr/w:numId/@w:val") for p in new] == [["1", "16"], ["1", "16"]]
    assert [_tags(p.find(f"{{{W_NS}}}pPr")) for p in new] == [["pStyle", "numPr", "rPr"], ["pStyle", "numPr", "rPr"]]


def test_a_numbering_prefix_that_matches_no_paragraph_or_several_is_a_match_problem(tmp_path):
    """The prefix follows the anchor rules; a failure is reported like an anchor failure and nothing is inserted."""
    package = _package(tmp_path, _item("Butir.", 16) + _item("Butir.", 16) + para("Jangkar."))
    edits = [
        _edit("E-1", "insert_after", "Jangkar.", "Baru.", numbering=Numbering("continue", "Tidak ada.")),
        _edit("E-2", "insert_after", "Jangkar.", "Baru.", numbering=Numbering("new", "Butir.")),
    ]
    first, second = apply_edits(package, edits, AUTHOR, DATE)
    assert [problem.kind for problem in first.problems + second.problems] == ["match", "match"]
    assert first.problems[0].message == "Numbering prefix 'Tidak ada.': no paragraph matches the anchor"
    assert second.problems[0].message == "Numbering prefix 'Butir.': the prefix matches 2 paragraphs; lengthen it to pick one"
    assert len(second.problems[0].candidates) == 2
    assert _texts(package) == ["Butir.", "Butir.", "Jangkar."]


def test_a_numbering_prefix_can_name_a_paragraph_an_earlier_edit_inserted(tmp_path):
    """Like an anchor, a prefix with no match among the original paragraphs is looked up in inserted ones."""
    package = _package(tmp_path, _item("Butir satu.", 16) + para("Jangkar."))
    edits = [
        _edit("E-1", "insert_after", "Jangkar.", "Butir dua.", numbering=Numbering("continue", "Butir satu.")),
        _edit("E-2", "insert_after", "Butir dua.", "Butir tiga.", numbering=Numbering("continue", "Butir dua.")),
    ]
    outcomes = apply_edits(package, edits, AUTHOR, DATE)
    assert [outcome.status for outcome in outcomes] == ["applied", "applied"]
    assert _texts(package) == ["Butir satu.", "Jangkar.", "Butir dua.", "Butir tiga."]
    assert _num_ids(package) == ["16", "-", "16", "16"]


def test_numbering_that_names_a_paragraph_without_list_numbering_is_an_error(tmp_path):
    """continue and new need a numbered list item to copy from; the edit then changes nothing."""
    package = _package(tmp_path, para("Paragraf biasa.") + para("Jangkar."), numbering=NUMBERING)
    edits = [
        _edit("E-1", "insert_after", "Jangkar.", "Baru.", numbering=Numbering("continue", "Paragraf biasa.")),
        _edit("E-2", "replace", "Jangkar.", "Baru.", numbering=Numbering("new", "Paragraf biasa.")),
    ]
    outcomes = apply_edits(package, edits, AUTHOR, DATE)
    assert [problem.kind for outcome in outcomes for problem in outcome.problems] == ["numbering", "numbering"]
    assert outcomes[0].problems[0].message == "Numbering prefix 'Paragraf biasa.' names a paragraph that is not a numbered list item"
    assert _texts(package) == ["Paragraf biasa.", "Jangkar."]
    assert _xpath(package.body, ".//w:ins | .//w:del") == []


def test_numbering_new_adds_one_num_like_the_named_list_that_restarts_at_1(tmp_path):
    """'new <prefix>' appends a w:num on the named list's abstract numbering with startOverride 1; every new paragraph uses it."""
    package = _package(tmp_path, _item(RM_1, 2) + para("Jangkar."), numbering=NUMBERING)
    [outcome] = apply_edits(package, [_edit("E-1", "insert_after", "Jangkar.", "Satu.", "Dua.", numbering=Numbering("new", RM_1))], AUTHOR, DATE)
    assert outcome.status == "applied"
    nums = package.read_part("word/numbering.xml").findall(f"{{{W_NS}}}num")
    assert [num.get(f"{{{W_NS}}}numId") for num in nums] == ["2", "9", "10"]
    assert _xpath(nums[2], "./w:abstractNumId/@w:val") == ["2"]
    assert _xpath(nums[2], "./w:lvlOverride[@w:ilvl='0']/w:startOverride/@w:val") == ["1"]
    assert _num_ids(package) == ["2", "-", "10", "10"]


def test_replace_with_numbering_tracks_the_old_properties_and_extra_lines_join_the_new_list(tmp_path):
    """I-3: the objectives leave their bullet list for a new decimal list; each w:pPrChange keeps the old numId."""
    package = _package(tmp_path, _item(RM_1, 2) + _item(TUJUAN_1, 9) + _item(TUJUAN_2, 9), numbering=NUMBERING)
    edits = [
        _edit("E-1", "replace", TUJUAN_1, "Tujuan satu.", "Tujuan sisipan.", numbering=Numbering("new", RM_1)),
        _edit("E-2", "replace", TUJUAN_2, "Tujuan dua.", numbering=Numbering("continue", "Tujuan satu.")),
    ]
    outcomes = apply_edits(package, edits, AUTHOR, DATE)
    assert [outcome.status for outcome in outcomes] == ["applied", "applied"]
    assert _texts(package) == [RM_1, "Tujuan satu.", "Tujuan sisipan.", "Tujuan dua."]
    assert _num_ids(package) == ["2", "10", "10", "10"]
    first, extra, second = package.body.findall(f"{{{W_NS}}}p")[1:4]
    assert _tags(first.find(f"{{{W_NS}}}pPr")) == ["widowControl", "numPr", "spacing", "jc", "pPrChange"]
    assert _xpath(first, "./w:pPr/w:pPrChange/@w:author") == [AUTHOR]
    assert _xpath(first, "./w:pPr/w:pPrChange/w:pPr/w:numPr/w:numId/@w:val") == ["9"]
    assert _xpath(second, "./w:pPr/w:pPrChange/w:pPr/w:numPr/w:numId/@w:val") == ["9"]
    assert _xpath(extra, "./w:pPr/w:pPrChange") == []


def test_numbering_none_takes_inserts_and_a_replaced_paragraph_out_of_the_list(tmp_path):
    """'none' removes w:numPr; on a replace the removal is tracked."""
    package = _package(tmp_path, _item("Butir satu.", 16) + _item("Butir dua.", 16))
    edits = [
        _edit("E-1", "insert_after", "Butir satu.", "Catatan.", style="List", numbering=Numbering("none")),
        _edit("E-2", "replace", "Butir dua.", "Bukan butir.", numbering=Numbering("none")),
    ]
    outcomes = apply_edits(package, edits, AUTHOR, DATE)
    assert [(outcome.status, outcome.warnings) for outcome in outcomes] == [("applied", []), ("applied", [])]
    assert _num_ids(package) == ["16", "-", "-"]
    assert _xpath(package.body, "./w:p[3]/w:pPr/w:pPrChange/w:pPr/w:numPr/w:numId/@w:val") == ["16"]


def test_numbering_that_leaves_a_replaced_paragraph_as_it_was_writes_no_property_change(tmp_path):
    """continue on an item of the same list changes nothing, so no w:pPrChange is written."""
    package = _package(tmp_path, _item("Butir satu.", 16) + _item("Butir dua.", 16))
    edit = _edit("E-1", "replace", "Butir dua.", "Butir baru.", numbering=Numbering("continue", "Butir satu."))
    [outcome] = apply_edits(package, [edit], AUTHOR, DATE)
    assert outcome.status == "applied"
    assert _xpath(package.body, ".//w:pPrChange") == []


def test_numbering_new_needs_the_named_lists_num_definition(tmp_path):
    """The new w:num copies the named list's abstract numbering, so that list's w:num must exist."""
    body = _item("Butir.", 99) + para("Jangkar.")
    edit = _edit("E-1", "insert_after", "Jangkar.", "Baru.", numbering=Numbering("new", "Butir."))
    for numbering in (NUMBERING, None):
        [outcome] = apply_edits(_package(tmp_path, body, numbering=numbering), [edit], AUTHOR, DATE)
        assert [problem.kind for problem in outcome.problems] == ["numbering"]
        assert outcome.problems[0].message == "Numbering prefix 'Butir.': numId 99 has no w:num in word/numbering.xml"


def test_a_property_change_on_a_paragraph_that_already_holds_one_is_a_conflict(tmp_path):
    """A w:pPr holds one w:pPrChange, so a replace that would need a second is refused whole."""
    earlier = "<w:numPr><w:ilvl w:val='0'/><w:numId w:val='16'/></w:numPr><w:pPrChange w:id='4' w:author='B'><w:pPr/></w:pPrChange>"
    package = _package(tmp_path, para("Butir lama.", ppr=earlier))
    [outcome] = apply_edits(package, [_edit("E-1", "replace", "Butir lama.", "Butir baru.", numbering=Numbering("none"))], AUTHOR, DATE)
    assert [problem.kind for problem in outcome.problems] == ["conflict"]
    assert "already holds a tracked paragraph property change" in outcome.problems[0].message
    assert _texts(package) == ["Butir lama."]


def test_numbering_on_a_replace_of_an_inserted_paragraph_changes_it_directly(tmp_path):
    """A paragraph this run inserted is new text: its w:numPr changes without a w:pPrChange."""
    package = _package(tmp_path, _item("Butir satu.", 16) + para("Jangkar."))
    edits = [
        _edit("E-1", "insert_after", "Jangkar.", "Sisipan."),
        _edit("E-2", "replace", "Sisipan.", "Butir dua.", numbering=Numbering("continue", "Butir satu.")),
    ]
    outcomes = apply_edits(package, edits, AUTHOR, DATE)
    assert [outcome.status for outcome in outcomes] == ["applied", "applied"]
    assert _num_ids(package) == ["16", "-", "16"]
    assert _xpath(package.body, ".//w:pPrChange") == []
