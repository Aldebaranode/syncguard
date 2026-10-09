"""values.json: the {{DATA:...}} tokens of the manuscript draft, formatted as they appear in the text."""

import json

from fakes import FIXTURES, make_data
from harness.analysis import analyze_session
from harness.constants import TARGET_VALIDATOR
from harness.dryrun import FIXTURE_FAULT_MS, build_fixture_session
from harness.jsonio import append_jsonl, write_json
from harness.session import read_events
from harness.report import load_session_data
from harness.values import TOKEN_NAMES, campaign_dates, compute_values, format_hours, write_values

MOCK = "E6FAA3F2B3E9158BA9BF93BE1F7F8D3095EAD04D"
FAULT = FIXTURE_FAULT_MS


def safety(evidence: list | None = None, observed: bool = True, jailed_until: str = "1970-01-01T00:00:00Z",
           tombstoned: bool = False, byzantine: dict | None = None, session: str = "SFIX", t_ms: int = FAULT + 600_000,
           first: int = 127, last: int = 500) -> dict:
    """A safety.json with the fields the tokens read."""
    prometheus = ({"available": True, "cometbft_consensus_byzantine_validators": byzantine}
                  if byzantine is not None else {"available": False, "reason": "no series"})
    return {
        "session": session, "t_ms": t_ms,
        "monitor": {"samples": 700, "dual_signer_seconds": 0, "indeterminate_seconds": 0, "observed": observed,
                    "observed_problems": [] if observed else [{"code": "gap", "ms": 9000, "scenario": "failover",
                                                               "iteration": 1}]},
        "evidence_scan": {"first_height": first, "last_height": last, "heights_scanned": last - first + 1,
                          "evidence": evidence or [], "covers_max_age": False},
        "signing_info": {"missed_blocks_counter": 3, "jailed_until": jailed_until, "tombstoned": tombstoned},
        "prometheus": prometheus,
    }


def node(label: str, key: str, real: str | None, staged: str | None, state: tuple, lock: int | None) -> dict:
    """One node of a snapshot: key-side addresses (None means the file does not exist), state tuple, lock PID."""

    def fact(address):
        """A key file fact."""
        return {"exists": address is not None, "address": address}

    return {"node": label, "state": {"height": state[0], "round": state[1], "step": state[2]},
            "keys": [fact(key), fact(real), fact(staged)],
            "locks": [{"pid": lock, "pid_alive": True}] if lock is not None else []}


def snapshots(iteration: int = 1, takeover_key_of_primary: str = MOCK) -> list:
    """The three snapshot points of the layout the thesis expects from Algorithms 3.1 to 3.4."""
    target = TARGET_VALIDATOR
    return [
        {"session": "SFIX", "iteration": iteration, "point": "before_fault", "nodes": [
            node("validator1", target, None, None, (180, 0, 3), 4242), node("validator2", MOCK, None, None, (175, 0, 3), None)]},
        {"session": "SFIX", "iteration": iteration, "point": "after_takeover", "nodes": [
            node("validator1", takeover_key_of_primary, target, None, (181, 0, 3), None),
            node("validator2", target, None, None, (200, 0, 3), 4243)]},
        {"session": "SFIX", "iteration": iteration, "point": "after_failback", "nodes": [
            node("validator1", target, None, None, (250, 0, 3), 4242),
            node("validator2", MOCK, target, None, (201, 0, 3), None)]},
    ]


def monitor_lines(directory, dual_at_ms: int | None = None, covered: bool = True) -> None:
    """monitor.jsonl with a sample every 5 s across the fixture iteration window (60 s before the fault to 160 s after).

    One optional sample has both nodes on the validator; covered=False leaves the file empty.
    """
    path = directory / "monitor.jsonl"
    path.write_text("")
    if not covered:
        return
    times = list(range(FAULT - 60_000, FAULT + 160_001, 5000))
    if dual_at_ms is not None and dual_at_ms not in times:
        times.append(dual_at_ms)
    for t_ms in sorted(times):
        addresses = {"validator1-node": TARGET_VALIDATOR, "validator2-node": MOCK}
        if t_ms == dual_at_ms:
            addresses = {"validator1-node": TARGET_VALIDATOR, "validator2-node": TARGET_VALIDATOR}
        append_jsonl(path, {"session": "SFIX", "t_ms": t_ms, "addresses": addresses})


def series(tmp_path, evidence: list | None = None, dual_at_ms: int | None = None, snaps: list | None = None,
           covered: bool = True):
    """The fixture session with safety.json, monitor.jsonl, and snapshots.jsonl, as loaded SessionData."""
    directory = build_fixture_session(FIXTURES, tmp_path / "data")
    analyze_session(directory)
    write_json(directory / "safety.json", safety(evidence))
    monitor_lines(directory, dual_at_ms, covered)
    for snapshot in snaps if snaps is not None else snapshots():
        append_jsonl(directory / "snapshots.jsonl", snapshot)
    return load_session_data(directory)


def test_block_time_is_the_mean_header_interval_of_the_series_heights(tmp_path):
    """S4.block_time.mean: header-time span over height span of the stored heights, seconds, comma decimal."""
    from harness.signing import load_heights
    session = series(tmp_path)
    records = load_heights(session.directory / "heights.jsonl")
    expected = (records[-1].header_time_ms - records[0].header_time_ms) / (records[-1].height - records[0].height) / 1000
    values = compute_values([session])["values"]
    assert values["S4.block_time.mean"] == f"{expected:.2f}".replace(".", ",")


def test_scan_range_lists_each_session_and_the_total(tmp_path):
    """evidence.scan_range: first and last scanned height per session, then the union, with the max-age coverage."""
    session = series(tmp_path)
    write_json(session.directory / "safety.json", {**safety(first=1200, last=4800),
                                                    "evidence_scan": {**safety(first=1200, last=4800)["evidence_scan"],
                                                                      "covers_max_age": True}})
    session = load_session_data(session.directory)
    values = compute_values([session])["values"]
    assert values["evidence.scan_range"] == ("SFIX: 1.200 sampai 4.800 (mencakup max_age_num_blocks); "
                                             "total: 1.200 sampai 4.800")


def test_tenderduty_outcome_comes_from_the_spike_note(tmp_path):
    """The first line of data/tenderduty-spike.md decides the sentence; a missing or unreadable note is uncomputed."""
    session = series(tmp_path)
    data_dir = tmp_path / "notes"
    data_dir.mkdir()
    missing = compute_values([session], data_dir)
    assert "tenderduty.outcome" in missing["uncomputed"]
    (data_dir / "tenderduty-spike.md").write_text("kompatibel\nsesi: S8\n", encoding="utf-8")
    assert compute_values([session], data_dir)["values"]["tenderduty.outcome"] == (
        "Tenderduty berjalan terhadap RPC node Story pada Sesi S8 dan dipakai sebagai verifikasi silang")
    (data_dir / "tenderduty-spike.md").write_text("tidak kompatibel: endpoint /validators tidak ada\n", encoding="utf-8")
    assert compute_values([session], data_dir)["values"]["tenderduty.outcome"] == (
        "RPC node Story tidak kompatibel dengan Tenderduty (endpoint /validators tidak ada), sehingga Tenderduty tidak dipakai")
    (data_dir / "tenderduty-spike.md").write_text("mungkin\n", encoding="utf-8")
    assert "tenderduty.outcome" in compute_values([session], data_dir)["uncomputed"]


def test_the_draft_c_tokens_are_part_of_the_token_names():
    """The three tokens drafts C adds beside draft D's."""
    assert {"S4.block_time.mean", "evidence.scan_range", "tenderduty.outcome"} <= set(TOKEN_NAMES)


def failback_row(**fields) -> dict:
    """A failback analysis row with the fields the criterion reads."""
    return {"config_id": "fixture", "iteration": 1, "t_primary_healthy_ms": 1, "t_failback_considered_ms": 2,
            "t_failback_initiated_ms": FAULT, "t_failback_complete_ms": FAULT + 1000, "t_release_complete_ms": FAULT + 500,
            "t_primary_signature_ms": FAULT + 2000, "gfb_s": 5.0, "absent_heights": 2, "execution_s": 2.0,
            "grace_wait_s": 60.0, **fields}


def failover_row(**fields) -> dict:
    """A failover analysis row with every field the tokens read; unset ones are empty."""
    row = {"config_id": "c", "iteration": 1, "ttd_s": None, "ttr_s": None, "orchestration_s": None, "node_start_s": None,
           "absent_heights": None, "invalid_reason": None, "t_fault_ms": 0, "t_first_signature_ms": None,
           "t_takeover_ms": 1}
    return {**row, **fields}


def test_every_draft_token_is_computed_or_explained(tmp_path):
    """Each name of the draft is either a value or listed with a reason, never both and never neither."""
    result = compute_values([series(tmp_path)])
    values, uncomputed = set(result["values"]), set(result["uncomputed"])
    assert values | uncomputed == set(TOKEN_NAMES)
    assert not values & uncomputed
    assert len(TOKEN_NAMES) == len(set(TOKEN_NAMES))
    assert "partition.tested" in uncomputed and "monitoring.versions" in uncomputed


def test_series_statistics_and_phases(tmp_path):
    """One fixture iteration: seconds to one decimal; SD and interval need n of at least 2."""
    result = compute_values([series(tmp_path)])
    values = result["values"]
    assert result["roles"]["S4"] == "SFIX"
    assert (values["S4.n"], values["S4.ttr.n"]) == ("1", "1")
    assert (values["S4.ttd.mean"], values["S4.ttd.min"], values["S4.ttd.max"]) == ("51,6", "51,6", "51,6")
    assert (values["S4.ttr.mean"], values["S4.ttr.min"]) == ("68,9", "68,9")
    assert "S4.ttr.sd" in result["uncomputed"] and "S4.ttd.ci_low" in result["uncomputed"]
    assert (values["S4.ttd.over_bound"], values["S4.ttr.over_bound"]) == ("1", "1")
    assert (values["S4.orchestration.mean"], values["S4.node_start.mean"]) == ("1,1", "16,2")
    assert values["S4.syncguard_version"] == "fixture"
    assert values["S4.clock_offset_ms"] == "0,0"


def test_missed_heights_and_shares(tmp_path):
    """Absent heights 15: 7,89 percent of the 190-block allowance and 15 / 27.360 of the story-1 allowance."""
    values = compute_values([series(tmp_path)])["values"]
    assert (values["S4.missed.min"], values["S4.missed.max"]) == ("15", "15")
    assert values["S4.missed.allowance_pct_max"] == "7,89"
    assert values["S4.missed.mainnet_pct_max"] == "0,05"


def test_failback_tokens(tmp_path):
    """G_fb 16,8 s, grace 60,0 s, execution 2,1 s, 3 missed heights, release before the primary restart."""
    values = compute_values([series(tmp_path)])["values"]
    assert values["S4.fb.n"] == "1"
    assert (values["S4.gfb.mean"], values["S4.gfb.max"]) == ("16,8", "16,8")
    assert values["S4.fb.grace.mean"] == "60,0"
    assert (values["S4.fb.execution.mean"], values["S4.fb.execution.max"]) == ("2,1", "2,1")
    assert values["S4.fb.missed.max"] == "3"
    assert values["S4.fb.bbm.k"] == "1"


