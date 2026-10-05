"""security_negatives (III-14) and lock_negative (IV-11 layer 3) runners."""

import json
from pathlib import Path

from harness.configs import extract_secret
from harness.constants import PASSIVE_PEER_URL, PRIMARY_NODE
from harness.jsonio import write_json
from harness.preconditions import lock_preconditions, running_syncguard, security_preconditions
from harness.scenarios.common import require
from harness.security import NegativeTarget, build_cases, resolve_control, run_case

LOCKPROBE_PACKAGE = "./experiments/lockprobe"
LOCK_TEST_PACKAGES = ("./internal/state", "./internal/manager")
LOCK_TEST_PATTERN = "TestManager_Lock|TestClaimInitialRole"


def run_security_negatives(env, session) -> list:
    """Send the eight requests (seven negatives and the correct-HMAC control) to the passive syncguard and store the rows.

    Each request is signed at send time by run_case. resolve_control runs before anything is
    written, so a correct-HMAC case counts as LULUS only when the control got 500 with files unchanged.
    """
    require(session, "security_negatives", security_preconditions(env))
    secret = extract_secret(env.paths.passive_config.read_text(encoding="utf-8"))
    target = NegativeTarget(PASSIVE_PEER_URL, env.paths.backup_key, env.paths.passive_log, env.utc_offset_seconds)
    rows = []
    sent_times = []
    for case in build_cases():
        sent_times.append(env.clock.now_ms())
        rows.append(run_case(env.http, target, case, secret, env.clock))
    rows = resolve_control(rows)
    for sent_ms, row in zip(sent_times, rows):
        session.events.append("security_request", t_ms=sent_ms, number=row["number"], case=row["case"],
                              expected=row["expected"], actual=row["actual"], status=row["status"],
                              correct_hmac=row["correct_hmac"], refusal_logged=row["refusal_logged"])
    write_json(session.directory / "security_negatives.json", {"session": session.id, "rows": rows})
    return rows


def parse_probe_output(text: str) -> dict:
    """The JSON object the probe printed last, or the raw text when there is none."""
    lines = [line for line in text.splitlines() if line.strip()]
    if lines:
        try:
            return json.loads(lines[-1])
        except json.JSONDecodeError:
            pass
    return {"outcome": "unparsed", "raw": text.strip()}


def run_lock_negative(env, session) -> dict:
    """A second lock acquisition against the live primary state while the active syncguard holds it, plus the lock unit tests."""
    require(session, "lock_negative", lock_preconditions(env))
    lock_path = Path(f"{env.paths.primary_state}.lock")
    content_before = lock_path.read_text(encoding="utf-8").strip()
    active_pid = running_syncguard(env).get(PRIMARY_NODE)
    go = ["go", "-C", str(env.paths.syncguard_dir)]
    probe_argv = go + ["run", LOCKPROBE_PACKAGE, "-state", str(env.paths.primary_state)]
    probe = env.commands.run(probe_argv, timeout=300)
    session.events.append("lock_probe", command=" ".join(probe_argv), exit_code=probe.exit_code)
    unit_argv = go + ["test", *LOCK_TEST_PACKAGES, "-run", LOCK_TEST_PATTERN, "-v"]
    unit = env.commands.run(unit_argv, timeout=600)
    session.events.append("lock_unit_tests", command=" ".join(unit_argv), exit_code=unit.exit_code)
    content_after = lock_path.read_text(encoding="utf-8").strip() if lock_path.exists() else ""
    parsed = parse_probe_output(probe.stdout)
    passed = parsed.get("outcome") == "refused" and unit.exit_code == 0 and content_before == content_after
    result = {
        "session": session.id,
        "methods": ["lockprobe", "unit_tests"],
        "lock_path": str(lock_path),
        "lock_content_before": content_before,
        "lock_content_after": content_after,
        "active_pid": active_pid,
        "probe": parsed,
        "probe_exit_code": probe.exit_code,
        "unit_test_exit_code": unit.exit_code,
        "unit_test_output": unit.stdout,
        "passed": passed,
    }
    write_json(session.directory / "lock_negative.json", result)
    return result
