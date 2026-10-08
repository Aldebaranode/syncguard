"""values.json: the {{DATA:...}} tokens of the manuscript draft (docs/thesis/drafts/D-bab4-bab5.md, "Data tokens").

Each value is formatted as it appears in the text: decimal comma, thousands dot, seconds to one
decimal, percentages to two. A token the harness cannot compute is listed with the reason instead
of a value. Token names keep the draft's session label S4 for the failover series; roles records
which session actually played that part.
"""

from dataclasses import dataclass
from pathlib import Path

from harness.analysis import injected_faults, iteration_log_events, iteration_outcomes, run_windows
from harness.clock import parse_rfc3339_ms
from harness.constants import BACKUP_NODE, PRIMARY_NODE, TARGET_VALIDATOR
from harness.jsonio import write_json
from harness.labels import (
    ACTION_LABELS,
    METHOD_LABELS,
    OUTCOME_LABELS,
    PARTITION_OUTCOME_LABELS,
    PROBE_LABELS,
    RESTART_LABELS,
    SCENARIO_LABELS,
    SYMPTOM_LABELS,
    monitor_problems_text,
    translate_reasons,
)
from harness.layout import SNAPSHOT_POINTS, layout_deviations
from harness.logexcerpt import excerpt_for, last_health_line, raw_log_lines
from harness.logparse import event_from_dict, parse_log_file
from harness.metrics import summarize_field
from harness.metrics import took_over
from harness.monitor import coverage, dual_signer_seconds, load_monitor_samples
from harness.numfmt import format_id
from harness.report import (
    ASSUMED_MANUAL_RECOVERY_S,
    LITERATURE_AVAILABILITY,
    REFERENCE_WINDOW_S,
    byzantine_maximum,
    last_with_result,
    mean_valid_ttr,
    plotted_failover,
    soak_kill_completed,
    series_session,
    signer_figure_name,
    failover_figure_name,
)
from harness.session import read_events

# Bounds of the manuscript's success criteria: 15 s for TTD and 30 s for TTR (draft C, E-III-81 and 82).
TTD_BOUND_S = 15.0
TTR_BOUND_S = 30.0
SOAK_AVAILABILITY_MINIMUM_PERCENT = 99.5
# story-1 jail allowance in blocks, as given in draft A (E-1-02) and used by draft D for the mainnet comparison.
MAINNET_ALLOWANCE_BLOCKS = 27_360
# Detection-to-fault bounds of the failure classes (draft D, S4.unintended_failover.count): 15 s plus the docker
# command time for crash, 35 s for hang.
CRASH_BOUND_MS = 15_000
HANG_BOUND_MS = 35_000  # analytic hang TTD: between 3 x 2 x 5 s and 5 s + 30 s (ticker catch-up, failover.go:289)
HANG_WAIT_MS = 60_000  # the hang and partition runners' detection wait
WINDOWED_FAULTS = ("hang", "partition")  # faults the host cannot see failing at once: the 60 s wait and 35 s bound apply
CRASH_WAIT_MS = 180_000  # the failover runner's takeover wait; also how long a clearing kill explains a detection
CLEARING_KILLS = ("abort_kill", "fault_cleared_after_failed_unpause")
LIVE_LOGS = ((PRIMARY_NODE, "syncguard.log"), (BACKUP_NODE, "syncguard-passive.log"))
FAIL_CLOSED_MARKERS = ("failover_aborted", "peer_no_takeover")
MONTHS = ("Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober",
          "November", "Desember")
SERIES_BASIC = ("S4.n", "S4.syncguard_version", "S4.image_ids", "S4.clock_offset_ms")
TTD_TOKENS = ("S4.ttd.min", "S4.ttd.max", "S4.ttd.mean", "S4.ttd.sd", "S4.ttd.ci_low", "S4.ttd.ci_high")
TTR_TOKENS = ("S4.ttr.n", "S4.ttr.min", "S4.ttr.max", "S4.ttr.mean", "S4.ttr.sd", "S4.ttr.ci_low", "S4.ttr.ci_high")
BOUND_TOKENS = ("S4.ttd.over_bound", "S4.ttr.over_bound")
PHASE_TOKENS = ("S4.orchestration.mean", "S4.node_start.mean")
MISSED_TOKENS = ("S4.missed.min", "S4.missed.max", "S4.missed.allowance_pct_max", "S4.missed.mainnet_pct_max")
SSR_TOKENS = ("S4.ssr.k", "S4.ssr.pct", "S4.ssr.failed")
LOG_COUNT_TOKENS = ("S4.failclosed.count", "S4.refusals.count")
UNINTENDED_TOKENS = ("S4.unintended_failover.count",)
FAILBACK_TOKENS = ("S4.fb.n", "S4.fb.k", "S4.fb.grace.mean", "S4.fb.execution.mean", "S4.fb.execution.max",
                   "S4.gfb.mean", "S4.gfb.max", "S4.fb.missed.max", "S4.fb.bbm.k")
EXCERPT_TOKENS = ("S4.excerpt.iteration", "S4.excerpt.normal", "S4.excerpt.detection", "S4.excerpt.failover",
                  "S4.excerpt.failback")
CAMPAIGN_TOKENS = ("campaign.dates", "monitoring.versions")
HANG_TOKENS = ("hang.n", "hang.session", "hang.detected.k", "hang.ttd.max", "hang.restart_outcome", "hang.action")
DRILL_TOKENS = ("drill.session", "drill.recovery_s", "drill.alert_s", "drill.reaction_s", "drill.commands",
                "baseline.availability")
SOAK_TOKENS = ("soak.session", "soak.fault_offset", "soak.hours", "soak.host_pauses", "soak.kill_outcome",
               "soak.downtime_s", "soak.availability", "soak.availability_blocks")
DERIVED_TOKENS = ("availability.gain_pp", "downtime.reduction_pct", "sens.assumed.gain_pp", "sens.literature.gain_pp")
SAFETY_TOKENS = ("monitor.samples", "dualkey.seconds", "dualkey.indeterminate_seconds", "monitor.unobserved_sessions",
                 "evidence.count", "evidence.final_range", "byzantine.max", "signing.tombstoned",
                 "signing.jailed_until", "S4.dual.iterations", "evidence.heights", "byzantine.sessions")
SNAPSHOT_TOKENS = ("snapshot.iteration", "snapshot.deviation")
NEGATIVE_TOKENS = ("neg.session", "neg.k", "neg.failed", "lock.method", "lock.probe_outcome")
OVERHEAD_TOKENS = ("overhead.session", "overhead.cpu_mean_max", "overhead.rss_max_mb", "overhead.writes_per_minute")
FIGURE_TOKENS = ("S4.fig.iteration", "S4.fig.file", "S4.fig.heights", "S4.fig.last_signed", "S4.fig.first_signed",
                 "S4.fig.signers")