FAILBACK_PHASES = ((1, 10, 1_551, 8_708), (2, 77, 5_862, 2_677), (3, 3, 818, 1_297))  # iteration and phase ms


def phased_failback(iteration: int, notify_ms: int, release_ms: int, complete_ms: int) -> dict:
    """A failback row whose start, notification, release, and completion lie the given phase lengths apart."""
    notified = FAULT + notify_ms
    released = notified + release_ms
    return failback_row(iteration=iteration, t_failback_initiated_ms=FAULT, t_failback_notified_ms=notified,
                        t_release_complete_ms=released, t_failback_complete_ms=released + complete_ms, sources={})


def phase_session(failbacks: list):
    """A series session with one failover row per failback row."""
    failover = [failover_row(iteration=row["iteration"], ttr_s=20.0, sources={}) for row in failbacks]
    return make_data(analysis={"failover": failover, "failback": failbacks}, session="S5")


def test_failback_phase_tokens(tmp_path):
    """Largest start-to-notification to two decimals; release and completion phases to one decimal."""
    session = phase_session([phased_failback(*phases) for phases in FAILBACK_PHASES])
    values = compute_values([session])["values"]
    assert values["S4.fb.notify.max"] == "0,08"
    assert (values["S4.fb.release.mean"], values["S4.fb.release.min"], values["S4.fb.release.max"]) == ("2,7", "0,8", "5,9")
    assert (values["S4.fb.complete.mean"], values["S4.fb.complete.min"], values["S4.fb.complete.max"]) == (
        "4,2", "1,3", "8,7")


def test_failback_phase_tokens_skip_a_phase_without_stamps():
    """No release line in any failback: the release and completion tokens carry a reason; notify.max stays."""
    from harness.values import FAILBACK_TOKENS
    phase_names = ("S4.fb.notify.max", "S4.fb.release.mean", "S4.fb.release.min", "S4.fb.release.max",
                   "S4.fb.complete.mean", "S4.fb.complete.min", "S4.fb.complete.max")
    assert set(phase_names) <= set(FAILBACK_TOKENS) <= set(TOKEN_NAMES)
    rows = [{**phased_failback(*phases), "t_release_complete_ms": None} for phases in FAILBACK_PHASES]
    result = compute_values([phase_session(rows)])
    assert result["values"]["S4.fb.notify.max"] == "0,08"
    for name in phase_names[1:]:
        assert name in result["uncomputed"], name


def test_excerpts_are_verbatim_log_lines(tmp_path):
    """Detection lines from the first failed check to the detection; merged lines carry the node name."""
    result = compute_values([series(tmp_path)])
    values = result["values"]
    assert values["S4.excerpt.iteration"] == "1"
    detection = values["S4.excerpt.detection"].splitlines()
    assert len(detection) == 4 and "CometBFT health check failed" in detection[0]
    assert detection[-1].endswith("module=failover node=validator-1") and "Maximum failures reached" in detection[-1]
    failover = values["S4.excerpt.failover"].splitlines()
    assert failover[0].startswith("validator-1: time=\"2026-10-05 09:54:39.620\"")
    assert "Initiating failover" in failover[0]
    assert any(line.startswith("validator-2: ") and "Successfully took over" in line for line in failover)
    assert "Failover complete - node is now passive" in failover[-1]
    failback = values["S4.excerpt.failback"].splitlines()
    assert "Primary node healthy, initiating failback" in failback[0]
    assert "Failback complete - node is now active" in failback[-1]
    assert any("Successfully released validator duties" in line for line in failback)
    assert any("Restarting container: validator1-node" in line for line in failback)
    assert "S4.excerpt.normal" in result["uncomputed"]


def test_excerpt_normal_takes_the_last_health_line_of_each_node_before_the_fault(tmp_path):
    """With health lines before t_gagal the normal excerpt shows the last [active] and the last [passive] line verbatim."""
    session = series(tmp_path)
    logs = session.directory / "logs"
    active = logs / "smoke-active.txt"
    passive = logs / "smoke-passive.txt"
    early_active = 'time="2026-10-05 09:53:40.000" level=info msg="[active] height=120 peers=5 healthy=true" module=failover node=validator-1\n'
    early_passive = 'time="2026-10-05 09:53:41.000" level=info msg="[passive] height=120 peers=5 healthy=true" module=failover node=validator-2\n'
    active.write_text(early_active + active.read_text())
    passive.write_text(early_passive + passive.read_text())
    values = compute_values([session])["values"]
    assert values["S4.excerpt.normal"] == (early_active + early_passive).rstrip("\n")


def test_campaign_dates_use_indonesian_months():
    """One day, a span inside a month, across months, and across years."""
    assert campaign_dates(["2026-10-06 09:00:00.000"]) == "6 Oktober 2026"
    assert campaign_dates(["2026-10-09 09:00:00.000", "2026-10-06 11:00:00.000"]) == "6 sampai 9 Oktober 2026"
    assert campaign_dates(["2026-10-30 09:00:00.000", "2026-11-02 11:00:00.000"]) == "30 Oktober sampai 2 November 2026"
    assert campaign_dates(["2026-12-30 09:00:00.000", "2027-01-02 11:00:00.000"]) == "30 Desember 2026 sampai 2 Januari 2027"


def test_format_hours_drops_a_zero_decimal():
    """24,0 h reads as 24; 23,5 h keeps its decimal."""
    assert format_hours(24.004) == "24"
    assert format_hours(23.52) == "23,5"


def hang_times(ttd_s: float | None) -> dict:
    """Pause issued at 0 ms, ended 100 ms later, detected ttd_s after the issue (no detection for None)."""
    detect_ms = None if ttd_s is None else round(ttd_s * 1000)
    return {"t_fault_ms": 0, "t_fault_ended_ms": 100, "t_detect_ms": detect_ms, "ttd_s": ttd_s}


def test_hang_tokens_and_per_iteration_listing():
    """Counts, the largest TTD of the detected iterations, and Indonesian words; differing iterations are listed one by one."""
    rows = [{"iteration": 1, "fault_injected": True, "detected": True, **hang_times(31.2), "restart_outcome": "succeeded", "action": "unpause"},
            {"iteration": 2, "fault_injected": True, "detected": True, **hang_times(33.4), "restart_outcome": "failed", "action": "kill"},
            {"iteration": 3, "fault_injected": True, "detected": False, **hang_times(None), "restart_outcome": "not_attempted", "action": "none"}]
    values = compute_values([make_data(analysis={"hang": rows}, session="S5")])["values"]
    assert (values["hang.n"], values["hang.session"], values["hang.detected.k"]) == ("3", "Sesi S5", "2")
    assert values["hang.ttd.max"] == "33,4"
    assert values["hang.restart_outcome"] == "iterasi 1: berhasil; iterasi 2: gagal; iterasi 3: tidak dijalankan"
    assert values["hang.action"].startswith("iterasi 1: docker unpause; iterasi 2: docker kill; iterasi 3: tidak ada")


def test_hang_tokens_with_identical_iterations_are_one_phrase():
    """All iterations alike: one phrase, no listing."""
    rows = [{"iteration": n, "fault_injected": True, "detected": True, **hang_times(30.0), "restart_outcome": "succeeded",
             "action": "unpause"} for n in (1, 2)]
    values = compute_values([make_data(analysis={"hang": rows}, session="S5")])["values"]
    assert values["hang.restart_outcome"] == "berhasil"
    assert values["hang.action"] == "docker unpause"


S7_HANG = ((1, 1791516836763, 1791516839109, 1791516872498, 35.735),
           (2, 1791517130014, 1791517131772, 1791517162544, 32.530),
           (3, 1791517381312, 1791517381386, 1791517412518, 31.206))  # iteration, pause issued and ended, detection, TTD


def s7_hang_rows() -> list:
    """Hang rows shaped like Sesi S7: TTD 35,735 / 32,530 / 31,206 s, pause commands 2,346 / 1,758 / 0,074 s."""
    return [{"iteration": iteration, "fault_injected": True, "detected": True, "t_fault_ms": issued,
             "t_fault_ended_ms": ended, "t_detect_ms": detected, "ttd_s": ttd, "restart_outcome": "failed", "action": "kill"}
            for iteration, issued, ended, detected, ttd in S7_HANG]


def test_hang_bound_tokens_compare_the_ttd_with_the_35_second_bound():
    """Smallest TTD, iterations above 35 s and the largest excess, the pause behind the largest TTD, and TTD from the pause end."""
    values = compute_values([make_data(analysis={"hang": s7_hang_rows()}, session="S7")])["values"]
    assert values["hang.ttd.min"] == "31,2"
    assert values["hang.ttd.over_bound"] == "1"
    assert values["hang.ttd.excess_max"] == "0,7"
    assert values["hang.pause_s.at_max"] == "2,3"
    assert (values["hang.ttd_from_pause.min"], values["hang.ttd_from_pause.max"]) == ("30,8", "33,4")


def test_hang_excess_reads_zero_when_no_ttd_exceeds_the_bound():
    """Every detected TTD at or below 35 s: no iteration over the bound and an excess of 0,0."""
    rows = [row for row in s7_hang_rows() if row["ttd_s"] < 35]
    values = compute_values([make_data(analysis={"hang": rows}, session="S7")])["values"]
    assert (values["hang.ttd.over_bound"], values["hang.ttd.excess_max"]) == ("0", "0,0")
    assert values["hang.pause_s.at_max"] == "1,8"


def test_hang_bound_tokens_are_explained_without_a_detected_ttd():
    """No detected iteration: the TTD tokens carry the reason; the bound tokens are part of the hang tokens."""
    from harness.values import HANG_TOKENS
    new = ("hang.ttd.min", "hang.ttd.over_bound", "hang.ttd.excess_max", "hang.pause_s.at_max",
           "hang.ttd_from_pause.min", "hang.ttd_from_pause.max")
    assert set(new) <= set(HANG_TOKENS) <= set(TOKEN_NAMES)
    rows = [{**row, "detected": False, "t_detect_ms": None, "ttd_s": None} for row in s7_hang_rows()]
    result = compute_values([make_data(analysis={"hang": rows}, session="S7")])
    for name in new:
        assert "no detected hang iteration has a TTD" in result["uncomputed"][name], name


def drill(**fields) -> dict:
    """A recovered drill.json."""
    return {"session": "S6", "outcome": "recovered", "alert_observed": True, "alert_s": 20.0, "reaction_s": 5.0,
            "recovery_s": 600.0, "commands": [{"command": "a"}, {"command": "b"}], "restore_completed": True, **fields}


def soak_session(tmp_path, availability: float = 0.9990, ratio: float = 0.99962):
    """A soak session with soak.json, a soak_start event, and the time-based analysis block."""
    directory = tmp_path / "soak"
    directory.mkdir()
    append_jsonl(directory / "events.jsonl", {"session": "S7", "t_ms": 1, "kind": "soak_start", "hours": 24.0,
                                              "kill_offset_hours": 6.0, "start_height": 10})
    soak = {"session": "S7", "heights_collected": True, "start_ms": 0, "end_ms": 86_400_000, "start_height": 10,
            "end_height": 30000, "kill_outcome": "complete", "host_pauses": 2, "height_stalls": 0, "header_gaps": [],
            "availability": {"signed": 29000, "total": 29011, "ratio": ratio}}
    timed = {"window_s": 86400.0, "downtime_s": (1 - availability) * 86400, "availability": availability,
             "availability_blocks": ratio}
    return make_data({"soak": soak}, {"soak": timed}, session="S7", directory=directory)


