"""Tests for filling {{DATA:name}} tokens inside tracked insertions."""

import json

import pytest
from lxml import etree

from docx_builder import W_NS
from docx_builder import para, table
from redline.fill import FillError, fill_table_tokens, fill_tokens, load_table_file, load_values, load_values_source
from redline.tracked import RevisionMarks

NSMAP = {"w": W_NS}


def _document() -> etree._Element:
    """A document with tokens inside two insertions and one token in untracked text."""
    return etree.fromstring(
        f'<w:document xmlns:w="{W_NS}"><w:body>'
        '<w:p><w:ins w:id="1" w:author="a"><w:r><w:t xml:space="preserve">MTTD {{DATA:S4.ttd.mean}} detik, '
        "n = {{DATA:S4.n}}</w:t></w:r></w:ins></w:p>"
        '<w:p><w:ins w:id="2" w:author="a"><w:r><w:t xml:space="preserve">{{DATA:S4.n}} siklus, '
        "{{DATA:soak.availability}} persen</w:t></w:r></w:ins></w:p>"
        "<w:p><w:r><w:t>Lama {{DATA:S4.n}}</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )


def _texts(document: etree._Element) -> list[str]:
    """Text of every w:t in document order."""
    return document.xpath("//w:t/text()", namespaces=NSMAP)


def test_tokens_inside_insertions_are_replaced_and_counted():
    """Every occurrence with a value is replaced; counts are per token name."""
    document = _document()
    result = fill_tokens(document, {"S4.ttd.mean": "12,4", "S4.n": "10", "unused.key": "1"})
    assert _texts(document) == [
        "MTTD 12,4 detik, n = 10",
        "10 siklus, {{DATA:soak.availability}} persen",
        "Lama {{DATA:S4.n}}",
    ]
    assert result.filled == {"S4.ttd.mean": 1, "S4.n": 2}
    assert result.missing_values == {"soak.availability": 1}
    assert result.unused_values == ["unused.key"]
    assert result.outside_insertions == {"S4.n": 1}


def test_values_file_must_map_names_to_strings_or_numbers(tmp_path):
    """Numbers become text as written; other value types are refused."""
    good = tmp_path / "values.json"
    good.write_text(json.dumps({"S4.n": 10, "S4.ttd.mean": "12,4"}), encoding="utf-8")
    assert load_values(good) == {"S4.n": "10", "S4.ttd.mean": "12,4"}

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"S4.n": [10]}), encoding="utf-8")
    with pytest.raises(FillError, match="value of 'S4.n' must be a string or a number"):
        load_values(bad)

    not_object = tmp_path / "list.json"
    not_object.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(FillError, match="must hold one JSON object"):
        load_values(not_object)


INSERTED_TOKEN = (
    '<w:p><w:pPr><w:rPr><w:ins w:id="5" w:author="A" w:date="2026-10-05T08:00:00Z"/></w:rPr></w:pPr>'
    '<w:ins w:id="6" w:author="A" w:date="2026-10-05T08:00:00Z"><w:r><w:t>{{TABLE:hasil}}</w:t></w:r></w:ins></w:p>'
)
HASIL = "| Iterasi | TTD (detik) |\n|---|---|\n| 1 | 12,4 |\n"


def _doc(body: str) -> etree._Element:
    """A w:document holding body XML."""
    return etree.fromstring(f'<w:document xmlns:w="{W_NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>{body}</w:body></w:document>')


def _marks() -> RevisionMarks:
    """Revision marks for table insertion."""
    return RevisionMarks("Syahid Nurrohim", "2026-10-05T08:00:00Z", 100)


def test_values_directory_gives_values_json_and_table_files(tmp_path):
    """A values directory holds an optional values.json and one <name>.md per table token."""
    (tmp_path / "values.json").write_text(json.dumps({"S4.n": 10}), encoding="utf-8")
    (tmp_path / "hasil.md").write_text(HASIL, encoding="utf-8")
    values, tables = load_values_source(tmp_path)
    assert values == {"S4.n": "10"}
    assert tables == {"hasil": tmp_path / "hasil.md"}
    assert load_values_source(tmp_path / "values.json") == ({"S4.n": "10"}, {})


def test_table_file_must_hold_exactly_one_markdown_table(tmp_path):
    """Anything besides one table (blank lines aside) is refused."""
    good = tmp_path / "hasil.md"
    good.write_text("\n" + HASIL + "\n", encoding="utf-8")
    assert len(load_table_file(good).rows) == 2
    bad = tmp_path / "bad.md"
    bad.write_text("Tabel 4.5\n" + HASIL, encoding="utf-8")
    with pytest.raises(FillError, match="must hold exactly one Markdown table"):
        load_table_file(bad)


