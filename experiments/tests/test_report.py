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
    written = {path.name for path in write_reports([directory], out, ARCHIVED_SESSIONS_CSV)}
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


def safety(observed: bool, dual: int | None = 0, indeterminate: int | None = 0, prometheus: dict | None = None) -> dict:
    """A safety.json with the monitor fields the table reads."""
    return {
        "session": "S9",
        "monitor": {"samples": 100, "dual_signer_seconds": dual, "indeterminate_seconds": indeterminate,
                    "observed": observed, "observed_problems": [] if observed else ["gap of 9000 ms between samples"]},
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
    assert rows[0]["observed_problems"] == "gap of 9000 ms between samples"
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
    soak = {"session": "S7", "heights_collected": True}
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
    rows = block_rate_rows([make_data(analysis={"incidents": [incident]})])
    assert rows[0]["utc_offset_seconds"] == 25200 and rows[0]["blocks"] == 20


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
            "restart_outcome": "failed", "action": "kill", "reason": "r", "detection_source": "x", "clear_ok": True}
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
    rows = session_map_rows([series], ARCHIVED_SESSIONS_CSV)
    assert rows[0]["start"] == "2026-06-18 12:00:11"
    assert rows[-1]["start"] == "2026-10-05 09:52:48,000"
    assert "," in rows[-1]["end"] and "." not in rows[-1]["end"]


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
    assert "takeover dan failback selesai tanpa intervensi" in text and "tidak terdeteksi" in text
    assert "| Ya |" in text and "| Tidak |" in text
    assert "0 (tidak teramati penuh)" in text


def test_write_reports_writes_tabel_partisi_only_when_a_run_exists(tmp_path):
    """The table is skipped without partition results and written with them."""
    from harness.jsonio import write_json
    directory = build_fixture_session(FIXTURES, tmp_path / "data")
    analyze_session(directory)
    names = {path.name for path in write_reports([directory], tmp_path / "none", ARCHIVED_SESSIONS_CSV)}
    assert "tabel-partisi.md" not in names
    write_json(directory / "partition-1.json", partition_result(session="SFIX"))
    analyze_session(directory)
    names = {path.name for path in write_reports([directory], tmp_path / "some", ARCHIVED_SESSIONS_CSV)}
    assert {"tabel-partisi.md", "tabel-partisi.csv"} <= names
