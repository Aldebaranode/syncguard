"""Tests for looking up paragraph style ids by display name in styles.xml."""

import pytest
from lxml import etree

from docx_builder import DEFAULT_STYLES
from redline.styles import DEFAULT_ALIASES_PATH, ResolvedStyle, StyleAlias, StyleCatalog, StyleNotFound, load_aliases


def _catalog() -> StyleCatalog:
    """Build a catalog from the fixture styles part."""
    return StyleCatalog(etree.fromstring(DEFAULT_STYLES.encode("utf-8")))


def test_built_in_name_is_found_by_its_display_spelling():
    """Word stores built-in names in lower case ('heading 3'); drafts write 'Heading 3'."""
    assert _catalog().paragraph_style_id("Heading 3") == "Heading3"
    assert _catalog().paragraph_style_id("Normal") == "Normal"


def test_custom_name_is_found():
    """Custom styles are looked up by their own name."""
    assert _catalog().paragraph_style_id("Kode Program") == "KodeProgram"


def test_absent_style_is_an_error_listing_what_exists():
    """A name that is not defined is an error, never a fallback."""
    with pytest.raises(StyleNotFound) as raised:
        _catalog().paragraph_style_id("Caption")
    assert str(raised.value) == (
        "paragraph style 'Caption' is not defined in word/styles.xml "
        "(paragraph styles: Normal, heading 3, Kode Program)"
    )


def test_table_style_name_is_not_a_paragraph_style():
    """Only paragraph styles qualify, since the style goes on a w:p."""
    with pytest.raises(StyleNotFound, match="'Table Grid' is not defined"):
        _catalog().paragraph_style_id("Table Grid")


ALIASES = {"List": StyleAlias("Normal", list_item=True), "Missing": StyleAlias("Body Text")}


def test_resolve_prefers_defined_names_then_aliases():
    """A defined name wins; otherwise the alias names the manuscript style to use."""
    catalog = StyleCatalog(etree.fromstring(DEFAULT_STYLES.encode("utf-8")), ALIASES)
    assert catalog.resolve("Heading 3") == ResolvedStyle("Heading3", list_item=False, alias_of=None)
    assert catalog.resolve("list") == ResolvedStyle("Normal", list_item=True, alias_of="Normal")


def test_resolve_fails_for_names_neither_defined_nor_usefully_aliased():
    """No definition and no alias, or an alias to an undefined style, is StyleNotFound."""
    catalog = StyleCatalog(etree.fromstring(DEFAULT_STYLES.encode("utf-8")), ALIASES)
    with pytest.raises(StyleNotFound, match="'Caption' is not defined"):
        catalog.resolve("Caption")
    with pytest.raises(StyleNotFound, match="alias 'Missing' points to 'Body Text', which is not defined"):
        catalog.resolve("Missing")


def test_default_paragraph_style_is_known():
    """A paragraph without w:pStyle has the default paragraph style."""
    assert _catalog().default_style_id() == "Normal"


def test_shipped_alias_map_maps_draft_names_to_normal():
    """The alias file maps List, Caption, Table and Figure to Normal; List marks list items, Figure a figure layout."""
    aliases = load_aliases(DEFAULT_ALIASES_PATH)
    assert aliases == {
        "List": StyleAlias("Normal", list_item=True),
        "Caption": StyleAlias("Normal", list_item=False),
        "Table": StyleAlias("Normal", list_item=False),
        "Figure": StyleAlias("Normal", list_item=False, layout="figure"),
    }


def test_malformed_alias_file_is_refused(tmp_path):
    """Every alias needs a style name."""
    bad = tmp_path / "aliases.json"
    bad.write_text('{"List": {"list_item": true}}', encoding="utf-8")
    with pytest.raises(ValueError, match="alias 'List' needs a 'style' name"):
        load_aliases(bad)


def test_resolve_carries_the_layout_of_an_alias():
    """An alias with a layout passes it on, so the caller can build the paragraph properties."""
    aliases = {"Figure": StyleAlias("Normal", layout="figure")}
    catalog = StyleCatalog(etree.fromstring(DEFAULT_STYLES.encode("utf-8")), aliases)
    assert catalog.resolve("figure") == ResolvedStyle("Normal", list_item=False, alias_of="Normal", layout="figure")