RATE_TOKENS = ("S4.rate.before.mean", "S4.rate.after.mean", "S4.rate.following.mean")
CRITERION_TOKENS = ("crit1.status", "crit2.status", "crit3.status", "crit4.status", "crit5.status")
PARTITION_TOKENS = ("partition.tested", "partition.method", "partition.symptom")
TOKEN_NAMES = (SERIES_BASIC + TTD_TOKENS + TTR_TOKENS + BOUND_TOKENS + PHASE_TOKENS + MISSED_TOKENS + SSR_TOKENS
               + LOG_COUNT_TOKENS + UNINTENDED_TOKENS + FAILBACK_TOKENS + EXCERPT_TOKENS + CAMPAIGN_TOKENS
               + HANG_TOKENS + DRILL_TOKENS + SOAK_TOKENS + DERIVED_TOKENS + SAFETY_TOKENS + SNAPSHOT_TOKENS
               + NEGATIVE_TOKENS + OVERHEAD_TOKENS + FIGURE_TOKENS + RATE_TOKENS + CRITERION_TOKENS
               + PARTITION_TOKENS)
EXTRA_TOKEN_NAMES = ("S4.series_aborted", "S4.series_aborted.reason", "hang.not_injected", "partition.outcomes")
SUMMARY_ATTRIBUTES = {"min": "minimum", "max": "maximum", "mean": "mean", "sd": "sd", "ci_low": "ci_low",
                      "ci_high": "ci_high"}


def sec(value: float) -> str:
    """Seconds to one decimal."""
    return format_id(value, 1)


def pct(value: float) -> str:
    """A percentage value to two decimals."""
    return format_id(value, 2)


def count(value: float) -> str:
    """A whole number with a thousands dot."""
    return format_id(value, 0)


def average(values: list) -> float:
    """Arithmetic mean of a non-empty list."""
    return sum(values) / len(values)


def mean_of(rows: list, field: str) -> float | None:
    """Mean of the rows' non-empty values, or None when there are none."""
    values = [row[field] for row in rows if row[field] is not None]
    return average(values) if values else None


def format_hours(hours: float) -> str:
    """Hours to one decimal, written as a whole number when the decimal is zero."""
    rounded = round(hours, 1)
    return str(int(rounded)) if rounded == int(rounded) else format_id(rounded, 1)


def campaign_dates(start_locals: list) -> str:
    """First and last date of the sessions' local start times in Indonesian, such as '6 sampai 9 Oktober 2026'."""
    dates = sorted(tuple(int(part) for part in text[:10].split("-")) for text in start_locals)
    (first_y, first_m, first_d), (last_y, last_m, last_d) = dates[0], dates[-1]
    first = f"{first_d} {MONTHS[first_m - 1]} {first_y}"
    last = f"{last_d} {MONTHS[last_m - 1]} {last_y}"
    if first == last:
        return first
    if (first_y, first_m) == (last_y, last_m):
        return f"{first_d} sampai {last}"
    if first_y == last_y:
        return f"{first_d} {MONTHS[first_m - 1]} sampai {last}"
    return f"{first} sampai {last}"


class Sink:
    """Collects token values and the reasons tokens were not computed."""

    def __init__(self):
        """Start empty."""
        self.values = {}
        self.uncomputed = {}
        self.extras = {}

    def put(self, name: str, value: str) -> None:
        """Record a computed token."""
        self.values[name] = value

    def put_extra(self, name: str, value: str) -> None:
        """Record a harness extra: a value the draft does not define as a token."""
        self.extras[name] = value

    def put_formatted(self, name: str, value, formatter, reason: str) -> None:
        """Record formatter(value), or the reason when the value is None."""
        if value is None:
            self.skip(name, reason)
        else:
            self.put(name, formatter(value))

    def skip(self, names, reason: str) -> None:
        """Record tokens that could not be computed, each with the reason."""
        for name in ([names] if isinstance(names, str) else names):
            self.uncomputed[name] = reason

    def skip_unset(self, names, reason: str) -> None:
        """Record the reason for every name of a group that did not get a value."""
        self.skip([name for name in names if name not in self.values and name not in self.uncomputed], reason)


@dataclass(frozen=True)
class Roles:
    """The sessions that play each part of the campaign (None when no session does)."""

    series: object
    hang: object
    partition: object
    drill: object
    soak: object
    security: object
    lock: object
    overhead: object
    safety: list


def last_with(sessions: list, condition):
    """The last session for which condition(session) is true, or None."""
    chosen = [session for session in sessions if condition(session)]
    return chosen[-1] if chosen else None


def find_roles(sessions: list) -> Roles:
    """Assign sessions to roles by what they contain, with the same rules as the report: the series is report.series_session."""
    return Roles(
        series=series_session(sessions),
        hang=last_with(sessions, lambda session: bool(session.analysis["hang"])),
        partition=last_with(sessions, lambda session: bool(session.analysis.get("partition"))),
        drill=last_with_result(sessions, "drill"),
        soak=last_with_result(sessions, "soak"),
        security=last_with_result(sessions, "security_negatives"),
        lock=last_with_result(sessions, "lock_negative"),
        overhead=last_with_result(sessions, "overhead"),
        safety=[session for session in sessions if "safety" in session.optional],
    )


def session_id(session) -> str | None:
    """A session's id, or None for no session."""
    return session.metadata["session"] if session is not None else None


def roles_summary(roles: Roles) -> dict:
    """Which session played which role, for values.json."""
    return {"S4": session_id(roles.series), "hang": session_id(roles.hang),
            "partition": session_id(roles.partition), "drill": session_id(roles.drill),
            "soak": session_id(roles.soak), "security": session_id(roles.security), "lock": session_id(roles.lock),
            "overhead": session_id(roles.overhead), "safety": [session_id(session) for session in roles.safety]}


def events_of(session) -> list:
    """The session's events.jsonl rows."""
    return read_events(session.directory / "events.jsonl")


def session_end_ms(session, events: list) -> int:
    """The last instant the session recorded: its safety.json time, else its last event."""
    times = [row["t_ms"] for row in events]
    if "safety" in session.optional:
        times.append(session.optional["safety"]["t_ms"])
    return max(times, default=session.metadata["start_ms"])


