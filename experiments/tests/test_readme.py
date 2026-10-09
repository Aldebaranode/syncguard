"""The measurement protocol names every marker the parser recognises."""

from harness.constants import EXPERIMENTS_DIR
from harness.logparse import PREFIX_MARKERS
from harness.report import FAILBACK_METRICS, SAFETY_COLUMNS, SENSITIVITY_COLUMNS
from harness.values import TOKEN_NAMES


def readme_text() -> str:
    """experiments/README.md as text."""
    return (EXPERIMENTS_DIR / "README.md").read_text(encoding="utf-8")


def test_readme_lists_every_marker_message():
    """Each prefix appears verbatim (trailing separators stripped)."""
    text = readme_text()
    for marker, message in PREFIX_MARKERS:
        assert message.rstrip(": ").rstrip() in text, marker


def test_readme_defines_the_core_instants_and_rules():
    """The definitions Plan 4 copies into 3.3.5.1."""
    text = readme_text()
    for term in ("t_gagal", "t_deteksi", "t_aktif_cadangan", "busybox adjtimex", "Forward evidence scan",
                 "block_id_flag", "1.3.6.7.2", "190", "G_fb", "absent_heights", "offset_uncertainty_ms",
                 "TIDAK TERBUKTI", "kontrol_hmac_benar", "observed", "indeterminate_seconds", "values.json"):
        assert term in text, term


def test_readme_names_the_columns_the_draft_asks_for():
    """The report columns and metrics that draft D relies on are described in the protocol."""
    text = readme_text()
    assert "G_fb (s)" in text
    for column in SAFETY_COLUMNS:
        if column.header in ("Monitor mengamati seluruh interval", "Detik tak tentu"):
            assert column.header in text, column.header
    for column in SENSITIVITY_COLUMNS[1:6]:
        assert column.header in text, column.header
    assert [label for _, label in FAILBACK_METRICS][-1] == "G_fb"


def test_readme_explains_the_tokens_the_harness_cannot_compute():
    """The two tokens without a rule are named, and the token count matches the code."""
    text = readme_text()
    assert "monitoring.versions" in text and "partition.tested" in text
    assert f"{len(TOKEN_NAMES)} tokens" in text


def test_readme_explains_the_chain_check_of_unobserved_intervals():
    """The interval rule, the analysis block, the two Tabel 4.7 columns, the four tokens, and that observed stays."""
    text = readme_text()
    for term in ("unobserved_intervals", "votes_inside", "monitor_gaps", "Interval tak teramati",
                 "Suara 48DC2183 di dalam interval", "monitor.unobserved.count", "monitor.unobserved.max_s",
                 "monitor.unobserved.votes", "monitor.unobserved.heights", "does not change `observed`"):
        assert term in text, term


def test_readme_has_no_em_dash():
    """House style."""
    assert chr(0x2014) not in readme_text()  # U+2014 EM DASH


def test_readme_states_the_35_second_hang_bound_and_the_extras():
    """The hang TTD bound is 35 s with its ticker derivation, not 45 s, and the extras are described."""
    text = readme_text()
    assert "interval + 30 s = 35 s" in text and "= 45 s" not in text
    assert "S4.series_aborted" in text and "hang.not_injected" in text


def test_readme_says_records_the_measured_ttd_once():
    """The hang row has no duplicated phrase."""
    assert readme_text().count("records the measured TTD") == 1


def test_readme_documents_the_partition_scenario():
    """The partition procedure, its result file, its table, and its reconnect cleanup are described."""
    text = readme_text()
    for term in ("docker network disconnect", "docker network connect", "partition-<n>.json", "tabel-partisi",
                 "cleanup_failed", "run partition", "partition.method", "partition.symptom", "partition.outcomes",
                 "--ip", "detection_symptom", "10.0.0.21", "reconnect_failed"):
        assert term in text, term


