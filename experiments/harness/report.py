"""Manuscript tables and figures from analyzed sessions (R10).

Each table is written as name.csv (raw values: epoch ms, dot decimals, full
addresses) and name.md (Indonesian number formatting, local clock times).
Tables without data are skipped and listed in index.md.
"""

import csv
import json
from collections import Counter
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Callable

from harness.analysis import monitor_gap_totals
from harness.clock import format_clock, format_local
from harness.constants import BACKUP_NODE, TARGET_VALIDATOR
from harness.figures import FAILOVER_NOTE, failover_series, plot_failover, plot_signer_counts
from harness.jsonio import read_json, read_jsonl
from harness.labels import (
    ACTION_LABELS,
    METHOD_LABELS,
    HANG_FOLLOWUP_LABELS,
    HANG_OUTCOME_LABELS,
    OUTCOME_LABELS,
    PARTITION_OUTCOME_LABELS,
    PORT_REPAIR_LABELS,
    PROBE_LABELS,
    RESTART_LABELS,
    SYMPTOM_LABELS,
    UNOBSERVED_INTERVALS_HEADER,
    UNOBSERVED_VOTES_HEADER,
    label_of,
    monitor_problems_text,
    translate_reasons,
    unobserved_intervals_text,
)
from harness.metrics import FailoverIteration, summarize_field
from harness.numfmt import MISSING, format_id, format_percent
from harness.signing import load_heights, shift_to_host_clock, signer_counts

# Baselines of the manuscript under revision (docs/thesis/telaah2-traceability.md, ruling D3): the internal
# 1.800 s manual-recovery assumption and the 99,1% literature availability row. Both are shown beside the
# measured drill in the sensitivity table, on the 24 h reference window of equation 3.4.
ASSUMED_MANUAL_RECOVERY_S = 1800.0
LITERATURE_AVAILABILITY = 0.991
REFERENCE_WINDOW_S = 86_400
LITERATURE_DOWNTIME_S = round((1 - LITERATURE_AVAILABILITY) * REFERENCE_WINDOW_S, 1)
NOT_OBSERVED = "tidak teramati"
NOT_AVAILABLE = "tidak tersedia"
NOT_RECORDED = "tidak dicatat"  # a result written before the runner recorded the field
OPTIONAL_FILES = ("safety", "security_negatives", "lock_negative", "overhead", "drill", "soak")
SOURCE_LABELS = {
    "fault": "gagal",
    "first_failed_check": "cek gagal pertama",
    "detect": "deteksi",
    "takeover": "alih peran",
    "first_signature": "tanda tangan pertama",
    "primary_healthy": "cek sehat primer",
    "failback_considered": "failback dipertimbangkan",
    "failback_initiated": "failback dimulai",
    "failback_complete": "failback selesai",
    "release_complete": "cadangan lepas",
    "failback_notified": "cadangan menerima notifikasi failback",
    "primary_signature": "tanda tangan primer",
}
SOURCE_TIME_FIELDS = {
    "fault": "t_fault_ms",
    "first_failed_check": "t_first_failed_check_ms",
    "detect": "t_detect_ms",
    "takeover": "t_takeover_ms",
    "first_signature": "t_first_signature_ms",
    "primary_healthy": "t_primary_healthy_ms",
    "failback_considered": "t_failback_considered_ms",
    "failback_initiated": "t_failback_initiated_ms",
    "failback_notified": "t_failback_notified_ms",
    "release_complete": "t_release_complete_ms",
    "failback_complete": "t_failback_complete_ms",
    "primary_signature": "t_primary_signature_ms",
}
WINDOW_LABELS = {
    "before_fault_60s": "60 s sebelum t_gagal",
    "after_recovery_60s": "60 s pertama setelah t_aktif_cadangan",
    "following_120s": "120 s berikutnya",
}
FAILOVER_METRICS = (("ttd_s", "TTD"), ("ttr_s", "TTR"))
FAILBACK_METRICS = (
    ("grace_wait_s", "Tunggu grace"),
    ("to_complete_s", "Ke failback selesai"),
    ("to_signature_s", "Ke tanda tangan primer"),
    ("execution_s", "Eksekusi failback"),
    ("gfb_s", "G_fb"),
)


def plain(value, row) -> str:
    """Text as is; empty becomes MISSING."""
    return MISSING if value is None or value == "" else str(value)


def seconds(value, row) -> str:
    """Seconds with three decimals."""
    return format_id(value, 3)


def whole(value, row) -> str:
    """A whole number with a thousands dot."""
    return format_id(value, 0)


def decimal1(value, row) -> str:
    """One decimal."""
    return format_id(value, 1)


def decimal2(value, row) -> str:
    """Two decimals."""
    return format_id(value, 2)


def clock(value, row) -> str:
    """Local time of day of an epoch-ms value, using the row's UTC offset."""
    return MISSING if value is None else format_clock(value, row["utc_offset_seconds"])


def percent(value, row) -> str:
    """A ratio as a percentage with two decimals."""
    return format_percent(value, 2)


def yes_no(value, row) -> str:
    """Ya, Tidak, or MISSING."""
    if value is None:
        return MISSING
    return "Ya" if value else "Tidak"


def labelled(labels: dict):
    """A table formatter that shows the Indonesian word for a stored code."""

    def shown(value, row) -> str:
        """The word for the code, or MISSING."""
        return plain(label_of(labels, value), row)

    return shown


def iteration_outcome(value, row) -> str:
    """Hasil iterasi; a series-stop row names the iteration after which the series ended."""
    word = plain(label_of(OUTCOME_LABELS, value), row)
    if row.get("series_stop"):
        return f"Seri dihentikan setelah iterasi {row['iteration']}: {word}"
    return word


def method_words(value, row) -> str:
    """The lock-test methods (stored as a comma list of codes) as Indonesian words joined with 'dan'."""
    return " dan ".join(label_of(METHOD_LABELS, method) for method in value.split(", "))


def reason_text(value, row) -> str:
    """Invalid-reason text in Indonesian."""
    return plain(translate_reasons(value), row)


def local_time(text: str) -> str:
    """A local date and time with the table's decimal comma in the milliseconds; whole-second text stays as is."""
    return text.replace(".", ",") if "." in text else text


