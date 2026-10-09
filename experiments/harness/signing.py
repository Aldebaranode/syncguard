"""Per-height signing records from /commit, and the signing metrics built on them (R4).

Times in a HeightRecord come from the containers' clock until
shift_to_host_clock maps them onto the host clock; every metric below expects
host-clock records.
"""

from dataclasses import asdict, dataclass, replace
from pathlib import Path

from harness.clock import container_to_host_ms, parse_rfc3339_ms
from harness.constants import BLOCK_ID_FLAG_ABSENT, BLOCK_ID_FLAG_COMMIT, BLOCK_ID_FLAG_NIL, TIP_MARGIN
from harness.jsonio import append_jsonl, read_jsonl

WINDOW_BEFORE = "before_fault_60s"
WINDOW_AFTER = "after_recovery_60s"
WINDOW_FOLLOWING = "following_120s"
WINDOW_AFTER_FAILBACK = "after_failback_120s"


@dataclass(frozen=True)
class HeightRecord:
    """One committed height: header time, proposer, commit round, and who voted how."""

    height: int
    header_time_ms: int
    proposer: str
    commit_round: int
    signers: dict
    nil_voters: dict
    absent: int


def parse_commit(result: dict) -> HeightRecord:
    """Build a HeightRecord from a /commit result: flag 2 signed the block, 3 voted nil, 1 was absent."""
    header = result["signed_header"]["header"]
    commit = result["signed_header"]["commit"]
    signers = {}
    nil_voters = {}
    absent = 0
    for vote in commit["signatures"]:
        flag = vote["block_id_flag"]
        if flag == BLOCK_ID_FLAG_COMMIT:
            signers[vote["validator_address"]] = parse_rfc3339_ms(vote["timestamp"])
        elif flag == BLOCK_ID_FLAG_NIL:
            nil_voters[vote["validator_address"]] = parse_rfc3339_ms(vote["timestamp"])
        elif flag == BLOCK_ID_FLAG_ABSENT:
            absent += 1
    return HeightRecord(
        height=int(header["height"]),
        header_time_ms=parse_rfc3339_ms(header["time"]),
        proposer=header["proposer_address"],
        commit_round=int(commit["round"]),
        signers=signers,
        nil_voters=nil_voters,
        absent=absent,
    )


def collect_heights(rpc, first: int, last: int) -> list:
    """Read every height in [first, last]; heights within TIP_MARGIN of the tip are refused (late precommits)."""
    readable = rpc.tip_height() - TIP_MARGIN
    if last > readable:
        raise ValueError(f"height {last} is above the readable limit {readable} (tip minus {TIP_MARGIN})")
    return [parse_commit(rpc.commit(height)) for height in range(max(first, 1), last + 1)]


def load_heights(path: Path) -> list:
    """Read heights.jsonl back into records sorted by height."""
    records = [
        HeightRecord(
            height=row["height"],
            header_time_ms=row["header_time_ms"],
            proposer=row["proposer"],
            commit_round=row["commit_round"],
            signers=row["signers"],
            nil_voters=row["nil_voters"],
            absent=row["absent"],
        )
        for row in read_jsonl(path)
    ]
    return sorted(records, key=lambda record: record.height)


def store_heights(path: Path, session_id: str, records: list) -> int:
    """Append the records whose height is not stored yet; return how many were added."""
    stored = {record.height for record in load_heights(path)}
    added = 0
    for record in records:
        if record.height in stored:
            continue
        append_jsonl(path, {"session": session_id, **asdict(record)})
        stored.add(record.height)
        added += 1
    return added


def shift_to_host_clock(records: list, offset_ms: float) -> list:
    """Map every container-clock time in the records onto the host clock."""
    shifted = []
    for record in records:
        shifted.append(replace(
            record,
            header_time_ms=container_to_host_ms(record.header_time_ms, offset_ms),
            signers={address: container_to_host_ms(t, offset_ms) for address, t in record.signers.items()},
            nil_voters={address: container_to_host_ms(t, offset_ms) for address, t in record.nil_voters.items()},
        ))
    return shifted