def test_tenderduty_tokens_read_the_soak_and_the_drill_sessions(tmp_path):
    """tenderduty.rows counts the table rows; the soak has both readings; the drill has none, so its two are explained."""
    from fakes import write_tenderduty_evidence
    soak = soak_session(tmp_path)
    write_tenderduty_evidence(soak.directory, "S7")
    drill_session = make_data({"drill": drill()}, session="S6", directory=tmp_path / "S6")
    result = compute_values([drill_session, soak])
    values = result["values"]
    assert values["tenderduty.rows"] == "1"
    assert (values["tenderduty.soak.missed"], values["tenderduty.soak.chain_absent"]) == ("3", "3")
    for name in ("tenderduty.drill.missed", "tenderduty.drill.chain_absent"):
        assert "Sesi S6 has no start and end reading" in result["uncomputed"][name], name


def test_tenderduty_tokens_are_explained_while_the_range_lacks_commit_data_or_a_tip(tmp_path):
    """Tips 101 and 112 with no commit stored for 110 and 111: both soak tokens name the missing heights. A start
    written by hand without a resolved tip: both name the unknown range."""
    from fakes import write_tenderduty_evidence
    soak = soak_session(tmp_path)
    write_tenderduty_evidence(soak.directory, "S7", end_tip=112)
    result = compute_values([soak])
    for name in ("tenderduty.soak.missed", "tenderduty.soak.chain_absent"):
        assert result["uncomputed"][name] == ("Sesi S7: 2 of the heights 101 to 111 that the readings cover have no "
                                              "commit data in heights.jsonl or tenderduty-heights.jsonl"), name
    (tmp_path / "hand").mkdir()
    hand = soak_session(tmp_path / "hand")
    write_tenderduty_evidence(hand.directory, "S7", start_tip=None)
    result = compute_values([hand])
    for name in ("tenderduty.soak.missed", "tenderduty.soak.chain_absent"):
        assert "no tip_height" in result["uncomputed"][name], name
    assert result["values"]["tenderduty.rows"] == "1"


def test_tenderduty_tokens_without_a_soak_or_drill_session(tmp_path):
    """No soak.json and no drill.json: the role tokens name the missing file; the row count is 0."""
    from harness.values import TENDERDUTY_TOKENS
    assert set(TENDERDUTY_TOKENS) == {"tenderduty.rows", "tenderduty.soak.missed", "tenderduty.soak.chain_absent",
                                      "tenderduty.drill.missed", "tenderduty.drill.chain_absent"}
    assert set(TENDERDUTY_TOKENS) <= set(TOKEN_NAMES)
    result = compute_values([series(tmp_path)])
    assert result["values"]["tenderduty.rows"] == "0"
    assert "soak.json" in result["uncomputed"]["tenderduty.soak.missed"]
    assert "drill.json" in result["uncomputed"]["tenderduty.drill.chain_absent"]


def test_the_reduction_token_takes_its_mean_ttr_from_the_tables_rule(tmp_path, monkeypatch):
    """downtime.reduction_pct and the sensitivity table read the mean TTR from one function, report.mean_valid_ttr."""
    from harness.report import sensitivity_rows
    failover = [failover_row(iteration=1, ttr_s=60.0), failover_row(iteration=2, ttr_s=80.0)]
    sessions = [make_data(analysis={"failover": failover}, session="S4"),
                make_data({"drill": drill()}, session="S6"), soak_session(tmp_path)]
    table = sensitivity_rows(sessions, failover)[0]["reduction_pct"]
    assert compute_values(sessions)["values"]["downtime.reduction_pct"] == f"{table:.2f}".replace(".", ",")
    monkeypatch.setattr("harness.values.mean_valid_ttr", lambda rows: (300.0, "stub"))
    assert compute_values(sessions)["values"]["downtime.reduction_pct"] == "50,00"


def test_drill_soak_and_derived_tokens(tmp_path):
    """The formulas of the draft: baseline availability, gain in points, reduction against the mean TTR, sensitivity gains."""
    failover = [failover_row(ttr_s=60.0, ttd_s=10.0, orchestration_s=1.0, node_start_s=1.0, absent_heights=20),
                failover_row(iteration=2, ttr_s=80.0, ttd_s=12.0, orchestration_s=1.0, node_start_s=1.0, absent_heights=25)]
    sessions = [make_data(analysis={"failover": failover}, session="S4"),
                make_data({"drill": drill()}, session="S6"), soak_session(tmp_path)]
    values = compute_values(sessions)["values"]
    assert (values["drill.session"], values["drill.recovery_s"], values["drill.alert_s"]) == ("S6", "600,0", "20,0")
    assert (values["drill.reaction_s"], values["drill.commands"]) == ("5,0", "2")
    baseline = (86400 - 600) / 86400 * 100
    assert values["baseline.availability"] == f"{baseline:.2f}".replace(".", ",")
    assert (values["soak.session"], values["soak.fault_offset"], values["soak.hours"]) == ("S7", "6 jam", "24")
    assert (values["soak.host_pauses"], values["soak.kill_outcome"]) == ("2", "takeover dan failback selesai tanpa intervensi")
    assert values["soak.availability"] == "99,90" and values["soak.availability_blocks"] == "99,96"
    assert values["soak.downtime_s"] == "86,4"
    assert values["availability.gain_pp"] == f"{99.9 - baseline:.2f}".replace(".", ",")
    assert values["downtime.reduction_pct"] == "88,33"
    assert values["sens.assumed.gain_pp"] == f"{99.9 - (86400 - 1800) / 86400 * 100:.2f}".replace(".", ",")
    assert values["sens.literature.gain_pp"] == "0,80"
    assert values["crit3.status"] == "Terpenuhi"


ENGLISH_BASES = ("S4.ttd.mean", "S4.ttd.sd", "S4.ttr.mean", "S4.ttr.sd", "S4.ssr.pct", "baseline.availability",
                 "soak.availability", "availability.gain_pp", "evidence.count", "dualkey.seconds", "S4.n",
                 "monitor.unobserved.count")


def test_english_copies_of_the_abstract_numbers(tmp_path):
    """<name>.en holds the English format of each listed value; the .en names are tokens."""
    from harness.values import ENGLISH_TOKENS
    assert ENGLISH_TOKENS == ENGLISH_BASES
    assert {f"{name}.en" for name in ENGLISH_BASES} <= set(TOKEN_NAMES)
    failover = [failover_row(ttr_s=60.0, ttd_s=10.25, orchestration_s=1.0, node_start_s=1.0, absent_heights=20),
                failover_row(iteration=2, ttr_s=80.0, ttd_s=12.0, orchestration_s=1.0, node_start_s=1.0, absent_heights=25)]
    sessions = [make_data(analysis={"failover": failover}, session="S4"),
                make_data({"drill": drill(recovery_s=1234.5)}, session="S6"), soak_session(tmp_path)]
    values = compute_values(sessions)["values"]
    assert (values["S4.ttd.mean"], values["S4.ttd.mean.en"]) == ("11,1", "11.1")
    assert (values["S4.ttr.sd"], values["S4.ttr.sd.en"]) == ("14,1", "14.1")
    assert (values["baseline.availability"], values["baseline.availability.en"]) == ("98,57", "98.57")
    assert (values["soak.availability"], values["soak.availability.en"]) == ("99,90", "99.90")
    assert values["S4.n.en"] == "2"


def test_an_english_copy_is_skipped_with_its_base_tokens_reason(tmp_path):
    """One iteration has no SD, so S4.ttd.sd.en carries the reason of S4.ttd.sd; without a soak the soak copy is explained."""
    result = compute_values([series(tmp_path)])
    assert result["values"]["S4.ttd.mean.en"] == "51.6"
    assert result["uncomputed"]["S4.ttd.sd.en"] == result["uncomputed"]["S4.ttd.sd"]
    assert result["uncomputed"]["soak.availability.en"] == result["uncomputed"]["soak.availability"]


def test_a_drill_without_a_recovery_leaves_the_drill_tokens_explained():
    """No recovery time: the drill tokens and everything derived from them are listed with a reason."""
    sessions = [make_data({"drill": drill(outcome="no_signature", recovery_s=None)}, session="S6")]
    result = compute_values(sessions)
    for name in ("drill.recovery_s", "baseline.availability", "availability.gain_pp", "downtime.reduction_pct"):
        assert name in result["uncomputed"]


def test_an_unobserved_alert_is_not_printed_as_a_time():
    """alert_observed false: alert_s and reaction_s are explained instead of printed."""
    sessions = [make_data({"drill": drill(alert_observed=False, alert_s=None, reaction_s=None)}, session="S6")]
    result = compute_values(sessions)
    assert "drill.alert_s" in result["uncomputed"] and "drill.reaction_s" in result["uncomputed"]
    assert result["values"]["drill.recovery_s"] == "600,0"


def test_safety_tokens_over_sessions(tmp_path):
    """Sums, the latest scan, unobserved sessions with their problems, evidence heights, and Byzantine sessions."""
    evidence = [{"height": 200, "count": 1, "types": ["DuplicateVoteEvidence"]}]
    first = series(tmp_path, evidence=evidence)
    write_json(first.directory / "safety.json", safety(evidence, byzantine={"a": 1.0}))
    first = load_session_data(first.directory)
    second = make_data({"safety": safety(observed=False, session="S8", t_ms=FAULT + 900_000, first=600, last=900,
                                         jailed_until="2026-10-06T00:00:00Z", tombstoned=True, byzantine={"a": 0.0})},
                       session="S8")
    values = compute_values([first, second])["values"]
    assert values["monitor.samples"] == "1.400"
    assert (values["dualkey.seconds"], values["dualkey.indeterminate_seconds"]) == ("0", "0")
    assert values["monitor.unobserved_sessions"] == "S8 (failover iterasi 1: jeda 9,0 detik antarsampel informatif)"
    assert values["evidence.count"] == "1"
    assert values["evidence.final_range"] == "600 sampai 900"
    assert values["byzantine.max"] == "1"
    assert values["byzantine.sessions"] == "SFIX"
    assert values["signing.tombstoned"] == "true"
    assert values["signing.jailed_until"] == "2026-10-06T00:00:00Z"
    assert values["evidence.heights"] == "SFIX: 200 (DuplicateVoteEvidence)"


UNOBSERVED_TOKENS = ("monitor.unobserved.count", "monitor.unobserved.max_s", "monitor.unobserved.votes",
                     "monitor.unobserved.heights")


def gap_entry(start_ms: int, end_ms: int, heights: list, votes: list) -> dict:
    """One votes_inside entry; votes are (height, t_ms, kind) triples."""
    return {"start_ms": start_ms, "end_ms": end_ms, "ms": end_ms - start_ms, "heights": heights,
            "votes": [{"height": height, "t_ms": t_ms, "kind": kind} for height, t_ms, kind in votes]}


