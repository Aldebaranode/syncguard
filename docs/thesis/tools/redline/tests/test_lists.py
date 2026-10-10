"""Tests for restarting numbered lists with a new w:num and tracked paragraph property changes."""

from lxml import etree

from docx_builder import DEFAULT_STYLES, R_NS, W_NS, para
from redline.lists import restart_lists
from redline.structure import paragraph_style_names
from redline.tracked import RevisionMarks

NSMAP = {"w": W_NS}
DATE = "2026-10-05T08:00:00Z"
AUTHOR = "Syahid Nurrohim"
NUMBERING = (
    f'<w:numbering xmlns:w="{W_NS}">'
    '<w:abstractNum w:abstractNumId="3"><w:lvl w:ilvl="0"><w:start w:val="1"/></w:lvl></w:abstractNum>'
    '<w:num w:numId="11"><w:abstractNumId w:val="3"/></w:num>'
    '<w:num w:numId="12"><w:abstractNumId w:val="3"/><w:lvlOverride w:ilvl="1"><w:startOverride w:val="4"/></w:lvlOverride></w:num>'
    "</w:numbering>"
)


def _item(text: str, num_id: int = 11, level: int = 0, extra: str = "") -> str:
    """A numbered list paragraph."""
    numbering = f'<w:numPr><w:ilvl w:val="{level}"/><w:numId w:val="{num_id}"/></w:numPr>'
    return para(text, ppr=numbering + extra)


def _run(body: str, prefixes: list[str], numbering: str = NUMBERING):
    """Restart the named lists; return the result, the document and the numbering root."""
    document = etree.fromstring(f'<w:document xmlns:w="{W_NS}" xmlns:r="{R_NS}"><w:body>{body}</w:body></w:document>')
    numbering_root = etree.fromstring(numbering)
    names = paragraph_style_names(etree.fromstring(DEFAULT_STYLES.encode("utf-8")))
    marks = RevisionMarks.for_document(document, AUTHOR, DATE)
    return restart_lists(document, numbering_root, names, prefixes, marks), document, numbering_root


def _num_ids(document: etree._Element) -> list[str]:
    """The w:numId of every numbered paragraph, in order."""
    return document.xpath("//w:p/w:pPr/w:numPr/w:numId/@w:val", namespaces=NSMAP)


BODY = (
    para("4.1.3 Implementasi", ppr='<w:pStyle w:val="Heading3"/>')
    + _item("Pertama satu.") + _item("Pertama dua.") + para("Penjelasan di antara butir.") + _item("Pertama tiga.")
    + para("4.1.4 Berikutnya", ppr='<w:pStyle w:val="Heading3"/>')
    + _item("Kedua satu.") + _item("Kedua dua.")
)


def test_each_named_list_gets_its_own_num_with_a_start_override():
    """The new w:num copies the abstract numbering and restarts at 1; paragraphs move to it."""
    result, document, numbering = _run(BODY, ["Pertama satu", "Kedua satu"])
    assert [restart.new_num_id for restart in result.restarts] == ["13", "14"]
    assert _num_ids(document) == ["13", "13", "13", "14", "14"]
    new = numbering.findall("w:num", NSMAP)[2:]
    assert [num.get(f"{{{W_NS}}}numId") for num in new] == ["13", "14"]
    assert new[0].find("w:abstractNumId", NSMAP).get(f"{{{W_NS}}}val") == "3"
    assert new[0].xpath("w:lvlOverride[@w:ilvl='0']/w:startOverride/@w:val", namespaces=NSMAP) == ["1"]
    assert [etree.QName(child).localname for child in numbering] == ["abstractNum", "num", "num", "num", "num"]


def test_a_list_ends_at_the_next_heading():
    """Items after a heading that share the old numId are not swept into the restarted list."""
    result, document, _ = _run(BODY, ["Pertama satu"])
    assert _num_ids(document) == ["13", "13", "13", "11", "11"]
    assert result.restarts[0].paragraphs == 3


