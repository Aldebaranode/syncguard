"""Tests for the edit-list parser."""

from redline.edits import load_edit_files, parse_edits

GOOD = """# Draft X

Intro text that is not an edit.

### E-1-01 (closes: I-7, I-8)
Operation: replace
Anchor: Blockchain merupakan teknologi yang menjadi pilar dan landasan pada setiap project
Text:
*Blockchain* merupakan teknologi dasar [1].
Baris kedua dengan {{DATA:S4.n}} dan [REF:story-genesis].
Rationale: I-8 opening sentence.

## 1.2 Rumusan Masalah

### E-1-02 (closes: I-2)
Operation: insert_after
Occurrence: 2
Anchor: Tabel 2.1 Keaslian Penelitian
Style: Heading 3
Text:
2.1.4 Keaslian Penelitian
Rationale: new heading.

### E-1-03 (closes: T-8)
Operation: delete
Anchor: Output : status - node berubah menjadi pasif
Rationale: old pseudocode line.

## New references

- [REF:story-genesis] PIP Labs, "Story mainnet genesis file".
"""


def test_parses_replace_block_fields_and_line_number():
    """A replace block yields id, closes, anchor, text lines, rationale and the heading line."""
    result = parse_edits(GOOD, source="x.md")
    assert result.problems == []
    first = result.edits[0]
    assert first.edit_id == "E-1-01"
    assert first.closes == "I-7, I-8"
    assert first.operation == "replace"
    assert first.anchor.startswith("Blockchain merupakan")
    assert first.text_lines == (
        "*Blockchain* merupakan teknologi dasar [1].",
        "Baris kedua dengan {{DATA:S4.n}} dan [REF:story-genesis].",
    )
    assert first.rationale == "I-8 opening sentence."
    assert first.source == "x.md"
    assert first.line == 5
    assert first.occurrence is None
    assert first.style is None


def test_parses_insert_with_style_and_occurrence_in_any_order():
    """Optional Occurrence and Style fields are read wherever they sit before Text."""
    second = parse_edits(GOOD, source="x.md").edits[1]
    assert second.operation == "insert_after"
    assert second.occurrence == 2
    assert second.style == "Heading 3"
    assert second.text_lines == ("2.1.4 Keaslian Penelitian",)


def test_parses_delete_without_text_and_ignores_non_edit_sections():
    """A delete has no Text; headings and reference lists outside blocks are skipped."""
    result = parse_edits(GOOD, source="x.md")
    assert [edit.edit_id for edit in result.edits] == ["E-1-01", "E-1-02", "E-1-03"]
    third = result.edits[2]
    assert third.operation == "delete"
    assert third.text_lines == ()


def _problems(block: str) -> list[str]:
    """Parse one block and return its problem messages."""
    result = parse_edits(block, source="bad.md")
    return [f"{p.edit_id}@{p.line}: {p.message}" for p in result.problems]


def test_missing_operation_is_reported_with_id_and_line():
    """A block without Operation is malformed and reported, not guessed."""
    block = "\n### E-9-01 (closes: X-1)\nAnchor: Some paragraph\nText:\nNew\nRationale: r\n"
    assert _problems(block) == ["E-9-01@2: missing Operation"]


def test_unknown_operation_is_reported():
    """Only replace, insert_after, insert_before and delete are accepted."""
    block = "### E-9-02 (closes: X-1)\nOperation: move\nAnchor: A\nText:\nB\nRationale: r\n"
    assert _problems(block) == ["E-9-02@1: unknown Operation 'move'"]


def test_text_without_rationale_is_reported():
    """Text runs until Rationale; a block that ends first is malformed."""
    block = "### E-9-03 (closes: X-1)\nOperation: replace\nAnchor: A\nText:\nB\n\n## Next section\n"
    assert _problems(block) == ["E-9-03@1: missing Rationale"]


def test_delete_with_text_is_reported():
    """A delete carries no Text; one that does is ambiguous."""
    block = "### E-9-04 (closes: X-1)\nOperation: delete\nAnchor: A\nText:\nB\nRationale: r\n"
    assert _problems(block) == ["E-9-04@1: delete must not have Text"]