def gap_block(*entries) -> list:
    """A monitor_gaps block with one failover run holding the given intervals."""
    return [{"scenario": "failover", "iteration": 1, "intervals": list(entries)}]


def test_unobserved_interval_tokens_total_the_safety_sessions_that_have_the_block(tmp_path):
    """Intervals, the longest in seconds, the votes, and the distinct heights over the safety sessions whose analysis
    has monitor_gaps; a safety session without the block and a session without safety.json are left out."""
    first = series(tmp_path)
    first.analysis["monitor_gaps"] = gap_block(
        gap_entry(0, 15_000, [10, 11], [(10, 4000, "commit"), (11, 7000, "nil")]),
        gap_entry(20_000, 47_000, [12], [(12, 30_000, "commit")]))
    second = make_data({"safety": safety(session="S8", t_ms=FAULT + 900_000)}, session="S8",
                       analysis={"monitor_gaps": gap_block(gap_entry(100_000, 111_000, [50], []))})
    without_block = make_data({"safety": safety(session="S7", t_ms=FAULT + 800_000)}, session="S7")
    no_safety = make_data(session="S6", analysis={"monitor_gaps": gap_block(
        gap_entry(0, 90_000, [1, 2, 3], [(1, 100, "commit")]))})
    values = compute_values([first, second, without_block, no_safety])["values"]
    assert [values[name] for name in UNOBSERVED_TOKENS] == ["3", "27,0", "3", "4"]


def test_a_block_without_intervals_gives_zero_tokens(tmp_path):
    """A safety session whose runs were fully observed: no interval, no vote, no height."""
    session = series(tmp_path)
    session.analysis["monitor_gaps"] = [{"scenario": "failover", "iteration": 1, "intervals": []}]
    values = compute_values([session])["values"]
    assert [values[name] for name in UNOBSERVED_TOKENS] == ["0", "0,0", "0", "0"]


def test_unobserved_interval_tokens_are_explained_when_no_safety_session_has_the_block(tmp_path):
    """The series session's analysis predates its monitor.jsonl, so it has no block: each token gets the reason."""
    result = compute_values([series(tmp_path)])
    assert set(UNOBSERVED_TOKENS) <= set(TOKEN_NAMES)
    for name in UNOBSERVED_TOKENS:
        assert name not in result["values"]
        assert "monitor_gaps" in result["uncomputed"][name]


def test_byzantine_max_is_not_available_without_prometheus_series(tmp_path):
    """No session has a series: the token reads tidak tersedia."""
    values = compute_values([series(tmp_path)])["values"]
    assert values["byzantine.max"] == "tidak tersedia"
    assert values["byzantine.sessions"] == ""
    assert values["evidence.heights"] == ""
    assert values["monitor.unobserved_sessions"] == ""


def test_dual_holder_iterations_come_from_the_monitor_and_the_iteration_windows(tmp_path):
    """A dual-signer sample inside iteration 1 names that iteration."""
    values = compute_values([series(tmp_path, dual_at_ms=FAULT + 30_000)])["values"]
    assert values["S4.dual.iterations"] == "SFIX iterasi 1"
    assert values["S4.ssr.k"] == "0"
    assert values["S4.ssr.failed"].startswith("iterasi 1 (syarat keselamatan: 1 detik kedua node memuat 48DC2183")
    assert values["crit4.status"] == "Tidak terpenuhi"


def test_ssr_counts_clean_iterations_and_assigns_evidence_to_iterations(tmp_path):
    """No dual seconds and no evidence: 1 of 1. Evidence at a height after the fault height fails that iteration."""
    clean = compute_values([series(tmp_path)])["values"]
    assert (clean["S4.ssr.k"], clean["S4.ssr.pct"], clean["S4.ssr.failed"], clean["crit4.status"]) == (
        "1", "100,00", "", "Terpenuhi")
    evidence = [{"height": 200, "count": 1, "types": ["DuplicateVoteEvidence"]}]
    dirty = compute_values([series(tmp_path / "other", evidence=evidence)])["values"]
    assert dirty["S4.ssr.k"] == "0"
    assert "bukti equivocation" in dirty["S4.ssr.failed"]


def test_snapshot_iteration_and_deviation(tmp_path):
    """The expected layout gives no deviation; a primary that still holds the validator key after takeover is reported."""
    ok = compute_values([series(tmp_path)])["values"]
    assert (ok["snapshot.iteration"], ok["snapshot.deviation"]) == ("1", "")
    bad = compute_values([series(tmp_path / "bad", snaps=snapshots(takeover_key_of_primary=TARGET_VALIDATOR))])["values"]
    assert "iterasi 1, after_takeover, validator1: berkas kunci memuat 48DC2183" in bad["snapshot.deviation"]


def test_snapshot_deviation_flags_a_lower_state_tuple_and_a_leftover_staged_file(tmp_path):
    """The active node's tuple must not be below the previous holder's; no .staged may remain after takeover."""
    snaps = snapshots()
    snaps[1]["nodes"][1]["state"]["height"] = 100
    snaps[1]["nodes"][0]["keys"][2] = {"exists": True, "address": MOCK}
    text = compute_values([series(tmp_path, snaps=snaps)])["values"]["snapshot.deviation"]
    assert "validator2: tuple state lebih rendah" in text
    assert "validator1: berkas .staged tersisa" in text


def test_snapshot_iteration_needs_all_three_points(tmp_path):
    """An iteration with two points is not shown; without any complete iteration both tokens are explained."""
    result = compute_values([series(tmp_path, snaps=snapshots()[:2])])
    assert "snapshot.iteration" in result["uncomputed"] and "snapshot.deviation" in result["uncomputed"]


RAW_POINT_TIMES = {"before_fault": "2026-10-09 12:01:02.400", "after_takeover": "2026-10-09 12:02:40.000",
                   "after_failback": "2026-10-09 12:05:10.900"}


def raw_capture_session(directory, session: str = "S12", iteration: int = 1, with_files: bool = True):
    """A session whose snapshots of one iteration name raw files for both nodes at all three points.

    with_files False leaves the raw files off the disk, as a snapshot written before raw capture would.
    """
    from harness.clock import parse_log_time
    snapshots = []
    for point, local in RAW_POINT_TIMES.items():
        nodes = []
        for label in ("validator1", "validator2"):
            relative = f"raw/{iteration}-{point}-{label}.txt"
            if with_files:
                (directory / "raw").mkdir(parents=True, exist_ok=True)
                (directory / relative).write_text(f"$ cat priv_validator_state.json\n{label} {point}\n", encoding="utf-8")
            nodes.append({"node": label, "raw_file": relative})
        snapshots.append({"session": session, "iteration": iteration, "point": point,
                          "t_ms": parse_log_time(local, 25200), "nodes": nodes})
    return make_data(session=session, directory=directory, snapshots=snapshots)


def test_raw_tokens_show_both_state_files_of_each_snapshot_point(tmp_path):
    """Each point holds validator1's file then validator2's, each under its container name; times are HH:MM:SS."""
    values = compute_values([raw_capture_session(tmp_path / "S12")])["values"]
    assert (values["raw.session"], values["raw.iteration"]) == ("S12", "1")
    assert values["raw.before_fault"] == ("# validator1-node\n$ cat priv_validator_state.json\nvalidator1 before_fault\n"
                                          "# validator2-node\n$ cat priv_validator_state.json\nvalidator2 before_fault")
    assert values["raw.after_failback"].endswith("validator2 after_failback")
    assert "validator1 after_takeover" in values["raw.after_takeover"]
    assert values["raw.times"] == "12:01:02, 12:02:40, 12:05:10"


def test_raw_tokens_come_from_the_last_session_with_raw_files(tmp_path):
    """A later session whose snapshots name raw files that are not on disk does not count."""
    first = raw_capture_session(tmp_path / "S11", session="S11", iteration=2)
    later = raw_capture_session(tmp_path / "S12", with_files=False)
    values = compute_values([first, later])["values"]
    assert (values["raw.session"], values["raw.iteration"]) == ("S11", "2")


def test_raw_tokens_are_explained_without_raw_files(tmp_path):
    """Snapshots without raw files (taken before raw capture existed) leave all six tokens with a reason."""
    from harness.values import RAW_TOKENS
    result = compute_values([series(tmp_path)])
    assert set(RAW_TOKENS) == {"raw.session", "raw.iteration", "raw.before_fault", "raw.after_takeover",
                               "raw.after_failback", "raw.times"}
    assert set(RAW_TOKENS) <= set(TOKEN_NAMES)
    for name in RAW_TOKENS:
        assert "raw" in result["uncomputed"][name]


def negatives(failed: tuple = ()) -> dict:
    """security_negatives.json with eight rows."""
    rows = [{"number": number, "case": f"c{number}", "status": "GAGAL" if number in failed else "LULUS"}
            for number in range(1, 9)]
    return {"session": "S8", "rows": rows}


def test_negative_lock_and_overhead_tokens():
    """Cases 1 to 7 passed, Indonesian method and outcome words, and the larger of the two overhead values."""
    lock = {"session": "S8", "methods": ["lockprobe", "unit_tests"], "probe": {"outcome": "refused"}, "passed": True}
    stat = lambda mean, maximum: {"mean": mean, "maximum": maximum}  # noqa: E731  (one-line fixture helper)
    overhead = {"session": "S8", "valid": True, "passive_state_writes_per_minute": 12.04, "pid_changes": {},
                "nodes_without_samples": [],
                "nodes": {"validator-1": {"pid": 1, "cpu_percent": stat(0.4, 3.0), "rss_kb": stat(1000.0, 20480.0)},
                          "validator-2": {"pid": 2, "cpu_percent": stat(0.9, 2.0), "rss_kb": stat(900.0, 30720.0)}}}
    values = compute_values([make_data({"security_negatives": negatives((2, 4)), "lock_negative": lock,
                                        "overhead": overhead}, session="S8")])["values"]
    assert (values["neg.session"], values["neg.k"], values["neg.failed"]) == ("S8", "5", "2, 4")
    assert (values["lock.method"], values["lock.probe_outcome"]) == ("lockprobe dan uji unit", "ditolak")
    assert values["overhead.session"] == "S8"
    assert (values["overhead.cpu_mean_max"], values["overhead.rss_max_mb"], values["overhead.writes_per_minute"]) == (
        "0,90", "30,0", "12,04")


def test_unproven_cases_are_not_counted_as_passed():
    """A case marked TIDAK TERBUKTI is not a pass and its number is listed as not passed."""
    rows = negatives()["rows"]
    rows[1]["status"] = "TIDAK TERBUKTI"
    values = compute_values([make_data({"security_negatives": {"session": "S8", "rows": rows}}, session="S8")])["values"]
    assert (values["neg.k"], values["neg.failed"]) == ("6", "2")


def test_an_invalid_overhead_run_is_explained_not_printed():
    """overhead.json with valid false is flagged in the table and its tokens are listed as not computed."""
    overhead = {"session": "S8", "valid": False, "passive_state_writes_per_minute": 1.0, "pid_changes": {},
                "nodes_without_samples": ["validator-2"], "nodes": {}}
    result = compute_values([make_data({"overhead": overhead}, session="S8")])
    assert "overhead.cpu_mean_max" in result["uncomputed"]
    assert "invalid" in result["uncomputed"]["overhead.cpu_mean_max"]