def test_readme_documents_the_host_port_repair_of_the_partition_runner():
    """Why the port is checked, the decision order, the events, the fail-closed rules, the keys, column, and token."""
    text = readme_text()
    for term in ("HostConfig.PortBindings", "NetworkSettings.Ports", "docker restart -t 50 validator1-node",
                 "port_check", "port_republish", "port_repair", "port_repair_ms", "port_repair_evidence",
                 "not_needed", "restarted", "refused", "not_running", "restart_failed", "still_missing", "unknown",
                 "Perbaikan porta host", "tidak dicatat", "partition.port_repair", "environment repair"):
        assert term in text, term
    section = text.split("| partition |", 1)[1].split("\n| security |", 1)[0]
    for term in ("loaded_reads", "loaded_read_ms", "15 s command timeout", "port_republish_issued",
                 "decision_unfinished", "restart_unfinished"):
        assert term in section, term
    assert "null only when the run ended before a verified reconnect" not in section
    assert "never restarts a node that may hold 48DC2183" in section
    assert "never starts or restarts a container that syncguard stopped" in section
    assert "it never repairs the environment." not in text


def commands_block() -> list:
    """The command lines of the README Commands section, without the interpreter prefix."""
    text = readme_text()
    section = text.split("\n## Commands\n", 1)[1].split("\n## ", 1)[0]
    block = section.split("```")[1]
    prefix = ".venv/bin/python -m harness "
    return [line[len(prefix):] for line in block.strip().splitlines() if line.startswith(prefix)]


def test_readme_commands_parse_with_the_cli_and_cover_every_command_and_scenario():
    """Each documented line is accepted by the real parser, and no subcommand or scenario is left out."""
    import shlex
    from harness.__main__ import HANDLERS, SCENARIO_HANDLERS, build_parser
    parser = build_parser()
    lines = commands_block()
    parsed = [parser.parse_args(shlex.split(line.replace("$(mktemp -d)", "/tmp/x"))) for line in lines]
    assert {args.command for args in parsed} == set(HANDLERS)
    assert {args.scenario for args in parsed if args.command == "run"} == set(SCENARIO_HANDLERS)


def test_readme_commands_show_the_series_options():
    """hang and partition take --iterations like failover; failover documents the snapshot default of 1."""
    lines = commands_block()
    assert any(line.startswith("run hang") and "--iterations" in line for line in lines)
    assert any(line.startswith("run partition") and "--iterations" in line for line in lines)
    assert any(line.startswith("scan-campaign --from-session") for line in lines)
    assert "defaults to `1`" in readme_text()


def test_readme_documents_the_manual_recovery_after_a_forced_stop():
    """The recovery section gives the exact docker commands for a paused and a disconnected primary."""
    text = readme_text()
    assert "## Pemulihan manual bila harness terhenti paksa" in text
    section = text.split("## Pemulihan manual bila harness terhenti paksa", 1)[1].split("\n## ", 1)[0]
    assert "docker kill validator1-node" in section and "never `docker unpause`" in section
    assert "docker network connect --ip 10.0.0.21 --alias validator1-node <network> validator1-node" in section
    assert "SIGHUP" in section and "SIGTERM" in section and "KeyboardInterrupt" in section


def test_readme_uses_the_draft_names_for_the_primary_instants():
    """t_sehat_utama and t_aktif_utama, with the first-unsigned-height rule and its reason."""
    text = readme_text()
    for name in ("t_sehat_utama", "t_aktif_utama", "t_lepas"):
        assert name in text, name
    for old in ("t_sehat_primer", "t_aktif_primer", "t_lepas_cadangan"):
        assert old not in text, old
    assert "no summary pools sessions" in text and "Summaries pool every iteration" not in text
    assert "then then" not in text


def test_readme_documents_the_raw_state_output_of_the_snapshots():
    """The two commands, their working directory, the raw file layout, raw_file, the key-file rule, and the raw tokens."""
    from harness.values import RAW_TOKENS
    text = readme_text()
    for term in ("cat priv_validator_state.json", "ls -l priv_validator_state.json.lock", "raw/<iteration>-<point>-<node>.txt",
                 "raw_file", "[exit <code>]", "priv_validator_key.json", "# validator1-node"):
        assert term in text, term
    for name in RAW_TOKENS:
        assert f"`{name}`" in text, name