def test_insert_without_text_is_reported():
    """replace and insert edits need at least one Text line."""
    block = "### E-9-05 (closes: X-1)\nOperation: insert_after\nAnchor: A\nRationale: r\n"
    assert _problems(block) == ["E-9-05@1: insert_after needs Text"]


def test_style_on_replace_is_parsed_and_left_for_apply_to_judge():
    """Style on a replace still parses, so its anchor can be matched; apply reports the rule."""
    block = "### E-9-06 (closes: X-1)\nOperation: replace\nAnchor: A\nStyle: List\nText:\nB\nRationale: r\n"
    result = parse_edits(block, source="bad.md")
    assert result.problems == []
    assert result.edits[0].style == "List"


def test_thematic_break_ends_a_block():
    """A '---' rule after the last block separates it from the next section."""
    block = "### E-9-11 (closes: X-1)\nOperation: delete\nAnchor: A\nRationale: r\n\n---\n\nNot part of it.\n"
    result = parse_edits(block, source="x.md")
    assert result.problems == []
    assert [edit.edit_id for edit in result.edits] == ["E-9-11"]


def test_occurrence_must_be_positive_integer():
    """Occurrence counts matches from 1."""
    block = "### E-9-07 (closes: X-1)\nOperation: delete\nAnchor: A\nOccurrence: zero\nRationale: r\n"
    assert _problems(block) == ["E-9-07@4: Occurrence must be a positive integer, got 'zero'"]


def test_missing_anchor_and_duplicate_field_are_reported():
    """Every block needs one Anchor; a field given twice is ambiguous."""
    block = (
        "### E-9-08 (closes: X-1)\nOperation: delete\nOperation: delete\nRationale: r\n"
    )
    assert _problems(block) == ["E-9-08@3: duplicate field Operation", "E-9-08@1: missing Anchor"]


def test_bad_heading_and_unexpected_lines_are_reported():
    """The heading must carry the closes list; stray lines inside a block are malformed."""
    block = (
        "### E-9-09 without closes\nOperation: delete\nAnchor: A\nRationale: r\n\n"
        "### E-9-10 (closes: X-1)\nOperation: delete\nAnchor: A\nsome stray line\nRationale: r\n"
        "trailing line\n"
    )
    assert _problems(block) == [
        "E-9-09@1: heading must look like '### E-<id> (closes: <finding ids>)'",
        "E-9-10@9: unexpected line 'some stray line'",
        "E-9-10@11: unexpected line after Rationale 'trailing line'",
    ]


def test_duplicate_ids_are_reported_across_files(tmp_path):
    """Edit ids must be unique over every file given to one run."""
    block = "### E-1-01 (closes: X-1)\nOperation: delete\nAnchor: A\nRationale: r\n"
    first = tmp_path / "a.md"
    second = tmp_path / "b.md"
    first.write_text(block, encoding="utf-8")
    second.write_text(block, encoding="utf-8")
    result = load_edit_files([first, second])
    assert len(result.edits) == 1
    assert [(p.edit_id, p.source, p.message) for p in result.problems] == [
        ("E-1-01", str(second), f"duplicate edit id (first defined in {first}:1)")
    ]


def test_row_anchor_with_table_field_is_parsed():
    """'Anchor: ROW: <first cell>' may carry 'Table: <first header cell>' to pick the table."""
    block = (
        "### E-9-12 (closes: X-1)\nOperation: delete\nAnchor: ROW: health.interval\n"
        "Table: Parameter | Nilai Default | Keterangan\nRationale: r\n"
    )
    result = parse_edits(block, source="x.md")
    assert result.problems == []
    assert result.edits[0].anchor == "ROW: health.interval"
    assert result.edits[0].table == "Parameter | Nilai Default | Keterangan"


def test_table_field_without_row_anchor_and_empty_row_anchor_are_reported():
    """Table: only narrows a ROW: anchor; ROW: and TABLE: need a value."""
    block = (
        "### E-9-13 (closes: X-1)\nOperation: delete\nAnchor: Paragraf biasa\nTable: Parameter\nRationale: r\n\n"
        "### E-9-14 (closes: X-1)\nOperation: delete\nAnchor: TABLE:\nRationale: r\n"
    )
    assert _problems(block) == [
        "E-9-13@1: Table is only defined for ROW: anchors",
        "E-9-14@7: ROW: and TABLE: anchors need the text of a cell or row",
    ]


