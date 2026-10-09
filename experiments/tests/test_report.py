"""Manuscript tables: Indonesian Markdown, raw CSV, figures, and the index."""

import csv

import pytest

from fakes import FIXTURES, make_data
from harness.analysis import analyze_session
from harness.constants import ARCHIVED_SESSIONS_CSV
from harness.dryrun import build_fixture_session
from harness.report import (
    DRILL_COLUMNS,
    SAFETY_COLUMNS,
    Column,
    block_rate_rows,
    drill_rows,
    hang_rows,
    markdown_table,
    overhead_rows,
    partition_rows,
    safety_rows,
    seconds,
    security_rows,
    sensitivity_rows,
    soak_rows,
    summary_rows,
    whole,
    write_reports,
    write_table,
)


def test_markdown_table_escapes_pipes():
    """A pipe inside a cell cannot break the table."""
    assert markdown_table(["A", "B"], [["x|y", "1"]]) == "| A | B |\n|---|---|\n| x\\|y | 1 |"


def test_write_table_formats_markdown_and_keeps_csv_raw(tmp_path):
    """Markdown gets 25.463 and 68,910; the CSV keeps 25463 and 68.91."""
    columns = [Column("Tinggi", lambda row: row["h"], whole), Column("TTR (s)", lambda row: row["t"], seconds)]
    csv_path, md_path = write_table(tmp_path, "contoh", "Contoh", columns, [{"h": 25463, "t": 68.91}])
    assert "| 25.463 | 68,910 |" in md_path.read_text()
    assert md_path.read_text().startswith("Contoh\n\n")
    assert list(csv.reader(csv_path.open())) == [["Tinggi", "TTR (s)"], ["25463", "68.91"]]


def test_summary_rows_group_by_configuration():
    """Two configurations give two summaries; neither mixes the other's values."""
    rows = [{"session": "S", "config_id": "a", "ttr_s": 10.0}, {"session": "S", "config_id": "a", "ttr_s": 12.0},
            {"session": "S", "config_id": "b", "ttr_s": 50.0}]
    summaries = summary_rows(rows, (("ttr_s", "TTR"),))
    assert [(row["config_id"], row["n"], row["mean"]) for row in summaries] == [("a", 2, 11.0), ("b", 1, 50.0)]


def test_summary_rows_report_the_invalid_count_with_reasons():
    """Rows with an invalid_reason are counted per reason beside n."""
    rows = [{"session": "S", "config_id": "a", "ttr_s": 10.0, "invalid_reason": None},
            {"session": "S", "config_id": "a", "ttr_s": None, "invalid_reason": "no signing gap observed"},
            {"session": "S", "config_id": "a", "ttr_s": None, "invalid_reason": "no signing gap observed"}]
    summary = summary_rows(rows, (("ttr_s", "TTR"),))[0]
    assert (summary["n"], summary["total"]) == (1, 3)
    assert summary["invalid"] == "no signing gap observed (2)"


def test_reports_from_the_fixture_session(tmp_path):
    """Every table with data, both figures, and an index that lists the tables without data."""
    directory = build_fixture_session(FIXTURES, tmp_path / "data")
    analyze_session(directory)
    out = tmp_path / "results"
    written = {path.name for path in write_reports([directory], out, ARCHIVED_SESSIONS_CSV, tmp_path / "notes.csv",
                                                    tmp_path / "tenderduty-run.log")}
    for name in ("tabel-4-3-failover", "tabel-4-3-ringkasan", "tabel-dekomposisi", "tabel-laju-blok",
                 "tabel-4-5-failback", "tabel-4-5-ringkasan", "tabel-sensitivitas", "tabel-peta-sesi"):
        assert {f"{name}.md", f"{name}.csv"} <= written
    assert {"gambar-failover-SFIX-iterasi-1.png", "gambar-penanda-tangan-SFIX.png", "index.md"} <= written
    failover = (out / "tabel-4-3-failover.md").read_text()
    assert "| SFIX | 1 | 09:53:48,000 | 09:54:39,620 | 09:54:56,910 | 144 | 51,620 | 68,910 |" in failover
    assert "deteksi: smoke-active.txt:7" in failover
    assert "Hasil iterasi" in failover
    assert "1791168828000" in (out / "tabel-4-3-failover.csv").read_text()
    failback = (out / "tabel-4-5-failback.md").read_text()
    assert "76,803" in failback
    assert "G_fb (s)" in failback and "16,798" in failback
    assert "| 15 | 1 | 16 | 7,50% | 7,89% |" in (out / "tabel-dekomposisi.md").read_text()
    assert "G_fb" in (out / "tabel-4-5-ringkasan.md").read_text()
    session_map = (out / "tabel-peta-sesi.md").read_text()
    assert "| W1 | Skenario A, Skenario B |" in session_map
    assert "| SFIX |" in session_map
    assert "1.800,0" in (out / "tabel-sensitivitas.md").read_text()
    index = (out / "index.md").read_text()
    assert "tabel-uji-negatif" in index and "tanpa data" in index
    assert "gambar-failover-SFIX-iterasi-1.png" in index


GAP_PROBLEM = {"code": "gap", "ms": 9000, "scenario": "failover", "iteration": 1}


def safety(observed: bool, dual: int | None = 0, indeterminate: int | None = 0, prometheus: dict | None = None) -> dict:
    """A safety.json with the monitor fields the table reads."""
    return {
        "session": "S9",
        "monitor": {"samples": 100, "dual_signer_seconds": dual, "indeterminate_seconds": indeterminate,
                    "observed": observed, "observed_problems": [] if observed else [GAP_PROBLEM]},
        "evidence_scan": {"first_height": 10, "last_height": 500, "heights_scanned": 491,
                          "evidence": [{"height": 20, "count": 1, "types": ["DuplicateVoteEvidence"]}],
                          "covers_max_age": False},
        "signing_info": {"missed_blocks_counter": 3, "jailed_until": "1970-01-01T00:00:00Z", "tombstoned": False},
        "prometheus": prometheus if prometheus is not None else {"available": False, "reason": "no series"},
    }


def test_safety_rows_carry_observed_and_indeterminate_seconds():
    """The monitor's observed flag, problems, and indeterminate seconds travel into the row."""
    rows = safety_rows([make_data({"safety": safety(False, dual=0, indeterminate=7)})])
    assert rows[0]["monitor_observed"] is False
    assert rows[0]["indeterminate_seconds"] == 7
    assert rows[0]["observed_problems"] == [GAP_PROBLEM]
    assert rows[0]["evidence_found"] == 1
    assert rows[0]["byzantine_max"] is None


def test_safety_table_never_prints_an_unobserved_zero_as_clean(tmp_path):
    """observed false: the dual-signer cell says so; observed true: a plain 0; no samples: tidak teramati."""
    rows = []
    for observed, dual in ((False, 0), (True, 0), (False, None)):
        rows += safety_rows([make_data({"safety": safety(observed, dual=dual)})])
    _, md_path = write_table(tmp_path, "keamanan", "Keamanan", SAFETY_COLUMNS, rows)
    lines = md_path.read_text().splitlines()
    headers = [cell.strip() for cell in lines[2].strip("|").split("|")]
    column = headers.index("Detik kedua node memuat 48DC2183")
    cells = [line.strip("|").split("|")[column].strip() for line in lines[4:]]
    assert cells == ["0 (tidak teramati penuh)", "0", "tidak teramati"]
    assert "Monitor mengamati seluruh interval" in headers and "Detik tak tentu" in headers


def gap_entry(start_ms: int, end_ms: int, heights: list, votes: list) -> dict:
    """One votes_inside entry; votes are (height, t_ms, kind) triples."""
    return {"start_ms": start_ms, "end_ms": end_ms, "ms": end_ms - start_ms, "heights": heights,
            "votes": [{"height": height, "t_ms": t_ms, "kind": kind} for height, t_ms, kind in votes]}


