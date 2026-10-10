"""The reference registry: one record per cited work, built from the drafts and read by `cite`.

Sources:
  - F-literature.md inventory tables ("Merged reference inventory", "Inventory additions") list
    every distinct work with its key, old number, type code, year and peer-review status, and
    state key equivalences as 'key (= other-key)'.
  - Entry texts come from reference sections: E's cleaned-entry table (old numbers), and the
    "Mandatory additions", "New references" and "New verified sources" sections of the drafts.
    When sources give different texts for one work, F > B > C > A > D > E decides.
  - The technical records are the ones F's "N-entry split moves ... to footnotes" sentence names;
    without that sentence, the first seven of the "Minimum split" list.
Every [REF:key] in the edit Text of the drafts must resolve to a record.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from redline.edits import parse_edits

SOURCE_PRIORITY = ("F", "B", "C", "A", "D", "E")
DEFINITION_SECTIONS = ("Cleaned entries", "Mandatory additions", "New references", "New verified sources")
INVENTORY_SECTIONS = ("Merged reference inventory", "Inventory additions")
TECHNICAL_COUNT = 7

HEADING = re.compile(r"^#{1,6}\s+(.*)$")
NUMBER_ROW = re.compile(r"^\|\s*\[(\d+)\]\s*\|")
KEY_LINE = re.compile(r"^`\[REF:([^\]]+)\]`\s*$")
INLINE_KEY = re.compile(r"^(?:-\s+)?`?\[REF:([^\]]+)\]`?\s+(\S.*)$")
INVENTORY_KEY = re.compile(r"^(?:\[(\d+)\]\s+)?([A-Za-z0-9][A-Za-z0-9-]*)(?:\s*\(=\s*([A-Za-z0-9-]+)\))?(?:\s*\(new\))?$")
UNVERIFIED = re.compile(r"\s*`?\[UNVERIFIED:\s*([^\]]+)\]`?")
REF_TOKEN = re.compile(r"\[REF:([^\]\s]+)\]")
DOI = re.compile(r"doi:\s*(10\.[^\s,]+?)\.?(?=\s|$)")
SPLIT_SENTENCE = re.compile(r"The (\d+)-entry split moves (.+?) to footnotes")
MINIMUM_SPLIT = re.compile(r"Minimum split, (\d+) entries: (.+?)\. Result")


class RegistryError(ValueError):
    """The registry file is not usable."""


@dataclass(frozen=True)
class InventoryRow:
    """One row of F's inventory: a distinct work."""

    record_id: str
    number: str | None
    equivalents: list[str]
    type_code: str
    year: int | None
    peer_reviewed: bool


@dataclass(frozen=True)
class Definition:
    """An IEEE entry text for a key or an old number, with the draft it came from."""

    key: str
    text: str
    label: str
    line: int


@dataclass
class Record:
    """A registry record: one work, its IEEE entry, type, year, technical flag and aliases."""

    record_id: str
    ieee: str
    record_type: str
    year: int | None
    technical: bool
    aliases: list[str]
    unverified: list[str] = field(default_factory=list)


@dataclass
class RegistryBuild:
    """The records plus every decision and problem met while building them."""

    records: list[Record] = field(default_factory=list)
    merges: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class Registry:
    """Loaded records with an alias index for resolving citation markers."""

    def __init__(self, records: list[Record]):
        """Index every record by its id and aliases; an alias may name one record only."""
        self.records = records
        self._by_alias: dict[str, Record] = {}
        for record in records:
            for alias in [record.record_id, *record.aliases]:
                owner = self._by_alias.get(alias)
                if owner is not None and owner is not record:
                    raise RegistryError(f"alias '{alias}' belongs to both {owner.record_id} and {record.record_id}")
                self._by_alias[alias] = record

    def resolve(self, alias: str) -> Record | None:
        """The record an old number or key names, or None."""
        return self._by_alias.get(alias)


def parse_inventory(markdown: str) -> list[InventoryRow]:
    """Read the rows of F's inventory tables."""
    rows = []
    for _, line in _section_lines(markdown, INVENTORY_SECTIONS):
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 5 or cells[0].startswith(("Key", "---")):
            continue
        key = INVENTORY_KEY.match(cells[0])
        if key is None:
            continue
        year = re.search(r"\d{4}", cells[3])
        rows.append(
            InventoryRow(
                record_id=key.group(2),
                number=key.group(1),
                equivalents=[key.group(3)] if key.group(3) else [],
                type_code=cells[2],
                year=int(year.group(0)) if year else None,
                peer_reviewed=cells[4].lower().startswith("yes"),
            )
        )
    return rows