def test_anchor_source_line_is_accepted_as_a_note():
    """'Anchor source:' (draft F) documents where the anchor text comes from; it is kept, not applied."""
    block = (
        "### E-F-01 (closes: R-3)\nOperation: replace\nAnchor: Blockchain adalah struktur data\n"
        "Anchor source: draft B edit E-II-26\nText:\nBaru.\nRationale: r\n"
    )
    result = parse_edits(block, source="F.md")
    assert result.problems == []
    assert result.edits[0].anchor_source == "draft B edit E-II-26"


def test_numbering_key_is_parsed_on_replace_and_both_inserts():
    """'Numbering:' takes 'continue <prefix>', 'new <prefix>' or 'none'."""
    block = (
        "### E-9-30 (closes: X-1)\nOperation: insert_before\nAnchor: A\nStyle: List\n"
        "Numbering: continue Butir satu dari daftar.\nText:\nB\nRationale: r\n\n"
        "### E-9-31 (closes: X-1)\nOperation: replace\nAnchor: A\nNumbering: new Bagaimana rancangan mekanisme\nText:\nB\nRationale: r\n\n"
        "### E-9-32 (closes: X-1)\nOperation: insert_after\nAnchor: A\nNumbering: none\nText:\nB\nRationale: r\n"
    )
    result = parse_edits(block, source="x.md")
    assert result.problems == []
    assert [(edit.numbering.mode, edit.numbering.prefix) for edit in result.edits] == [
        ("continue", "Butir satu dari daftar."),
        ("new", "Bagaimana rancangan mekanisme"),
        ("none", None),
    ]


def test_numbering_is_optional():
    """An edit without the key has no Numbering."""
    assert parse_edits(GOOD, source="x.md").edits[0].numbering is None


def test_numbering_on_other_operations_on_table_anchors_or_malformed_is_reported():
    """Numbering is defined for replace and the inserts on paragraph anchors, in one of three forms."""
    block = (
        "### E-9-33 (closes: X-1)\nOperation: delete\nAnchor: A\nNumbering: none\nRationale: r\n\n"
        "### E-9-34 (closes: X-1)\nOperation: replace_image\nAnchor: Gambar 4.1 Judul\nNumbering: none\nText:\n{{FIGURE:a}}\nRationale: r\n\n"
        "### E-9-35 (closes: X-1)\nOperation: insert_after\nAnchor: A\nNumbering: restart A\nText:\nB\nRationale: r\n\n"
        "### E-9-36 (closes: X-1)\nOperation: insert_after\nAnchor: A\nNumbering: continue\nText:\nB\nRationale: r\n\n"
        "### E-9-37 (closes: X-1)\nOperation: insert_after\nAnchor: ROW: a\nNumbering: none\nText:\n| a |\nRationale: r\n"
    )
    assert _problems(block) == [
        "E-9-33@4: Numbering is only defined for replace, insert_after and insert_before",
        "E-9-34@10: Numbering is only defined for replace, insert_after and insert_before",
        "E-9-35@18: Numbering must be 'continue <prefix>', 'new <prefix>' or 'none', got 'restart A'",
        "E-9-36@26: Numbering must be 'continue <prefix>', 'new <prefix>' or 'none', got 'continue'",
        "E-9-37@34: Numbering is not defined for ROW: and TABLE: anchors",
    ]


def test_replace_image_takes_exactly_one_text_line():
    """replace_image is a defined operation; its Text is the single FIGURE token."""
    good = "### E-9-20 (closes: X-1)\nOperation: replace_image\nAnchor: Gambar 4.1 Judul\nText:\n{{FIGURE:S4.fig}}\nRationale: r\n"
    assert _problems(good) == []
    two = good.replace("{{FIGURE:S4.fig}}\n", "{{FIGURE:S4.fig}}\nsatu lagi\n")
    assert _problems(two) == ["E-9-20@1: replace_image takes exactly one Text line (the {{FIGURE:name}} token)"]
    none = "### E-9-21 (closes: X-1)\nOperation: replace_image\nAnchor: Gambar 4.1 Judul\nRationale: r\n"
    assert _problems(none) == ["E-9-21@1: replace_image needs Text"]