def test_safety_table_shows_unobserved_intervals_and_the_votes_inside_them_after_the_observed_column(tmp_path):
    """'n interval, maks s detik' or 'tidak ada', and the count of 48DC2183 votes; 'tidak tersedia' without the
    monitor_gaps block. The CSV keeps the counts."""
    block = [{"scenario": "failover", "iteration": 1, "intervals": [
        gap_entry(0, 11_000, [5], [(5, 3000, "commit")]),
        gap_entry(50_000, 77_000, [9, 10], [(9, 60_000, "commit"), (10, 63_000, "nil")])]}]
    rows = []
    for gaps in (block, [{"scenario": "failover", "iteration": 1, "intervals": []}], None):
        rows += safety_rows([make_data({"safety": safety(False)}, analysis={"monitor_gaps": gaps})])
    csv_path, md_path = write_table(tmp_path, "keamanan", "Keamanan", SAFETY_COLUMNS, rows)
    lines = md_path.read_text().splitlines()
    headers = [cell.strip() for cell in lines[2].strip("|").split("|")]
    observed = headers.index("Monitor mengamati seluruh interval")
    assert headers[observed + 1:observed + 3] == ["Interval tak teramati", "Suara 48DC2183 di dalam interval"]
    cells = [[cell.strip() for cell in line.strip("|").split("|")][observed + 1:observed + 3] for line in lines[4:]]
    assert cells == [["2 interval, maks 27,0 detik", "3"], ["tidak ada", "0"], ["tidak tersedia", "tidak tersedia"]]
    raw = list(csv.reader(csv_path.open()))
    column = raw[0].index("Interval tak teramati")
    assert [row[column:column + 2] for row in raw[1:]] == [["2", "3"], ["0", "0"], ["", ""]]


def test_safety_byzantine_maximum_comes_from_prometheus():
    """The largest per-instance maximum is reported when Prometheus answered."""
    prometheus = {"available": True, "cometbft_consensus_byzantine_validators": {"a": 0.0, "b": 1.0}}
    assert safety_rows([make_data({"safety": safety(True, prometheus=prometheus)})])[0]["byzantine_max"] == 1.0


def test_security_rows_keep_the_control_and_unproven_status():
    """All eight rows are listed; a TIDAK TERBUKTI status is kept as is."""
    rows = [{"number": number, "case": f"c{number}", "request": "r", "expected": 401, "actual": 401,
             "files_unchanged": True, "role_unchanged": True, "correct_hmac": number in (2, 8),
             "refusal_logged": True, "status": "TIDAK TERBUKTI" if number == 2 else "LULUS"}
            for number in range(1, 9)]
    result = security_rows([make_data({"security_negatives": {"session": "S9", "rows": rows}})])
    assert len(result) == 8
    assert result[1]["status"] == "TIDAK TERBUKTI"
    assert result[1]["session"] == "S9"


def test_hang_rows_include_iteration_and_clear_result():
    """Hang rows come from analysis.json with the session id."""
    hang = [{"iteration": 1, "outcome": "detected", "detected": True, "ttd_s": 30.0, "restart_outcome": "failed",
             "action": "kill", "reason": "r", "detection_source": "syncguard.log:9", "clear_ok": True}]
    rows = hang_rows([make_data(analysis={"hang": hang})])
    assert [(row["session"], row["iteration"], row["clear_ok"]) for row in rows] == [("S9", 1, True)]


def test_overhead_rows_flag_invalid_runs_and_nodes_without_samples():
    """An invalid run is marked on every row, and a node without samples still gets a row."""
    stat = {"mean": 1.0, "maximum": 2.0}
    overhead = {"session": "S9", "valid": False, "nodes_without_samples": ["validator-2"],
                "pid_changes": {"validator-1": {"start": 1, "end": 2}},
                "passive_state_writes_per_minute": 12.0,
                "nodes": {"validator-1": {"pid": 1, "cpu_percent": stat, "rss_kb": {"mean": 2048.0, "maximum": 4096.0}}}}
    rows = overhead_rows([make_data({"overhead": overhead})])
    assert [(row["node"], row["valid"]) for row in rows] == [("validator-1", False), ("validator-2", False)]
    assert rows[1]["cpu_mean"] is None
    assert rows[0]["pid_changes"] == "validator-1: 1 ke 2"


def test_drill_rows_print_an_unobserved_alert_as_not_observed(tmp_path):
    """alert_observed false: the alert cells are empty and the Alert column says tidak teramati."""
    drill = {"session": "S9", "operator": "Heed", "outcome": "recovered", "alert_observed": False, "t_alert_ms": None,
             "alert_s": None, "reaction_s": None, "t_fault_ms": 1791168828000, "t_operator_start_ms": 1791168850000,
             "t_first_signature_ms": 1791168900000, "recovery_s": 72.0, "commands": [{"command": "x"}],
             "restore_completed": True}
    rows = drill_rows([make_data({"drill": drill})])
    _, md_path = write_table(tmp_path, "drill", "Drill", DRILL_COLUMNS, rows)
    text = md_path.read_text()
    assert "tidak teramati" in text
    assert "72,000" in text


def test_soak_rows_show_time_based_and_block_based_availability():
    """The soak table reads D and the time-based availability from analysis.json and the block ratio from soak.json."""
    soak = {"session": "S9", "heights_collected": True, "start_ms": 0, "end_ms": 86_400_000, "start_height": 10,
            "end_height": 30000, "kill_outcome": "complete", "host_pauses": 0, "height_stalls": 0,
            "header_gaps": [], "signed_heights": 29000, "nil_heights": 1, "absent_heights": 10,
            "total_heights": 29011, "availability": {"signed": 29000, "total": 29011, "ratio": 0.99962}}
    analysis = {"soak": {"window_s": 86400.0, "downtime_s": 80.0, "availability": 1 - 80 / 86400,
                         "availability_blocks": 0.99962}}
    row = soak_rows([make_data({"soak": soak}, analysis)])[0]
    assert row["hours"] == 24.0
    assert row["downtime_s"] == 80.0
    assert row["availability_time"] == pytest.approx(1 - 80 / 86400)
    assert row["availability_blocks"] == 0.99962


def test_soak_rows_without_collected_heights_keep_the_run_facts():
    """A soak whose final heights failed shows its duration and failover outcome and no availability."""
    soak = {"session": "S9", "heights_collected": False, "start_ms": 0, "end_ms": 3_600_000, "start_height": 10,
            "end_height": None, "kill_outcome": "complete", "host_pauses": 1, "height_stalls": 0}
    row = soak_rows([make_data({"soak": soak})])[0]
    assert row["heights_collected"] is False
    assert row["availability_time"] is None and row["signed"] is None


def failover_row(config: str, ttr: float | None) -> dict:
    """A failover row reduced to the fields the sensitivity table reads."""
    return {"config_id": config, "ttr_s": ttr}


def test_sensitivity_has_three_rows_with_gain_and_reduction():
    """Drill, 1.800 s assumption, and 99,1 percent literature; every row compares against the same soak and TTR mean."""
    drill = {"session": "S6", "outcome": "recovered", "recovery_s": 600.0, "restore_completed": True}
    soak = {"session": "S7", "heights_collected": True, "kill_outcome": "complete"}
    analysis = {"soak": {"availability": 0.999, "downtime_s": 86.4}}
    sessions = [make_data({"drill": drill, "soak": soak}, analysis, session="S7")]
    rows = sensitivity_rows(sessions, [failover_row("a", 60.0), failover_row("a", 80.0)])
    assert [row["baseline_s"] for row in rows] == [600.0, 1800.0, pytest.approx(777.6)]
    assert rows[0]["baseline_availability"] == pytest.approx((86400 - 600) / 86400 * 100)
    assert rows[1]["baseline_availability"] == pytest.approx(97.91666, abs=1e-4)
    assert rows[2]["baseline_availability"] == pytest.approx(99.1)
    assert rows[0]["system_availability"] == pytest.approx(99.9)
    assert rows[1]["gain_pp"] == pytest.approx(99.9 - (86400 - 1800) / 86400 * 100)
    assert rows[0]["reduction_pct"] == pytest.approx((600 - 70) / 600 * 100)
    assert rows[2]["reduction_pct"] == pytest.approx((777.6 - 70) / 777.6 * 100)


