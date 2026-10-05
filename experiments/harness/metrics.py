"""Per-iteration failover and failback metrics (R5) from parsed log events and host-clock signing records.

Failover instants: t_gagal (fault command issued), t_deteksi ("Maximum failures
reached" on validator-1), takeover ("Successfully took over" on validator-2),
t_aktif_cadangan (first block_id_flag 2 signature of the validator after
t_gagal). Failback durations all start at the primary's first healthy check
after it went passive, the instant syncguard starts its grace period from.
"""

from dataclasses import dataclass

from harness.constants import BACKUP_NODE, PRIMARY_NODE
from harness.logparse import first_event, first_matching
from harness.signing import signing_gap
from harness.stats import summarize


NO_TAKEOVER_REASON = "tanpa pengambilalihan"


def seconds_between(start_ms: int | None, end_ms: int | None) -> float | None:
    """(end - start) in seconds, or None when either instant is missing."""
    if start_ms is None or end_ms is None:
        return None
    return (end_ms - start_ms) / 1000


def event_time(event) -> int | None:
    """An event's time, or None for a missing event."""
    return None if event is None else event.t_ms


def event_sources(named: dict) -> dict:
    """file:line sources of the events that were found."""
    return {name: event.source for name, event in named.items() if event is not None}


def commit_source(height: int) -> str:
    """Evidence reference of a signature read from /commit."""
    return f"/commit?height={height}"


def is_failed_check(event) -> bool:
    """A health-check line reporting the node unreachable or unhealthy."""
    if event.marker == "health_failed":
        return True
    return event.marker == "health_status" and event.details.get("healthy") is False


def is_healthy_passive(event) -> bool:
    """A health-check line of a passive node reporting healthy."""
    return event.details.get("role") == "passive" and event.details.get("healthy") is True


@dataclass(frozen=True)
class FailoverIteration:
    """TTD, TTR, and the TTR decomposition of one failover, with evidence sources."""

    session: str
    iteration: int
    config_id: str
    t_fault_ms: int
    t_first_failed_check_ms: int | None
    t_detect_ms: int | None
    t_takeover_ms: int | None
    t_first_signature_ms: int | None
    first_signature_height: int | None
    last_signed_height: int | None
    gap_heights: int | None
    absent_heights: int | None
    nil_heights: int | None
    ttd_s: float | None
    ttr_s: float | None
    detection_s: float | None
    orchestration_s: float | None
    node_start_s: float | None
    clock_uncertainty_ms: float | None
    invalid_reason: str | None
    sources: dict


def failover_iteration(session_id: str, iteration: int, config_id: str, t_fault_ms: int, fault_source: str,
                       events: list, records: list, validator: str,
                       clock_uncertainty_ms: float | None = None) -> FailoverIteration:
    """Compute one failover's instants and durations from its events and host-clock records.

    A row with no observed signing gap, no takeover line between t_gagal and the first signature, or a
    negative phase duration, is invalid: it keeps its
    instants and TTD but its TTR and decomposition are empty and invalid_reason says why.
    """
    failed_check = first_matching(
        events, lambda event: event.node == PRIMARY_NODE and event.t_ms >= t_fault_ms and is_failed_check(event))
    detect = first_event(events, "max_failures", PRIMARY_NODE, after_ms=t_fault_ms)
    takeover = first_event(events, "takeover_complete", BACKUP_NODE, after_ms=t_fault_ms)
    gap = signing_gap(records, validator, t_fault_ms)
    sources = {"fault": fault_source}
    sources.update(event_sources({"first_failed_check": failed_check, "detect": detect, "takeover": takeover}))
    if gap.first_signed_height is not None:
        sources["first_signature"] = commit_source(gap.first_signed_height)
    t_detect = event_time(detect)
    t_takeover = event_time(takeover)
    ttd = seconds_between(t_fault_ms, t_detect)
    ttr = seconds_between(t_fault_ms, gap.first_signed_ms)
    orchestration = seconds_between(t_detect, t_takeover)
    node_start = seconds_between(t_takeover, gap.first_signed_ms)
    invalid_reason = gap.invalid_reason
    no_takeover = takeover is None or (gap.first_signed_ms is not None and takeover.t_ms > gap.first_signed_ms)
    if invalid_reason is None and no_takeover:
        invalid_reason = NO_TAKEOVER_REASON
    for name, value in (("orchestration_s", orchestration), ("node_start_s", node_start)):
        if invalid_reason is None and value is not None and value < 0:
            invalid_reason = f"negative {name}"
    if invalid_reason is not None:
        ttr = None
        orchestration = None
        node_start = None
    return FailoverIteration(
        session=session_id,
        iteration=iteration,
        config_id=config_id,
        t_fault_ms=t_fault_ms,
        t_first_failed_check_ms=event_time(failed_check),
        t_detect_ms=t_detect,
        t_takeover_ms=t_takeover,
        t_first_signature_ms=gap.first_signed_ms,
        first_signature_height=gap.first_signed_height,
        last_signed_height=gap.last_signed_height,
        gap_heights=gap.gap_heights,
        absent_heights=gap.absent_heights,
        nil_heights=gap.nil_heights,
        ttd_s=ttd,
        ttr_s=ttr,
        detection_s=ttd,
        orchestration_s=orchestration,
        node_start_s=node_start,
        clock_uncertainty_ms=clock_uncertainty_ms,
        invalid_reason=invalid_reason,
        sources=sources,
    )