def test_inserted_token_paragraph_becomes_a_tracked_table_in_its_place(tmp_path):
    """A token paragraph that an edit inserted is swapped for the tracked table, formatted like the nearest table before it."""
    (tmp_path / "hasil.md").write_text(HASIL, encoding="utf-8")
    document = _doc(table([["A", "B"]], width=6000) + para("Sebelum.") + INSERTED_TOKEN + para("Sesudah."))
    result = fill_table_tokens(document, {"hasil": tmp_path / "hasil.md"}, _marks())
    body = document.find(f"{{{W_NS}}}body")
    assert [etree.QName(child).localname for child in body] == ["tbl", "p", "tbl", "p"]
    new_table = body.findall(f"{{{W_NS}}}tbl")[1]
    assert new_table.xpath("./w:tblPr/w:tblW/@w:w", namespaces=NSMAP) == ["6000"]
    assert new_table.xpath(".//w:ins/w:r/w:t/text()", namespaces=NSMAP) == ["Iterasi", "TTD (detik)", "1", "12,4"]
    assert result.tables_filled == ["hasil"]


def test_token_paragraph_in_original_text_is_deleted_and_the_table_follows(tmp_path):
    """A token in untracked text is removed as a tracked deletion; the table goes after it."""
    (tmp_path / "hasil.md").write_text(HASIL, encoding="utf-8")
    document = _doc(table([["A", "B"]]) + para("{{TABLE:hasil}}"))
    fill_table_tokens(document, {"hasil": tmp_path / "hasil.md"}, _marks())
    body = document.find(f"{{{W_NS}}}body")
    assert [etree.QName(child).localname for child in body] == ["tbl", "p", "tbl"]
    assert body.xpath("./w:p/w:pPr/w:rPr/w:del", namespaces=NSMAP) != []
    assert body.xpath("./w:p/w:del/w:r/w:delText/text()", namespaces=NSMAP) == ["{{TABLE:hasil}}"]


def test_table_tokens_without_files_and_files_without_tokens_are_reported(tmp_path):
    """Unfilled tokens and unused files are both listed."""
    (tmp_path / "lain.md").write_text(HASIL, encoding="utf-8")
    document = _doc(table([["A", "B"]]) + INSERTED_TOKEN)
    result = fill_table_tokens(document, {"lain": tmp_path / "lain.md"}, _marks())
    assert result.tables_filled == []
    assert result.tables_without_file == ["hasil"]
    assert result.files_without_token == ["lain"]


def test_tables_with_tracked_rows_still_serve_as_the_reference(tmp_path):
    """After the edits every table may carry tracked rows; its properties are still the right reference."""
    (tmp_path / "hasil.md").write_text(HASIL, encoding="utf-8")
    tracked_table = table([["A", "B"]], width=7000).replace("<w:tr>", '<w:tr><w:trPr><w:ins w:id="9" w:author="A"/></w:trPr>', 1)
    document = _doc(tracked_table + INSERTED_TOKEN)
    result = fill_table_tokens(document, {"hasil": tmp_path / "hasil.md"}, _marks())
    assert result.errors == {}
    new_table = document.find(f"{{{W_NS}}}body").findall(f"{{{W_NS}}}tbl")[1]
    assert new_table.xpath("./w:tblPr/w:tblW/@w:w", namespaces=NSMAP) == ["7000"]


def test_a_table_token_whose_table_cannot_fit_is_listed_for_review():
    """fill_table_tokens records the builder's review note per token."""
    import tempfile
    from pathlib import Path

    row = "| " + " | ".join(["failover.state_sync_interval"] * 4) + " |"
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "hasil.md"
        path.write_text(f"| A | B | C | D |\n|---|---|---|---|\n{row}\n", encoding="utf-8")
        section = '<w:sectPr><w:pgSz w:w="11909" w:h="16834"/><w:pgMar w:top="2268" w:right="1701" w:bottom="1701" w:left="2268" w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>'
        document = _doc(table([["A", "B"], ["1", "2"]], width=6000) + INSERTED_TOKEN + section)
        result = fill_table_tokens(document, {"hasil": path}, _marks())
    assert list(result.review) == ["hasil"] and result.review["hasil"].startswith("table needs review")
