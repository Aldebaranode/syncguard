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
                 "cleanup_failed", "run partition", "partition.method"):
        assert term in text, term