def session_log_events(session) -> list:
    """Syncguard log events of a session: the ones in events.jsonl plus the session's share of the copied logs, deduplicated."""
    rows = events_of(session)
    found = [event_from_dict(row) for row in rows if row["kind"] == "syncguard"]
    start, end = session.metadata["start_ms"], session_end_ms(session, rows)
    for node, name in LIVE_LOGS:
        path = session.directory / "logs" / name
        if path.exists():
            found += parse_log_file(path, node, session.metadata["host_utc_offset_seconds"], start, end)
    unique = {}
    for event in found:
        unique.setdefault((event.node, event.t_ms, event.marker), event)
    return sorted(unique.values(), key=lambda event: event.t_ms)


def failover_windows(rows: list) -> dict:
    """Run window (start_ms, end_ms) of every failover iteration, by iteration, from analysis.run_windows."""
    return {window["iteration"]: (window["start_ms"], window["end_ms"])
            for window in run_windows(rows) if window["scenario"] == "failover"}


def unintended_detections(session) -> list:
    """Max-failures events that are not an iteration's own detection and that no injected fault explains.

    Each injected fault (a crash, a hang that took effect, or a partition whose disconnect succeeded)
    claims the first detection after it; a hang or partition claims only one inside the 60 s detection wait. Of the rest, one within the class bound
    after a fault command (command time plus 15 s for a crash, 35 s for a hang or partition) is explained.
    """
    rows = events_of(session)
    faults = [item["event"] for item in injected_faults(rows, "failover") if item["followed"]]
    faults += injected_hangs(rows, session.analysis["hang"])
    faults += [item["event"] for item in injected_faults(rows, "partition") if item["followed"]]
    faults.sort(key=lambda fault: fault["t_ms"])
    detections = [event for event in session_log_events(session) if event.marker == "max_failures"]
    claimed = set()
    for fault in faults:
        wait = HANG_WAIT_MS if fault.get("scenario") in WINDOWED_FAULTS else CRASH_WAIT_MS
        own = next((event for event in detections if fault["t_ms"] <= event.t_ms <= fault["t_ms"] + wait
                    and event_key(event) not in claimed), None)
        if own is not None:
            claimed.add(event_key(own))
    clearing = [row for row in rows if row["kind"] in CLEARING_KILLS
                or (row["kind"] == "fault_cleared" and row.get("action") == "kill")]

    def explained(event) -> bool:
        """Whether a fault command or the harness's own clearing kill was issued shortly enough before the event."""
        for fault in faults:
            bound = HANG_BOUND_MS if fault.get("scenario") in WINDOWED_FAULTS else CRASH_BOUND_MS
            if fault["t_ms"] <= event.t_ms <= fault.get("ended_ms", fault["t_ms"]) + bound:
                return True
        return any(kill["t_ms"] <= event.t_ms <= kill["t_ms"] + CRASH_WAIT_MS for kill in clearing)

    return [event for event in detections if event_key(event) not in claimed and not explained(event)]


def injected_hangs(rows: list, hang_results: list) -> list:
    """Hang fault events that took effect: the hang result says so, whatever the pause command's exit code.

    An iteration without a hang result falls back to a pause that exited 0.
    """
    injected = {result["iteration"]: result["fault_injected"] for result in hang_results}
    return [event for event in (row for row in rows if row["kind"] == "fault_injected" and row.get("scenario") == "hang")
            if injected.get(event["iteration"], event.get("exit_code") == 0)]


def event_key(event) -> tuple:
    """Identity of a log event across the two places it can be read from."""
    return event.node, event.t_ms, event.marker


def summary_tokens(sink: Sink, base: str, rows: list, field: str) -> None:
    """The min, max, mean, SD, and interval tokens of one field over one configuration's rows."""
    names = {key: f"{base}.{key}" for key in SUMMARY_ATTRIBUTES}
    if all(row[field] is None for row in rows):
        sink.skip(names.values(), f"no iteration has a value for {field}")
        return
    summary = summarize_field(rows, field)
    for key, attribute in SUMMARY_ATTRIBUTES.items():
        value = getattr(summary, attribute)
        if value is None and summary.n < 2:
            sink.skip(names[key], "needs at least two valid iterations")
        elif value is None:
            sink.skip(names[key], summary.ci_reason)
        else:
            sink.put(names[key], sec(value))


def series_tokens(sink: Sink, session) -> None:
    """Counts, TTD and TTR statistics, phase means, and missed-height tokens of the failover series."""
    rows = session.analysis["failover"]
    metadata = session.metadata
    sink.put("S4.n", count(len(rows)))
    sink.put("S4.syncguard_version", metadata["syncguard_version"])
    images = metadata.get("images") or {}
    if images:
        sink.put("S4.image_ids", "; ".join(f"{image} {image_id}" for image, image_id in images.items()))
    else:
        sink.skip("S4.image_ids", "metadata.json has no image ids")
    sink.put("S4.clock_offset_ms", sec(metadata["clock_offset"]["offset_ms"]))
    summary_tokens(sink, "S4.ttd", rows, "ttd_s")
    summary_tokens(sink, "S4.ttr", rows, "ttr_s")
    valid_ttr = [row for row in rows if row["ttr_s"] is not None]
    sink.put("S4.ttr.n", count(len(valid_ttr)))
    sink.put("S4.ttd.over_bound", count(sum(1 for row in rows if row["ttd_s"] is not None and row["ttd_s"] > TTD_BOUND_S)))
    sink.put("S4.ttr.over_bound", count(sum(1 for row in valid_ttr if row["ttr_s"] > TTR_BOUND_S)))
    for name, field in (("S4.orchestration.mean", "orchestration_s"), ("S4.node_start.mean", "node_start_s")):
        value = mean_of(rows, field)
        sink.put_formatted(name, value, sec, f"no iteration has a value for {field}")
    missed = [row["absent_heights"] for row in rows if row["absent_heights"] is not None]
    if missed:
        sink.put("S4.missed.min", count(min(missed)))
        sink.put("S4.missed.max", count(max(missed)))
        sink.put("S4.missed.mainnet_pct_max", pct(max(missed) / MAINNET_ALLOWANCE_BLOCKS * 100))
    shares = [incident["missed_share"]["share_of_allowance"] for incident in session.analysis["incidents"]
              if incident["missed_share"]]
    if shares:
        sink.put("S4.missed.allowance_pct_max", pct(max(shares) * 100))
    sink.skip_unset(MISSED_TOKENS, "no iteration has an absent-height count")


