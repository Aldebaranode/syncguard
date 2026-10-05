"""Signing timeline and the metrics derived from it."""

import pytest

from fakes import FakeHttp, commit_answers, load_fixture, smoke_records
from harness.constants import TARGET_VALIDATOR
from harness.rpc import CometRpc
from harness.signing import (
    HeightRecord,
    availability,
    block_rate,
    collect_heights,
    header_gaps,
    incident_block_rates,
    load_heights,
    max_missed_blocks,
    missed_share,
    shift_to_host_clock,
    signer_counts,
    signing_gap,
    store_heights,
)

FAULT_MS = 1791168828000  # declared test input, 2026-10-05T02:53:48.000Z
RELEASE_MS = 1791168960768  # 'Successfully released validator duties', smoke-passive.txt:20


def by_height(records):
    """Index records by height."""
    return {record.height: record for record in records}


def synthetic(count: int, spacing_ms: int = 3000):
    """Heights 1..count with header times 0, spacing, 2*spacing, ..."""
    return [HeightRecord(h, (h - 1) * spacing_ms, "P", 0, {}, {}, 0) for h in range(1, count + 1)]


def test_parse_commit_flags():
    """Height 143: 48DC2183 voted nil; height 128: one absent entry."""
    records = by_height(smoke_records())
    nil_height = records[143]
    assert TARGET_VALIDATOR not in nil_height.signers
    assert nil_height.nil_voters == {TARGET_VALIDATOR: 1791168894098}
    assert nil_height.header_time_ms == 1791168890359
    assert nil_height.proposer.startswith("7EFAECCD")
    assert records[128].absent == 1
    assert len(records[128].signers) == 3
    assert records[130].commit_round == 1


def test_failover_gap_from_the_smoke_test():
    """Last signature 127 before the fault, first 144 after it, 16 heights missed."""
    gap = signing_gap(smoke_records(), TARGET_VALIDATOR, FAULT_MS)
    assert (gap.last_signed_height, gap.last_signed_ms) == (127, 1791168827759)
    assert (gap.first_signed_height, gap.first_signed_ms) == (144, 1791168896910)
    assert gap.gap_heights == 16
    assert gap.absent_heights == 15
    assert gap.nil_heights == 1
    assert gap.invalid_reason is None


def test_failback_gap_skips_the_nil_vote():
    """After the backup released: 163 is the backup's last, 168 the primary's first; nil at 167 is not signed."""
    gap = signing_gap(smoke_records(), TARGET_VALIDATOR, RELEASE_MS)
    assert (gap.last_signed_height, gap.first_signed_height) == (163, 168)
    assert gap.first_signed_ms == 1791168976425
    assert gap.gap_heights == 4
    assert gap.absent_heights == 3
    assert gap.nil_heights == 1


def test_signature_between_fault_and_death_is_not_recovery():
    """A flag-2 precommit 80 ms after the fault (height 127) is not the first signature after the gap."""
    gap = signing_gap(smoke_records(), TARGET_VALIDATOR, 1791168827679)
    assert (gap.last_signed_height, gap.last_signed_ms) == (127, 1791168827759)
    assert (gap.first_signed_height, gap.first_signed_ms) == (144, 1791168896910)
    assert gap.gap_heights == 16


def test_gap_not_observed_when_target_never_misses():
    """Every collected height is signed after the fault: no unsigned height, so no recovery instant."""
    records = [record for record in smoke_records() if record.height in (126, 127)]
    gap = signing_gap(records, TARGET_VALIDATOR, 1791168821000)
    assert gap.first_signed_height is None
    assert gap.invalid_reason == "no signing gap observed"


def test_gap_refuses_missing_heights():
    """A missing commit inside the gap could hide a signature."""
    records = [record for record in smoke_records() if record.height != 135]
    with pytest.raises(ValueError, match="135"):
        signing_gap(records, TARGET_VALIDATOR, FAULT_MS)


def test_gap_without_a_later_signature():
    """No signature after the reference leaves first and missed empty."""
    gap = signing_gap(smoke_records(), TARGET_VALIDATOR, 1791169000000)
    assert gap.first_signed_height is None
    assert gap.gap_heights is None
    assert gap.last_signed_height == 169
    assert gap.invalid_reason == "no signing gap observed"