def test_figure_and_rate_tokens(tmp_path):
    """The plotted iteration, file names with the real session id, the height range, and the signed heights around the fault."""
    values = compute_values([series(tmp_path)])["values"]
    assert values["S4.fig.iteration"] == "1"
    assert values["S4.fig.file"] == "gambar-failover-SFIX-iterasi-1.png"
    assert values["S4.fig.heights"] == "126 sampai 169"
    assert (values["S4.fig.last_signed"], values["S4.fig.first_signed"]) == ("127", "144")
    assert values["S4.fig.signers"] == "gambar-penanda-tangan-SFIX.png"


def test_block_rate_means_use_covered_windows_only():
    """Windows without complete data are left out of the mean; a label with none is explained."""
    def incident(iteration, before, after):
        """One incident with block rates for two windows."""
        return {"session": "S4", "iteration": iteration, "missed_share": None, "block_rates": [
            {"label": "before_fault_60s", "covered": before is not None, "blocks_per_minute": before},
            {"label": "after_recovery_60s", "covered": after is not None, "blocks_per_minute": after},
            {"label": "following_120s", "covered": False, "blocks_per_minute": None}]}
    failover = [failover_row(iteration=n, ttr_s=10.0) for n in (1, 2, 3)]
    analysis = {"failover": failover, "incidents": [incident(1, 20.0, 18.0), incident(2, 22.0, None), incident(3, None, 20.0)]}
    result = compute_values([make_data(analysis=analysis, session="S4")])
    assert result["values"]["S4.rate.before.mean"] == "21,00"
    assert result["values"]["S4.rate.after.mean"] == "19,00"
    assert "S4.rate.following.mean" in result["uncomputed"]


def rate_window(label: str, start_ms: int, length_ms: int, rate: float | None) -> dict:
    """One stored block-rate window; a rate of None is a window without complete data."""
    return {"label": label, "start_ms": start_ms, "end_ms": start_ms + length_ms, "covered": rate is not None,
            "blocks_per_minute": rate}


# iteration, t_aktif_cadangan, notification and primary signature after it (ms), rate of the 120 s after the primary signs
FAILBACK_TIMING = ((1, 100_000, 55_084, 83_037, 17.5), (2, 1_000_000, 63_845, 85_020, 18.5),
                   (3, 2_000_000, 190_000, 200_000, None))


def failback_timing_session():
    """A series whose failback lands inside window 2 (iteration 1), inside window 3 (1 and 2), or after both (3)."""
    failover, failback, incidents = [], [], []
    for iteration, first, notify_after, signature_after, rate in FAILBACK_TIMING:
        failover.append(failover_row(iteration=iteration, ttr_s=20.0, t_first_signature_ms=first, sources={}))
        failback.append(failback_row(iteration=iteration, t_failback_notified_ms=first + notify_after,
                                     t_primary_signature_ms=first + signature_after, sources={}))
        incidents.append({"session": "S5", "iteration": iteration, "missed_share": None, "block_rates": [
            rate_window("before_fault_60s", first - 90_000, 60_000, 20.0),
            rate_window("after_recovery_60s", first, 60_000, 19.0),
            rate_window("following_120s", first + 60_000, 120_000, 18.0),
            rate_window("after_failback_120s", first + signature_after, 120_000, rate)]})
    return make_data(analysis={"failover": failover, "failback": failback, "incidents": incidents}, session="S5")


def test_rate_after_failback_and_where_the_failback_falls_against_the_windows():
    """Mean of the covered fourth windows; notification and primary signature after t_aktif_cadangan; window counts."""
    values = compute_values([failback_timing_session()])["values"]
    assert values["S4.rate.after_failback.mean"] == "18,00"
    assert (values["S4.fb.notify_offset.min"], values["S4.fb.notify_offset.max"]) == ("55,1", "190,0")
    assert (values["S4.fb.primary_sig_offset.min"], values["S4.fb.primary_sig_offset.max"]) == ("83,0", "200,0")
    assert values["S4.rate.window2_with_failback.k"] == "1"
    assert values["S4.rate.window3_with_failback.k"] == "2"


def test_failback_timing_tokens_are_rate_tokens_and_explained_without_a_failback():
    """No failback row: the offsets carry a reason, both window counts are 0, and the fourth-window mean is explained."""
    from harness.values import RATE_TOKENS
    new = ("S4.rate.after_failback.mean", "S4.fb.notify_offset.min", "S4.fb.notify_offset.max",
           "S4.fb.primary_sig_offset.min", "S4.fb.primary_sig_offset.max", "S4.rate.window2_with_failback.k",
           "S4.rate.window3_with_failback.k")
    assert set(new) <= set(RATE_TOKENS) <= set(TOKEN_NAMES)
    session = failback_timing_session()
    session.analysis["failback"] = []
    for incident in session.analysis["incidents"]:
        incident["block_rates"] = incident["block_rates"][:3]
    result = compute_values([session])
    assert (result["values"]["S4.rate.window2_with_failback.k"], result["values"]["S4.rate.window3_with_failback.k"]) == (
        "0", "0")
    for name in new[:5]:
        assert name in result["uncomputed"], name


def test_criteria_statuses_follow_the_conditions_of_the_draft():
    """crit1: TTD mean and max at or below 15 s; crit2: 30 s; each fails when either exceeds its bound."""
    failover = [failover_row(iteration=n, ttd_s=ttd, ttr_s=ttr)
                for n, (ttd, ttr) in enumerate(((10.0, 20.0), (15.0, 31.0)), start=1)]
    values = compute_values([make_data(analysis={"failover": failover}, session="S4")])["values"]
    assert values["crit1.status"] == "Terpenuhi"
    assert values["crit2.status"] == "Tidak terpenuhi"


def test_criteria_fail_when_a_followed_iteration_lacks_its_measurement(tmp_path):
    """One iteration without a TTR or TTD makes crit1 or crit2 not met, and the extras count and explain it."""
    failover = [failover_row(iteration=1, ttd_s=10.0, ttr_s=20.0),
                failover_row(iteration=2, ttd_s=10.0, ttr_s=None, invalid_reason="tanpa pengambilalihan"),
                failover_row(iteration=3, ttd_s=None, ttr_s=20.0)]
    result = compute_values([make_data(analysis={"failover": failover}, session="S4")])
    assert result["values"]["crit1.status"] == "Tidak terpenuhi" and result["values"]["crit2.status"] == "Tidak terpenuhi"
    extras = result["extras"]
    assert extras["S4.missing_measurements"] == "2"
    assert extras["S4.missing_measurements.reason"] == (
        "iterasi 2: TTR tidak terukur (tanpa pengambilalihan); iterasi 3: TTD tidak terukur")


def test_no_missing_measurement_leaves_the_extras_at_zero():
    """Complete rows give a count of zero and no reason."""
    failover = [failover_row(iteration=1, ttd_s=10.0, ttr_s=20.0)]
    extras = compute_values([make_data(analysis={"failover": failover}, session="S4")])["extras"]
    assert (extras["S4.missing_measurements"], extras["S4.missing_measurements.reason"]) == ("0", "")


def test_crit3_and_the_gains_need_a_soak_whose_kill_completed(tmp_path):
    """A soak whose failover did not complete measured no recovery: crit3 and every availability gain are uncomputed."""
    failover = [failover_row(ttr_s=60.0, ttd_s=10.0)]
    soak = soak_session(tmp_path)
    soak.optional["soak"]["kill_outcome"] = "no_takeover"
    sessions = [make_data(analysis={"failover": failover}, session="S4"), make_data({"drill": drill()}, session="S6"), soak]
    result = compute_values(sessions)
    assert result["values"]["soak.availability"] == "99,90"
    for name in ("crit3.status", "availability.gain_pp", "sens.assumed.gain_pp", "sens.literature.gain_pp"):
        assert name in result["uncomputed"], name
        assert "no_takeover" in result["uncomputed"][name]
    from harness.report import sensitivity_rows
    assert {row["gain_pp"] for row in sensitivity_rows(sessions, failover)} == {None}


def fault_row(t_ms: int, scenario: str = "failover", iteration: int = 2) -> dict:
    """A successful fault_injected event row."""
    return {"session": "SFIX", "t_ms": t_ms, "kind": "fault_injected", "scenario": scenario, "iteration": iteration,
            "exit_code": 0, "ended_ms": t_ms + 500, "height": 300}


def test_an_iterations_own_detection_is_not_unintended(tmp_path):
    """The fixture's single detection belongs to its fault, even 51,6 s after it, so the count is 0."""
    assert compute_values([series(tmp_path)])["values"]["S4.unintended_failover.count"] == "0"


def test_a_second_detection_without_a_fault_is_unintended(tmp_path):
    """A detection line in the copied log, after the iteration's own, counts."""
    session = series(tmp_path)
    late = ('time="2026-10-05 09:58:00.000" level=error msg="Maximum failures reached, initiating failover" '
            'module=failover node=validator-1\n')
    (session.directory / "logs" / "syncguard.log").write_text(late)
    assert compute_values([session])["values"]["S4.unintended_failover.count"] == "1"


def test_a_hang_detection_inside_the_wait_is_its_own_and_one_beyond_the_bound_is_not(tmp_path):
    """The hang's detection within 60 s of the pause is its own; a second one 70 s after it exceeds the 35 s bound."""
    session = series(tmp_path)
    events_path = session.directory / "events.jsonl"
    append_jsonl(events_path, fault_row(FAULT + 400_000, "hang", 2))
    append_jsonl(events_path, syncguard_row("max_failures", "validator-1", FAULT + 440_000, 2))
    assert compute_values([session])["values"]["S4.unintended_failover.count"] == "0"
    append_jsonl(events_path, syncguard_row("max_failures", "validator-1", FAULT + 470_000, 2))
    assert compute_values([session])["values"]["S4.unintended_failover.count"] == "1"


def syncguard_row(marker: str, node: str, t_ms: int, iteration: int = 1) -> dict:
    """An events.jsonl row of a recognised syncguard log line."""
    return {"session": "SFIX", "t_ms": t_ms, "kind": "syncguard", "iteration": iteration, "marker": marker, "node": node,
            "level": "warning", "message": marker, "source": f"smoke-passive.txt:{t_ms % 1000}", "details": {}}


def test_refusals_count_lines_and_fail_closed_needs_an_iteration_without_takeover(tmp_path):
    """Refusal lines are counted; an iteration is fail-closed only if it ended without takeover and has a marker."""
    session = series(tmp_path)
    baseline = compute_values([session])["values"]
    assert (baseline["S4.refusals.count"], baseline["S4.failclosed.count"]) == ("0", "0")
    events_path = session.directory / "events.jsonl"
    for row in (syncguard_row("transition_refused", "validator-2", FAULT + 50_001),
                syncguard_row("failover_aborted", "validator-1", FAULT + 50_002)):
        append_jsonl(events_path, row)
    with_takeover = compute_values([session])["values"]
    assert (with_takeover["S4.refusals.count"], with_takeover["S4.failclosed.count"]) == ("2", "0")
    rows = [row for row in read_events(events_path) if row.get("marker") != "takeover_complete"]
    events_path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
    analyze_session(session.directory)
    without = compute_values([load_session_data(session.directory)])["values"]
    assert without["S4.failclosed.count"] == "1"