def failback_tokens(sink: Sink, session, criterion_inputs) -> None:
    """Failback counts and durations, G_fb, the break-before-make count, and the criterion-5 count."""
    rows = session.analysis["failback"]
    sink.put("S4.fb.n", count(len(rows)))
    for name, field, reduce in (("S4.fb.grace.mean", "grace_wait_s", average), ("S4.fb.execution.mean", "execution_s", average),
                                ("S4.fb.execution.max", "execution_s", max), ("S4.gfb.mean", "gfb_s", average),
                                ("S4.gfb.max", "gfb_s", max)):
        values = [row[field] for row in rows if row[field] is not None]
        sink.put_formatted(name, reduce(values) if values else None, sec, f"no failback has a value for {field}")
    missed = [row["absent_heights"] for row in rows if row["absent_heights"] is not None]
    sink.put_formatted("S4.fb.missed.max", max(missed) if missed else None, count, "no failback has a gap")
    sink.put("S4.fb.bbm.k", count(break_before_make_count(session, rows)))
    reason = criterion_inputs.missing_reason
    if reason:
        sink.skip("S4.fb.k", reason)
    else:
        sink.put("S4.fb.k", count(sum(1 for row in rows if criterion_inputs.failback_ok(row))))


def break_before_make_count(session, rows: list) -> int:
    """Failbacks in which the backup's release precedes the primary's restart of its container."""
    events = session_log_events(session)
    total = 0
    for row in rows:
        start, end = row["t_failback_initiated_ms"], row["t_failback_complete_ms"]
        released = row["t_release_complete_ms"]
        if start is None or end is None or released is None:
            continue
        restarts = [event.t_ms for event in events if event.marker == "restarting_container" and event.node == PRIMARY_NODE
                    and start <= event.t_ms <= end]
        if restarts and released < min(restarts):
            total += 1
    return total


@dataclass
class SeriesSafety:
    """What the success-plus-safety and failback criteria need from the series session's safety and monitor files."""

    missing_reason: str | None
    evidence_by_iteration: dict
    samples: list
    windows: dict
    spans: dict
    outcomes: dict
    unintended: list
    mttr: float | None

    def covered(self, start_ms: int, end_ms: int) -> bool:
        """Whether the monitor observed [start_ms, end_ms], by the one coverage rule of monitor.coverage."""
        observed, _ = coverage(self.samples, start_ms, end_ms)
        return observed

    def dual_seconds(self, start_ms: int, end_ms: int) -> int:
        """Distinct seconds in [start_ms, end_ms] in which both nodes reported the validator."""
        inside = [sample for sample in self.samples if start_ms <= sample.t_ms <= end_ms and sample.error is None]
        return dual_signer_seconds(inside, TARGET_VALIDATOR)

    def failback_ok(self, row: dict) -> bool:
        """Criterion 5 of draft C for one failback.

        Needs a monitored failback interval with no dual seconds, no evidence, G_fb within MTTR, and no
        unintended detection anywhere in the iteration (from its start to the next iteration's start).
        """
        start = row["t_failback_initiated_ms"] or row["t_failback_considered_ms"]
        end = row["t_primary_signature_ms"] or row["t_failback_complete_ms"]
        if start is None or end is None or row["gfb_s"] is None or self.mttr is None:
            return False
        if self.evidence_by_iteration.get(row["iteration"]):
            return False
        if not self.covered(start, end) or self.dual_seconds(start, end):
            return False
        span_start, span_end = self.spans[row["iteration"]]
        if any(span_start <= event.t_ms < span_end for event in self.unintended):
            return False
        return row["gfb_s"] <= self.mttr


def iteration_spans(rows: list, session_end: int) -> dict:
    """(start, end) of every iteration: its start event to the next iteration's start, or the session end."""
    starts = sorted((row["t_ms"], row["iteration"]) for row in rows if row["kind"] == "iteration_start")
    ends = [start for start, _ in starts[1:]] + [session_end]
    return {iteration: (start, end) for (start, iteration), end in zip(starts, ends)}


def evidence_by_iteration(session, rows: list) -> dict:
    """Evidence entries of the safety scan assigned to the iteration whose fault height is the latest at or below them."""
    faults = sorted((item["event"]["height"], item["event"]["iteration"]) for item in injected_faults(rows, "failover")
                    if item["followed"] and "height" in item["event"])
    assigned = {}
    for entry in session.optional["safety"]["evidence_scan"]["evidence"]:
        owners = [iteration for height, iteration in faults if height <= entry["height"]]
        if owners:
            assigned.setdefault(owners[-1], []).append(entry)
    return assigned


def series_safety(session) -> SeriesSafety:
    """Collect the inputs of the SSR and failback criteria; missing_reason says what is absent."""
    rows = events_of(session)
    monitor_path = session.directory / "monitor.jsonl"
    reason = None
    if "safety" not in session.optional:
        reason = "the series session has no safety.json (run finish)"
    elif not monitor_path.exists():
        reason = "the series session has no monitor.jsonl"
    return SeriesSafety(
        missing_reason=reason,
        evidence_by_iteration=evidence_by_iteration(session, rows) if "safety" in session.optional else {},
        samples=load_monitor_samples(monitor_path),
        windows=failover_windows(rows),
        spans=iteration_spans(rows, session_end_ms(session, rows)),
        outcomes=iteration_outcomes(rows),
        unintended=unintended_detections(session),
        mttr=mean_of(session.analysis["failover"], "ttr_s"),
    )


def ssr_failures(row: dict, inputs: SeriesSafety) -> list:
    """The success and safety conditions one failover iteration fails, in Indonesian."""
    iteration = row["iteration"]
    failures = []
    outcome = inputs.outcomes.get(iteration)
    if row["ttr_s"] is None:
        failures.append(f"syarat keberhasilan: TTR tidak valid ({translate_reasons(row['invalid_reason']) or 'tanpa tanda tangan setelah kegagalan'})")
    elif outcome is None:
        failures.append("syarat keberhasilan: iterasi tanpa hasil tercatat (dibatalkan oleh kesalahan)")
    elif outcome != "complete":
        failures.append(f"syarat keberhasilan: iterasi berakhir dengan hasil {OUTCOME_LABELS.get(outcome, outcome)}")
    if inputs.evidence_by_iteration.get(iteration):
        failures.append("syarat keselamatan: bukti equivocation pada pemindaian maju")
    start, end = inputs.windows[iteration]
    if not inputs.covered(start, end):
        failures.append("syarat keselamatan: tidak teramati (monitor tidak mencakup seluruh jendela iterasi)")
    else:
        dual = inputs.dual_seconds(start, end)
        if dual:
            failures.append(f"syarat keselamatan: {dual} detik kedua node memuat 48DC2183")
    return failures