def prefix8(value, row) -> str:
    """The first eight characters of an address (IV-20)."""
    return value[:8] if value else MISSING


def kilobytes_as_mb(value, row) -> str:
    """KB shown as MB with one decimal."""
    return MISSING if value is None else format_id(value / 1024, 1)


def not_available(value, row) -> str:
    """A whole number, or NOT_AVAILABLE when the source (such as Prometheus) gave nothing."""
    return NOT_AVAILABLE if value is None else format_id(value, 0)


def dual_signer_cell(value, row) -> str:
    """Dual-signer seconds; a count from a monitor that did not observe the whole interval never reads as a clean 0."""
    if value is None:
        return NOT_OBSERVED
    if row["monitor_observed"] is False:
        return f"{format_id(value, 0)} (tidak teramati penuh)"
    return format_id(value, 0)


def partition_dual_cell(value, row) -> str:
    """Dual-signer seconds of a partition run; indeterminate seconds or partial coverage never read as a clean 0."""
    if value is None:
        return NOT_OBSERVED
    indeterminate = row["monitor_indeterminate_seconds"]
    if indeterminate:
        return f"{format_id(value, 0)} (tak tentu {format_id(indeterminate, 0)} detik)"
    return dual_signer_cell(value, row)


def port_repair_cell(value, row) -> str:
    """The host-port repair of a partition run in Indonesian; a result written before the field existed reads NOT_RECORDED."""
    if "port_repair" not in row:
        return NOT_RECORDED
    return plain(label_of(PORT_REPAIR_LABELS, value), row)


def problems_json(problems: list) -> str:
    """Coded monitor-coverage problems as JSON text, the raw CSV value."""
    return json.dumps(problems, sort_keys=True)


def problems_words(value, row) -> str:
    """Coded monitor-coverage problems (JSON text) as Indonesian words for Markdown."""
    return monitor_problems_text(json.loads(value))


def unobserved_whole(value, row) -> str:
    """A whole number, or NOT_OBSERVED when the monitor produced no samples."""
    return NOT_OBSERVED if value is None else format_id(value, 0)


def unobserved_intervals_cell(value, row) -> str:
    """The session's unobserved monitor intervals and the longest; NOT_AVAILABLE without the monitor_gaps block."""
    if value is None:
        return NOT_AVAILABLE
    return unobserved_intervals_text(value, row["unobserved_longest_ms"])


def alert_clock(value, row) -> str:
    """The alert instant; a drill that saw no missed height prints NOT_OBSERVED."""
    if row.get("alert_observed") is False:
        return NOT_OBSERVED
    return clock(value, row)


def alert_seconds(value, row) -> str:
    """Alert-derived seconds; NOT_OBSERVED when the alert was not seen."""
    if row.get("alert_observed") is False:
        return NOT_OBSERVED
    return seconds(value, row)


def evidence_text(row: dict) -> str:
    """Sumber Bukti cell: label and file:line (or /commit height) per instant, in the order the instants happened."""

    def moment(name: str) -> float:
        """The instant of a source, or infinity when the row has none."""
        value = row.get(SOURCE_TIME_FIELDS.get(name, ""))
        return float("inf") if value is None else value

    ordered = sorted(row["sources"].items(), key=lambda item: moment(item[0]))
    return "; ".join(f"{SOURCE_LABELS.get(name, name)}: {source}" for name, source in ordered)


@dataclass(frozen=True)
class Column:
    """One column: header, raw value for the CSV, and how the Markdown table shows that value."""

    header: str
    value: Callable
    shown: Callable = plain