def events_between(events: list, marker: str, start_ms: int, end_ms: int | None, node: str = PRIMARY_NODE) -> list:
    """Events of one marker on one node (the primary by default) in [start_ms, end_ms], by time.

    The range is open-ended when end_ms is None.
    """
    chosen = [event for event in events if event.marker == marker and event.node == node
              and event.t_ms >= start_ms and (end_ms is None or event.t_ms <= end_ms)]
    return sorted(chosen, key=lambda event: event.t_ms)


def last_matching(ordered_events: list):
    """The latest of time-ordered events, or None when there are none."""
    return ordered_events[-1] if ordered_events else None


@dataclass(frozen=True)
class FailbackIteration:
    """Failback instants and durations of one cycle, counted from the primary's first healthy check.

    gfb_s is the signing gap of the failback: the primary's first signature minus the backup's
    "Received failback notification from peer" line (equation 3.7 of the thesis draft).
    """

    session: str
    iteration: int
    config_id: str
    t_primary_healthy_ms: int | None
    t_failback_considered_ms: int | None
    t_failback_initiated_ms: int | None
    t_failback_complete_ms: int | None
    t_release_complete_ms: int | None
    t_failback_notified_ms: int | None
    t_primary_signature_ms: int | None
    primary_signature_height: int | None
    gap_heights: int | None
    absent_heights: int | None
    nil_heights: int | None
    failback_attempts: int
    grace_wait_s: float | None
    to_complete_s: float | None
    to_signature_s: float | None
    execution_s: float | None
    gfb_s: float | None
    sources: dict


def failback_iteration(session_id: str, iteration: int, config_id: str, t_fault_ms: int,
                       events: list, records: list, validator: str) -> FailbackIteration:
    """Compute one failback; the primary's first signature is the first one after the backup confirmed its release."""
    went_passive = first_event(events, "failover_complete", PRIMARY_NODE, after_ms=t_fault_ms)
    healthy = None
    if went_passive is not None:
        healthy = first_event(events, "health_status", PRIMARY_NODE, after_ms=went_passive.t_ms,
                              where=is_healthy_passive)
    complete = first_event(events, "failback_complete", PRIMARY_NODE, after_ms=t_fault_ms)
    end_ms = complete.t_ms if complete is not None else None
    initiated_events = events_between(events, "failback_initiated", t_fault_ms, end_ms)
    initiated = last_matching(initiated_events)
    considered_end = initiated.t_ms if initiated is not None else end_ms
    considered = last_matching(events_between(events, "failback_considered", t_fault_ms, considered_end))
    released = first_event(events, "release_complete", BACKUP_NODE, after_ms=t_fault_ms)
    notified = None
    if released is not None:
        notified = last_matching(events_between(events, "failback_notified", t_fault_ms, released.t_ms, BACKUP_NODE))
    signature_ms = None
    signature_height = None
    gap_heights = absent = nil = None
    if released is not None:
        gap = signing_gap(records, validator, released.t_ms)
        signature_ms = gap.first_signed_ms
        signature_height = gap.first_signed_height
        gap_heights = gap.gap_heights
        absent = gap.absent_heights
        nil = gap.nil_heights
    sources = event_sources({
        "primary_healthy": healthy,
        "failback_considered": considered,
        "failback_initiated": initiated,
        "failback_complete": complete,
        "release_complete": released,
        "failback_notified": notified,
    })
    if signature_height is not None:
        sources["primary_signature"] = commit_source(signature_height)
    t_healthy = event_time(healthy)
    t_complete = event_time(complete)
    return FailbackIteration(
        session=session_id,
        iteration=iteration,
        config_id=config_id,
        t_primary_healthy_ms=t_healthy,
        t_failback_considered_ms=event_time(considered),
        t_failback_initiated_ms=event_time(initiated),
        t_failback_complete_ms=t_complete,
        t_release_complete_ms=event_time(released),
        t_failback_notified_ms=event_time(notified),
        t_primary_signature_ms=signature_ms,
        primary_signature_height=signature_height,
        gap_heights=gap_heights,
        absent_heights=absent,
        nil_heights=nil,
        failback_attempts=len(initiated_events),
        grace_wait_s=seconds_between(t_healthy, event_time(considered)),
        to_complete_s=seconds_between(t_healthy, t_complete),
        to_signature_s=seconds_between(t_healthy, signature_ms),
        execution_s=seconds_between(event_time(initiated), t_complete),
        gfb_s=seconds_between(event_time(notified), signature_ms),
        sources=sources,
    )


def summarize_field(rows: list, field: str):
    """Summary of one numeric field over iteration rows of a single configuration (IV-3).

    Rows without a value are left out, so n can be lower than the number of rows.
    """
    config_ids = {row["config_id"] for row in rows}
    if len(config_ids) > 1:
        raise ValueError(f"iterations mix configurations {sorted(config_ids)}; summarize each configuration separately")
    values = [row[field] for row in rows if row[field] is not None]
    return summarize(values)