def test_shift_to_host_clock():
    """Every time moves by the offset: host = container - offset."""
    shifted = by_height(shift_to_host_clock(smoke_records(), 50.0))
    assert shifted[126].header_time_ms == 1791168821876 - 50
    assert shifted[144].signers[TARGET_VALIDATOR] == 1791168896910 - 50
    assert shifted[143].nil_voters[TARGET_VALIDATOR] == 1791168894098 - 50


def test_slashing_allowance_and_missed_share():
    """window 200 and min 0.05: jailed above 190 missed blocks."""
    assert max_missed_blocks(200, 0.05) == 190
    share = missed_share(16, 200, 0.05)
    assert share.allowance == 190
    assert share.share_of_window == 0.08
    assert share.share_of_allowance == pytest.approx(16 / 190)
    from_gap = missed_share(signing_gap(smoke_records(), TARGET_VALIDATOR, FAULT_MS).absent_heights, 200, 0.05)
    assert from_gap.missed == 15


def test_block_rate_inside_a_covered_window():
    """Heights 2..21 fall in [3 s, 63 s): 20 blocks per minute."""
    window = block_rate(synthetic(30), "w", 3000, 63000)
    assert (window.covered, window.blocks, window.blocks_per_minute) == (True, 20, 20.0)


def test_block_rate_needs_full_coverage():
    """No block after the window end means the window is not covered."""
    window = block_rate(synthetic(30), "w", 60000, 120000)
    assert (window.covered, window.blocks, window.blocks_per_minute) == (False, None, None)


def test_incident_windows():
    """Fault at 90 s and recovery at 99 s on a 3 s chain: 20 blocks per minute in all three windows."""
    windows = incident_block_rates(synthetic(100), 90000, 99000)
    assert [window.label for window in windows] == ["before_fault_60s", "after_recovery_60s", "following_120s"]
    assert [window.blocks for window in windows] == [20, 20, 40]
    assert [window.blocks_per_minute for window in windows] == [20.0, 20.0, 20.0]


def test_availability_over_contiguous_heights():
    """48DC2183 signed 126, 127, 144, 145 of heights 126-145."""
    records = [record for record in smoke_records() if record.height <= 145]
    result = availability(records, TARGET_VALIDATOR)
    assert (result.first_height, result.last_height, result.total, result.signed) == (126, 145, 20, 4)
    assert result.ratio == 0.2


def test_header_gaps_flag_slow_rounds():
    """Consecutive heights more than 6 s apart (round-1 commits in the smoke test)."""
    gaps = header_gaps(smoke_records(), 6000)
    assert [(gap["from_height"], gap["to_height"], gap["gap_ms"]) for gap in gaps] == [
        (130, 131, 7359), (134, 135, 7326), (138, 139, 7296), (142, 143, 7350), (166, 167, 7302)]


def test_signer_counts():
    """Four signers normally, three while 48DC2183 is absent."""
    counts = dict(signer_counts(smoke_records()))
    assert counts[126] == 4
    assert counts[128] == 3
    assert counts[144] == 4


def test_collect_heights_refuses_the_unsettled_tip():
    """Tip 1188 makes 1186 the highest readable height."""
    http = FakeHttp({
        "http://localhost:26657/status": load_fixture("rpc/status_rpc1.json"),
        "http://localhost:26657/commit?height=*": commit_answers(load_fixture("rpc/commits_smoke.json")),
    })
    rpc = CometRpc(http, "http://localhost:26657")
    assert [record.height for record in collect_heights(rpc, 140, 142)] == [140, 141, 142]
    with pytest.raises(ValueError, match="readable limit 1186"):
        collect_heights(rpc, 140, 1187)


def test_store_heights_skips_stored_heights(tmp_path):
    """Overlapping collections add each height once."""
    path = tmp_path / "heights.jsonl"
    records = smoke_records()
    assert store_heights(path, "S4", records[:5]) == 5
    assert store_heights(path, "S4", records[3:8]) == 3
    loaded = load_heights(path)
    assert [record.height for record in loaded] == [record.height for record in records[:8]]
    assert loaded[0] == records[0]