def markdown_table(headers: list, rows: list) -> str:
    """A pipe table; pipes inside cells are escaped."""

    def cell(text) -> str:
        """Escape one cell."""
        return str(text).replace("|", "\\|")

    lines = ["| " + " | ".join(cell(header) for header in headers) + " |",
             "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(cell(item) for item in row) + " |" for row in rows]
    return "\n".join(lines)


def write_table(out_dir: Path, name: str, title: str, columns: list, rows: list) -> list:
    """Write name.csv (raw values) and name.md (title and Markdown table); return both paths."""
    headers = [column.header for column in columns]
    raw_rows = [[column.value(row) for column in columns] for row in rows]
    shown_rows = [[column.shown(value, row) for column, value in zip(columns, raw)]
                  for raw, row in zip(raw_rows, rows)]
    csv_path = out_dir / f"{name}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows([["" if value is None else value for value in raw] for raw in raw_rows])
    md_path = out_dir / f"{name}.md"
    md_path.write_text(f"{title}\n\n{markdown_table(headers, shown_rows)}\n", encoding="utf-8")
    return [csv_path, md_path]


@dataclass(frozen=True)
class SessionData:
    """Everything the report reads from one session directory."""

    directory: Path
    metadata: dict
    analysis: dict
    optional: dict
    snapshots: list


def load_session_data(directory: Path) -> SessionData:
    """Read metadata, analysis, the optional result files, and the snapshots of one session."""
    analysis_path = directory / "analysis.json"
    if not analysis_path.exists():
        raise FileNotFoundError(f"{analysis_path} is missing; run 'python -m harness analyze --session {directory.name}' first")
    optional = {name: read_json(directory / f"{name}.json") for name in OPTIONAL_FILES
                if (directory / f"{name}.json").exists()}
    return SessionData(directory, read_json(directory / "metadata.json"), read_json(analysis_path), optional,
                       read_jsonl(directory / "snapshots.jsonl"))


def with_offset(rows: list, metadata: dict) -> list:
    """Rows extended with the session's UTC offset for the clock columns."""
    return [{**row, "utc_offset_seconds": metadata["host_utc_offset_seconds"]} for row in rows]


def group_by_config(rows: list) -> dict:
    """Rows grouped by config_id, in first-seen order."""
    groups = {}
    for row in rows:
        groups.setdefault(row["config_id"], []).append(row)
    return groups


def invalid_reasons(group: list) -> str:
    """Each invalid_reason of a group with its count, or an empty string when every row is valid."""
    counts = Counter(row["invalid_reason"] for row in group if row.get("invalid_reason"))
    return "; ".join(f"{reason} ({count})" for reason, count in counts.items())


def group_by_session_config(rows: list) -> dict:
    """Rows grouped by (session, config_id), in first-seen order."""
    groups = {}
    for row in rows:
        groups.setdefault((row["session"], row["config_id"]), []).append(row)
    return groups


def summary_rows(rows: list, metrics: tuple) -> list:
    """One row per session, configuration, and metric: n of total, mean, sample SD, range, 95% t-interval, invalid reasons."""
    summaries = []
    for (session_id, config_id), group in group_by_session_config(rows).items():
        for field, label in metrics:
            if all(row[field] is None for row in group):
                stats = {"n": 0, "mean": None, "sd": None, "minimum": None, "maximum": None, "t_critical": None,
                         "ci_low": None, "ci_high": None}
            else:
                stats = asdict(summarize_field(group, field))
            summaries.append({"session": session_id, "config_id": config_id, "metric": label, "total": len(group),
                              "invalid": invalid_reasons(group), **stats})
    return summaries


def campaign_sessions(sessions: list) -> list:
    """The sessions whose failovers enter the failover tables: every session except a soak, whose failover is in the soak table."""
    return [session for session in sessions if "soak" not in session.optional]


def series_session(sessions: list):
    """The failover series: the campaign session with the most failover iterations (the first on a tie), or None."""
    candidates = [session for session in campaign_sessions(sessions) if session.analysis["failover"]]
    return max(candidates, key=lambda session: len(session.analysis["failover"]), default=None)


def last_with_result(sessions: list, name: str):
    """The last session that has the result file (drill, soak, ...), whatever its outcome, or None."""
    chosen = [session for session in sessions if name in session.optional]
    return chosen[-1] if chosen else None


def failover_rows(sessions: list) -> list:
    """Failover iterations of every session."""
    return [row for session in campaign_sessions(sessions)
            for row in with_offset(session.analysis["failover"], session.metadata)]


def series_stop_row(session, entry: dict) -> dict:
    """A failover-table row saying a series stopped after one iteration, with the runner's outcome."""
    row = {item.name: None for item in fields(FailoverIteration)}
    row.update(session=session.metadata["session"], iteration=entry["iteration"], outcome=entry["outcome"],
               fault_exit_code=None, sources={}, series_stop=True,
               utc_offset_seconds=session.metadata["host_utc_offset_seconds"])
    return row


def failover_table_rows(sessions: list) -> list:
    """The failover table: each session's iterations, then a row for every series that stopped early."""
    rows = []
    for session in campaign_sessions(sessions):
        rows += with_offset(session.analysis["failover"], session.metadata)
        rows += [series_stop_row(session, entry) for entry in session.analysis.get("series_aborted", [])]
    return rows


def failback_rows(sessions: list) -> list:
    """Failback iterations of every session."""
    return [row for session in campaign_sessions(sessions)
            for row in with_offset(session.analysis["failback"], session.metadata)]


def decomposition_rows(sessions: list) -> list:
    """Failover rows joined with their missed-block shares (absent heights against the window and the allowance)."""
    rows = []
    for session in campaign_sessions(sessions):
        incidents = {incident["iteration"]: incident for incident in session.analysis["incidents"]}
        for row in session.analysis["failover"]:
            share = (incidents.get(row["iteration"]) or {}).get("missed_share") or {}
            rows.append({**row, "share_of_window": share.get("share_of_window"),
                         "share_of_allowance": share.get("share_of_allowance")})
    return rows


def block_rate_rows(sessions: list) -> list:
    """One row per block-rate window per incident."""
    rows = []
    for session in campaign_sessions(sessions):
        for incident in session.analysis["incidents"]:
            for window in incident["block_rates"]:
                rows.append({"session": incident["session"], "iteration": incident["iteration"],
                             "utc_offset_seconds": session.metadata["host_utc_offset_seconds"], **window})
    return rows


def byzantine_maximum(prometheus: dict) -> float | None:
    """The largest byzantine-validators maximum over all instances, or None when Prometheus gave no series."""
    if not prometheus.get("available"):
        return None
    return max(prometheus.get("cometbft_consensus_byzantine_validators", {}).values(), default=None)


NO_GAP_TOTALS = {"intervals": None, "longest_ms": None, "votes": None, "heights": None}  # no monitor_gaps block


def safety_rows(sessions: list) -> list:
    """One row per session with a safety.json, including whether the dual-signer monitor observed the whole interval.

    Beside the observed flag, the row carries the session's unobserved monitor intervals and the votes of the
    validator inside them from the monitor_gaps block of analysis.json; all three are None without the block.
    """
    rows = []
    for session in sessions:
        safety = session.optional.get("safety")
        if safety is None:
            continue
        scan = safety["evidence_scan"]
        info = safety["signing_info"]
        monitor = safety["monitor"]
        gaps = session.analysis.get("monitor_gaps")
        totals = monitor_gap_totals(gaps) if gaps is not None else NO_GAP_TOTALS
        rows.append({
            "session": safety["session"],
            "monitor_samples": monitor["samples"],
            "dual_signer_seconds": monitor["dual_signer_seconds"],
            "monitor_observed": monitor["observed"],
            "unobserved_intervals": totals["intervals"],
            "unobserved_longest_ms": totals["longest_ms"],
            "unobserved_votes": totals["votes"],
            "indeterminate_seconds": monitor["indeterminate_seconds"],
            "observed_problems": monitor["observed_problems"],
            "scan_first": scan["first_height"],
            "scan_last": scan["last_height"],
            "heights_scanned": scan["heights_scanned"],
            "evidence_found": sum(item["count"] for item in scan["evidence"]),
            "covers_max_age": scan["covers_max_age"],
            "missed_blocks_counter": info["missed_blocks_counter"],
            "jailed_until": info["jailed_until"],
            "tombstoned": info["tombstoned"],
            "byzantine_max": byzantine_maximum(safety["prometheus"]),
        })
    return rows


def security_rows(sessions: list) -> list:
    """Negative-request rows of every session, the correct-HMAC control included."""
    return [{"session": session.metadata["session"], **row}
            for session in sessions if "security_negatives" in session.optional
            for row in session.optional["security_negatives"]["rows"]]


def lock_rows(sessions: list) -> list:
    """One lock-negative row per session."""
    rows = []
    for session in sessions:
        result = session.optional.get("lock_negative")
        if result is None:
            continue
        rows.append({
            "session": result["session"],
            "methods": ", ".join(result["methods"]),
            "lock_content_before": result["lock_content_before"],
            "lock_content_after": result["lock_content_after"],
            "active_pid": result["active_pid"],
            "probe_outcome": result["probe"].get("outcome"),
            "probe_error": result["probe"].get("error", ""),
            "unit_test_exit_code": result["unit_test_exit_code"],
            "passed": result["passed"],
        })
    return rows


def _key_address(keys: list, index: int) -> str | None:
    """Address of the index-th key-side file (key, .real, .staged), or None when it does not exist."""
    fact = keys[index]
    return fact.get("address") if fact.get("exists") else None


def snapshot_rows(sessions: list) -> list:
    """One row per snapshot point and node."""
    rows = []
    for session in sessions:
        for snapshot in session.snapshots:
            for node in snapshot["nodes"]:
                lock = node["locks"][0] if node["locks"] else {}
                state = node["state"]
                rows.append({
                    "session": snapshot["session"],
                    "iteration": snapshot["iteration"],
                    "point": snapshot["point"],
                    "node": node["node"],
                    "height": state.get("height"),
                    "round": state.get("round"),
                    "step": state.get("step"),
                    "key": _key_address(node["keys"], 0),
                    "real": _key_address(node["keys"], 1),
                    "staged": _key_address(node["keys"], 2),
                    "lock_pid": lock.get("pid"),
                    "lock_alive": lock.get("pid_alive"),
                })
    return rows


def hang_rows(sessions: list) -> list:
    """Hang iterations of every session."""
    return [{"session": session.metadata["session"], **row}
            for session in sessions for row in session.analysis["hang"]]


def partition_rows(sessions: list) -> list:
    """Partition runs of every session; the monitor fields of the result become columns."""
    rows = []
    for session in sessions:
        for row in session.analysis.get("partition", []):
            monitor = {f"monitor_{key}": value for key, value in row["monitor"].items()}
            rows.append({"session": session.metadata["session"], **row, **monitor})
    return rows


def overhead_rows(sessions: list) -> list:
    """One row per node per session with an overhead.json; a node without samples gets an empty row, and every row carries the run's validity."""
    rows = []
    for session in sessions:
        overhead = session.optional.get("overhead")
        if overhead is None:
            continue
        changes = "; ".join(f"{label}: {change['start']} ke {change['end']}"
                            for label, change in overhead["pid_changes"].items())
        common = {"session": overhead["session"], "valid": overhead["valid"], "pid_changes": changes}
        for label, node in overhead["nodes"].items():
            writes = overhead["passive_state_writes_per_minute"] if label == BACKUP_NODE else None
            rows.append({**common, "node": label, "pid": node["pid"],
                         "cpu_mean": node["cpu_percent"]["mean"], "cpu_max": node["cpu_percent"]["maximum"],
                         "rss_mean": node["rss_kb"]["mean"], "rss_max": node["rss_kb"]["maximum"],
                         "writes_per_minute": writes})
        for label in overhead["nodes_without_samples"]:
            rows.append({**common, "node": label, "pid": None, "cpu_mean": None, "cpu_max": None,
                         "rss_mean": None, "rss_max": None, "writes_per_minute": None})
    return rows


def drill_rows(sessions: list) -> list:
    """One row per session with a drill.json; fields a drill that stopped early never reached stay empty."""
    rows = []
    for session in sessions:
        drill = session.optional.get("drill")
        if drill is None:
            continue
        rows.append({"t_fault_ms": None, "t_alert_ms": None, "t_operator_start_ms": None,
                     "t_first_signature_ms": None, "alert_s": None, "reaction_s": None, "recovery_s": None,
                     "restore_completed": None, **drill,
                     "utc_offset_seconds": session.metadata["host_utc_offset_seconds"],
                     "command_count": len(drill["commands"]) if "commands" in drill else None})
    return rows


def soak_rows(sessions: list) -> list:
    """One row per session with a soak.json: run facts, block-based and time-based availability, and downtime D."""
    rows = []
    for session in sessions:
        soak = session.optional.get("soak")
        if soak is None:
            continue
        availability = soak.get("availability")
        timed = session.analysis.get("soak")
        rows.append({
            "session": soak["session"],
            "heights_collected": soak["heights_collected"],
            "hours": (soak["end_ms"] - soak["start_ms"]) / 3_600_000,
            "start_height": soak["start_height"],
            "end_height": soak["end_height"],
            "signed": availability["signed"] if availability else None,
            "total": availability["total"] if availability else None,
            "availability_blocks": availability["ratio"] if availability else None,
            "downtime_s": timed["downtime_s"] if timed else None,
            "availability_time": timed["availability"] if timed else None,
            "header_gaps": len(soak["header_gaps"]) if "header_gaps" in soak else None,
            "host_pauses": soak["host_pauses"],
            "height_stalls": soak["height_stalls"],
            "kill_outcome": soak["kill_outcome"],
        })
    return rows


def mean_valid_ttr(failover: list) -> tuple:
    """(mean TTR, basis text) of the configuration with the most valid TTR values; configurations are never pooled."""
    best_config, best_values = None, []
    for config_id, group in group_by_config(failover).items():
        values = [row["ttr_s"] for row in group if row["ttr_s"] is not None]
        if len(values) > len(best_values):
            best_config, best_values = config_id, values
    if not best_values:
        return None, "belum ada iterasi failover dengan TTR valid"
    return sum(best_values) / len(best_values), f"rata-rata TTR, konfigurasi {best_config}, n = {len(best_values)}"


def soak_kill_completed(soak_session) -> bool:
    """Whether the soak's injected failover completed; only then does its availability measure the system's recovery."""
    return soak_session.optional["soak"]["kill_outcome"] == "complete"


def soak_availability(sessions: list) -> tuple:
    """(time-based availability in percent, session id) of the last soak.

    The percentage is None when the soak has no collected heights or its failover did not complete.
    """
    soak = last_with_result(sessions, "soak")
    if soak is None:
        return None, None
    timed = soak.analysis.get("soak")
    measured = timed is not None and soak_kill_completed(soak)
    return (timed["availability"] * 100 if measured else None), soak.metadata["session"]


def sensitivity_rows(sessions: list, failover: list) -> list:
    """Three rows: the measured drill, the 1.800 s assumption, and the 99,1 percent literature value.

    Each row gives the baseline disruption per incident and its availability on the 24 h reference
    window, the soak availability of the system (equation 3.4), the difference in percentage points,
    and the reduction of the disruption per incident (baseline against the mean TTR).
    """
    drill_session = last_with_result(sessions, "drill")
    drill = drill_session.optional["drill"] if drill_session is not None else None
    recovery_s = drill.get("recovery_s") if drill is not None else None
    mean_ttr, ttr_basis = mean_valid_ttr(failover)
    system_availability, soak_session = soak_availability(sessions)
    basis = ttr_basis + ("; ketersediaan soak " + soak_session if soak_session else "; belum ada soak")
    if drill is None:
        drill_label = "Drill pemulihan manual (tidak dijalankan)"
    elif recovery_s is None:
        drill_label = f"Drill pemulihan manual ({drill['session']}, hasil: {label_of(OUTCOME_LABELS, drill['outcome'])})"
    else:
        drill_label = f"Drill pemulihan manual ({drill['session']})"
    baselines = [
        (drill_label, recovery_s),
        ("Asumsi internal 1.800 detik", ASSUMED_MANUAL_RECOVERY_S),
        ("Literatur 99,1 persen", LITERATURE_DOWNTIME_S),
    ]
    rows = []
    for label, baseline_s in baselines:
        baseline_availability = None
        if baseline_s is not None:
            baseline_availability = (REFERENCE_WINDOW_S - baseline_s) / REFERENCE_WINDOW_S * 100
        gain = None
        if baseline_availability is not None and system_availability is not None:
            gain = system_availability - baseline_availability
        reduction = None
        if baseline_s is not None and mean_ttr is not None:
            reduction = (baseline_s - mean_ttr) / baseline_s * 100
        rows.append({"baseline": label, "baseline_s": baseline_s, "baseline_availability": baseline_availability,
                     "system_availability": system_availability, "gain_pp": gain, "reduction_pct": reduction,
                     "basis": basis})
    return rows


def session_map_rows(sessions: list, archived_csv: Path) -> list:
    """Archived pairs from the forensics document, then every session of this report."""
    rows = []
    if archived_csv.exists():
        with archived_csv.open(encoding="utf-8") as handle:
            for item in csv.DictReader(handle):
                rows.append({"session": item["sesi"], "purpose": item["butir_naskah"], "start": item["mulai"],
                             "end": item["selesai"], "first_height": int(item["tinggi_awal"]),
                             "last_height": int(item["tinggi_akhir"]), "build": item["build"],
                             "source": item["sumber"]})
    for session in sessions:
        heights = load_heights(session.directory / "heights.jsonl")
        events = read_jsonl(session.directory / "events.jsonl")
        end_ms = max((event["t_ms"] for event in events), default=session.metadata["start_ms"])
        session_id = session.metadata["session"]
        rows.append({
            "session": session_id,
            "purpose": session.metadata["purpose"],
            "start": local_time(session.metadata["start_local"]),
            "end": local_time(format_local(end_ms, session.metadata["host_utc_offset_seconds"])),
            "first_height": heights[0].height if heights else None,
            "last_height": heights[-1].height if heights else None,
            "build": session.metadata["syncguard_version"],
            "source": f"experiments/data/{session_id}/metadata.json",
        })
    return rows


FAILOVER_COLUMNS = [
    Column("Sesi", lambda row: row["session"]),
    Column("Iterasi", lambda row: row["iteration"]),
    Column("t_gagal", lambda row: row["t_fault_ms"], clock),
    Column("t_deteksi", lambda row: row["t_detect_ms"], clock),
    Column("t_aktif_cadangan", lambda row: row["t_first_signature_ms"], clock),
    Column("Tinggi t_aktif_cadangan", lambda row: row["first_signature_height"], whole),
    Column("TTD (s)", lambda row: row["ttd_s"], seconds),
    Column("TTR (s)", lambda row: row["ttr_s"], seconds),
    Column("Sumber Bukti", evidence_text),
    Column("Ketidakpastian jam (ms)", lambda row: row["clock_uncertainty_ms"], decimal1),
    Column("Kode keluar kill", lambda row: row["fault_exit_code"], plain),
    Column("Alasan tidak valid", lambda row: row["invalid_reason"], reason_text),
    Column("Hasil iterasi", lambda row: row["outcome"], iteration_outcome),
    Column("Seri dihentikan", lambda row: bool(row.get("series_stop")), yes_no),
]
SUMMARY_COLUMNS = [
    Column("Sesi", lambda row: row["session"]),
    Column("Konfigurasi", lambda row: row["config_id"]),
    Column("Metrik", lambda row: row["metric"]),
    Column("n", lambda row: row["n"], whole),
    Column("Jumlah iterasi", lambda row: row["total"], whole),
    Column("Rata-rata (s)", lambda row: row["mean"], seconds),
    Column("SD sampel (s)", lambda row: row["sd"], seconds),
    Column("Min (s)", lambda row: row["minimum"], seconds),
    Column("Maks (s)", lambda row: row["maximum"], seconds),
    Column("t(0,975; n-1)", lambda row: row["t_critical"], seconds),
    Column("IK 95% bawah (s)", lambda row: row["ci_low"], seconds),
    Column("IK 95% atas (s)", lambda row: row["ci_high"], seconds),
    Column("Alasan tidak valid", lambda row: row["invalid"], reason_text),
]
DECOMPOSITION_COLUMNS = [
    Column("Sesi", lambda row: row["session"]),
    Column("Iterasi", lambda row: row["iteration"]),
    Column("Deteksi (s)", lambda row: row["detection_s"], seconds),
    Column("Orkestrasi (s)", lambda row: row["orchestration_s"], seconds),
    Column("Mulai node (s)", lambda row: row["node_start_s"], seconds),
    Column("TTR (s)", lambda row: row["ttr_s"], seconds),
    Column("Tinggi terlewat", lambda row: row["absent_heights"], whole),
    Column("Tinggi suara nil", lambda row: row["nil_heights"], whole),
    Column("Tinggi dalam jeda", lambda row: row["gap_heights"], whole),
    Column("Porsi jendela 200 blok", lambda row: row["share_of_window"], percent),
    Column("Porsi batas jail 190 blok", lambda row: row["share_of_allowance"], percent),
]
BLOCK_RATE_COLUMNS = [
    Column("Sesi", lambda row: row["session"]),
    Column("Iterasi", lambda row: row["iteration"]),
    Column("Jendela", lambda row: WINDOW_LABELS.get(row["label"], row["label"])),
    Column("Mulai", lambda row: row["start_ms"], clock),
    Column("Blok", lambda row: row["blocks"], whole),
    Column("Blok per menit", lambda row: row["blocks_per_minute"], decimal2),
    Column("Data lengkap", lambda row: row["covered"], yes_no),
]
FAILBACK_COLUMNS = [
    Column("Sesi", lambda row: row["session"]),
    Column("Iterasi", lambda row: row["iteration"]),
    Column("Cek sehat pertama primer", lambda row: row["t_primary_healthy_ms"], clock),
    Column("Failback dimulai", lambda row: row["t_failback_initiated_ms"], clock),
    Column("Notifikasi failback pada cadangan", lambda row: row["t_failback_notified_ms"], clock),
    Column("Cadangan lepas", lambda row: row["t_release_complete_ms"], clock),
    Column("Failback selesai", lambda row: row["t_failback_complete_ms"], clock),
    Column("Tanda tangan pertama primer", lambda row: row["t_primary_signature_ms"], clock),
    Column("Tunggu grace (s)", lambda row: row["grace_wait_s"], seconds),
    Column("Ke failback selesai (s)", lambda row: row["to_complete_s"], seconds),
    Column("Ke tanda tangan primer (s)", lambda row: row["to_signature_s"], seconds),
    Column("Eksekusi (s)", lambda row: row["execution_s"], seconds),
    Column("G_fb (s)", lambda row: row["gfb_s"], seconds),
    Column("Percobaan failback", lambda row: row["failback_attempts"], whole),
    Column("Tinggi terlewat", lambda row: row["absent_heights"], whole),
    Column("Sumber Bukti", evidence_text),
]
SAFETY_COLUMNS = [
    Column("Sesi", lambda row: row["session"]),
    Column("Sampel monitor", lambda row: row["monitor_samples"], whole),
    Column("Detik kedua node memuat 48DC2183", lambda row: row["dual_signer_seconds"], dual_signer_cell),
    Column("Monitor mengamati seluruh interval", lambda row: row["monitor_observed"], yes_no),
    Column(UNOBSERVED_INTERVALS_HEADER, lambda row: row["unobserved_intervals"], unobserved_intervals_cell),
    Column(UNOBSERVED_VOTES_HEADER, lambda row: row["unobserved_votes"], not_available),
    Column("Detik tak tentu", lambda row: row["indeterminate_seconds"], unobserved_whole),
    Column("Masalah pengamatan monitor", lambda row: problems_json(row["observed_problems"]), problems_words),
    Column("Tinggi awal pindai", lambda row: row["scan_first"], whole),
    Column("Tinggi akhir pindai", lambda row: row["scan_last"], whole),
    Column("Blok dipindai", lambda row: row["heights_scanned"], whole),
    Column("Bukti ditemukan", lambda row: row["evidence_found"], whole),
    Column("Melewati umur maks bukti", lambda row: row["covers_max_age"], yes_no),
    Column("missed_blocks_counter", lambda row: row["missed_blocks_counter"], whole),
    Column("jailed_until", lambda row: row["jailed_until"]),
    Column("tombstoned", lambda row: row["tombstoned"], yes_no),
    Column("Byzantine maks", lambda row: row["byzantine_max"], not_available),
]
SECURITY_COLUMNS = [
    Column("Sesi", lambda row: row["session"]),
    Column("No", lambda row: row["number"]),
    Column("Kasus", lambda row: row["case"]),
    Column("Permintaan", lambda row: row["request"]),
    Column("Diharapkan", lambda row: row["expected"]),
    Column("Aktual", lambda row: row["actual"]),
    Column("HMAC benar", lambda row: row["correct_hmac"], yes_no),
    Column("Berkas kunci tidak berubah", lambda row: row["files_unchanged"], yes_no),
    Column("Peran tidak berubah", lambda row: row["role_unchanged"], yes_no),
    Column("Penolakan tercatat di log", lambda row: row["refusal_logged"], yes_no),
    Column("Status", lambda row: row["status"]),
]
LOCK_COLUMNS = [
    Column("Sesi", lambda row: row["session"]),
    Column("Metode", lambda row: row["methods"], method_words),
    Column("Isi lock sebelum", lambda row: row["lock_content_before"]),
    Column("Isi lock sesudah", lambda row: row["lock_content_after"]),
    Column("PID syncguard aktif", lambda row: row["active_pid"]),
    Column("Hasil probe", lambda row: row["probe_outcome"], labelled(PROBE_LABELS)),
    Column("Pesan probe", lambda row: row["probe_error"]),
    Column("Exit code uji unit", lambda row: row["unit_test_exit_code"]),
    Column("Lulus", lambda row: row["passed"], yes_no),
]
SNAPSHOT_COLUMNS = [
    Column("Sesi", lambda row: row["session"]),
    Column("Iterasi", lambda row: row["iteration"]),
    Column("Titik", lambda row: row["point"]),
    Column("Node", lambda row: row["node"]),
    Column("Tinggi", lambda row: row["height"], whole),
    Column("Round", lambda row: row["round"]),
    Column("Step", lambda row: row["step"]),
    Column("Kunci", lambda row: row["key"], prefix8),
    Column(".real", lambda row: row["real"], prefix8),
    Column(".staged", lambda row: row["staged"], prefix8),
    Column("PID lock", lambda row: row["lock_pid"]),
    Column("PID hidup", lambda row: row["lock_alive"], yes_no),
]
HANG_COLUMNS = [
    Column("Sesi", lambda row: row["session"]),
    Column("Iterasi", lambda row: row["iteration"]),
    Column("Hasil", lambda row: row["outcome"], labelled(HANG_OUTCOME_LABELS)),
    Column("Terdeteksi", lambda row: row["detected"], yes_no),
    Column("TTD (s)", lambda row: row["ttd_s"], seconds),
    Column("TTR (s)", lambda row: row["ttr_s"], seconds),
    Column("Tindak lanjut", lambda row: row["followup"], labelled(HANG_FOLLOWUP_LABELS)),
    Column("Restart kontainer", lambda row: row["restart_outcome"], labelled(RESTART_LABELS)),
    Column("Tindakan akhir", lambda row: row["action"], labelled(ACTION_LABELS)),
    Column("Alasan", lambda row: row["reason"]),
    Column("Sumber deteksi", lambda row: row["detection_source"]),
    Column("Pembersihan berhasil", lambda row: row["clear_ok"], yes_no),
]
PARTITION_COLUMNS = [
    Column("Sesi", lambda row: row["session"]),
    Column("Iterasi", lambda row: row["iteration"]),
    Column("Hasil", lambda row: row["outcome"], labelled(PARTITION_OUTCOME_LABELS)),
    Column("Tinggi saat pemutusan", lambda row: row["fault_height"], whole),
    Column("Terdeteksi", lambda row: row["detected"], yes_no),
    Column("Gejala deteksi", lambda row: row["detection_symptom"], labelled(SYMPTOM_LABELS)),
    Column("TTD (s)", lambda row: row["ttd_s"], seconds),
    Column("Takeover", lambda row: row["takeover"], yes_no),
    Column("TTR (s)", lambda row: row["ttr_s"], seconds),
    Column("Detik kedua node memuat 48DC2183", lambda row: row["monitor_dual_signer_seconds"], partition_dual_cell),
    Column("Detik tak tentu", lambda row: row["monitor_indeterminate_seconds"], unobserved_whole),
    Column("Monitor mengamati seluruh interval", lambda row: row["monitor_observed"], yes_no),
    Column("Masalah pengamatan monitor", lambda row: problems_json(row["monitor_observed_problems"]), problems_words),
    Column("Failback selesai", lambda row: row["failback_complete"], yes_no),
    Column("Perbaikan port host", lambda row: row.get("port_repair"), port_repair_cell),
]
OVERHEAD_COLUMNS = [
    Column("Sesi", lambda row: row["session"]),
    Column("Node", lambda row: row["node"]),
    Column("PID", lambda row: row["pid"]),
    Column("CPU rata-rata (%)", lambda row: row["cpu_mean"], decimal2),
    Column("CPU maks (%)", lambda row: row["cpu_max"], decimal2),
    Column("RSS rata-rata (MB)", lambda row: row["rss_mean"], kilobytes_as_mb),
    Column("RSS maks (MB)", lambda row: row["rss_max"], kilobytes_as_mb),
    Column("Tulis state per menit", lambda row: row["writes_per_minute"], decimal2),
    Column("Run valid", lambda row: row["valid"], yes_no),
    Column("Perubahan PID", lambda row: row["pid_changes"]),
]
DRILL_COLUMNS = [
    Column("Sesi", lambda row: row["session"]),
    Column("Operator", lambda row: row["operator"]),
    Column("Hasil", lambda row: row["outcome"], labelled(OUTCOME_LABELS)),
    Column("t_gagal", lambda row: row["t_fault_ms"], clock),
    Column("Alert", lambda row: row["t_alert_ms"], alert_clock),
    Column("Operator mulai", lambda row: row["t_operator_start_ms"], clock),
    Column("Tanda tangan pertama", lambda row: row["t_first_signature_ms"], clock),
    Column("Ke alert (s)", lambda row: row["alert_s"], alert_seconds),
    Column("Reaksi operator (s)", lambda row: row["reaction_s"], alert_seconds),
    Column("Pemulihan (s)", lambda row: row["recovery_s"], seconds),
    Column("Jumlah perintah", lambda row: row["command_count"], whole),
    Column("Tata letak dipulihkan", lambda row: row["restore_completed"], yes_no),
]
SOAK_COLUMNS = [
    Column("Sesi", lambda row: row["session"]),
    Column("Durasi (jam)", lambda row: row["hours"], decimal2),
    Column("Tinggi awal", lambda row: row["start_height"], whole),
    Column("Tinggi akhir", lambda row: row["end_height"], whole),
    Column("Tinggi ditandatangani", lambda row: row["signed"], whole),
    Column("Total tinggi", lambda row: row["total"], whole),
    Column("Ketersediaan persamaan 3.8 (blok)", lambda row: row["availability_blocks"],
           lambda value, row: format_percent(value, 3)),
    Column("Downtime D (s)", lambda row: row["downtime_s"], decimal1),
    Column("Ketersediaan persamaan 3.4 (waktu)", lambda row: row["availability_time"],
           lambda value, row: format_percent(value, 3)),
    Column("Jeda header > 30 s", lambda row: row["header_gaps"], whole),
    Column("Jeda host", lambda row: row["host_pauses"], whole),
    Column("Tinggi macet", lambda row: row["height_stalls"], whole),
    Column("Hasil failover", lambda row: row["kill_outcome"], labelled(OUTCOME_LABELS)),
    Column("Tinggi terkumpul", lambda row: row["heights_collected"], yes_no),
]
SENSITIVITY_COLUMNS = [
    Column("Acuan", lambda row: row["baseline"]),
    Column("Gangguan per insiden baseline (detik)", lambda row: row["baseline_s"], decimal1),
    Column("Availability baseline (persen, jendela 24 jam)", lambda row: row["baseline_availability"], decimal2),
    Column("Availability dengan sistem failover (persen, soak, persamaan 3.4)",
           lambda row: row["system_availability"], decimal2),
    Column("Selisih (poin persentase)", lambda row: row["gain_pp"], decimal2),
    Column("Penurunan gangguan per insiden (persen)", lambda row: row["reduction_pct"], decimal2),
    Column("Dasar nilai sistem", lambda row: row["basis"]),
]
SESSION_MAP_COLUMNS = [
    Column("Sesi", lambda row: row["session"]),
    Column("Butir naskah atau tujuan", lambda row: row["purpose"]),
    Column("Mulai", lambda row: row["start"]),
    Column("Selesai", lambda row: row["end"]),
    Column("Tinggi awal", lambda row: row["first_height"], whole),
    Column("Tinggi akhir", lambda row: row["last_height"], whole),
    Column("Build", lambda row: row["build"]),
    Column("Sumber", lambda row: row["source"]),
]


def session_records(session: SessionData) -> list:
    """The session's stored heights on the host clock."""
    return shift_to_host_clock(load_heights(session.directory / "heights.jsonl"),
                               session.metadata["clock_offset"]["offset_ms"])


def plotted_failover(session: SessionData):
    """(row, series) of the first failover with a valid TTR, which the failover figure plots; None when there is none."""
    records = session_records(session)
    complete = [row for row in session.analysis["failover"] if row["ttr_s"] is not None]
    if not records or not complete:
        return None
    return complete[0], failover_series(records, TARGET_VALIDATOR, complete[0]["t_fault_ms"])


def failover_figure_name(session_id: str, iteration: int) -> str:
    """File name of the failover figure of one iteration."""
    return f"gambar-failover-{session_id}-iterasi-{iteration}.png"


def signer_figure_name(session_id: str) -> str:
    """File name of the signer-count figure of one session."""
    return f"gambar-penanda-tangan-{session_id}.png"


def figure_outputs(sessions: list, out_dir: Path) -> tuple:
    """Per session: the first failover with a valid TTR, and the signer count over all stored heights."""
    written = []
    captions = []
    for session in sessions:
        records = session_records(session)
        if not records:
            continue
        session_id = session.metadata["session"]
        plotted = plotted_failover(session)
        if plotted is not None:
            row, series = plotted
            title = (f"Sesi {session_id}, iterasi {row['iteration']}: tinggi blok dan tanda tangan 48DC2183 "
                     f"(tinggi {format_id(series.first_height, 0)} sampai {format_id(series.last_height, 0)})")
            name = failover_figure_name(session_id, row["iteration"])
            recovery_s = (row["t_first_signature_ms"] - row["t_fault_ms"]) / 1000
            written.append(plot_failover(series, recovery_s, title, out_dir / name))
            captions.append((name, f"{title}; {FAILOVER_NOTE}"))
        counts = signer_counts(records)
        title = (f"Sesi {session_id}: jumlah penanda tangan per tinggi "
                 f"(tinggi {format_id(counts[0][0], 0)} sampai {format_id(counts[-1][0], 0)})")
        name = signer_figure_name(session_id)
        written.append(plot_signer_counts(counts, len(session.metadata["validators"]), title, out_dir / name))
        captions.append((name, title))
    return written, captions


def write_index(out_dir: Path, written: list, captions: list, skipped: list, session_ids: list) -> Path:
    """index.md: the sessions, the tables written, the figure captions, and the tables without data."""
    lines = ["# Keluaran harness", "", "Sesi: " + ", ".join(session_ids), "", "## Tabel", ""]
    lines += [f"- {path.name}" for path in written if path.suffix == ".md"]
    lines += ["", "## Gambar", ""]
    lines += [f"- {name}: {caption}" for name, caption in captions]
    lines += ["", "## Tabel tanpa data", ""]
    lines += [f"- {name}: tanpa data pada sesi ini" for name in skipped]
    path = out_dir / "index.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_reports(session_dirs: list, out_dir: Path, archived_csv: Path) -> list:
    """Write every table that has data, the figures, and index.md; return the written paths."""
    sessions = [load_session_data(Path(directory)) for directory in session_dirs]
    out_dir.mkdir(parents=True, exist_ok=True)
    failover = failover_rows(sessions)
    failback = failback_rows(sessions)
    series = series_session(sessions)
    series_rows = [row for row in failover if series is not None and row["session"] == series.metadata["session"]]
    tables = [
        ("tabel-4-3-failover", "Tabel 4.3 Failover per iterasi", FAILOVER_COLUMNS, failover_table_rows(sessions)),
        ("tabel-4-3-ringkasan", "Ringkasan TTD dan TTR per konfigurasi", SUMMARY_COLUMNS,
         summary_rows(failover, FAILOVER_METRICS)),
        ("tabel-dekomposisi", "Dekomposisi TTR per iterasi", DECOMPOSITION_COLUMNS, decomposition_rows(sessions)),
        ("tabel-laju-blok", "Laju blok di sekitar failover", BLOCK_RATE_COLUMNS, block_rate_rows(sessions)),
        ("tabel-4-5-failback", "Tabel 4.5 Failback per iterasi", FAILBACK_COLUMNS, failback),
        ("tabel-4-5-ringkasan", "Ringkasan failback per konfigurasi", SUMMARY_COLUMNS,
         summary_rows(failback, FAILBACK_METRICS)),
        ("tabel-4-7-keamanan", "Tabel 4.7 Bukti keamanan per sesi", SAFETY_COLUMNS, safety_rows(sessions)),
        ("tabel-uji-negatif", "Uji negatif autentikasi peer", SECURITY_COLUMNS, security_rows(sessions)),
        ("tabel-uji-lock", "Uji negatif lock", LOCK_COLUMNS, lock_rows(sessions)),
        ("tabel-snapshot", "Snapshot state, lock, dan kunci", SNAPSHOT_COLUMNS, snapshot_rows(sessions)),
        ("tabel-hang", "Skenario hang (docker pause)", HANG_COLUMNS, hang_rows(sessions)),
        ("tabel-partisi", "Skenario partisi jaringan (docker network disconnect)", PARTITION_COLUMNS,
         partition_rows(sessions)),
        ("tabel-overhead", "Overhead syncguard", OVERHEAD_COLUMNS, overhead_rows(sessions)),
        ("tabel-drill", "Drill pemulihan manual", DRILL_COLUMNS, drill_rows(sessions)),
        ("tabel-soak", "Uji soak", SOAK_COLUMNS, soak_rows(sessions)),
        ("tabel-sensitivitas", "Sensitivitas acuan", SENSITIVITY_COLUMNS, sensitivity_rows(sessions, series_rows)),
        ("tabel-peta-sesi", "Peta sesi", SESSION_MAP_COLUMNS, session_map_rows(sessions, archived_csv)),
    ]
    written = []
    skipped = []
    for name, title, columns, rows in tables:
        if rows:
            written += write_table(out_dir, name, title, columns, rows)
        else:
            skipped.append(name)
    figures, captions = figure_outputs(sessions, out_dir)
    written += figures
    session_ids = [session.metadata["session"] for session in sessions]
    written.append(write_index(out_dir, written, captions, skipped, session_ids))
    return written