def ssr_tokens(sink: Sink, session, inputs: SeriesSafety) -> None:
    """Success-plus-safety counts and the failing iterations with their conditions."""
    if inputs.missing_reason:
        sink.skip(SSR_TOKENS, inputs.missing_reason)
        return
    rows = session.analysis["failover"]
    failed = [(row["iteration"], ssr_failures(row, inputs)) for row in rows]
    passed = sum(1 for _, failures in failed if not failures)
    sink.put("S4.ssr.k", count(passed))
    sink.put("S4.ssr.pct", pct(passed / len(rows) * 100))
    sink.put("S4.ssr.failed", "; ".join(f"iterasi {iteration} ({'; '.join(failures)})"
                                        for iteration, failures in failed if failures))


def log_count_tokens(sink: Sink, session, all_sessions: list) -> None:
    """Fail-closed iterations, refusal lines, and detections that no injected fault explains."""
    events = session_log_events(session)
    refused = [event for event in events if event.marker in ("transition_refused", "failover_aborted")]
    sink.put("S4.refusals.count", count(len(refused)))
    without_takeover = {row["iteration"] for row in session.analysis["failover"]
                        if not took_over(row["t_takeover_ms"], row["invalid_reason"])}
    closed = set()
    for row in events_of(session):
        if row["kind"] != "syncguard" or row.get("iteration") not in without_takeover:
            continue
        refusal_on_backup = row["marker"] == "transition_refused" and row["node"] == BACKUP_NODE
        if row["marker"] in FAIL_CLOSED_MARKERS or refusal_on_backup:
            closed.add(row["iteration"])
    sink.put("S4.failclosed.count", count(len(closed)))
    sink.put("S4.unintended_failover.count", count(sum(len(unintended_detections(item)) for item in all_sessions)))


def excerpt_tokens(sink: Sink, session) -> None:
    """Verbatim log excerpts of the first iteration whose detection, failover, and failback can all be cut."""
    lines = raw_log_lines(session.directory, session.metadata["host_utc_offset_seconds"], events_of(session))
    failbacks = {row["iteration"]: row for row in session.analysis["failback"]}
    rows = events_of(session)
    for row in session.analysis["failover"]:
        failback = failbacks.get(row["iteration"])
        if failback is None:
            continue
        excerpts = excerpt_for(row, failback, lines, iteration_log_events(rows, row["iteration"]))
        if excerpts is None:
            continue
        sink.put("S4.excerpt.iteration", str(row["iteration"]))
        sink.put("S4.excerpt.detection", excerpts["detection"])
        sink.put("S4.excerpt.failover", excerpts["failover"])
        sink.put("S4.excerpt.failback", excerpts["failback"])
        active = last_health_line(lines, PRIMARY_NODE, "active", row["t_fault_ms"])
        backup = last_health_line(lines, BACKUP_NODE, "passive", row["t_fault_ms"])
        if active and backup:
            sink.put("S4.excerpt.normal", "\n".join(text for *_, text in sorted([active, backup])))
        else:
            sink.skip("S4.excerpt.normal", "the logs have no [active] and [passive] health line before t_gagal")
        return
    sink.skip(EXCERPT_TOKENS, "no iteration has the detection, failover, and failback lines in the copied logs")


def hang_tokens(sink: Sink, session) -> None:
    """Hang counts over the pauses that took effect, the largest TTD of the detected ones, and Indonesian words.

    A pause that never took effect (fault_injected false) is not a hang iteration; it is listed in the
    hang.not_injected extra with its reason.
    """
    all_rows = session.analysis["hang"]
    rows = [row for row in all_rows if row["fault_injected"]]
    sink.put_extra("hang.not_injected", "; ".join(f"iterasi {row['iteration']}: {row['reason']}"
                                                   for row in all_rows if not row["fault_injected"]))
    if not rows:
        sink.skip(HANG_TOKENS, "no hang iteration took effect (every pause command failed)")
        return
    sink.put("hang.n", count(len(rows)))
    sink.put("hang.session", f"Sesi {session_id(session)}")
    detected = [row for row in rows if row["detected"]]
    sink.put("hang.detected.k", count(len(detected)))
    ttd = [row["ttd_s"] for row in detected if row["ttd_s"] is not None]
    sink.put_formatted("hang.ttd.max", max(ttd) if ttd else None, sec, "no detected hang iteration has a TTD")
    sink.put("hang.restart_outcome", per_iteration_words(rows, "restart_outcome", RESTART_LABELS))
    sink.put("hang.action", per_iteration_words(rows, "action", ACTION_LABELS))


def partition_method(command: str) -> str:
    """The method sentence built from the recorded argv, such as 'docker network disconnect NET validator1-node'."""
    words = command.split()
    return f"pemutusan jaringan Docker kontainer {words[-1]} ({' '.join(words[:3])})"


def partition_session_phrase(session_ids: list) -> str:
    """'diuji pada Sesi S7', 'diuji pada Sesi S7 dan S8', or 'diuji pada Sesi S6, S7, dan S8'."""
    if len(session_ids) == 1:
        return f"diuji pada Sesi {session_ids[0]}"
    return f"diuji pada Sesi {', '.join(session_ids[:-1])}{',' if len(session_ids) > 2 else ''} dan {session_ids[-1]}"


def symptom_phrase(rows: list) -> str | None:
    """Each detection symptom in Indonesian with how many of the runs showed it, or None when no run was detected."""
    shown = [row["detection_symptom"] for row in rows if row.get("detection_symptom")]
    counts = {symptom: shown.count(symptom) for symptom in dict.fromkeys(shown)}
    if not counts:
        return None
    return "; ".join(f"{SYMPTOM_LABELS.get(symptom, symptom)} ({count} dari {len(rows)} percobaan)"
                     for symptom, count in counts.items())


def partition_tokens(sink: Sink, sessions: list) -> None:
    """The partition tokens over the runs whose disconnect took effect, plus every run's outcome as an extra.

    partition.tested names the sessions whatever the outcomes were, and reads tidak dijalankan only when
    every disconnect command failed; partition.method comes from the recorded argv; partition.symptom
    counts how syncguard saw the partition.
    """
    pairs = [(session_id(session), row) for session in sessions for row in session.analysis.get("partition", [])]
    if not pairs:
        sink.skip(PARTITION_TOKENS, "no session has partition results")
        return
    sink.put_extra("partition.outcomes", "; ".join(
        f"iterasi {row['iteration']}: {PARTITION_OUTCOME_LABELS.get(row['outcome'], row['outcome'])}"
        for _, row in pairs))
    effective = [(sid, row) for sid, row in pairs if row["fault_exit_code"] == 0]
    if not effective:
        sink.put("partition.tested", "tidak dijalankan")
        sink.skip(("partition.method", "partition.symptom"), "no partition disconnect took effect")
        return
    sink.put("partition.tested", partition_session_phrase(list(dict.fromkeys(sid for sid, _ in effective))))
    rows = [row for _, row in effective]
    sink.put("partition.method", partition_method(rows[0]["command"]))
    sink.put_formatted("partition.symptom", symptom_phrase(rows), lambda text: text, "no partition run was detected")