def test_a_refusal_by_the_primary_does_not_make_an_iteration_fail_closed(tmp_path):
    """Only a refusal logged by the backup counts; a primary-side refusal alone does not."""
    session = series(tmp_path)
    rows = [row for row in read_events(session.directory / "events.jsonl") if row.get("marker") != "takeover_complete"]
    rows.append(syncguard_row("transition_refused", "validator-1", FAULT + 50_001))
    (session.directory / "events.jsonl").write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
    analyze_session(session.directory)
    values = compute_values([load_session_data(session.directory)])["values"]
    assert values["S4.failclosed.count"] == "0"


def test_write_values_writes_json_with_roles_and_reasons(tmp_path):
    """values.json holds values, roles, and uncomputed reasons."""
    path = write_values([series(tmp_path)], tmp_path / "out")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert path.name == "values.json"
    assert set(data) == {"values", "roles", "uncomputed", "extras"}
    assert data["values"]["S4.n"] == "1"
    assert data["roles"]["S4"] == "SFIX"
    assert set(data["values"]) | set(data["uncomputed"]) == set(TOKEN_NAMES)


def test_unobserved_monitor_means_no_safe_iteration(tmp_path):
    """An empty monitor.jsonl proves nothing: ssr.k is 0, the iteration fails with 'tidak teramati', crit4 is not met."""
    values = compute_values([series(tmp_path, covered=False)])["values"]
    assert values["S4.ssr.k"] == "0"
    assert "syarat keselamatan: tidak teramati" in values["S4.ssr.failed"]
    assert values["crit4.status"] == "Tidak terpenuhi"
    assert values["S4.fb.k"] == "0"
    assert values["crit5.status"] == "Tidak terpenuhi"


def test_a_monitor_gap_inside_the_window_means_not_observed(tmp_path):
    """Samples at both ends but a 30 s hole inside: the window is not covered."""
    session = series(tmp_path)
    path = session.directory / "monitor.jsonl"
    kept = [json.loads(line) for line in path.read_text().splitlines()]
    kept = [row for row in kept if not FAULT + 20_000 <= row["t_ms"] <= FAULT + 50_000]
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in kept))
    assert compute_values([session])["values"]["S4.ssr.k"] == "0"


def test_ping_pong_after_the_failback_still_fails_criterion_5(tmp_path):
    """An unintended detection after the failback interval but inside the iteration window fails the failback."""
    session = series(tmp_path)
    assert compute_values([session])["values"]["S4.fb.k"] == "1"
    append_jsonl(session.directory / "events.jsonl", syncguard_row("max_failures", "validator-1", FAULT + 252_000))
    assert compute_values([session])["values"]["S4.fb.k"] == "0"


def test_dual_seconds_outside_every_iteration_window_are_named(tmp_path):
    """A dual-holder sample after the iteration ended is reported as outside the iterations."""
    values = compute_values([series(tmp_path, dual_at_ms=FAULT + 300_000)])["values"]
    assert values["S4.dual.iterations"] == "SFIX di luar iterasi"


def test_series_abort_is_reported_as_a_harness_extra(tmp_path):
    """S4.series_aborted and its reason are extras, not draft-D tokens."""
    session = series(tmp_path)
    clean = compute_values([session])
    assert clean["extras"] == {"S4.series_aborted": "Tidak", "S4.series_aborted.reason": "", "hang.not_injected": "",
                               "S4.missing_measurements": "0", "S4.missing_measurements.reason": "",
                                "partition.outcomes": ""}
    session.analysis["series_aborted"] = [{"iteration": 2, "outcome": "no_failback"}]
    extras = compute_values([session])["extras"]
    assert extras["S4.series_aborted"] == "Ya"
    assert extras["S4.series_aborted.reason"] == "setelah iterasi 2: takeover terjadi tetapi failback tidak selesai"
    assert not set(extras) & set(TOKEN_NAMES)


def test_hang_n_counts_only_injected_iterations_and_lists_the_rest():
    """A pause that never took effect is not a hang iteration; it is reported separately with its reason."""
    rows = [{"iteration": 1, "fault_injected": True, "detected": True, **hang_times(31.0), "restart_outcome": "succeeded",
             "action": "unpause", "reason": "ok"},
            {"iteration": 2, "fault_injected": False, "detected": False, **hang_times(None), "restart_outcome": "not_attempted",
             "action": "none", "reason": "pause exited 1"}]
    result = compute_values([make_data(analysis={"hang": rows}, session="S5")])
    assert (result["values"]["hang.n"], result["values"]["hang.detected.k"]) == ("1", "1")
    assert result["extras"]["hang.not_injected"] == "iterasi 2: pause exited 1"


def test_tabel_4_6_and_the_s4_tokens_read_the_same_rows(tmp_path):
    """With a soak session whose failover is a copy of the series, n of the summary equals S4.ttr.n."""
    from harness.report import failover_rows, summary_rows
    from test_report import fixture_pair
    series_session, soak = fixture_pair(tmp_path)
    sessions = [series_session, soak]
    summary = [row for row in summary_rows(failover_rows(sessions), (("ttr_s", "TTR"),)) if row["session"] == "SFIX"]
    result = compute_values(sessions)
    assert result["roles"]["S4"] == "SFIX"
    assert str(summary[0]["n"]) == result["values"]["S4.ttr.n"] == "1"


def test_the_last_drill_is_the_role_even_when_it_did_not_recover():
    """One rule for drill and soak: the last session with the result file; an unrecovered drill is explained."""
    sessions = [make_data({"drill": drill()}, session="S6"),
                make_data({"drill": drill(outcome="no_signature", recovery_s=None)}, session="S8")]
    result = compute_values(sessions)
    assert result["roles"]["drill"] == "S8"
    assert "drill.recovery_s" in result["uncomputed"]


def test_fb_n_counts_only_iterations_with_a_takeover(tmp_path):
    """No takeover marker: the iteration has no failback row, so S4.fb.n is 0 while S4.n still counts the iteration."""
    session = series(tmp_path)
    rows = [row for row in read_events(session.directory / "events.jsonl") if row.get("marker") != "takeover_complete"]
    (session.directory / "events.jsonl").write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
    analyze_session(session.directory)
    values = compute_values([load_session_data(session.directory)])["values"]
    assert (values["S4.n"], values["S4.fb.n"]) == ("1", "0")
    assert values["S4.ttr.n"] == "0"


def rewrite_events(session, change) -> None:
    """Rewrite events.jsonl with change(row) applied to every row; a None result drops the row."""
    path = session.directory / "events.jsonl"
    rows = [change(row) for row in read_events(path)]
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows if row is not None))


def test_evidence_is_attributed_to_a_followed_failed_kill(tmp_path):
    """A failed kill whose container stopped is an iteration; evidence after its fault height belongs to it, not to iteration 1."""
    from harness.values import evidence_by_iteration, events_of
    evidence = [{"height": 200, "count": 1, "types": ["DuplicateVoteEvidence"]}]
    session = series(tmp_path, evidence=evidence)
    base = {"session": "SFIX", "scenario": "failover", "iteration": 2}
    append_jsonl(session.directory / "events.jsonl", {**base, "t_ms": FAULT + 300_000, "kind": "fault_injected",
                                                      "exit_code": 1, "ended_ms": FAULT + 300_100, "height": 190,
                                                      "command": "docker kill validator1-node"})
    append_jsonl(session.directory / "events.jsonl", {**base, "t_ms": FAULT + 300_200, "kind": "fault_command_failed",
                                                      "exit_code": 1, "container_state": "exited"})
    assert list(evidence_by_iteration(session, events_of(session))) == [2]


def test_ssr_failed_names_the_outcome_in_indonesian(tmp_path):
    """The raw runner code never reaches the thesis text."""
    session = series(tmp_path)
    append_jsonl(session.directory / "events.jsonl", {"session": "SFIX", "t_ms": FAULT + 200_000, "kind": "iteration_result",
                                                      "scenario": "failover", "iteration": 1, "outcome": "no_failback"})
    failed = compute_values([session])["values"]["S4.ssr.failed"]
    assert "takeover terjadi tetapi failback tidak selesai" in failed and "no_failback" not in failed


def test_a_crash_detection_later_than_180_seconds_is_unintended(tmp_path):
    """The runner waits 180 s for the takeover, so a detection 200 s after the kill is not that iteration's own."""
    session = series(tmp_path)
    rewrite_events(session, lambda row: {**row, "t_ms": FAULT + 200_000} if row.get("marker") == "max_failures" else row)
    assert compute_values([session])["values"]["S4.unintended_failover.count"] == "1"


def hang_result(injected: bool) -> dict:
    """A minimal hang-2 analysis row."""
    return {"iteration": 2, "fault_injected": injected, "detected": injected, **hang_times(40.0), "restart_outcome": "failed",
            "action": "kill", "reason": "r"}


def test_a_hang_whose_pause_exited_nonzero_but_took_effect_claims_its_detection(tmp_path):
    """The hang result says fault_injected true: its detection is its own; with false it would be unintended."""
    session = series(tmp_path)
    events_path = session.directory / "events.jsonl"
    append_jsonl(events_path, {**fault_row(FAULT + 400_000, "hang", 2), "exit_code": 1})
    append_jsonl(events_path, syncguard_row("max_failures", "validator-1", FAULT + 440_000, 2))
    session.analysis["hang"] = [hang_result(True)]
    assert compute_values([session])["values"]["S4.unintended_failover.count"] == "0"
    session.analysis["hang"] = [hang_result(False)]
    assert compute_values([session])["values"]["S4.unintended_failover.count"] == "1"


def test_a_detection_after_the_harness_clearing_kill_is_not_unintended(tmp_path):
    """fault_cleared with a kill, an abort kill, or the follow-up kill explains a detection within 180 s."""
    for kind, extra in (("fault_cleared", {"action": "kill"}), ("abort_kill", {}), ("fault_cleared_after_failed_unpause", {})):
        session = series(tmp_path / kind)
        events_path = session.directory / "events.jsonl"
        append_jsonl(events_path, syncguard_row("max_failures", "validator-1", FAULT + 520_000, 2))
        assert compute_values([session])["values"]["S4.unintended_failover.count"] == "1"
        append_jsonl(events_path, {"session": "SFIX", "t_ms": FAULT + 500_000, "kind": kind, "scenario": "hang",
                                   "iteration": 2, "exit_code": 0, **extra})
        assert compute_values([session])["values"]["S4.unintended_failover.count"] == "0", kind


def test_samples_where_both_nodes_are_unreachable_do_not_cover_a_window(tmp_path):
    """Two unreachable nodes cannot show a single holder, so those samples leave a hole in the coverage."""
    session = series(tmp_path)
    path = session.directory / "monitor.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:
        if FAULT + 20_000 <= row["t_ms"] <= FAULT + 50_000:
            row["addresses"] = {"validator1-node": "unreachable", "validator2-node": "unreachable"}
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
    assert compute_values([session])["values"]["S4.ssr.k"] == "0"


