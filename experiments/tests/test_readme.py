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