def count_absent(records: list, validator: str) -> int:
    """Heights at which the validator is in neither signers nor nil_voters.

    Only these are missed for Story's x/slashing (infractions.go, `missed := signed == comet.BlockIDFlagAbsent`);
    a nil vote is not, so every count of missed heights uses this rule.
    """
    return sum(1 for record in records if validator not in record.signers and validator not in record.nil_voters)


def require_contiguous(records: list, low: int, high: int) -> None:
    """Raise when any height in [low, high] is missing from the records."""
    present = {record.height for record in records}
    missing = [height for height in range(low, high + 1) if height not in present]
    if missing:
        raise ValueError(f"heights missing between {low} and {high}: {missing[:5]}")


@dataclass(frozen=True)
class SigningGap:
    """The validator's last signature before its first unsigned height and its first signature after it.

    gap_heights counts every height between the two signatures; absent_heights are those where the
    validator is in neither signers nor nil_voters (the only ones Story's x/slashing counts as missed);
    nil_heights are those where it voted nil. invalid_reason is set when no gap was observed.
    """

    validator: str
    reference_ms: int
    last_signed_height: int | None
    last_signed_ms: int | None
    first_signed_height: int | None
    first_signed_ms: int | None
    gap_heights: int | None
    absent_heights: int | None
    nil_heights: int | None
    invalid_reason: str | None


def signing_gap(records: list, validator: str, reference_ms: int) -> SigningGap:
    """Locate the signing gap that follows reference_ms on host-clock records.

    A height counts as signed only with block_id_flag 2. A signature that lands after the fault
    but before the process died belongs to the old regime, so the gap starts at the first unsigned
    height whose header time is at or after the reference (earlier unsigned heights are not the
    outage); the recovery signature must lie above that height. Every height between the two signatures must
    be present, because a missing commit could hide a signature.
    """
    ordered = sorted(records, key=lambda record: record.height)
    signed_records = [record for record in ordered if validator in record.signers]
    first_unsigned = next(
        (record.height for record in ordered
         if record.header_time_ms >= reference_ms and validator not in record.signers),
        None,
    )
    if first_unsigned is None:
        last_record = signed_records[-1] if signed_records else None
        return SigningGap(
            validator=validator,
            reference_ms=reference_ms,
            last_signed_height=last_record.height if last_record else None,
            last_signed_ms=last_record.signers[validator] if last_record else None,
            first_signed_height=None,
            first_signed_ms=None,
            gap_heights=None,
            absent_heights=None,
            nil_heights=None,
            invalid_reason="no signing gap observed",
        )
    last_record = next((record for record in reversed(signed_records) if record.height < first_unsigned), None)
    first_record = next((record for record in signed_records if record.height > first_unsigned), None)
    gap_heights = absent_heights = nil_heights = None
    if last_record is not None and first_record is not None:
        require_contiguous(ordered, last_record.height, first_record.height)
        gap_records = [record for record in ordered if last_record.height < record.height < first_record.height]
        gap_heights = len(gap_records)
        nil_heights = sum(1 for record in gap_records if validator in record.nil_voters)
        absent_heights = count_absent(gap_records, validator)
    return SigningGap(
        validator=validator,
        reference_ms=reference_ms,
        last_signed_height=last_record.height if last_record else None,
        last_signed_ms=last_record.signers[validator] if last_record else None,
        first_signed_height=first_record.height if first_record else None,
        first_signed_ms=first_record.signers[validator] if first_record else None,
        gap_heights=gap_heights,
        absent_heights=absent_heights,
        nil_heights=nil_heights,
        invalid_reason=None if first_record is not None else "no signature after the gap",
    )


@dataclass(frozen=True)
class MissedShare:
    """Absent heights against the slashing window and the jailing allowance.

    Pass absent_heights, not gap_heights: Story's x/slashing (infractions.go, `missed := signed ==
    comet.BlockIDFlagAbsent`) counts only absent votes, so nil votes do not count as missed.
    """

    missed: int
    window: int
    allowance: int
    share_of_window: float
    share_of_allowance: float


