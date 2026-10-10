"""Tests for building the reference registry from the drafts."""

import json

import pytest

from redline.registry import (
    RegistryError,
    build_registry,
    load_registry,
    parse_definitions,
    parse_inventory,
    write_registry,
)

E_DRAFT = """# E

## Cleaned entries (current numbering)

| Old no. | Cleaned IEEE entry | Verification and change |
|---|---|---|
| [1] | S. Dong, "Blockchain overview," *PeerJ Comput. Sci.*, 2023, doi: 10.7717/peerj-cs.1705. | Verified. |
| [17] | Amygdala Labs, "Horcrux," GitHub repository. [Online]. | Moved. |

## Mandatory additions (verified)

`[REF:nist-gcm]`
M. Dworkin, "GCM," NIST SP 800-38D, Nov. 2007, doi: 10.6028/NIST.SP.800-38D.
Verified at csrc.nist.gov.

`[REF:ieee-1633]`
IEEE Std 1633-2016, doi: 10.1109/IEEESTD.2017.7827907.

`[REF:cometbft-evidence]`
CometBFT, "Genesis," CometBFT specification.

## Renumbering map by first appearance

- `[REF:cometbft-evidence]` and others: not a definition.
"""

B_DRAFT = """# B

### E-II-01 (closes: X-1)
Operation: insert_after
Anchor: Jangkar paragraf yang cukup panjang untuk contoh ini.
Text:
Kunci [REF:nist80038d] dan [REF:cometbft-evidence] serta [REF:tidak-ada].
Rationale: r

## New references

[REF:nist80038d] M. Dworkin, "GCM," NIST, Gaithersburg, MD, USA, NIST SP 800-38D, Nov. 2007, doi: 10.6028/NIST.SP.800-38D.

[REF:cometbft-evidence] CometBFT, "Evidence," CometBFT v0.38.x specification.

[REF:gong1992] L. Gong, "A security risk," *ACM SIGOPS Oper. Syst. Rev.*, 1992.
"""

D_DRAFT = """# D

## New references

- `[REF:gray1989]` C. Gray and D. Cheriton, "Leases," in *Proc. SOSP*, 1989. (Defined in draft F round 2.)
"""

F_DRAFT = """# F

## Merged reference inventory

| Key or old number | Short citation | Type | Year | Peer-reviewed | Cited where |
|---|---|---|---|---|---|
| [1] dong2023 | Dong et al. | JNL | 2023 | yes | 1.1 |
| [17] horcrux | Horcrux repository | DOC | n.d. | no | 1.1 |
| nist-gcm (= nist80038d) | Dworkin | STD | 2007 | no | E-II-56 |
| cometbft-evidence | CometBFT, Evidence | DOC | n.d. | no | E-II-38 |
| gong1992 | Gong | JNL | 1992 | not counted (newsletter) | E-II-60 |
| grandjean2025 | Grandjean et al. | CNF | 2025 (online 30 Nov. 2024) | yes | E-II-15 |

## New verified sources

`[REF:grandjean2025]`
D. Grandjean, "Ethereum PoS," in *FC 2024 Workshops*, 2025, doi: 10.1007/x. `[UNVERIFIED: page range]`
Supports: something.

### Inventory additions

| Key | Short citation | Type | Year | Peer-reviewed | Cited where |
|---|---|---|---|---|---|
| gray1989 | Gray and Cheriton, SOSP | CNF | 1989 | yes | E-F-07 |

## Round 3

Splits: The 3-entry split moves horcrux, gong1992 and nist-gcm to footnotes; 1 of 3.
"""


def _drafts() -> dict[str, str]:
    """The fixture drafts keyed by their file names."""
    return {"B-bab2.md": B_DRAFT, "D-bab4.md": D_DRAFT, "E-references.md": E_DRAFT, "F-literature.md": F_DRAFT}