def per_iteration_words(rows: list, field: str, words: dict) -> str:
    """One Indonesian phrase when every iteration agrees, otherwise the phrase of each iteration."""
    phrases = [(row["iteration"], words.get(row[field], str(row[field]))) for row in rows]
    if len({phrase for _, phrase in phrases}) == 1:
        return phrases[0][1]
    return "; ".join(f"iterasi {iteration}: {phrase}" for iteration, phrase in phrases)


@dataclass(frozen=True)
class Baselines:
    """Measured values the derived tokens combine: drill recovery, soak availability, and the mean TTR (None when absent)."""

    recovery_s: float | None
    soak_percent: float | None
    mean_ttr: float | None
    soak_reason: str = "there is no soak availability"

    @property
    def baseline_percent(self) -> float | None:
        """Availability of the drill baseline on the 24 h reference window, in percent."""
        if self.recovery_s is None:
            return None
        return (REFERENCE_WINDOW_S - self.recovery_s) / REFERENCE_WINDOW_S * 100


def drill_tokens(sink: Sink, session) -> float | None:
    """Drill tokens from a recovered drill; returns the recovery time, or None when the drill measured none."""
    drill = session.optional["drill"]
    sink.put("drill.session", session_id(session))
    if drill.get("recovery_s") is None:
        sink.skip(("drill.recovery_s", "baseline.availability"), f"the drill ended with outcome {drill['outcome']} and no recovery time")
        sink.skip(("drill.alert_s", "drill.reaction_s", "drill.commands"), f"the drill ended with outcome {drill['outcome']}")
        return None
    sink.put("drill.recovery_s", sec(drill["recovery_s"]))
    sink.put("drill.commands", count(len(drill["commands"])))
    if drill["alert_observed"]:
        sink.put("drill.alert_s", sec(drill["alert_s"]))
        sink.put("drill.reaction_s", sec(drill["reaction_s"]))
    else:
        sink.skip(("drill.alert_s", "drill.reaction_s"), "the drill saw no missed height, so the alert was not observed")
    sink.put("baseline.availability", pct((REFERENCE_WINDOW_S - drill["recovery_s"]) / REFERENCE_WINDOW_S * 100))
    return drill["recovery_s"]


def soak_tokens(sink: Sink, session) -> float | None:
    """Soak tokens from analysis.json's soak block; returns the time-based availability in percent, or None."""
    soak = session.optional["soak"]
    timed = session.analysis.get("soak")
    sink.put("soak.session", session_id(session))
    sink.put("soak.hours", format_hours((soak["end_ms"] - soak["start_ms"]) / 3_600_000))
    sink.put("soak.host_pauses", count(soak["host_pauses"]))
    sink.put("soak.kill_outcome", OUTCOME_LABELS.get(soak["kill_outcome"], soak["kill_outcome"]))
    starts = [row for row in events_of(session) if row["kind"] == "soak_start"]
    if starts:
        sink.put("soak.fault_offset", f"{format_hours(starts[-1]['kill_offset_hours'])} jam")
    else:
        sink.skip("soak.fault_offset", "events.jsonl has no soak_start event")
    if timed is None:
        sink.skip(("soak.downtime_s", "soak.availability", "soak.availability_blocks"),
                  "soak.json has no collected heights, so availability was not computed")
        return None
    sink.put("soak.downtime_s", sec(timed["downtime_s"]))
    sink.put("soak.availability", pct(timed["availability"] * 100))
    sink.put("soak.availability_blocks", pct(timed["availability_blocks"] * 100))
    return timed["availability"] * 100


def derived_tokens(sink: Sink, known: Baselines) -> None:
    """Tokens that combine the drill, the soak, and the mean TTR."""
    soak = known.soak_percent
    if soak is None:
        sink.skip(DERIVED_TOKENS, known.soak_reason)
        return
    sink.put("sens.assumed.gain_pp", pct(soak - (REFERENCE_WINDOW_S - ASSUMED_MANUAL_RECOVERY_S) / REFERENCE_WINDOW_S * 100))
    sink.put("sens.literature.gain_pp", pct(soak - LITERATURE_AVAILABILITY * 100))
    if known.recovery_s is None:
        sink.skip(("availability.gain_pp", "downtime.reduction_pct"), "there is no drill recovery time")
        return
    sink.put("availability.gain_pp", pct(soak - known.baseline_percent))
    if known.mean_ttr is None:
        sink.skip("downtime.reduction_pct", "the failover series has no valid TTR")
    else:
        sink.put("downtime.reduction_pct", pct((known.recovery_s - known.mean_ttr) / known.recovery_s * 100))


def latest_safety(roles: Roles) -> dict:
    """The safety.json with the latest finish time."""
    return max((session.optional["safety"] for session in roles.safety), key=lambda safety: safety["t_ms"])


def dual_iterations(roles: Roles) -> str | None:
    """Session and iteration ids whose run window holds dual-holder seconds; None when a safety session has no monitor.jsonl."""
    found = []
    for session in roles.safety:
        path = session.directory / "monitor.jsonl"
        if not path.exists():
            return None
        samples = [sample for sample in load_monitor_samples(path) if sample.error is None]
        windows = run_windows(events_of(session))
        for window in windows:
            inside = [sample for sample in samples if window["start_ms"] <= sample.t_ms <= window["end_ms"]]
            if dual_signer_seconds(inside, TARGET_VALIDATOR):
                found.append(f"{session_id(session)} {run_name(window)}")
        outside = [sample for sample in samples
                   if not any(window["start_ms"] <= sample.t_ms <= window["end_ms"] for window in windows)]
        if dual_signer_seconds(outside, TARGET_VALIDATOR):
            found.append(f"{session_id(session)} di luar iterasi")
    return ", ".join(dict.fromkeys(found))


def run_name(window: dict) -> str:
    """'iterasi k' for a run with an iteration number, otherwise the Indonesian scenario name (the soak)."""
    if window["iteration"] is not None:
        return f"iterasi {window['iteration']}"
    return SCENARIO_LABELS.get(window["scenario"], window["scenario"])