def max_missed_blocks(window: int, min_signed_per_window: float) -> int:
    """Blocks a validator may miss per window before jailing.

    cosmos-sdk x/slashing jails when missed > window - round(window * min_signed_per_window).
    """
    return window - round(window * min_signed_per_window)


def missed_share(missed: int, window: int, min_signed_per_window: float) -> MissedShare:
    """Express missed heights as a share of the window and of the allowance."""
    allowance = max_missed_blocks(window, min_signed_per_window)
    return MissedShare(missed, window, allowance, missed / window, missed / allowance)


@dataclass(frozen=True)
class BlockRateWindow:
    """Blocks with a header time in [start_ms, end_ms); blocks and rate are None when not covered."""

    label: str
    start_ms: int
    end_ms: int
    covered: bool
    blocks: int | None
    blocks_per_minute: float | None


def window_covered(records: list, start_ms: int, end_ms: int) -> bool:
    """True when a block precedes the window, a block follows it, and no height between them is missing."""
    before = [record.height for record in records if record.header_time_ms < start_ms]
    after = [record.height for record in records if record.header_time_ms >= end_ms]
    if not before or not after:
        return False
    present = {record.height for record in records}
    return all(height in present for height in range(max(before), min(after) + 1))


def block_rate(records: list, label: str, start_ms: int, end_ms: int) -> BlockRateWindow:
    """Blocks per minute from header times inside one window."""
    if not window_covered(records, start_ms, end_ms):
        return BlockRateWindow(label, start_ms, end_ms, False, None, None)
    blocks = sum(1 for record in records if start_ms <= record.header_time_ms < end_ms)
    return BlockRateWindow(label, start_ms, end_ms, True, blocks, blocks * 60_000 / (end_ms - start_ms))


def incident_block_rates(records: list, fault_ms: int, recovery_ms: int, primary_signature_ms: int | None = None) -> list:
    """The R4 windows: 60 s before the fault, the first 60 s after recovery, and the 120 s after that.

    IV-13 adds a fourth window, the 120 s from the primary's first signature after failback, when that
    signature was observed (primary_signature_ms is None otherwise and the window is left out).
    """
    windows = [
        block_rate(records, WINDOW_BEFORE, fault_ms - 60_000, fault_ms),
        block_rate(records, WINDOW_AFTER, recovery_ms, recovery_ms + 60_000),
        block_rate(records, WINDOW_FOLLOWING, recovery_ms + 60_000, recovery_ms + 180_000),
    ]
    if primary_signature_ms is not None:
        windows.append(block_rate(records, WINDOW_AFTER_FAILBACK, primary_signature_ms, primary_signature_ms + 120_000))
    return windows


@dataclass(frozen=True)
class Availability:
    """Heights the validator signed over a contiguous range of heights."""

    validator: str
    first_height: int
    last_height: int
    total: int
    signed: int
    ratio: float


def availability(records: list, validator: str) -> Availability:
    """Share of heights signed with block_id_flag 2; the heights must be contiguous."""
    if not records:
        raise ValueError("no heights to compute availability over")
    ordered = sorted(records, key=lambda record: record.height)
    require_contiguous(ordered, ordered[0].height, ordered[-1].height)
    signed = sum(1 for record in ordered if validator in record.signers)
    return Availability(validator, ordered[0].height, ordered[-1].height, len(ordered), signed, signed / len(ordered))


def header_gaps(records: list, threshold_ms: int) -> list:
    """Consecutive heights whose header times lie more than threshold_ms apart."""
    ordered = sorted(records, key=lambda record: record.height)
    gaps = []
    for previous, current in zip(ordered, ordered[1:]):
        if current.height != previous.height + 1:
            continue
        gap_ms = current.header_time_ms - previous.header_time_ms
        if gap_ms > threshold_ms:
            gaps.append({"from_height": previous.height, "to_height": current.height, "gap_ms": gap_ms})
    return gaps


def signer_counts(records: list) -> list:
    """(height, number of block_id_flag 2 signatures) for each record, by height."""
    return [(record.height, len(record.signers)) for record in sorted(records, key=lambda record: record.height)]