def test_sensitivity_without_a_drill_or_soak_keeps_three_rows_with_empty_cells():
    """The assumption and the literature value need no measurement; the drill and system cells stay empty."""
    rows = sensitivity_rows([make_data()], [])
    assert [row["baseline_s"] for row in rows] == [None, 1800.0, pytest.approx(777.6)]
    assert rows[1]["system_availability"] is None and rows[1]["gain_pp"] is None
    assert rows[1]["reduction_pct"] is None
    assert "tidak dijalankan" in rows[0]["baseline"]


def test_sensitivity_ignores_a_drill_that_did_not_recover():
    """A drill without a recovery time is not a baseline."""
    drill = {"session": "S6", "outcome": "no_signature", "recovery_s": None}
    rows = sensitivity_rows([make_data({"drill": drill})], [])
    assert rows[0]["baseline_s"] is None


def test_sensitivity_uses_the_configuration_with_most_valid_ttr():
    """Two configurations are never pooled; the larger one gives the TTR mean and the basis names it."""
    failover = [failover_row("a", 10.0), failover_row("b", 50.0), failover_row("b", 70.0), failover_row("b", None)]
    rows = sensitivity_rows([make_data()], failover)
    assert rows[1]["reduction_pct"] == pytest.approx((1800 - 60) / 1800 * 100)
    assert "b" in rows[1]["basis"] and "n = 2" in rows[1]["basis"]


def test_block_rate_rows_expand_windows_per_incident():
    """One row per window with the session's offset."""
    incident = {"session": "S9", "iteration": 1, "block_rates": [
        {"label": "before_fault_60s", "start_ms": 1, "end_ms": 2, "covered": True, "blocks": 20,
         "blocks_per_minute": 20.0}]}
    failover = [{"session": "S9", "iteration": 1, "config_id": "c", "ttr_s": 20.0}]
    rows = block_rate_rows([make_data(analysis={"failover": failover, "incidents": [incident]})])
    assert rows[0]["utc_offset_seconds"] == 25200 and rows[0]["blocks"] == 20


def test_block_rate_table_names_the_window_after_the_primary_signs_again(tmp_path):
    """The fourth window reads 120 detik setelah tanda tangan pertama primer, like the other windows' Indonesian names."""
    from harness.report import BLOCK_RATE_COLUMNS
    incident = {"session": "S9", "iteration": 1, "block_rates": [
        {"label": "after_failback_120s", "start_ms": 1791168879620, "end_ms": 1791168999620, "covered": True,
         "blocks": 38, "blocks_per_minute": 19.0}]}
    failover = [{"session": "S9", "iteration": 1, "config_id": "c", "ttr_s": 20.0}]
    rows = block_rate_rows([make_data(analysis={"failover": failover, "incidents": [incident]})])
    _, md_path = write_table(tmp_path, "laju", "Laju", BLOCK_RATE_COLUMNS, rows)
    assert "| S9 | 1 | 120 detik setelah tanda tangan pertama primer | 09:54:39,620 | 38 | 19,00 | Ya |" in md_path.read_text()


def snapshot_node(label: str) -> dict:
    """A snapshot node with no key-side file and no lock."""
    return {"node": label, "state": {"height": 100, "round": 0, "step": 3}, "keys": [{"exists": False}] * 3, "locks": []}


def failover_session(session: str, iterations: int, stopped: bool = False):
    """A session with failover, failback, incident, and snapshot rows for every iteration; stopped adds a series stop."""
    numbers = range(1, iterations + 1)
    failover = [{"session": session, "iteration": n, "config_id": "c", "ttd_s": 10.0, "ttr_s": 20.0,
                 "invalid_reason": None} for n in numbers]
    failback = [{"session": session, "iteration": n, "config_id": "c", "t_failback_initiated_ms": None,
                 "t_failback_notified_ms": None, "t_release_complete_ms": None, "t_failback_complete_ms": None}
                for n in numbers]
    window = {"label": "before_fault_60s", "start_ms": 0, "end_ms": 60_000, "covered": False, "blocks": None,
              "blocks_per_minute": None}
    incidents = [{"session": session, "iteration": n, "missed_share": None, "block_rates": [window]} for n in numbers]
    snapshots = [{"session": session, "iteration": 1, "point": "before_fault", "t_ms": 1_000,
                  "nodes": [snapshot_node("validator1"), snapshot_node("validator2")]}]
    stops = [{"iteration": iterations, "outcome": "no_failback"}] if stopped else []
    analysis = {"failover": failover, "failback": failback, "incidents": incidents, "series_aborted": stops}
    return make_data(analysis=analysis, session=session, snapshots=snapshots)


def test_failover_tables_keep_only_the_series_session():
    """The stopped S4 and the one-iteration raw capture S12 leave every failover table; only the S5 series stays."""
    from harness.report import (decomposition_rows, failback_rows, failover_rows, failover_table_rows,
                                snapshot_rows)
    sessions = [failover_session("S4", 1, stopped=True), failover_session("S5", 10), failover_session("S12", 1)]
    for builder in (failover_table_rows, failover_rows, failback_rows, decomposition_rows, block_rate_rows,
                    snapshot_rows):
        assert {row["session"] for row in builder(sessions)} == {"S5"}, builder.__name__
    assert len(failover_table_rows(sessions)) == 10
    assert not any(row.get("series_stop") for row in failover_table_rows(sessions))


def test_failback_table_splits_the_failback_after_the_execution_column(tmp_path):
    """Mulai ke notifikasi, Notifikasi ke lepas, and Lepas ke selesai follow Eksekusi (s); a missing stamp leaves a dash."""
    from harness.report import FAILBACK_COLUMNS, failback_rows
    headers = [column.header for column in FAILBACK_COLUMNS]
    execution = headers.index("Eksekusi (s)")
    assert headers[execution + 1:execution + 4] == ["Mulai ke notifikasi (s)", "Notifikasi ke lepas (s)",
                                                    "Lepas ke selesai (s)"]
    session = failover_session("S5", 2)
    session.analysis["failback"][0].update(t_failback_initiated_ms=1_000, t_failback_notified_ms=1_077,
                                           t_release_complete_ms=6_939, t_failback_complete_ms=9_616)
    rows = failback_rows([session])
    assert [(row["notify_s"], row["release_s"], row["complete_s"]) for row in rows] == [
        (0.077, 5.862, 2.677), (None, None, None)]
    full_rows = [{**{column: None for column in ("t_primary_healthy_ms", "t_primary_signature_ms", "grace_wait_s",
                                                 "to_complete_s", "to_signature_s", "execution_s", "gfb_s",
                                                 "failback_attempts", "absent_heights")}, "sources": {}, **row}
                 for row in rows]
    _, md_path = write_table(tmp_path, "failback", "Failback", FAILBACK_COLUMNS, full_rows)
    lines = md_path.read_text().splitlines()
    cells = [[cell.strip() for cell in line.strip("|").split("|")][execution + 1:execution + 4] for line in lines[4:]]
    assert cells == [["0,077", "5,862", "2,677"], ["-", "-", "-"]]


def test_snapshot_table_shows_the_local_time_of_each_point(tmp_path):
    """Waktu follows Titik: the snapshot's t_ms as a local clock time on the session's offset; the CSV keeps epoch ms."""
    from harness.report import SNAPSHOT_COLUMNS, snapshot_rows
    headers = [column.header for column in SNAPSHOT_COLUMNS]
    assert headers.index("Waktu") == headers.index("Titik") + 1
    session = failover_session("S5", 1)
    session.snapshots[0]["t_ms"] = 1791168879620
    csv_path, md_path = write_table(tmp_path, "snapshot", "Snapshot", SNAPSHOT_COLUMNS, snapshot_rows([session]))
    assert "| before_fault | 09:54:39,620 | validator1 |" in md_path.read_text()
    assert ",before_fault,1791168879620,validator1," in csv_path.read_text()


