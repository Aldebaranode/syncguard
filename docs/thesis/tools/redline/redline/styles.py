"""Look up paragraph style ids by display name in word/styles.xml, with a draft-name alias map.

Word stores built-in style names in lower case ("heading 3", "caption") and shows them
capitalised ("Heading 3"); custom styles keep their own spelling. Names are therefore compared
without regard to case, and aliases (w:aliases) count as names. Only paragraph styles qualify.

Drafts may name styles the manuscript does not define ("List", "Caption", "Table"). The alias
map in style_aliases.json maps such a draft name to a style the manuscript does define, and can
mark the name as a list item so that list numbering is kept where the anchor provides it, or
name a paragraph layout ("figure") that gives the new paragraph its alignment and spacing.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

from redline.wordml import qn

DEFAULT_ALIASES_PATH = Path(__file__).with_name("style_aliases.json")


class StyleNotFound(LookupError):
    """The named paragraph style is not defined in the document and not usefully aliased."""


@dataclass(frozen=True)
class StyleAlias:
    """A draft style name's stand-in: a defined style name, and whether it means a list item."""

    style: str
    list_item: bool = False
    layout: str | None = None


@dataclass(frozen=True)
class ResolvedStyle:
    """The style id a draft name resolves to; alias_of names the alias target when one was used."""

    style_id: str
    list_item: bool
    alias_of: str | None
    layout: str | None = None


def load_aliases(path: Path = DEFAULT_ALIASES_PATH) -> dict[str, StyleAlias]:
    """Read the alias map: {draft name: {"style": defined name, "list_item": bool}}."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    aliases: dict[str, StyleAlias] = {}
    for name, entry in data.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("style"), str):
            raise ValueError(f"alias '{name}' needs a 'style' name")
        aliases[name] = StyleAlias(entry["style"], bool(entry.get("list_item", False)), entry.get("layout"))
    return aliases


class StyleCatalog:
    """The paragraph styles of one document, searchable by display name, plus draft aliases."""

    def __init__(self, styles_root: etree._Element, aliases: dict[str, StyleAlias] | None = None):
        """Collect (name, style id) pairs for every paragraph style, in file order."""
        self._names: list[tuple[str, str]] = []
        self._aliases: list[tuple[str, str]] = []
        self._default_id: str | None = None
        self._draft_aliases = {name.casefold(): alias for name, alias in (aliases or {}).items()}
        for style in styles_root.iter(qn("style")):
            if style.get(qn("type")) != "paragraph":
                continue
            style_id = style.get(qn("styleId"))
            if style.get(qn("default")) in ("1", "true", "on"):
                self._default_id = style_id
            name = style.find(qn("name"))
            if name is not None:
                self._names.append((name.get(qn("val")), style_id))
            aliases_element = style.find(qn("aliases"))
            if aliases_element is not None:
                for alias in aliases_element.get(qn("val"), "").split(","):
                    self._aliases.append((alias.strip(), style_id))

    def paragraph_style_id(self, display_name: str) -> str:
        """Return the style id whose name or alias equals display_name, ignoring case."""
        wanted = display_name.strip().casefold()
        for name, style_id in self._names + self._aliases:
            if name.casefold() == wanted:
                return style_id
        defined = ", ".join(name for name, _ in self._names)
        raise StyleNotFound(
            f"paragraph style '{display_name}' is not defined in word/styles.xml (paragraph styles: {defined})"
        )

    def resolve(self, display_name: str) -> ResolvedStyle:
        """Resolve a draft style name: a defined name first, then the alias map."""
        try:
            return ResolvedStyle(self.paragraph_style_id(display_name), list_item=False, alias_of=None)
        except StyleNotFound:
            alias = self._draft_aliases.get(display_name.strip().casefold())
            if alias is None:
                raise
        try:
            style_id = self.paragraph_style_id(alias.style)
        except StyleNotFound as error:
            raise StyleNotFound(f"alias '{display_name}' points to '{alias.style}', which is not defined") from error
        return ResolvedStyle(style_id, list_item=alias.list_item, alias_of=alias.style, layout=alias.layout)

    def default_style_id(self) -> str | None:
        """The style id of the default paragraph style (used by paragraphs without w:pStyle)."""
        return self._default_id