def parse_definitions(markdown: str, label: str) -> list[Definition]:
    """Read entry texts from the reference sections of one draft."""
    definitions = []
    pending_key: tuple[str, int] | None = None
    for line_number, line in _section_lines(markdown, DEFINITION_SECTIONS):
        stripped = line.strip()
        if pending_key is not None and stripped:
            definitions.append(Definition(pending_key[0], stripped, label, pending_key[1]))
            pending_key = None
            continue
        number_row = NUMBER_ROW.match(stripped)
        if number_row is not None:
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            definitions.append(Definition(number_row.group(1), cells[1], label, line_number))
            continue
        key_line = KEY_LINE.match(stripped)
        if key_line is not None:
            pending_key = (key_line.group(1), line_number)
            continue
        inline = INLINE_KEY.match(stripped)
        if inline is not None:
            definitions.append(Definition(inline.group(1), _strip_draft_note(inline.group(2)), label, line_number))
    return definitions


def build_registry(drafts: dict[str, str]) -> RegistryBuild:
    """Build the records from draft texts keyed by file name (the first letter names the draft)."""
    build = RegistryBuild()
    labels = {name: Path(name).name[0].upper() for name in drafts}
    inventory = [row for name, text in drafts.items() for row in parse_inventory(text)]
    definitions = [item for name, text in drafts.items() for item in parse_definitions(text, labels[name])]
    attached: set[int] = set()
    for row in inventory:
        record = _record_for(row, definitions, attached, build)
        if record is not None:
            build.records.append(record)
    _attach_or_exclude(definitions, attached, build)
    _mark_technical(drafts, labels, build)
    _check_used_keys(drafts, build)
    return build


def write_registry(records: list[Record], path: Path) -> None:
    """Write the records as JSON."""
    data = {
        "records": [
            {
                "id": record.record_id,
                "ieee": record.ieee,
                "type": record.record_type,
                "year": record.year,
                "technical": record.technical,
                "aliases": record.aliases,
                "unverified": record.unverified,
            }
            for record in records
        ]
    }
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_registry(path: Path) -> Registry:
    """Read a registry JSON file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    records = [
        Record(
            record_id=item["id"],
            ieee=item["ieee"],
            record_type=item["type"],
            year=item["year"],
            technical=item["technical"],
            aliases=list(item["aliases"]),
            unverified=list(item.get("unverified", [])),
        )
        for item in data["records"]
    ]
    return Registry(records)


def _section_lines(markdown: str, titles: tuple[str, ...]) -> list[tuple[int, str]]:
    """(line number, line) for every line inside a section whose heading starts with one of titles."""
    selected = []
    inside = False
    for number, line in enumerate(markdown.splitlines(), start=1):
        heading = HEADING.match(line)
        if heading is not None:
            inside = heading.group(1).startswith(titles)
            continue
        if inside:
            selected.append((number, line))
    return selected


def _strip_draft_note(text: str) -> str:
    """Drop a trailing '(Defined in ...)' note that a draft adds after a reused entry."""
    position = text.find(" (Defined in")
    return text[:position].rstrip() if position >= 0 else text.rstrip()


def _record_for(row: InventoryRow, definitions: list[Definition], attached: set[int], build: RegistryBuild) -> Record | None:
    """Turn one inventory row and its entry texts into a record."""
    keys = {row.record_id, *row.equivalents}
    matching = [(index, item) for index, item in enumerate(definitions) if item.key in keys or item.key == row.number]
    for equivalent in row.equivalents:
        build.merges.append(f"{equivalent} merged into {row.record_id}: F's inventory states '{row.record_id} (= {equivalent})'")
    if not matching:
        build.errors.append(f"{row.record_id} has no IEEE entry text in any draft")
        return None
    attached.update(index for index, _ in matching)
    ranked = sorted((item for _, item in matching), key=lambda item: SOURCE_PRIORITY.index(item.label))
    chosen = ranked[0]
    _note_conflict(row.record_id, chosen, ranked, build)
    text, unverified = _split_unverified(chosen.text)
    aliases = ([row.number] if row.number else []) + sorted(keys | {item.key for _, item in matching if not item.key.isdigit()})
    return Record(row.record_id, text, _record_type(row, text, build), row.year, False, aliases, unverified)


def _note_conflict(record_id: str, chosen: Definition, ranked: list[Definition], build: RegistryBuild) -> None:
    """Report when the drafts give different entry texts for one work, and which one was used."""
    chosen_text = _normalized(chosen.text)
    others = [item for item in ranked if _normalized(item.text) != chosen_text]
    if others:
        labels = ", ".join(sorted({item.label for item in others}, key=SOURCE_PRIORITY.index))
        build.conflicts.append(f"{record_id}: {chosen.label} chosen over {labels} (texts differ)")


def _record_type(row: InventoryRow, text: str, build: RegistryBuild) -> str:
    """Map F's type code and peer-review column to a registry type."""
    code = row.type_code.split()[0]
    if row.peer_reviewed:
        return "peer_reviewed"
    if code in ("JNL", "CNF"):
        build.notes.append(f"{row.record_id}: {row.type_code} not counted as peer-reviewed by F; typed 'web'")
        return "web"
    if code == "DOC":
        return "repository" if ("GitHub repository" in text or "source code" in text) else "documentation"
    return {"PRE": "preprint", "STD": "standard", "BOK": "book", "WP": "web"}.get(code, "web")