def test_the_series_stop_row_stays_when_the_series_session_itself_stopped():
    """S5 stopped after its third iteration: its three rows and its stop row; the stop row of S4 stays out."""
    from harness.report import failover_table_rows
    sessions = [failover_session("S4", 1, stopped=True), failover_session("S5", 3, stopped=True)]
    rows = failover_table_rows(sessions)
    assert [(row["session"], row["iteration"], bool(row.get("series_stop"))) for row in rows] == [
        ("S5", 1, False), ("S5", 2, False), ("S5", 3, False), ("S5", 3, True)]


def fixture_pair(tmp_path):
    """SFIX as the failover series and a copy of it acting as the soak session, as loaded SessionData."""
    from dataclasses import replace

    from harness.report import load_session_data
    directory = build_fixture_session(FIXTURES, tmp_path / "data")
    analyze_session(directory)
    series = load_session_data(directory)
    copy_rows = [{**row, "session": "SSOAK"} for row in series.analysis["failover"]]
    soak = {"session": "SSOAK", "heights_collected": False, "start_ms": 0, "end_ms": 3_600_000, "start_height": 1,
            "end_height": None, "kill_outcome": "complete", "host_pauses": 0, "height_stalls": 0}
    copy = replace(series, metadata={**series.metadata, "session": "SSOAK"},
                   analysis={**series.analysis, "session": "SSOAK", "failover": copy_rows}, optional={"soak": soak})
    return series, copy


def test_summaries_are_per_session_and_the_soak_failover_is_not_pooled(tmp_path):
    """The soak session's failover stays out of Tabel 4.6 and the failover table; the summary names its session."""
    from harness.report import failback_rows, failover_rows
    series, soak = fixture_pair(tmp_path)
    rows = failover_rows([series, soak])
    assert {row["session"] for row in rows} == {"SFIX"}
    assert {row["session"] for row in failback_rows([series, soak])} == {"SFIX"}
    summary = summary_rows(rows, (("ttr_s", "TTR"),))
    assert [(item["session"], item["n"]) for item in summary] == [("SFIX", 1)]


def test_summary_groups_by_session_before_configuration():
    """The same config_id in two sessions gives two summaries."""
    rows = [{"session": "A", "config_id": "c", "ttr_s": 10.0}, {"session": "B", "config_id": "c", "ttr_s": 30.0}]
    assert [(item["session"], item["mean"]) for item in summary_rows(rows, (("ttr_s", "TTR"),))] == [("A", 10.0), ("B", 30.0)]


def test_aborted_series_shows_as_a_row_of_the_failover_table(tmp_path):
    """A series stopped after iteration 2 adds a row that names the iteration and the outcome in Indonesian."""
    from harness.report import FAILOVER_COLUMNS, failover_table_rows
    series, _ = fixture_pair(tmp_path)
    series.analysis["series_aborted"] = [{"iteration": 2, "outcome": "no_failback"}]
    rows = failover_table_rows([series])
    assert len(rows) == 2
    _, md_path = write_table(tmp_path, "f", "F", FAILOVER_COLUMNS, rows)
    assert "Seri dihentikan setelah iterasi 2: takeover terjadi tetapi failback tidak selesai" in md_path.read_text()


def test_markdown_shows_indonesian_labels_and_csv_keeps_the_code(tmp_path):
    """Outcome and action codes read as words in the Markdown table and stay codes in the CSV."""
    from harness.report import FAILOVER_COLUMNS, HANG_COLUMNS
    series, _ = fixture_pair(tmp_path)
    row = {**series.analysis["failover"][0], "outcome": "no_takeover", "utc_offset_seconds": 25200}
    csv_path, md_path = write_table(tmp_path, "f", "F", FAILOVER_COLUMNS, [row])
    assert "takeover tidak terjadi" in md_path.read_text() and "no_takeover" in csv_path.read_text()
    hang = {"session": "S5", "iteration": 1, "outcome": "detected", "detected": True, "ttd_s": 30.0,
            "restart_outcome": "failed", "action": "kill", "reason": "r", "detection_source": "x", "clear_ok": True,
            "ttr_s": None, "followup": "not_needed"}
    csv_path, md_path = write_table(tmp_path, "h", "H", HANG_COLUMNS, [hang])
    text = md_path.read_text()
    assert "terdeteksi" in text and "gagal" in text and "docker kill" in text
    assert ",kill," in csv_path.read_text() and ",failed," in csv_path.read_text()


def test_yes_no_reads_ya_and_tidak():
    """Capitalised, as the examiner's columns are."""
    from harness.report import yes_no
    assert (yes_no(True, {}), yes_no(False, {})) == ("Ya", "Tidak")


def test_evidence_is_ordered_by_event_time(tmp_path):
    """Sumber Bukti lists the instants in the order they happened, not in dictionary order."""
    from harness.report import evidence_text
    series, _ = fixture_pair(tmp_path)
    row = series.analysis["failover"][0]
    text = evidence_text({**row, "sources": dict(reversed(list(row["sources"].items())))})
    labels = [part.split(":")[0] for part in text.split("; ")]
    assert labels == ["gagal", "cek gagal pertama", "deteksi", "alih peran", "tanda tangan pertama"]
    failback = series.analysis["failback"][0]
    order = [part.split(":")[0] for part in evidence_text(failback).split("; ")]
    assert order == ["cek sehat primer", "failback dipertimbangkan", "failback dimulai",
                     "cadangan menerima notifikasi failback", "cadangan lepas", "failback selesai", "tanda tangan primer"]


def test_session_map_uses_the_table_time_format(tmp_path):
    """Live sessions show a decimal comma in the milliseconds; archived rows keep the seconds the source has."""
    from harness.report import session_map_rows
    series, _ = fixture_pair(tmp_path)
    rows = session_map_rows([series], ARCHIVED_SESSIONS_CSV, tmp_path / "notes.csv")
    assert rows[0]["start"] == "2026-06-18 12:00:11"
    assert rows[-1]["start"] == "2026-10-05 09:52:48,000"
    assert "," in rows[-1]["end"] and "." not in rows[-1]["end"]


NOTES_HEADER = "sesi,tujuan,bukti\n"


def test_session_map_takes_purpose_and_evidence_from_a_notes_row(tmp_path):
    """A session with a row in session-notes.csv shows its tujuan and bukti; one without keeps metadata purpose and path."""
    from harness.report import session_map_rows
    series, soak = fixture_pair(tmp_path)
    notes = tmp_path / "session-notes.csv"
    notes.write_text(NOTES_HEADER + 'SFIX,"Uji kering, bukan pengukuran",tabel-4-3-failover; logs/smoke-active.txt\n',
                     encoding="utf-8")
    rows = session_map_rows([series, soak], tmp_path / "no-archive.csv", notes)
    assert [(row["session"], row["purpose"], row["source"]) for row in rows] == [
        ("SFIX", "Uji kering, bukan pengukuran", "tabel-4-3-failover; logs/smoke-active.txt"),
        ("SSOAK", series.metadata["purpose"], "experiments/data/SSOAK/metadata.json")]


def test_session_map_without_a_notes_file_keeps_the_metadata(tmp_path):
    """No session-notes.csv: purpose and metadata path, as before; the evidence column is Bukti yang dihasilkan."""
    from harness.report import SESSION_MAP_COLUMNS, session_map_rows
    series, _ = fixture_pair(tmp_path)
    rows = session_map_rows([series], tmp_path / "no-archive.csv", tmp_path / "session-notes.csv")
    assert (rows[0]["purpose"], rows[0]["source"]) == (series.metadata["purpose"], "experiments/data/SFIX/metadata.json")
    headers = [column.header for column in SESSION_MAP_COLUMNS]
    assert headers[-1] == "Bukti yang dihasilkan" and "Sumber" not in headers


def test_session_notes_need_the_three_columns(tmp_path):
    """A notes file with a misspelled header is refused with the expected columns, never silently ignored."""
    from harness.report import session_map_rows
    series, _ = fixture_pair(tmp_path)
    notes = tmp_path / "session-notes.csv"
    notes.write_text("sesi,tujuan,sumber\nSFIX,x,y\n", encoding="utf-8")
    with pytest.raises(ValueError, match="bukti"):
        session_map_rows([series], tmp_path / "no-archive.csv", notes)