def safety_tokens(sink: Sink, roles: Roles) -> None:
    """Monitor, evidence, Byzantine, and signing-info tokens over every session that has a safety.json."""
    if not roles.safety:
        sink.skip(SAFETY_TOKENS, "no session has a safety.json (run finish)")
        return
    safeties = [session.optional["safety"] for session in roles.safety]
    monitors = [safety["monitor"] for safety in safeties]
    sink.put("monitor.samples", count(sum(monitor["samples"] for monitor in monitors)))
    for name, field in (("dualkey.seconds", "dual_signer_seconds"), ("dualkey.indeterminate_seconds", "indeterminate_seconds")):
        values = [monitor[field] for monitor in monitors]
        if None in values:
            sink.skip(name, "a monitor produced no samples, so the count is not defined")
        else:
            sink.put(name, count(sum(values)))
    sink.put("monitor.unobserved_sessions", "; ".join(
        f"{safety['session']} ({monitor_problems_text(safety['monitor']['observed_problems'])})"
        for safety in safeties if not safety["monitor"]["observed"]))
    sink.put("evidence.count", count(sum(item["count"] for safety in safeties for item in safety["evidence_scan"]["evidence"])))
    latest = latest_safety(roles)
    scan = latest["evidence_scan"]
    sink.put("evidence.final_range", f"{count(scan['first_height'])} sampai {count(scan['last_height'])}")
    maxima = [byzantine_maximum(safety["prometheus"]) for safety in safeties]
    available = [value for value in maxima if value is not None]
    sink.put("byzantine.max", count(max(available)) if available else "tidak tersedia")
    sink.put("byzantine.sessions", ", ".join(safety["session"] for safety, value in zip(safeties, maxima) if value))
    sink.put("signing.tombstoned", "true" if latest["signing_info"]["tombstoned"] else "false")
    sink.put("signing.jailed_until", latest["signing_info"]["jailed_until"])
    sink.put("evidence.heights", "; ".join(
        f"{safety['session']}: {item['height']} ({', '.join(item['types'])})"
        for safety in safeties for item in safety["evidence_scan"]["evidence"]))
    dual = dual_iterations(roles)
    sink.put_formatted("S4.dual.iterations", dual, str, "a safety session has no monitor.jsonl")


def snapshot_tokens(sink: Sink, session) -> None:
    """The first iteration with all three snapshot points, and its deviations from the expected layout."""
    by_iteration = {}
    for snapshot in session.snapshots:
        by_iteration.setdefault(snapshot["iteration"], {})[snapshot["point"]] = snapshot
    complete = [iteration for iteration, points in sorted(by_iteration.items()) if all(p in points for p in SNAPSHOT_POINTS)]
    if not complete:
        sink.skip(SNAPSHOT_TOKENS, "no iteration of the series has all three snapshot points")
        return
    sink.put("snapshot.iteration", str(complete[0]))
    sink.put("snapshot.deviation", "; ".join(layout_deviations(complete[0], by_iteration[complete[0]])))


def negative_tokens(sink: Sink, roles: Roles) -> None:
    """Security and lock results of the negative tests."""
    holder = roles.security or roles.lock
    if holder is None:
        sink.skip("neg.session", "no session has security_negatives.json or lock_negative.json")
    else:
        sink.put("neg.session", session_id(holder))
    if roles.security is None:
        sink.skip(("neg.k", "neg.failed"), "no session has security_negatives.json")
    else:
        rows = [row for row in roles.security.optional["security_negatives"]["rows"] if 1 <= row["number"] <= 7]
        sink.put("neg.k", count(sum(1 for row in rows if row["status"] == "LULUS")))
        sink.put("neg.failed", ", ".join(str(row["number"]) for row in rows if row["status"] != "LULUS"))
    if roles.lock is None:
        sink.skip(("lock.method", "lock.probe_outcome"), "no session has lock_negative.json")
        return
    lock = roles.lock.optional["lock_negative"]
    sink.put("lock.method", " dan ".join(METHOD_LABELS.get(method, method) for method in lock["methods"]))
    outcome = lock["probe"].get("outcome", "unparsed")
    sink.put("lock.probe_outcome", PROBE_LABELS.get(outcome, outcome))


def overhead_tokens(sink: Sink, session) -> None:
    """Overhead tokens from a valid run; an invalid run is explained."""
    overhead = session.optional["overhead"]
    if not overhead["valid"] or not overhead["nodes"]:
        sink.skip(OVERHEAD_TOKENS, "the overhead run is invalid (missing node samples or a PID change); see tabel-overhead")
        return
    nodes = overhead["nodes"].values()
    sink.put("overhead.session", session_id(session))
    sink.put("overhead.cpu_mean_max", pct(max(node["cpu_percent"]["mean"] for node in nodes)))
    sink.put("overhead.rss_max_mb", sec(max(node["rss_kb"]["maximum"] for node in nodes) / 1024))
    sink.put("overhead.writes_per_minute", pct(overhead["passive_state_writes_per_minute"]))


def figure_tokens(sink: Sink, session) -> None:
    """The plotted iteration and figure file names."""
    sink.put("S4.fig.signers", signer_figure_name(session_id(session)))
    plotted = plotted_failover(session)
    if plotted is None:
        sink.skip(("S4.fig.iteration", "S4.fig.file", "S4.fig.heights", "S4.fig.last_signed", "S4.fig.first_signed"),
                  "no failover of the series has a first signature and stored heights")
        return
    row, series = plotted
    sink.put("S4.fig.iteration", str(row["iteration"]))
    sink.put("S4.fig.file", failover_figure_name(session_id(session), row["iteration"]))
    sink.put("S4.fig.heights", f"{count(series.first_height)} sampai {count(series.last_height)}")
    sink.put("S4.fig.last_signed", count(row["last_signed_height"]))
    sink.put("S4.fig.first_signed", count(row["first_signature_height"]))


def rate_tokens(sink: Sink, session) -> None:
    """Mean blocks per minute in each window over the incidents whose window has complete data."""
    for token, label in (("S4.rate.before.mean", "before_fault_60s"), ("S4.rate.after.mean", "after_recovery_60s"),
                         ("S4.rate.following.mean", "following_120s")):
        rates = [window["blocks_per_minute"] for incident in session.analysis["incidents"]
                 for window in incident["block_rates"] if window["label"] == label and window["covered"]]
        sink.put_formatted(token, sum(rates) / len(rates) if rates else None, pct,
                           f"no incident has complete data for {label}")


def jailed_before(info: dict, finished_ms: int) -> bool:
    """Whether jailed_until is zero (1970) or at or before the safety scan's finish time."""
    text = info["jailed_until"]
    return not text or parse_rfc3339_ms(text.replace("Z", "+00:00")) <= finished_ms