def test_an_iteration_without_a_logged_outcome_is_never_a_success(tmp_path):
    """No iteration_result means the iteration was aborted by an exception."""
    session = series(tmp_path)
    assert compute_values([session])["values"]["S4.ssr.k"] == "1"
    rewrite_events(session, lambda row: None if row["kind"] == "iteration_result" else row)
    analyze_session(session.directory)
    values = compute_values([load_session_data(session.directory)])["values"]
    assert values["S4.ssr.k"] == "0" and "tanpa hasil tercatat" in values["S4.ssr.failed"]
    assert values["crit4.status"] == "Tidak terpenuhi"


def test_an_iteration_invalid_for_no_takeover_counts_as_without_takeover_everywhere(tmp_path):
    """A takeover line after the first signature: no failback row, S4.fb.n 0, and the iteration can be fail-closed."""
    session = series(tmp_path)
    rewrite_events(session, lambda row: {**row, "t_ms": FAULT + 200_000} if row.get("marker") == "takeover_complete" else row)
    append_jsonl(session.directory / "events.jsonl", syncguard_row("transition_refused", "validator-2", FAULT + 50_001))
    analyze_session(session.directory)
    values = compute_values([load_session_data(session.directory)])["values"]
    assert (values["S4.fb.n"], values["S4.failclosed.count"]) == ("0", "1")


def test_ssr_failure_translates_the_invalid_reason():
    """An invalid TTR is explained with the Indonesian reason, never the raw English runner text."""
    from types import SimpleNamespace
    from harness.values import ssr_failures
    inputs = SimpleNamespace(outcomes={1: "complete"}, evidence_by_iteration={}, windows={1: (0, 1)},
                             covered=lambda start, end: True, dual_seconds=lambda start, end: 0)
    row = failover_row(ttr_s=None, invalid_reason="no signature after the gap")
    failures = ssr_failures(row, inputs)
    assert "tidak ada tanda tangan setelah celah" in failures[0]
    assert "no signature after the gap" not in failures[0]


def partition_session(results: list, tmp_path):
    """The fixture session with partition-N.json files, as loaded SessionData."""
    session = series(tmp_path)
    for result in results:
        write_json(session.directory / f"partition-{result['iteration']}.json", result)
    analyze_session(session.directory)
    return load_session_data(session.directory)


def partition_run(iteration: int, outcome: str, command: str = "docker network disconnect story_net validator1-node",
                  exit_code: int = 0, symptom: str | None = "rpc_refused", session: str = "SFIX") -> dict:
    """A partition result with the fields the tokens read."""
    return {"session": session, "iteration": iteration, "outcome": outcome, "command": command,
            "fault_exit_code": exit_code, "detection_symptom": symptom}


def test_partition_tokens_are_explained_without_a_partition_run(tmp_path):
    """No run: every partition token carries a reason and the roles name no partition session."""
    result = compute_values([series(tmp_path)])
    assert {"partition.tested", "partition.method", "partition.symptom"} <= set(result["uncomputed"])
    assert result["roles"]["partition"] is None


def test_partition_tested_names_the_session_whatever_the_outcome(tmp_path):
    """A disconnect that took effect is tested, even when the run did not complete; outcomes are an extra."""
    session = partition_session([partition_run(1, "not_detected", symptom=None), partition_run(2, "no_takeover")], tmp_path)
    result = compute_values([session])
    assert result["values"]["partition.tested"] == "diuji pada Sesi SFIX"
    assert result["values"]["partition.method"] == (
        "pemutusan jaringan Docker kontainer validator1-node (docker network disconnect)")
    assert result["extras"]["partition.outcomes"] == "iterasi 1: partisi tidak terdeteksi; iterasi 2: takeover tidak terjadi"
    assert "not_detected" not in result["extras"]["partition.outcomes"]
    assert result["roles"]["partition"] == "SFIX"
    assert not {"partition.tested", "partition.method"} & set(result["uncomputed"])


def test_partition_tested_lists_every_session_that_ran_it(tmp_path):
    """Several sessions are listed in order; a run whose disconnect failed does not count for its session."""
    from harness.values import partition_session_phrase
    assert partition_session_phrase(["S7"]) == "diuji pada Sesi S7"
    assert partition_session_phrase(["S7", "S8"]) == "diuji pada Sesi S7 dan S8"
    assert partition_session_phrase(["S6", "S7", "S8"]) == "diuji pada Sesi S6, S7, dan S8"


def test_partition_is_not_run_when_every_disconnect_failed(tmp_path):
    """Only fault_command_failed runs: tidak dijalankan, and no symptom to report."""
    session = partition_session([partition_run(1, "fault_command_failed", exit_code=1, symptom=None)], tmp_path)
    result = compute_values([session])
    assert result["values"]["partition.tested"] == "tidak dijalankan"
    assert "partition.symptom" in result["uncomputed"]


def test_partition_symptom_counts_labels_over_the_runs(tmp_path):
    """The Indonesian label with how many of the runs showed it; runs without a detection have no symptom."""
    session = partition_session([partition_run(1, "complete"), partition_run(2, "complete"),
                                 partition_run(3, "complete", symptom="peer_count"),
                                 partition_run(4, "not_detected", symptom=None)], tmp_path)
    symptom = compute_values([session])["values"]["partition.symptom"]
    assert symptom == "koneksi RPC ditolak (2 dari 4 percobaan); jumlah peer di bawah min_peers (1 dari 4 percobaan)"
    single = partition_session([partition_run(1, "complete")], tmp_path / "one")
    assert compute_values([single])["values"]["partition.symptom"] == "koneksi RPC ditolak (1 dari 1 percobaan)"
    unknown = partition_session([partition_run(1, "complete", symptom="unknown")], tmp_path / "two")
    assert compute_values([unknown])["values"]["partition.symptom"] == "tidak teridentifikasi (1 dari 1 percobaan)"


def test_a_partition_detection_is_not_unintended(tmp_path):
    """The max-failures line a partition provokes is the run's own detection, not an unintended failover."""
    session = partition_session([partition_run(2, "complete")], tmp_path)
    fault_ms = FAULT + 1_000_000
    base = {"session": "SFIX", "scenario": "partition", "iteration": 2}
    append_jsonl(session.directory / "events.jsonl", {**base, "t_ms": fault_ms, "kind": "fault_injected", "exit_code": 0,
                                                      "ended_ms": fault_ms + 100, "height": 300,
                                                      "command": "docker network disconnect story_net validator1-node"})
    append_jsonl(session.directory / "events.jsonl", {**base, "t_ms": fault_ms + 33_000, "kind": "syncguard",
                                                      "node": "validator-1", "marker": "max_failures", "level": "error",
                                                      "message": "m", "source": "syncguard.log:900", "fields": {}})
    assert compute_values([session])["values"]["S4.unintended_failover.count"] == "0"


def test_an_interval_beyond_the_nist_table_is_explained_not_raised():
    """32 iterations: mean and SD are values, the interval tokens carry the table reason."""
    failover = [failover_row(iteration=n, ttd_s=10.0 + n / 10, ttr_s=20.0 + n / 10) for n in range(1, 33)]
    result = compute_values([make_data(analysis={"failover": failover}, session="S4")])
    assert "S4.ttr.sd" in result["values"] and "S4.ttr.mean" in result["values"]
    assert "df=31" in result["uncomputed"]["S4.ttr.ci_low"]


def effective_partition_run(iteration: int, **fields) -> dict:
    """A complete partition result with every field the partition value tokens read; fields override."""
    base = {**partition_run(iteration, "complete"), "detected": True, "ttd_s": 33.0, "takeover": True, "ttr_s": 60.0,
            "backup_first_signature_height": 1200, "failback_complete": True, "restart_before_takeover": True}
    return {**base, **fields}


def test_partition_value_tokens_count_the_effective_runs(tmp_path):
    """Session, n, detected, takeover, failback counts, and the largest TTD and TTR over the runs whose disconnect took effect."""
    runs = [effective_partition_run(1, ttd_s=31.5, ttr_s=58.0), effective_partition_run(2, ttd_s=36.2, ttr_s=64.4),
            effective_partition_run(3, detected=False, ttd_s=None, takeover=False, ttr_s=None,
                                    backup_first_signature_height=None, failback_complete=False,
                                    restart_before_takeover=None, outcome="not_detected"),
            effective_partition_run(4, fault_exit_code=1, outcome="fault_command_failed", ttd_s=99.0, ttr_s=99.0)]
    values = compute_values([partition_session(runs, tmp_path)])["values"]
    assert values["partition.session"] == "Sesi SFIX"
    assert (values["partition.n"], values["partition.detected.k"], values["partition.takeover.k"],
            values["partition.failback.k"]) == ("3", "2", "2", "2")
    assert (values["partition.ttd.max"], values["partition.ttr.max"]) == ("36,2", "64,4")


def test_partition_session_joins_like_the_tested_phrase():
    """'Sesi S7', 'Sesi S7 dan S8', 'Sesi S6, S7, dan S8': the tested phrase without its prefix."""
    from harness.values import partition_session_label
    assert partition_session_label(["S7"]) == "Sesi S7"
    assert partition_session_label(["S7", "S8"]) == "Sesi S7 dan S8"
    assert partition_session_label(["S6", "S7", "S8"]) == "Sesi S6, S7, dan S8"


def test_partition_maxima_are_skipped_with_a_reason_when_nothing_was_measured(tmp_path):
    """No detected run: no TTD maximum; no backup signature: no TTR maximum."""
    run = effective_partition_run(1, detected=False, ttd_s=None, takeover=False, ttr_s=None,
                                  backup_first_signature_height=None, failback_complete=False,
                                  restart_before_takeover=None, outcome="not_detected")
    result = compute_values([partition_session([run], tmp_path)])
    assert {"partition.ttd.max", "partition.ttr.max"} <= set(result["uncomputed"])
    assert result["values"]["partition.detected.k"] == "0"


def test_partition_restart_before_takeover_reads_the_recorded_evidence(tmp_path):
    """'ya pada k dari n percobaan', with each run's phrase when the runs differ."""
    same = [effective_partition_run(1), effective_partition_run(2)]
    assert compute_values([partition_session(same, tmp_path / "a")])["values"][
        "partition.restart_before_takeover"] == "ya pada 2 dari 2 percobaan"
    mixed = [effective_partition_run(1), effective_partition_run(2, restart_before_takeover=False),
             effective_partition_run(3, restart_before_takeover=None, takeover=False)]
    assert compute_values([partition_session(mixed, tmp_path / "b")])["values"][
        "partition.restart_before_takeover"] == (
            "ya pada 1 dari 3 percobaan (iterasi 1: ya; iterasi 2: tidak; iterasi 3: tidak ada takeover)")
    none = [effective_partition_run(1, restart_before_takeover=False)]
    assert compute_values([partition_session(none, tmp_path / "c")])["values"][
        "partition.restart_before_takeover"] == "tidak pada satu pun dari 1 percobaan"