def test_report_reads_the_session_notes_beside_the_session_folders(tmp_path):
    """write_outputs passes data/session-notes.csv, the data directory of the sessions, to the session map."""
    from harness.outputs import write_outputs
    directory = build_fixture_session(FIXTURES, tmp_path / "data")
    analyze_session(directory)
    (tmp_path / "data" / "session-notes.csv").write_text(NOTES_HEADER + "SFIX,Uji kering,tabel-4-3-failover\n",
                                                          encoding="utf-8")
    write_outputs([directory], tmp_path / "out", ARCHIVED_SESSIONS_CSV)
    assert "| SFIX | Uji kering |" in (tmp_path / "out" / "tabel-peta-sesi.md").read_text()
    assert "| tabel-4-3-failover |" in (tmp_path / "out" / "tabel-peta-sesi.md").read_text()


def test_write_outputs_needs_a_session(tmp_path):
    """Without a session there is no data directory to read notes and campaign files from."""
    from harness.outputs import write_outputs
    with pytest.raises(ValueError, match="at least one session"):
        write_outputs([], tmp_path / "out", ARCHIVED_SESSIONS_CSV)


TENDERDUTY_SIGNING = {"missed_blocks_counter": 4, "jailed_until": "1970-01-01T00:00:00Z", "tombstoned": False}


TENDERDUTY_HEADER = ("| Sesi | Rentang tinggi insiden | Terlewat menurut log Tenderduty | Tinggi absen 48DC2183 (commit) "
                     "| Absen pada blok usulan 48DC2183 | Selisih | Terlewat menurut penghitung Tenderduty (seluruh sesi) "
                     "| Rentang tinggi sesi | Tinggi tanpa data commit | missed_blocks_counter | jailed_until |")
LOG_EXCERPT = FIXTURES / "tenderduty" / "tenderduty-run.txt"  # lines copied from data/tenderduty/tenderduty-run.log
# Lines in the shape of that log: the start of a subscription, a progress line, and a missed-block warning.
WATCH_TEXT = ("2026/10/09 17:25:35 tenderduty |  ⚙️ story-localnet watching for NewBlock and Vote events via "
              "tcp://localhost:26657\n")
PROGRESS_TEXT = "2026/10/09 20:37:22 tenderduty |  🧊 story-localnet block {height}\n"
MISSED_TEXT = ("2026/10/09 20:37:22 tenderduty |  ❌ warning      0x0FC41199CE588948861A8DA86D725A5A073AE91A missed block "
               "{height} on story-localnet\n")


def tenderduty_log_text(first: int, last: int, missed) -> str:
    """One subscription of the Tenderduty log whose progress lines name first and last, with a warning per missed block."""
    lines = [WATCH_TEXT, PROGRESS_TEXT.format(height=first)]
    lines += [MISSED_TEXT.format(height=height) for height in missed]
    lines.append(PROGRESS_TEXT.format(height=last))
    return "".join(lines)


def incident_session(tmp_path, result: dict, failback: list | None = None, session: str = "S10", **evidence):
    """A session with the optional result files result, S10's incident on disk, and the failback rows."""
    from fakes import write_incident_evidence
    directory = tmp_path / session
    write_incident_evidence(directory, session, **evidence)
    return make_data(result, {"failback": failback or []}, session=session, directory=directory)


def s10_soak(tmp_path, primary_signature_height: int | None = 14890, **evidence):
    """S10 as a soak with signing info: its incident on disk and the failback row of iteration 1."""
    failback = [{"iteration": 1, "primary_signature_height": primary_signature_height}]
    return incident_session(tmp_path, {"soak": {"session": "S10"}, "safety": {"signing_info": TENDERDUTY_SIGNING}},
                            failback, **evidence)


def tenderduty_table(tmp_path, session, log_path=LOG_EXCERPT) -> tuple:
    """(the session's one Tenderduty row, the Markdown text and the CSV text of the table it gives)."""
    from harness.report import TENDERDUTY_COLUMNS, tenderduty_rows
    from harness.tenderduty import read_tenderduty_log
    rows = tenderduty_rows([session], read_tenderduty_log(log_path))
    csv_path, md_path = write_table(tmp_path, "tenderduty", "Tenderduty", TENDERDUTY_COLUMNS, rows)
    return rows[0], md_path.read_text(), csv_path.read_text()


def test_the_incident_runs_from_the_fault_height_to_the_first_signature_on_the_primary(tmp_path):
    """Soak: the fault at 14858 to the primary's first signature after failback in the row of the fault's iteration,
    14890; the row of another iteration is not read. Drill: to its first signature after recovery."""
    from harness.report import incident_range
    failback = [{"iteration": 2, "primary_signature_height": 15000}, {"iteration": 1, "primary_signature_height": 14890}]
    soak = incident_session(tmp_path, {"soak": {"session": "S10"}}, failback)
    assert incident_range(soak) == ((14858, 14890), None)
    drill = incident_session(tmp_path, {"drill": {"session": "S11", "first_signature_height": 14880}}, session="S11")
    assert incident_range(drill) == ((14858, 14880), None)


def test_an_incident_without_a_stamp_has_no_range_and_says_why(tmp_path):
    """No primary signature after failback, no failback row, a drill without a first signature, a fault without a
    height, and no fault at all."""
    from harness.jsonio import append_jsonl
    from harness.report import incident_range
    no_primary = "Sesi S10: iteration 1 has no first signature of the primary after failback (primary_signature_height)"
    soak = incident_session(tmp_path, {"soak": {}}, [{"iteration": 1, "primary_signature_height": None}])
    assert incident_range(soak) == (None, no_primary)
    assert incident_range(incident_session(tmp_path / "rowless", {"soak": {}})) == (None, no_primary)
    drill = incident_session(tmp_path, {"drill": {"outcome": "no_signature", "first_signature_height": None}},
                             session="S11")
    assert incident_range(drill) == (
        None, "Sesi S11: the drill has no first signature after recovery (first_signature_height of drill.json)")
    append_jsonl(soak.directory / "events.jsonl", {"session": "S10", "t_ms": 1791553100000, "kind": "fault_injected",
                                                   "scenario": "failover", "iteration": 2, "exit_code": 0})
    assert incident_range(soak) == (None, "Sesi S10: the latest fault_injected event has no height (iteration 2)")
    faultless = make_data({"soak": {}}, session="S12", directory=tmp_path / "S12")
    assert incident_range(faultless) == (None, "Sesi S12 has no fault_injected event")


def test_tenderduty_row_compares_the_log_and_the_chain_over_the_incident(tmp_path):
    """S10's incident, the commits of 14858 to 14889: the log names 10 missed blocks for it and the chain 10 absent
    heights, none before a block 48DC2183 proposed, so Selisih is 0; the nil vote at 14875 counts on neither side.
    Without readings the session-wide cells stay empty."""
    row, text, raw = tenderduty_table(tmp_path, s10_soak(tmp_path))
    assert (row["incident_first"], row["incident_last"]) == (14858, 14889)
    assert (row["logged_missed"], row["chain_absent"], row["absent_on_proposed"], row["difference"]) == (10, 10, 0, 0)
    assert TENDERDUTY_HEADER in text
    assert "| S10 | 14.858 sampai 14.889 | 10 | 10 | 0 | 0 | - | - | - | 4 | 1970-01-01T00:00:00Z |" in text
    assert "S10,14858 sampai 14889,10,10,0,0,,,,4,1970-01-01T00:00:00Z" in raw


def test_tenderduty_row_shows_the_counter_of_the_whole_session_beside_the_incident(tmp_path):
    """Readings at tips 14850 and 14896 cover the commits of 14850 to 14895, all stored: Tenderduty's counter rose by
    12 over the session, 10 of them in the incident."""
    from harness.jsonio import append_jsonl
    session = s10_soak(tmp_path)
    for point, tip, missed in (("start", 14850, 5), ("end", 14896, 17)):
        append_jsonl(session.directory / "tenderduty.jsonl", {"session": "S10", "point": point, "t_ms": tip * 1000,
                                                              "missed_blocks": missed, "tip_height": tip})
    _, text, _ = tenderduty_table(tmp_path, session)
    assert "| S10 | 14.858 sampai 14.889 | 10 | 10 | 0 | 0 | 12 | 14.850 sampai 14.895 | 0 | 4 |" in text