def test_readme_documents_the_tenderduty_readings():
    """The command, the endpoint, the gauges, the chain label, the readings file, the hand-written start, the table, the tokens."""
    from harness.report import TENDERDUTY_COLUMNS
    from harness.tenderduty import GAUGES
    from harness.values import TENDERDUTY_TOKENS
    text = readme_text()
    for term in ("tenderduty --session", "http://127.0.0.1:28686/metrics", "chain_id", "story-localnet", "tenderduty.jsonl",
                 '"source"', "tabel-tenderduty", "Blok terlewat menurut Tenderduty dan tanda tangan commit"):
        assert term in text, term
    for metric in GAUGES.values():
        assert metric in text, metric
    for column in TENDERDUTY_COLUMNS:
        assert column.header in text, column.header
    for name in TENDERDUTY_TOKENS:
        assert f"`{name}`" in text, name
    assert "nil votes included" not in text
    assert "books a nil precommit as signed" in text
    for term in ('"tip_height"', '"start_tip_resolved"', "tenderduty-heights.jsonl", "start tip to end tip minus 1",
                 "books the commit of height h when block h+1 arrives", "first height whose header time",
                 "median precommit time of the height before it", "lies 2 below the first height whose header time",
                 "within one height either way", "the recorded tip for 15 and the tip plus 1 for 3",
                 "end tip is not above its start tip covers no height"):
        assert term in text, term


def test_readme_documents_the_tenderduty_incident_count():
    """The log, the h - 1 labelling, the nil and proposer rules, the incident range, the log coverage, and the two scopes."""
    text = readme_text()
    for term in ("data/tenderduty/tenderduty-run.log", "the commit of h - 1", "start + 1 to end", "start to end - 1",
                 "nil precommit", "proposed it", "primary_signature_height", "first_signature_height",
                 "watching for NewBlock and Vote events", "every 20th block", "two scopes", "seluruh sesi"):
        assert term in text, term


def test_readme_documents_the_new_report_columns_and_the_series_filter():
    """Waktu, the session notes and Bukti yang dihasilkan, the three failback phases, the fourth block-rate window."""
    from harness.report import FAILBACK_COLUMNS, WINDOW_LABELS
    text = readme_text()
    for term in ("Waktu", "session-notes.csv", "sesi,tujuan,bukti", "Bukti yang dihasilkan", WINDOW_LABELS["after_failback_120s"],
                 "after_failback_120s", "series session only"):
        assert term in text, term
    execution = [column.header for column in FAILBACK_COLUMNS].index("Eksekusi (s)")
    for column in FAILBACK_COLUMNS[execution + 1:execution + 4]:
        assert column.header in text, column.header


def test_readme_documents_the_new_tokens():
    """Every hang-bound, failback-phase, and failback-timing token, the English copies, and the last-fault coverage."""
    from harness.values import ENGLISH_TOKENS, FAILBACK_PHASE_TOKENS, HANG_BOUND_TOKENS, RATE_TOKENS
    text = readme_text()
    for name in HANG_BOUND_TOKENS + FAILBACK_PHASE_TOKENS + RATE_TOKENS[3:]:
        assert f"`{name}`" in text, name
    assert "ENGLISH_TOKENS" in text and "`<name>.en`" in text
    for name in ENGLISH_TOKENS:
        assert f"`{name}`" in text, name
    for term in ("last_fault_session", "last_fault_height", "last_fault_t_ms", "relation_to_last_fault",
                 "exposure_end_height", "exposure_end_t_ms", "until the failback of the last fault"):
        assert term in text, term


def test_readme_states_the_half_up_rounding():
    """Tables and tokens round a tie half up on the value's shortest decimal form."""
    assert "rounded half up on its shortest decimal form" in readme_text()


def test_readme_counts_window_3_by_overlap():
    """S4.rate.window3_with_failback.k counts failbacks whose notification-to-signature interval overlaps window 3."""
    text = readme_text()
    assert "overlaps the `following_120s` window" in text
    assert "`following_120s` window holds the notification or the primary's first signature" not in text


def test_readme_documents_the_last_fault_tokens_beside_the_plain_final_range():
    """evidence.final_range is the plain range again; the two last-fault tokens and their values are described."""
    from harness.values import LAST_FAULT_TOKENS
    text = readme_text()
    for name in LAST_FAULT_TOKENS:
        assert f"`{name}`" in text, name
    for term in ("`Sesi <id>, akhir sesi pada ketinggian <exposure_end_height>`", "stays the plain range"):
        assert term in text, term
    assert "kegagalan terakhir pada Sesi <id>" not in text