def test_partition_port_repair_reads_one_state_over_the_runs(tmp_path):
    """Every effective run restarted the primary: one label with its count."""
    runs = [effective_partition_run(1, port_repair="restarted"), effective_partition_run(2, port_repair="restarted")]
    values = compute_values([partition_session(runs, tmp_path)])["values"]
    assert values["partition.port_repair"] == "restart kontainer setelah penyambungan kembali (2 dari 2 percobaan)"


def test_partition_port_repair_counts_mixed_states_over_the_effective_runs_with_the_field(tmp_path):
    """Each state's label with k of n; n counts the effective runs that have the field, like partition.symptom."""
    runs = [effective_partition_run(1, port_repair="not_needed"), effective_partition_run(2, port_repair="restarted"),
            effective_partition_run(3, port_repair="refused"), effective_partition_run(4, port_repair="not_needed"),
            effective_partition_run(5),
            effective_partition_run(6, fault_exit_code=1, outcome="fault_command_failed", port_repair="unknown")]
    values = compute_values([partition_session(runs, tmp_path)])["values"]
    assert values["partition.port_repair"] == (
        "tidak diperlukan (2 dari 4 percobaan); restart kontainer setelah penyambungan kembali (1 dari 4 percobaan); "
        "ditolak karena kunci validator1 tidak terbukti kunci mock (1 dari 4 percobaan)")


def test_partition_port_repair_is_skipped_without_the_field(tmp_path):
    """Results written before the port check (S8) give a reason, not a state."""
    result = compute_values([partition_session([effective_partition_run(1)], tmp_path)])
    assert "partition.port_repair" in result["uncomputed"]
    assert "port_repair" in result["uncomputed"]["partition.port_repair"]


def test_partition_port_repair_is_a_draft_token():
    """The token is listed with the partition tokens, so the README count includes it."""
    from harness.values import PARTITION_TOKENS, TOKEN_NAMES
    assert "partition.port_repair" in PARTITION_TOKENS and "partition.port_repair" in TOKEN_NAMES


def test_partition_outcomes_name_the_session_when_runs_span_several_sessions():
    """Iteration numbers restart per session, so each entry names its session; one session keeps the plain text."""
    s8 = make_data(analysis={"partition": [partition_run(1, "no_failback", session="S8")]}, session="S8")
    s9 = make_data(analysis={"partition": [partition_run(1, "complete", session="S9"),
                                           partition_run(2, "not_detected", symptom=None, session="S9")]}, session="S9")
    assert compute_values([s8, s9])["extras"]["partition.outcomes"] == (
        "S8 iterasi 1: takeover terjadi tetapi failback tidak selesai; "
        "S9 iterasi 1: pemutusan, takeover, penyambungan kembali, dan failback selesai; "
        "S9 iterasi 2: partisi tidak terdeteksi")
    assert compute_values([s9])["extras"]["partition.outcomes"] == (
        "iterasi 1: pemutusan, takeover, penyambungan kembali, dan failback selesai; "
        "iterasi 2: partisi tidak terdeteksi")


def test_partition_restart_before_takeover_is_skipped_without_the_evidence_field(tmp_path):
    """A result written before the field existed gives a reason, not a guess."""
    run = effective_partition_run(1)
    del run["restart_before_takeover"]
    result = compute_values([partition_session([run], tmp_path)])
    assert "restart_before_takeover" in result["uncomputed"]["partition.restart_before_takeover"]


def test_partition_value_tokens_are_part_of_the_token_names():
    """The eight tokens the partition run adds."""
    assert {"partition.session", "partition.n", "partition.detected.k", "partition.ttd.max", "partition.takeover.k",
            "partition.ttr.max", "partition.failback.k", "partition.restart_before_takeover"} <= set(TOKEN_NAMES)


MONITORING = {"prom/prometheus:latest": "sha256:aa11bb22cc33dd44", "grafana/grafana:latest": "sha256:dd44ee55ff66aa11",
              "grafana/loki:latest": "sha256:1234567890abcdef", "grafana/promtail:latest": "sha256:feedfacecafe0123"}


def test_monitoring_versions_lists_the_image_ids_the_metadata_recorded(tmp_path):
    """One phrase over the four monitoring images, each with its tag and the first twelve hex digits of its id."""
    session = series(tmp_path)
    session.metadata["monitoring_images"] = MONITORING
    values = compute_values([session])["values"]
    assert values["monitoring.versions"] == (
        "Prometheus (prom/prometheus:latest, citra aa11bb22cc33), Grafana (grafana/grafana:latest, citra dd44ee55ff66), "
        "Loki (grafana/loki:latest, citra 1234567890ab), dan Promtail (grafana/promtail:latest, citra feedfacecafe)")


def test_monitoring_versions_are_explained_when_an_image_is_missing(tmp_path):
    """A stack that was not pulled, or metadata from before the field, leaves the token with its reason."""
    session = series(tmp_path)
    assert "monitoring_images" in compute_values([session])["uncomputed"]["monitoring.versions"]
    session.metadata["monitoring_images"] = {**MONITORING, "grafana/promtail:latest": None}
    assert "grafana/promtail:latest" in compute_values([session])["uncomputed"]["monitoring.versions"]


LAST_FAULT_FIELDS = ("last_fault_session", "last_fault_height", "last_fault_t_ms", "relation_to_last_fault")
SESSION_END_FIELDS = ("exposure_end_height", "exposure_end_t_ms")


def campaign_scan(evidence: list | None = None, covers: bool = False, last_fault_relation: dict | None = None) -> dict:
    """A campaign-scan.json with the fields the evidence tokens read; the last fault is at 4.000 in S6, whose session
    ended at 4.031."""
    relation = last_fault_relation or {"blocks_beyond_max_age": -99_231, "seconds_beyond_max_age": -171_200.0,
                                       "covers_max_age": False}
    return {"from_session": "S4", "sessions": ["S4", "S5", "S6"], "t_ms": 1,
            "evidence_scan": {"first_height": 1200, "last_height": 4800, "heights_scanned": 3601,
                              "evidence": evidence or [], "span_blocks": 3600, "span_seconds": 7200.0,
                              "max_age_num_blocks": 100000, "max_age_duration_s": 172800.0, "covers_max_age": covers},
            "relation": {"blocks_beyond_max_age": -96400, "seconds_beyond_max_age": -165600.0, "covers_max_age": covers},
            "last_fault_session": "S6", "last_fault_height": 4000, "last_fault_t_ms": 2,
            "exposure_end_height": 4031, "exposure_end_t_ms": 3, "relation_to_last_fault": relation}


def test_the_final_range_stays_plain_and_the_last_fault_coverage_has_its_own_tokens(tmp_path):
    """evidence.final_range is 'H sampai H_akhir' whatever campaign-scan.json holds; evidence.final_last_fault names
    the session end the coverage counts from, and evidence.final_covers says whether the tip passes both bounds."""
    from harness.values import LAST_FAULT_TOKENS
    assert LAST_FAULT_TOKENS == ("evidence.final_last_fault", "evidence.final_covers")
    assert set(LAST_FAULT_TOKENS) <= set(TOKEN_NAMES)
    data_dir = tmp_path / "notes"
    data_dir.mkdir()
    write_json(data_dir / "campaign-scan.json", campaign_scan())
    values = compute_values([series(tmp_path)], data_dir)["values"]
    assert values["evidence.final_range"] == "1.200 sampai 4.800"
    assert values["evidence.final_last_fault"] == "Sesi S6, akhir sesi pada ketinggian 4.031"
    assert values["evidence.final_covers"] == "tidak"
    covered = {"blocks_beyond_max_age": 100, "seconds_beyond_max_age": 3600.0, "covers_max_age": True}
    write_json(data_dir / "campaign-scan.json", campaign_scan(last_fault_relation=covered))
    values = compute_values([series(tmp_path / "again")], data_dir)["values"]
    assert (values["evidence.final_range"], values["evidence.final_covers"]) == ("1.200 sampai 4.800", "ya")


def test_the_last_fault_tokens_are_explained_for_an_older_campaign_scan(tmp_path):
    """A file written before H4 (no relation_to_last_fault) or before the session end was recorded: both tokens carry a
    reason, never a KeyError, and the plain range stays."""
    data_dir = tmp_path / "notes"
    data_dir.mkdir()
    for case, dropped in enumerate((LAST_FAULT_FIELDS + SESSION_END_FIELDS, SESSION_END_FIELDS)):
        older = {key: value for key, value in campaign_scan().items() if key not in dropped}
        write_json(data_dir / "campaign-scan.json", older)
        result = compute_values([series(tmp_path / f"case-{case}")], data_dir)
        assert result["values"]["evidence.final_range"] == "1.200 sampai 4.800"
        for name in ("evidence.final_last_fault", "evidence.final_covers"):
            assert "run scan-campaign again" in result["uncomputed"][name], (dropped, name)


def test_the_last_fault_tokens_are_explained_without_a_campaign_scan(tmp_path):
    """No data/campaign-scan.json: the two tokens name the missing file; the per-session range stays."""
    data_dir = tmp_path / "empty"
    data_dir.mkdir()
    result = compute_values([series(tmp_path)], data_dir)
    for name in ("evidence.final_last_fault", "evidence.final_covers"):
        assert "campaign-scan.json was not found" in result["uncomputed"][name], name
    assert result["values"]["evidence.final_range"] == "127 sampai 500"


def test_the_campaign_scan_replaces_the_per_session_evidence_tokens(tmp_path):
    """When data/campaign-scan.json exists, the evidence tokens describe the one scan over all sessions."""
    session = series(tmp_path)
    write_json(session.directory / "safety.json", safety(first=1200, last=1300))
    session = load_session_data(session.directory)
    data_dir = tmp_path / "notes"
    data_dir.mkdir()
    write_json(data_dir / "campaign-scan.json", campaign_scan([{"height": 2000, "count": 1, "types": ["duplicate_vote"]}]))
    values = compute_values([session], data_dir)["values"]
    assert values["evidence.count"] == "1"
    assert values["evidence.final_range"] == "1.200 sampai 4.800"
    assert values["evidence.heights"] == "S4 sampai S6: 2.000 (duplicate_vote)"
    assert values["evidence.scan_range"] == (
        "S4 sampai S6: 1.200 sampai 4.800 (3.600 blok dan 7.200,0 detik; batas max_age_num_blocks 100.000 blok "
        "dan max_age_duration 172.800,0 detik; tidak mencakup batas)")


def test_without_a_campaign_scan_the_session_scans_stay(tmp_path):
    """No file, no change: scan_range lists the sessions' own scans."""
    session = series(tmp_path)
    write_json(session.directory / "safety.json", safety(first=1200, last=1300))
    session = load_session_data(session.directory)
    data_dir = tmp_path / "empty"
    data_dir.mkdir()
    assert compute_values([session], data_dir)["values"]["evidence.scan_range"].startswith("SFIX: 1.200 sampai 1.300")


def test_a_campaign_scan_gives_values_even_without_a_safety_json(tmp_path):
    """The four evidence tokens leave the uncomputed list when the campaign scan supplies them."""
    data_dir = tmp_path / "notes"
    data_dir.mkdir()
    write_json(data_dir / "campaign-scan.json", campaign_scan())
    result = compute_values([series(tmp_path)], data_dir)
    assert "evidence.count" in result["values"] and "evidence.count" not in result["uncomputed"]