def test_original_paragraphs_record_the_previous_properties_in_a_ppr_change():
    """pPrChange holds the old pPr with the old numId, and is the last child of pPr."""
    _, document, _ = _run(BODY, ["Kedua satu"])
    paragraph = document.xpath("//w:p[w:pPr/w:numPr/w:numId/@w:val='13']", namespaces=NSMAP)[0]
    properties = paragraph.find("w:pPr", NSMAP)
    assert etree.QName(properties[-1]).localname == "pPrChange"
    assert properties.xpath("w:pPrChange/w:pPr/w:numPr/w:numId/@w:val", namespaces=NSMAP) == ["11"]


def test_paragraphs_inserted_by_us_change_directly():
    """A paragraph whose mark is our tracked insertion needs no pPrChange."""
    mark = f'<w:rPr><w:ins w:id="7" w:author="{AUTHOR}" w:date="{DATE}"/></w:rPr>'
    body = para("4.2.7 Uji", ppr='<w:pStyle w:val="Heading3"/>') + _item("Baru satu.", extra=mark)
    result, document, _ = _run(body, ["Baru satu"])
    assert document.xpath("count(//w:pPrChange)", namespaces=NSMAP) == 0
    assert _num_ids(document) == ["13"]
    assert result.restarts[0].paragraphs == 1


def test_a_levels_used_are_restarted_and_old_overrides_of_other_levels_are_kept():
    """Sub-levels used by the list restart too; an existing override of another level is copied."""
    body = _item("Awal.", num_id=12) + _item("Anak.", num_id=12, level=1)
    _, _, numbering = _run(body, ["Awal"])
    new = numbering.findall("w:num", NSMAP)[-1]
    assert new.xpath("w:lvlOverride/@w:ilvl", namespaces=NSMAP) == ["0", "1"]
    assert new.xpath("w:lvlOverride[@w:ilvl='1']/w:startOverride/@w:val", namespaces=NSMAP) == ["1"]


def test_an_override_of_a_level_the_list_does_not_use_is_copied_and_stays_on_the_old_num():
    """The new w:num gets a copy of the old one's other level overrides; the old w:num keeps them."""
    _, _, numbering = _run(_item("Awal.", num_id=12), ["Awal"])
    old, new = numbering.findall("w:num", NSMAP)[1], numbering.findall("w:num", NSMAP)[-1]
    assert old.xpath("w:lvlOverride/@w:ilvl", namespaces=NSMAP) == ["1"]
    assert new.xpath("w:lvlOverride/@w:ilvl", namespaces=NSMAP) == ["0", "1"]
    assert new.xpath("w:lvlOverride[@w:ilvl='1']/w:startOverride/@w:val", namespaces=NSMAP) == ["4"]


def test_a_prefix_that_matches_nothing_or_a_plain_paragraph_is_an_error():
    """Unknown prefixes, paragraphs without numbering and ambiguous prefixes are reported; nothing changes."""
    body = BODY + para("Tanpa nomor.") + _item("Pertama satu lagi.", num_id=12)
    result, document, numbering = _run(body, ["Tidak ada", "Tanpa nomor", "Pertama"])
    assert [error.split(":")[0] for error in result.errors] == ["Tidak ada", "Tanpa nomor", "Pertama"]
    assert "no paragraph starts with" in result.errors[0]
    assert "not a numbered list item" in result.errors[1]
    assert "matches 4 paragraphs" in result.errors[2]
    assert result.restarts == []
    assert len(numbering.findall("w:num", NSMAP)) == 2


def test_a_list_whose_numbering_definition_is_missing_is_an_error():
    """A numId without a w:num cannot be restarted."""
    result, _, _ = _run(_item("Hilang.", num_id=99), ["Hilang"])
    assert "numId 99" in result.errors[0]


def test_an_item_with_an_earlier_tracked_change_is_left_on_the_old_list_and_reported():
    """A second pPrChange would be invalid, so that item keeps its numId and the result names it."""
    changed = para("Pertama dua.", ppr='<w:numPr><w:ilvl w:val="0"/><w:numId w:val="11"/></w:numPr><w:pPrChange w:id="4" w:author="B"><w:pPr/></w:pPrChange>')
    result, document, _ = _run(_item("Pertama satu.") + changed, ["Pertama satu"])
    assert _num_ids(document) == ["13", "11"]
    assert result.restarts[0].paragraphs == 1
    assert result.errors == ["Pertama satu: the item 'Pertama dua.' already has a tracked paragraph change and stays on the old list"]