def test_tenderduty_row_counts_an_absent_height_before_a_proposed_block_apart(tmp_path):
    """48DC2183 absent at 14875 and the proposer of block 14876, which Tenderduty reports as proposed: the chain counts
    11 absent heights, 1 of them before a block 48DC2183 proposed, and Selisih stays 0."""
    from fakes import INCIDENT_VOTES
    from harness.constants import TARGET_VALIDATOR
    session = s10_soak(tmp_path, votes={**INCIDENT_VOTES, 14875: "absent"}, proposers={14876: TARGET_VALIDATOR})
    row, text, _ = tenderduty_table(tmp_path, session)
    assert (row["logged_missed"], row["chain_absent"], row["absent_on_proposed"], row["difference"]) == (10, 11, 1, 0)
    assert "| S10 | 14.858 sampai 14.889 | 10 | 11 | 1 | 0 |" in text


def test_tenderduty_row_with_an_incident_height_without_commit_data_leaves_the_chain_counts_empty(tmp_path):
    """Heights 14850 to 14880 only: the log count stands, and the chain counts and Selisih stay empty rather than
    counting a shorter range."""
    row, text, _ = tenderduty_table(tmp_path, s10_soak(tmp_path, last_height=14880))
    assert (row["logged_missed"], row["chain_absent"], row["absent_on_proposed"], row["difference"]) == (
        10, None, None, None)
    assert "| S10 | 14.858 sampai 14.889 | 10 | - | - | - | - | - | - | 4 |" in text


def test_tenderduty_rows_need_an_incident_that_the_log_covers(tmp_path):
    """No row without the log, with a log whose last subscription ends at block 14863 inside the incident, for an
    incident without its end stamp, or for a session that is neither a soak nor a drill, readings or not."""
    from fakes import write_tenderduty_evidence
    from harness.report import tenderduty_rows
    from harness.tenderduty import read_tenderduty_log
    log = read_tenderduty_log(LOG_EXCERPT)
    assert len(tenderduty_rows([s10_soak(tmp_path)], log)) == 1
    assert tenderduty_rows([s10_soak(tmp_path / "a")], read_tenderduty_log(tmp_path / "none.log")) == []
    lines = LOG_EXCERPT.read_text(encoding="utf-8").splitlines(keepends=True)
    (tmp_path / "short.log").write_text("".join(lines[:72]), encoding="utf-8")
    assert tenderduty_rows([s10_soak(tmp_path / "b")], read_tenderduty_log(tmp_path / "short.log")) == []
    assert tenderduty_rows([s10_soak(tmp_path / "c", primary_signature_height=None)], log) == []
    series = incident_session(tmp_path / "d", {}, [{"iteration": 1, "primary_signature_height": 14890}])
    write_tenderduty_evidence(series.directory, "S10")
    assert tenderduty_rows([series], log) == []


def test_the_session_cells_cover_the_reading_pair_with_edges_from_tenderduty_heights(tmp_path):
    """Tips 98 and 112 cover 98 to 111; heights.jsonl holds 100 to 109 and tenderduty-heights.jsonl the edges and once
    more 105, so no height lacks commit data; Tenderduty's counter rose from 2 to 6."""
    from fakes import tenderduty_height_line, write_tenderduty_evidence
    from harness.jsonio import append_jsonl
    from harness.report import reading_pair_cells
    directory = tmp_path / "S10"
    write_tenderduty_evidence(directory, "S10", start_tip=98, end_tip=112, missed=(2, 6))
    for height, vote in ((98, "signed"), (99, "signed"), (105, "absent"), (110, "absent"), (111, "signed")):
        append_jsonl(directory / "tenderduty-heights.jsonl", tenderduty_height_line("S10", height, vote))
    assert reading_pair_cells(make_data(session="S10", directory=directory)) == {
        "counter_missed": 4, "session_first": 98, "session_last": 111, "missing_heights": 0}


def test_the_session_range_of_a_hand_written_start_uses_its_resolved_tip(tmp_path):
    """A start written by hand has no tip: the start_tip_resolved line that names its t_ms gives it, and without that
    line the range and its missing heights stay empty while the counter change stands."""
    from fakes import TENDERDUTY_T0, write_tenderduty_evidence
    from harness.jsonio import append_jsonl
    from harness.report import reading_pair_cells
    directory = tmp_path / "S10"
    write_tenderduty_evidence(directory, "S10", start_tip=None)
    session = make_data(session="S10", directory=directory)
    assert reading_pair_cells(session) == {"counter_missed": 3, "session_first": None, "session_last": None,
                                           "missing_heights": None}
    append_jsonl(directory / "tenderduty.jsonl", {"session": "S10", "point": "start_tip_resolved",
                                                  "start_t_ms": TENDERDUTY_T0 + 1000, "tip_height": 101,
                                                  "t_ms": TENDERDUTY_T0 + 26_000})
    assert reading_pair_cells(session) == {"counter_missed": 3, "session_first": 101, "session_last": 108,
                                           "missing_heights": 0}


def test_the_session_cells_count_the_heights_of_the_pair_without_commit_data(tmp_path):
    """Tips 101 and 112 cover 101 to 111, but no file holds 110 and 111: Tinggi tanpa data commit reads 2. Only a
    start reading: every session-wide cell stays empty."""
    from fakes import write_tenderduty_evidence
    from harness.report import reading_pair_cells
    directory = tmp_path / "S10"
    write_tenderduty_evidence(directory, "S10", end_tip=112)
    session = make_data(session="S10", directory=directory)
    assert reading_pair_cells(session) == {"counter_missed": 3, "session_first": 101, "session_last": 111,
                                           "missing_heights": 2}
    readings = (directory / "tenderduty.jsonl").read_text().splitlines()
    (directory / "tenderduty.jsonl").write_text(readings[0] + "\n")
    assert reading_pair_cells(session) == {"counter_missed": None, "session_first": None, "session_last": None,
                                           "missing_heights": None}


def sfix_drill(tmp_path):
    """The fixture session with a drill.json whose first signature after recovery is 144, the backup's first signature.

    Its incident runs from the fault at 127: 48DC2183 is absent at 128 to 142 and votes nil at 143.
    """
    from harness.jsonio import write_json
    directory = build_fixture_session(FIXTURES, tmp_path / "data")
    analyze_session(directory)
    write_json(directory / "drill.json", {"session": "SFIX", "operator": "uji", "iteration": 1, "outcome": "recovered",
                                          "first_signature_height": 144})
    return directory


def test_write_reports_writes_the_tenderduty_table_for_an_incident_the_log_covers(tmp_path):
    """tabel-tenderduty carries its Indonesian title; without the log it is listed as a table without data. With a log
    that names the blocks 129 to 143 missed, the incident's 15 absent commits 128 to 142 match; the nil vote at
    143 counts on neither side."""
    directory = sfix_drill(tmp_path)
    log = tmp_path / "tenderduty-run.log"
    index = write_reports([directory], tmp_path / "none", ARCHIVED_SESSIONS_CSV, tmp_path / "notes.csv", log)[-1]
    assert "tabel-tenderduty: tanpa data" in index.read_text()
    log.write_text(tenderduty_log_text(120, 160, range(129, 144)), encoding="utf-8")
    names = {path.name for path in write_reports([directory], tmp_path / "some", ARCHIVED_SESSIONS_CSV,
                                                 tmp_path / "notes.csv", log)}
    assert {"tabel-tenderduty.md", "tabel-tenderduty.csv"} <= names
    text = (tmp_path / "some" / "tabel-tenderduty.md").read_text()
    assert text.splitlines()[0] == "Blok terlewat menurut Tenderduty dan tanda tangan commit"
    assert "| SFIX | 127 sampai 143 | 15 | 15 | 0 | 0 |" in text