def test_inventory_rows_give_ids_number_aliases_equivalents_and_years():
    """'[1] dong2023', 'nist-gcm (= nist80038d)' and year notes are read from F's tables."""
    rows = {row.record_id: row for row in parse_inventory(F_DRAFT)}
    assert rows["dong2023"].number == "1"
    assert rows["nist-gcm"].equivalents == ["nist80038d"]
    assert rows["grandjean2025"].year == 2025
    assert rows["horcrux"].year is None
    assert rows["gray1989"].type_code == "CNF"


def test_definitions_are_read_only_from_reference_sections():
    """Table rows, key-then-entry lines, inline keys and bullets count; edit Text and other sections do not."""
    keys = [(item.key, item.text) for item in parse_definitions(E_DRAFT, "E")]
    assert keys[0] == ("1", 'S. Dong, "Blockchain overview," *PeerJ Comput. Sci.*, 2023, doi: 10.7717/peerj-cs.1705.')
    assert [key for key, _ in keys] == ["1", "17", "nist-gcm", "ieee-1633", "cometbft-evidence"]
    assert [item.key for item in parse_definitions(B_DRAFT, "B")] == ["nist80038d", "cometbft-evidence", "gong1992"]
    [gray] = parse_definitions(D_DRAFT, "D")
    assert gray.text == 'C. Gray and D. Cheriton, "Leases," in *Proc. SOSP*, 1989.'


def test_build_merges_equivalent_keys_and_resolves_text_conflicts_by_source_priority():
    """F's '(= key)' merges two keys; when sources disagree, F > B > C > A > D > E decides and is reported."""
    result = build_registry(_drafts())
    records = {record.record_id: record for record in result.records}
    assert "nist80038d" in records["nist-gcm"].aliases
    assert "Gaithersburg" in records["nist-gcm"].ieee
    assert records["cometbft-evidence"].ieee.startswith('CometBFT, "Evidence,"')
    assert any("nist80038d" in line and "nist-gcm" in line for line in result.merges)
    assert any(line.startswith("cometbft-evidence:") and "B chosen over E" in line for line in result.conflicts)


def test_types_years_technical_flags_and_unverified_tags():
    """Types follow F's codes; the named split marks technical records; UNVERIFIED tags move to their own field."""
    records = {record.record_id: record for record in build_registry(_drafts()).records}
    assert records["dong2023"].record_type == "peer_reviewed"
    assert records["horcrux"].record_type == "repository"
    assert records["gong1992"].record_type == "web"
    assert records["nist-gcm"].record_type == "standard"
    assert [record_id for record_id, record in records.items() if record.technical] == ["horcrux", "nist-gcm", "gong1992"]
    assert records["grandjean2025"].unverified == ["page range"]
    assert "UNVERIFIED" not in records["grandjean2025"].ieee
    assert records["dong2023"].aliases == ["1", "dong2023"]


def test_definitions_outside_the_inventory_and_unknown_keys_are_reported():
    """A defined work missing from the inventory is excluded; a key used in edit Text without a record is an error."""
    result = build_registry(_drafts())
    assert "ieee-1633" not in {record.record_id for record in result.records}
    assert any(line.startswith("ieee-1633") for line in result.excluded)
    assert result.errors == ["[REF:tidak-ada] is used in B-bab2.md but no record has that key"]


def test_registry_file_roundtrip_and_alias_lookup(tmp_path):
    """The JSON file loads back into a registry that resolves numbers and keys."""
    path = tmp_path / "references.json"
    write_registry(build_registry(_drafts()).records, path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data["records"][0]) == {"id", "ieee", "type", "year", "technical", "aliases", "unverified"}
    registry = load_registry(path)
    assert registry.resolve("1").record_id == "dong2023"
    assert registry.resolve("nist80038d").record_id == "nist-gcm"
    assert registry.resolve("99") is None


def test_an_alias_shared_by_two_records_is_refused(tmp_path):
    """Each alias must name one work."""
    path = tmp_path / "references.json"
    record = {"ieee": "x", "type": "web", "year": None, "technical": False, "unverified": []}
    path.write_text(json.dumps({"records": [dict(record, id="a", aliases=["5"]), dict(record, id="b", aliases=["5"])]}), encoding="utf-8")
    with pytest.raises(RegistryError, match="alias '5' belongs to both a and b"):
        load_registry(path)