def criterion_tokens(sink: Sink, session, roles: Roles, known: Baselines) -> None:
    """Criteria 1 to 5 as Terpenuhi or Tidak terpenuhi, by the rules of COND-D-02, 03, 16, 19, and 20."""
    rows = session.analysis["failover"] if session is not None else []

    def verdict(met: bool) -> str:
        """The status word."""
        return "Terpenuhi" if met else "Tidak terpenuhi"

    for name, field, bound in (("crit1.status", "ttd_s", TTD_BOUND_S), ("crit2.status", "ttr_s", TTR_BOUND_S)):
        values = [row[field] for row in rows if row[field] is not None]
        if not values:
            sink.skip(name, f"no iteration has a value for {field}")
        else:
            all_measured = len(values) == len(rows)  # a missing measurement cannot be shown to meet the bound
            sink.put(name, verdict(all_measured and max(values) <= bound and sum(values) / len(values) <= bound))
    if known.soak_percent is None or known.baseline_percent is None:
        sink.skip("crit3.status", known.soak_reason if known.soak_percent is None else "needs the drill baseline")
    else:
        sink.put("crit3.status", verdict(known.soak_percent >= SOAK_AVAILABILITY_MINIMUM_PERCENT
                                        and known.soak_percent > known.baseline_percent))
    ssr_k, fb_k = sink.values.get("S4.ssr.k"), sink.values.get("S4.fb.k")
    if ssr_k is None:
        sink.skip("crit4.status", "S4.ssr.k was not computed")
    else:
        sink.put("crit4.status", verdict(ssr_k == sink.values["S4.n"]))
    if fb_k is None or not roles.safety:
        sink.skip("crit5.status", "needs S4.fb.k and a safety.json for the signing info")
    else:
        latest = latest_safety(roles)
        sink.put("crit5.status", verdict(fb_k == sink.values["S4.fb.n"] and jailed_before(latest["signing_info"], latest["t_ms"])))


def missing_measurement_text(row: dict) -> str | None:
    """'iterasi N: TTD dan TTR tidak terukur (alasan)' for a failover row lacking a measurement, else None."""
    missing = [label for label, field in (("TTD", "ttd_s"), ("TTR", "ttr_s")) if row[field] is None]
    if not missing:
        return None
    reason = f" ({translate_reasons(row['invalid_reason'])})" if row["invalid_reason"] else ""
    return f"iterasi {row['iteration']}: {' dan '.join(missing)} tidak terukur{reason}"


def missing_measurement_extras(sink: Sink, session) -> None:
    """Harness extras: how many followed iterations lack a TTD or TTR, and why, for the criteria's conditional text."""
    rows = session.analysis["failover"] if session is not None else []
    texts = [text for text in (missing_measurement_text(row) for row in rows) if text is not None]
    sink.put_extra("S4.missing_measurements", str(len(texts)))
    sink.put_extra("S4.missing_measurements.reason", "; ".join(texts))


def series_abort_extras(sink: Sink, session) -> None:
    """Harness extras: whether the failover series stopped early, and after which iteration with what outcome."""
    stops = session.analysis.get("series_aborted", []) if session is not None else []
    sink.put_extra("S4.series_aborted", "Ya" if stops else "Tidak")
    sink.put_extra("S4.series_aborted.reason", "; ".join(
        f"setelah iterasi {stop['iteration']}: {OUTCOME_LABELS.get(stop['outcome'], stop['outcome'])}" for stop in stops))


def compute_values(sessions: list) -> dict:
    """Every draft-D token the sessions allow, the roles that produced them, and the reasons for the rest."""
    roles = find_roles(sessions)
    sink = Sink()
    sink.put_formatted("campaign.dates", [session.metadata["start_local"] for session in sessions] or None,
                       campaign_dates, "no sessions were given")
    sink.skip("monitoring.versions", "Prometheus, Grafana, Loki and Promtail versions are not collected by the harness")
    partition_tokens(sink, sessions)
    series = roles.series
    if series is None:
        sink.skip_unset(SERIES_BASIC + TTD_TOKENS + TTR_TOKENS + BOUND_TOKENS + PHASE_TOKENS + MISSED_TOKENS + SSR_TOKENS
                        + LOG_COUNT_TOKENS + UNINTENDED_TOKENS + FAILBACK_TOKENS + EXCERPT_TOKENS + SNAPSHOT_TOKENS
                        + FIGURE_TOKENS + RATE_TOKENS + ("S4.dual.iterations",), "no session holds a failover series")
    else:
        series_tokens(sink, series)
        inputs = series_safety(series)
        ssr_tokens(sink, series, inputs)
        log_count_tokens(sink, series, sessions)
        failback_tokens(sink, series, inputs)
        excerpt_tokens(sink, series)
        snapshot_tokens(sink, series)
        figure_tokens(sink, series)
        rate_tokens(sink, series)
    if roles.hang is None:
        sink.skip(HANG_TOKENS, "no session has hang results")
    else:
        hang_tokens(sink, roles.hang)
    recovery_s = None
    if roles.drill is None:
        sink.skip(DRILL_TOKENS, "no session has drill.json")
    else:
        recovery_s = drill_tokens(sink, roles.drill)
    soak_percent, soak_reason = None, "there is no soak availability"
    if roles.soak is None:
        sink.skip(SOAK_TOKENS, "no session has soak.json")
    else:
        soak_percent = soak_tokens(sink, roles.soak)
        if soak_percent is not None and not soak_kill_completed(roles.soak):
            soak_reason = (f"the soak's failover ended with outcome {roles.soak.optional['soak']['kill_outcome']}, "
                           "so no complete failover was measured")
            soak_percent = None
    known = Baselines(recovery_s, soak_percent, mean_valid_ttr(series.analysis["failover"])[0] if series else None,
                      soak_reason)
    derived_tokens(sink, known)
    safety_tokens(sink, roles)
    negative_tokens(sink, roles)
    if roles.overhead is None:
        sink.skip(OVERHEAD_TOKENS, "no session has overhead.json")
    else:
        overhead_tokens(sink, roles.overhead)
    criterion_tokens(sink, series, roles, known)
    sink.skip_unset(TOKEN_NAMES, "no rule produced a value for this token")
    series_abort_extras(sink, series)
    missing_measurement_extras(sink, series)
    sink.extras.setdefault("hang.not_injected", "")
    sink.extras.setdefault("partition.outcomes", "")
    return {"values": sink.values, "roles": roles_summary(roles), "uncomputed": sink.uncomputed, "extras": sink.extras}


def write_values(sessions: list, out_dir: Path) -> Path:
    """Write values.json (values, roles, uncomputed reasons) into out_dir and return its path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "values.json"
    write_json(path, compute_values(sessions))
    return path