def test_report_reads_the_tenderduty_log_beside_the_session_folders(tmp_path):
    """write_outputs passes data/tenderduty/tenderduty-run.log, in the data directory of the sessions, to the table and
    to the tokens."""
    from harness.jsonio import read_json
    from harness.outputs import write_outputs
    directory = sfix_drill(tmp_path)
    log = tmp_path / "data" / "tenderduty" / "tenderduty-run.log"
    log.parent.mkdir(parents=True)
    log.write_text(tenderduty_log_text(120, 160, range(129, 144)), encoding="utf-8")
    write_outputs([directory], tmp_path / "out", ARCHIVED_SESSIONS_CSV)
    assert "| SFIX | 127 sampai 143 | 15 | 15 | 0 | 0 |" in (tmp_path / "out" / "tabel-tenderduty.md").read_text()
    values = read_json(tmp_path / "out" / "values.json")["values"]
    assert (values["tenderduty.drill.missed"], values["tenderduty.drill.chain_absent"]) == ("15", "15")


def test_sensitivity_uses_the_last_drill_and_soak_with_their_status():
    """An unrecovered last drill is the drill row (status shown, no baseline); a soak without heights gives no availability."""
    recovered = {"session": "S6", "outcome": "recovered", "recovery_s": 600.0}
    failed = {"session": "S8", "outcome": "no_signature", "recovery_s": None}
    soak = {"session": "S9", "heights_collected": False}
    sessions = [make_data({"drill": recovered}, session="S6"), make_data({"drill": failed}, session="S8"),
                make_data({"soak": soak}, session="S9")]
    rows = sensitivity_rows(sessions, [])
    assert rows[0]["baseline_s"] is None
    assert "S8" in rows[0]["baseline"] and "no_signature" not in rows[0]["baseline"]
    assert "tanpa tanda tangan" in rows[0]["baseline"]
    assert rows[1]["system_availability"] is None


def test_failover_figure_states_what_the_marks_are():
    """The axis label and the caption say points are at block header time and the dotted line at the precommit time."""
    from harness.figures import FAILOVER_NOTE
    assert "waktu header blok" in FAILOVER_NOTE and "precommit" in FAILOVER_NOTE


def test_failover_caption_carries_the_note_but_the_plot_title_stays_short(tmp_path):
    """The note belongs in the caption and on the axis; the figure title is only session, iteration, and heights."""
    from harness.figures import FAILOVER_NOTE
    from harness.report import figure_outputs
    series, _ = fixture_pair(tmp_path)
    _, captions = figure_outputs([series], tmp_path / "fig")
    assert captions[0][1].endswith(FAILOVER_NOTE)


def test_lock_table_uses_indonesian_methods_and_probe_outcome(tmp_path):
    """The lock table matches the lock.* tokens: words in Markdown, codes in the CSV."""
    from harness.report import LOCK_COLUMNS, lock_rows
    lock = {"session": "S8", "methods": ["lockprobe", "unit_tests"], "lock_content_before": "1", "lock_content_after": "1",
            "active_pid": 1, "probe": {"outcome": "refused"}, "unit_test_exit_code": 0, "passed": True}
    csv_path, md_path = write_table(tmp_path, "l", "L", LOCK_COLUMNS, lock_rows([make_data({"lock_negative": lock}, session="S8")]))
    assert "lockprobe dan uji unit" in md_path.read_text() and "ditolak" in md_path.read_text()
    assert "refused" in csv_path.read_text() and "unit_tests" in csv_path.read_text()


def test_series_stop_column_prints_tidak_on_normal_rows(tmp_path):
    """Seri dihentikan is Tidak for a normal iteration, not a dash."""
    from harness.report import FAILOVER_COLUMNS
    series, _ = fixture_pair(tmp_path)
    row = {**series.analysis["failover"][0], "utc_offset_seconds": 25200}
    _, md_path = write_table(tmp_path, "f", "F", FAILOVER_COLUMNS, [row])
    assert md_path.read_text().rstrip().endswith("| Tidak |")


def test_invalid_reasons_are_indonesian_in_markdown_and_codes_stay_in_csv(tmp_path):
    """Both the iteration column and the summary column translate the stored codes."""
    from harness.report import FAILOVER_COLUMNS, SUMMARY_COLUMNS
    series, _ = fixture_pair(tmp_path)
    reasons = {"no signing gap observed": "tidak ada celah penandatanganan teramati",
               "no signature after the gap": "tidak ada tanda tangan setelah celah",
               "negative orchestration_s": "durasi orkestrasi negatif",
               "negative node_start_s": "durasi start node negatif"}
    for code, word in reasons.items():
        row = {**series.analysis["failover"][0], "utc_offset_seconds": 25200, "invalid_reason": code, "ttr_s": None}
        csv_path, md_path = write_table(tmp_path, "f", "F", FAILOVER_COLUMNS, [row])
        assert word in md_path.read_text() and code in csv_path.read_text()
        summary = summary_rows([{**row, "session": "SFIX"}], (("ttr_s", "TTR"),))
        _, md_path = write_table(tmp_path, "s", "S", SUMMARY_COLUMNS, summary)
        assert word in md_path.read_text()


def test_summary_keeps_a_metric_row_with_n_zero_and_its_reasons(tmp_path):
    """When every iteration is invalid the summary still shows n 0, the iteration count, and why."""
    rows = [{"session": "S", "config_id": "a", "ttr_s": None, "invalid_reason": "no signing gap observed"}]
    summary = summary_rows(rows, (("ttr_s", "TTR"),))
    assert [(item["n"], item["total"], item["mean"]) for item in summary] == [(0, 1, None)]
    assert summary[0]["invalid"] == "no signing gap observed (1)"


def partition_result(**fields) -> dict:
    """A partition-1.json analysis row with the fields the table reads."""
    row = {"session": "S9", "iteration": 1, "outcome": "complete", "network": "net", "fault_height": 1200,
           "detected": True, "ttd_s": 33.0, "takeover": True, "ttr_s": 60.0, "failback_complete": True,
           "detection_symptom": "rpc_refused",
           "monitor": {"observed": True, "observed_problems": [], "dual_signer_seconds": 0, "indeterminate_seconds": 0}}
    return {**row, "monitor": {**row["monitor"], **fields.pop("monitor", {})}, **fields}


def test_partition_rows_flatten_the_monitor_and_keep_the_session():
    """One row per run with the monitor fields as columns."""
    rows = partition_rows([make_data(analysis={"partition": [partition_result()]}), make_data(session="S8")])
    assert len(rows) == 1
    assert (rows[0]["session"], rows[0]["fault_height"], rows[0]["monitor_observed"],
            rows[0]["monitor_dual_signer_seconds"]) == (
        "S9", 1200, True, 0)


def test_partition_table_reads_in_indonesian_and_never_shows_an_unobserved_zero(tmp_path):
    """Ya and Tidak words, the outcome label, and a count from an unobserved monitor flagged."""
    from harness.report import PARTITION_COLUMNS
    observed = partition_rows([make_data(analysis={"partition": [partition_result()]})])
    unobserved = partition_rows([make_data(analysis={"partition": [partition_result(
        outcome="not_detected", detected=False, ttd_s=None, takeover=False, ttr_s=None, failback_complete=False,
        monitor={"observed": False, "dual_signer_seconds": 0})]})])
    _, md_path = write_table(tmp_path, "p", "P", PARTITION_COLUMNS, observed + unobserved)
    text = md_path.read_text()
    assert "pemutusan, takeover, penyambungan kembali, dan failback selesai" in text and "partisi tidak terdeteksi" in text
    assert "tanpa intervensi" not in text
    assert "| Ya |" in text and "| Tidak |" in text
    assert "0 (tidak teramati penuh)" in text
    assert "koneksi RPC ditolak" in text and "Gejala deteksi" in text


def test_partition_table_flags_indeterminate_seconds_and_lists_the_problems(tmp_path):
    """A zero dual-signer count with indeterminate seconds never reads as a clean 0, and the problems are shown."""
    from harness.report import PARTITION_COLUMNS
    rows = partition_rows([make_data(analysis={"partition": [partition_result(monitor={
        "observed": False, "observed_problems": [{"code": "gap", "ms": 12_000, "scenario": "partition", "iteration": 1}],
        "dual_signer_seconds": 0, "indeterminate_seconds": 120})]})])
    csv_path, md_path = write_table(tmp_path, "p", "P", PARTITION_COLUMNS, rows)
    text = md_path.read_text()
    assert "0 (tak tentu 120 detik)" in text
    assert "Detik tak tentu" in text and "Masalah pengamatan monitor" in text
    assert "partisi iterasi 1: jeda 12,0 detik antarsampel informatif" in text and '"gap"' not in text
    assert '""code"": ""gap""' in csv_path.read_text()