def _attach_or_exclude(definitions: list[Definition], attached: set[int], build: RegistryBuild) -> None:
    """Merge leftover definitions into a record with the same DOI, or report them as excluded."""
    by_doi = {doi: record for record in build.records for doi in DOI.findall(record.ieee)}
    leftovers: dict[str, list[Definition]] = {}
    for index, item in enumerate(definitions):
        if index not in attached:
            leftovers.setdefault(item.key, []).append(item)
    for key, items in leftovers.items():
        dois = [doi for item in items for doi in DOI.findall(item.text)]
        target = next((by_doi[doi] for doi in dois if doi in by_doi), None)
        sources = ", ".join(sorted({item.label for item in items}))
        if target is not None:
            target.aliases.append(key)
            build.merges.append(f"{key} merged into {target.record_id}: same DOI {dois[0]}")
        else:
            build.excluded.append(f"{key} ({sources}): defined but not in F's inventory; left out")


def _mark_technical(drafts: dict[str, str], labels: dict[str, str], build: RegistryBuild) -> None:
    """Flag the records that F's footnote split names (seven at most)."""
    text = "\n".join(content for name, content in drafts.items() if labels[name] == "F")
    split = SPLIT_SENTENCE.search(text)
    if split is not None:
        names = _split_names(split.group(2))
        build.notes.append(f"technical: F's {split.group(1)}-entry split names {len(names)} entries: {', '.join(names)}")
    else:
        minimum = MINIMUM_SPLIT.search(text)
        names = _split_names(minimum.group(2))[:TECHNICAL_COUNT] if minimum else []
        build.notes.append(f"technical: no N-entry split sentence; first {TECHNICAL_COUNT} of the minimum split used: {', '.join(names)}")
    by_alias = {alias: record for record in build.records for alias in [record.record_id, *record.aliases]}
    for name in names:
        record = by_alias.get(name)
        if record is None:
            build.errors.append(f"technical entry '{name}' named by F has no record")
        else:
            record.technical = True


def _split_names(listing: str) -> list[str]:
    """Split 'a, b, [14] c and d' into keys, dropping old-number prefixes."""
    parts = re.split(r",\s*|\s+and\s+", listing)
    return [re.sub(r"^\[\d+\]\s*", "", part).strip() for part in parts if part.strip()]


def _check_used_keys(drafts: dict[str, str], build: RegistryBuild) -> None:
    """Every [REF:key] in edit Text must resolve to a record."""
    known = {alias for record in build.records for alias in [record.record_id, *record.aliases]}
    reported: set[str] = set()
    for name, text in drafts.items():
        for edit in parse_edits(text, source=name).edits:
            for line in edit.text_lines:
                for key in REF_TOKEN.findall(line):
                    if key not in known and key not in reported:
                        reported.add(key)
                        build.errors.append(f"[REF:{key}] is used in {Path(name).name} but no record has that key")


def _split_unverified(text: str) -> tuple[str, list[str]]:
    """Move `[UNVERIFIED: field]` tags out of an entry text."""
    tags = [tag.strip() for tag in UNVERIFIED.findall(text)]
    return UNVERIFIED.sub("", text).strip(), tags


def _normalized(text: str) -> str:
    """Entry text with whitespace collapsed and verification tags removed, for comparison."""
    return re.sub(r"\s+", " ", UNVERIFIED.sub("", text)).strip()
