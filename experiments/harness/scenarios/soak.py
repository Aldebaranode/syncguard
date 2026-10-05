"""soak (R9; D7, III-2): 24 h with syncguard active, one failover at a fixed offset, availability from commits."""

from dataclasses import asdict, dataclass, replace

from harness.adapters import Unreachable
from harness.constants import TARGET_VALIDATOR, TIP_MARGIN
from harness.jsonio import append_jsonl, write_json
from harness.preconditions import failover_preconditions
from harness.scenarios.common import (
    PreconditionError,
    abort_recorded,
    collect_heights_in_chunks,
    next_iteration,
    require,
)
from harness.scenarios.failover import run_failover_iteration
from harness.session import read_events
from harness.signing import availability, header_gaps, shift_to_host_clock

SCENARIO = "soak"


@dataclass(frozen=True)
class SoakSettings:
    """Duration, kill offset, progress cadence, and the header gap that counts as a pause."""

    hours: float = 24.0
    kill_offset_hours: float = 6.0
    progress_interval_s: float = 60.0
    pause_threshold_s: float = 30.0
    chunk_size: int = 500


def hours_ms(hours: float) -> int:
    """Hours in milliseconds."""
    return round(hours * 3_600_000)


def wait_with_progress(env, session, until_ms: int, interval_s: float) -> None:
    """Sleep until until_ms, logging the tip every interval, tip stalls, and gaps in the harness's own clock."""
    last_tip = None
    previous_ms = env.clock.now_ms()
    while env.clock.now_ms() < until_ms:
        remaining_s = (until_ms - env.clock.now_ms()) / 1000
        env.clock.sleep(min(interval_s, remaining_s))
        now = env.clock.now_ms()
        if now - previous_ms > 2 * interval_s * 1000:
            session.events.append("host_pause", gap_s=(now - previous_ms) / 1000)
        previous_ms = now
        try:
            tip = env.rpc.tip_height()
        except Unreachable as error:
            session.events.append("rpc_unreachable", error=str(error))
            continue
        append_jsonl(session.directory / "progress.jsonl", {"session": session.id, "t_ms": now, "tip": tip})
        if tip == last_tip:
            session.events.append("height_stall", tip=tip)
        last_tip = tip


def _soak_failover(env, session, failover_settings, probes) -> str:
    """The soak's single failover cycle; a refusal is logged and the soak continues."""
    try:
        return run_failover_iteration(env, session, next_iteration(session), replace(failover_settings, snapshots=False),
                                      probes)
    except PreconditionError as error:
        session.events.append("soak_kill_refused", failures=error.failures)
        return "refused"


def heights_summary(records: list, validator: str) -> dict:
    """Signed, nil, absent, and total heights of the validator, plus the signed share."""
    ordered = sorted(records, key=lambda record: record.height)
    signed = sum(1 for record in ordered if validator in record.signers)
    nil = sum(1 for record in ordered if validator in record.nil_voters)
    return {
        "signed_heights": signed,
        "nil_heights": nil,
        "absent_heights": len(ordered) - signed - nil,
        "total_heights": len(ordered),
    }


def run_soak(env, session, settings: SoakSettings, failover_settings, probes, monitor) -> dict:
    """Run the soak with caffeinate and the dual-signer monitor around it, then compute availability.

    An exception or Ctrl-C is logged (soak_aborted or soak_interrupted), the heights read so far are
    kept, caffeinate and the monitor stop, and the error re-raises. Heights are stored in chunks, so a
    transient error after 24 h loses at most one chunk; calling collect_heights_in_chunks again resumes.
    """
    require(session, SCENARIO, failover_preconditions(env))
    start_ms = env.clock.now_ms()
    start_height = env.rpc.tip_height()
    session.events.append("soak_start", hours=settings.hours, kill_offset_hours=settings.kill_offset_hours,
                          start_height=start_height)
    keep_awake = env.commands.spawn(["caffeinate", "-dims"])
    monitor.start()

    def keep_collected_heights() -> None:
        """Store the heights up to the readable tip so an aborted soak keeps its data."""
        collect_heights_in_chunks(env, session, start_height, env.rpc.tip_height() - TIP_MARGIN, settings.chunk_size)

    try:
        with abort_recorded(session, SCENARIO, None, keep_collected_heights, kind="soak"):
            wait_with_progress(env, session, start_ms + hours_ms(settings.kill_offset_hours),
                               settings.progress_interval_s)
            kill_outcome = _soak_failover(env, session, failover_settings, probes)
            wait_with_progress(env, session, start_ms + hours_ms(settings.hours), settings.progress_interval_s)
    finally:
        monitor.stop()
        keep_awake.stop()
    end_height = env.rpc.tip_height() - TIP_MARGIN
    soak_events = [row for row in read_events(session.directory / "events.jsonl") if row["t_ms"] >= start_ms]
    summary = {
        "start_ms": start_ms,
        "end_ms": env.clock.now_ms(),
        "start_height": start_height,
        "end_height": end_height,
        "kill_outcome": kill_outcome,
        "host_pauses": sum(1 for row in soak_events if row["kind"] == "host_pause"),
        "height_stalls": sum(1 for row in soak_events if row["kind"] == "height_stall"),
    }
    write_json(session.directory / "soak.json", {"session": session.id, "heights_collected": False, **summary})
    with abort_recorded(session, SCENARIO, None, kind="soak"):
        records = shift_to_host_clock(
            collect_heights_in_chunks(env, session, start_height, end_height, settings.chunk_size), session.offset_ms)
    summary = {
        **summary,
        "availability": asdict(availability(records, TARGET_VALIDATOR)),
        **heights_summary(records, TARGET_VALIDATOR),
        "header_gaps": header_gaps(records, round(settings.pause_threshold_s * 1000)),
    }
    write_json(session.directory / "soak.json", {"session": session.id, "heights_collected": True, **summary})
    session.events.append("soak_end", availability=summary["availability"]["ratio"])
    return summary