PORT_REPAIR_WORDS = {
    "not_needed": "tidak diperlukan",
    "restarted": "restart kontainer setelah penyambungan kembali",
    "refused": "ditolak karena kunci validator1 tidak terbukti kunci mock",
    "not_running": "tidak dilakukan karena kontainer tidak berjalan",
    "restart_failed": "restart kontainer gagal",
    "still_missing": "porta host tetap tidak diterbitkan setelah restart",
    "unknown": "porta host tidak terbaca",
    "decision_unfinished": "percobaan berhenti sebelum keputusan perbaikan",
    "restart_unfinished": "percobaan berhenti setelah restart kontainer dikirim, sebelum hasilnya terbaca",
}


def test_port_repair_labels_name_every_state_of_the_runner():
    """Each port_repair state the partition runner stores has its Indonesian words."""
    from harness.labels import PORT_REPAIR_LABELS
    assert PORT_REPAIR_LABELS == PORT_REPAIR_WORDS


def test_partition_table_shows_the_host_port_repair_after_failback(tmp_path):
    """Perbaikan porta host follows Failback selesai; Markdown reads the words, the CSV keeps the codes."""
    from harness.report import PARTITION_COLUMNS
    headers = [column.header for column in PARTITION_COLUMNS]
    assert headers.index("Perbaikan porta host") == headers.index("Failback selesai") + 1
    runs = [partition_result(iteration=number, port_repair=state) for number, state in enumerate(PORT_REPAIR_WORDS, 1)]
    csv_path, md_path = write_table(tmp_path, "p", "P", PARTITION_COLUMNS,
                                    partition_rows([make_data(analysis={"partition": runs})]))
    text = md_path.read_text()
    for words in PORT_REPAIR_WORDS.values():
        assert f"| {words} |" in text, words
    assert "restart_failed" in csv_path.read_text() and "restart_failed" not in text


def test_partition_table_reads_tidak_dicatat_for_a_result_written_before_the_port_check(tmp_path):
    """S8 has no port_repair field: tidak dicatat, never an empty cell or a guessed state."""
    from harness.report import PARTITION_COLUMNS
    old = partition_rows([make_data(analysis={"partition": [partition_result()]})])
    _, md_path = write_table(tmp_path, "p", "P", PARTITION_COLUMNS, old)
    assert md_path.read_text().splitlines()[-1].endswith("| Ya | tidak dicatat |")


def test_write_reports_writes_tabel_partisi_only_when_a_run_exists(tmp_path):
    """The table is skipped without partition results and written with them."""
    from harness.jsonio import write_json
    directory = build_fixture_session(FIXTURES, tmp_path / "data")
    analyze_session(directory)
    names = {path.name for path in write_reports([directory], tmp_path / "none", ARCHIVED_SESSIONS_CSV,
                                                 tmp_path / "notes.csv", tmp_path / "tenderduty-run.log")}
    assert "tabel-partisi.md" not in names
    write_json(directory / "partition-1.json", partition_result(session="SFIX"))
    analyze_session(directory)
    names = {path.name for path in write_reports([directory], tmp_path / "some", ARCHIVED_SESSIONS_CSV,
                                                 tmp_path / "notes.csv", tmp_path / "tenderduty-run.log")}
    assert {"tabel-partisi.md", "tabel-partisi.csv"} <= names


def test_safety_table_shows_monitor_problems_in_indonesian_and_keeps_the_codes_in_the_csv(tmp_path):
    """Markdown reads words with the run and the number; the CSV keeps the coded problem."""
    rows = safety_rows([make_data({"safety": safety(False)})])
    csv_path, md_path = write_table(tmp_path, "keamanan", "Keamanan", SAFETY_COLUMNS, rows)
    assert "failover iterasi 1: jeda 9,0 detik antarsampel informatif" in md_path.read_text()
    assert "gap" not in md_path.read_text()
    assert '""code"": ""gap""' in csv_path.read_text()


def test_every_monitor_problem_code_has_indonesian_words():
    """Each code the coverage rule emits, with and without the run it belongs to."""
    from harness.labels import monitor_problem_text
    assert monitor_problem_text({"code": "no_samples"}) == "tidak ada sampel monitor yang informatif"
    assert monitor_problem_text({"code": "late_first_sample", "ms": 7500, "scenario": "hang", "iteration": 2}) == (
        "hang iterasi 2: sampel informatif pertama 7,5 detik setelah awal jendela")
    assert monitor_problem_text({"code": "early_last_sample", "ms": 6000, "scenario": "baseline_drill", "iteration": 4}) == (
        "drill iterasi 4: sampel informatif terakhir 6,0 detik sebelum akhir jendela")
    assert monitor_problem_text({"code": "gap", "ms": 30_000, "scenario": "soak", "iteration": None}) == (
        "soak: jeda 30,0 detik antarsampel informatif")
    assert monitor_problem_text({"code": "sampling_errors", "count": 2}) == "2 kesalahan pengambilan sampel"


def test_hang_table_shows_ttr_and_the_follow_up_in_indonesian(tmp_path):
    """The hang row carries the analysis TTR and the follow-up after the hang, worded in Indonesian."""
    from harness.report import HANG_COLUMNS, hang_rows
    row = {"iteration": 1, "outcome": "complete", "detected": True, "ttd_s": 31.2, "ttr_s": 140.5,
           "restart_outcome": "failed", "action": "kill", "reason": "r", "detection_source": "s:1", "clear_ok": True,
           "followup": "complete"}
    _, md_path = write_table(tmp_path, "hang", "Hang", HANG_COLUMNS, hang_rows([make_data(analysis={"hang": [row]})]))
    text = md_path.read_text()
    assert "TTR (s)" in text and "140,500" in text
    assert "hang terdeteksi, diakhiri, dan tata letak standar pulih" in text
    assert "Tindak lanjut" in text and "failover, failback, dan tanda tangan pertama teramati" in text


def test_probe_labels_cover_every_outcome_the_lockprobe_prints():
    """Each outcome constant of experiments/lockprobe/probe.go, plus the harness's unparsed, has Indonesian words."""
    from harness.constants import EXPERIMENTS_DIR
    from harness.labels import PROBE_LABELS
    source = (EXPERIMENTS_DIR / "lockprobe" / "probe.go").read_text(encoding="utf-8")
    outcomes = {line.split('"')[1] for line in source.splitlines() if line.strip().startswith("outcome") and '"' in line}
    assert outcomes == {"refused", "acquired_and_released", "precondition_failed"}
    assert set(PROBE_LABELS) == outcomes | {"unparsed"}
    assert PROBE_LABELS["acquired_and_released"] == "diperoleh dan dilepas (tidak ditolak)"
    assert PROBE_LABELS["precondition_failed"] == "prasyarat probe tidak terpenuhi"


def test_the_failover_figure_plots_the_first_row_with_a_valid_ttr(monkeypatch):
    """A row with a first signature but no valid TTR (invalid iteration) is skipped for the figure."""
    from harness.report import plotted_failover
    rows = [{"iteration": 1, "t_fault_ms": 10, "t_first_signature_ms": 50, "ttr_s": None},
            {"iteration": 2, "t_fault_ms": 100, "t_first_signature_ms": 140, "ttr_s": 0.04}]
    monkeypatch.setattr("harness.report.session_records", lambda session: ["record"])
    monkeypatch.setattr("harness.report.failover_series", lambda records, validator, t_fault_ms: ("series", t_fault_ms))
    row, series = plotted_failover(make_data(analysis={"failover": rows}))
    assert (row["iteration"], series) == (2, ("series", 100))
    assert plotted_failover(make_data(analysis={"failover": rows[:1]})) is None
