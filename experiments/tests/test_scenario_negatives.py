"""security_negatives and lock_negative runners."""

import json
from pathlib import Path

import pytest

from fakes import SECRET, make_session, ready_env
from harness.adapters import HttpResponse
from harness.jsonio import read_json
from harness.scenarios.common import PreconditionError
from harness.scenarios.negatives import parse_probe_output, run_lock_negative, run_security_negatives
from harness.security import sign
from harness.session import read_events


def go_probe_argv(env):
    """The lockprobe command the runner issues."""
    return ("go", "-C", str(env.paths.syncguard_dir), "run", "./experiments/lockprobe",
            "-state", str(env.paths.primary_state))


def go_test_argv(env):
    """The lock unit-test command the runner issues."""
    return ("go", "-C", str(env.paths.syncguard_dir), "test", "./internal/state", "./internal/manager",
            "-run", "TestManager_Lock|TestClaimInitialRole", "-v")


PROBE_REFUSED = json.dumps({"lock_path": "x.lock", "owner_pid": 4242, "owner_alive": True,
                            "outcome": "refused", "error": "state is already locked"}) + "\n"


def security_answers(control_status: int = 500) -> dict:
    """Peer answers for the eight cases: five /validator_key requests (control last) and three /failover_notify."""
    unauthorized = HttpResponse(401, "Unauthorized\n")
    return {
        "http://localhost:8081/validator_key": [unauthorized] * 4 + [HttpResponse(control_status, "Failed to stage key\n")],
        "http://localhost:8081/failover_notify": [unauthorized, unauthorized, HttpResponse(405, "Method not allowed\n")],
    }


def test_security_negatives_send_eight_cases(tmp_path):
    """Seven refused requests plus the control, nothing changed, one event per request, no secret stored."""
    env = ready_env(tmp_path, extra_http=security_answers())
    env.paths.backup_key.with_name("priv_validator_key.json.real").write_text('{"address": "48DC"}')
    session = make_session(env.paths.data_dir, env.clock)
    rows = run_security_negatives(env, session)
    assert [row["status"] for row in rows] == ["LULUS"] * 8
    assert [row["actual"] for row in rows] == [401, 401, 401, 401, 401, 401, 405, 500]
    assert rows[-1]["case"] == "kontrol_hmac_benar"
    events = [row for row in read_events(session.directory / "events.jsonl") if row["kind"] == "security_request"]
    assert [event["number"] for event in events] == [1, 2, 3, 4, 5, 6, 7, 8]
    stored = (session.directory / "security_negatives.json").read_text()
    assert SECRET not in stored
    notify_requests = [request for request in env.http.requests if request["url"].endswith("/failover_notify")]
    now_s = env.clock.now_ms() // 1000
    assert notify_requests[1]["headers"]["X-Syncguard-Signature"] == sign(SECRET, now_s)


def test_security_negatives_mark_correct_hmac_cases_unproven_without_the_control(tmp_path):
    """A control that does not get 500 downgrades every passing correct-HMAC case before the result is written."""
    env = ready_env(tmp_path, extra_http=security_answers(control_status=401))
    session = make_session(env.paths.data_dir, env.clock)
    rows = run_security_negatives(env, session)
    by_number = {row["number"]: row["status"] for row in rows}
    assert [by_number[number] for number in (2, 4, 6)] == ["TIDAK TERBUKTI"] * 3
    assert [by_number[number] for number in (1, 3, 5, 7)] == ["LULUS"] * 4
    stored = read_json(session.directory / "security_negatives.json")
    assert [row["status"] for row in stored["rows"]] == [row["status"] for row in rows]
    logged = [row["status"] for row in read_events(session.directory / "events.jsonl") if row["kind"] == "security_request"]
    assert logged == [row["status"] for row in rows]


def test_security_negatives_refuse_without_a_healthy_peer(tmp_path):
    """No request is sent when the passive peer does not answer /health."""
    env = ready_env(tmp_path, extra_http={"http://localhost:8081/health": HttpResponse(503, "")})
    session = make_session(env.paths.data_dir, env.clock)
    with pytest.raises(PreconditionError):
        run_security_negatives(env, session)
    assert not any(request["url"].endswith("/validator_key") for request in env.http.requests)


def test_lock_negative_records_both_methods(tmp_path):
    """Probe refused, unit tests green, lock file unchanged: passed."""
    env = ready_env(tmp_path)
    env.commands.script[go_probe_argv(env)] = [(0, PROBE_REFUSED, "")]
    env.commands.script[go_test_argv(env)] = [(0, "--- PASS: TestManager_Lock (0.00s)\nok\n", "")]
    session = make_session(env.paths.data_dir, env.clock)
    result = run_lock_negative(env, session)
    assert result["methods"] == ["lockprobe", "unit_tests"]
    assert (result["lock_content_before"], result["lock_content_after"], result["active_pid"]) == ("4242", "4242", 4242)
    assert result["probe"]["outcome"] == "refused"
    assert result["passed"] is True
    kinds = [row["kind"] for row in read_events(session.directory / "events.jsonl")]
    assert kinds == ["preconditions_passed", "lock_probe", "lock_unit_tests"]


def test_lock_negative_fails_when_the_probe_acquires(tmp_path):
    """An acquired lock means the lock layer did not hold."""
    env = ready_env(tmp_path)
    acquired = PROBE_REFUSED.replace('"refused"', '"acquired_and_released"')
    env.commands.script[go_probe_argv(env)] = [(1, acquired, "exit status 1")]
    env.commands.script[go_test_argv(env)] = [(0, "ok\n", "")]
    session = make_session(env.paths.data_dir, env.clock)
    assert run_lock_negative(env, session)["passed"] is False


def test_lock_negative_refuses_a_foreign_lock_owner(tmp_path):
    """The probe never runs against a lock the active syncguard does not hold."""
    env = ready_env(tmp_path)
    Path(f"{env.paths.primary_state}.lock").write_text("999\n")
    session = make_session(env.paths.data_dir, env.clock)
    with pytest.raises(PreconditionError):
        run_lock_negative(env, session)
    assert not any(call[0] == "go" for call in env.commands.calls)


def test_parse_probe_output():
    """The last JSON line, or the raw text when it is not JSON."""
    assert parse_probe_output("building...\n" + PROBE_REFUSED)["outcome"] == "refused"
    assert parse_probe_output("go: cannot find main module") == {"outcome": "unparsed", "raw": "go: cannot find main module"}
