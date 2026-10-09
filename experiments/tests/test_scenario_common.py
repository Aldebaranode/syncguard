"""Building blocks shared by the runners."""

import json

import pytest

from fakes import (
    TARGET_ADDRESS,
    FakeClock,
    FakeCommands,
    FakeHttp,
    make_session,
    paths_under,
    ready_env,
    signed_commit_answer,
)
from harness.adapters import Unreachable
from harness.environment import build_environment
from harness.logparse import LogEvent
from harness.scenarios.common import (
    PreconditionError,
    abort_recorded,
    collect_heights_in_chunks,
    container_ports,
    container_status,
    docker_action,
    log_observation,
    missing_host_ports,
    next_iteration,
    record_window,
    require,
)
from harness.session import read_events
from harness.signing import HeightRecord, load_heights

NOW = 1791169200000


def test_require_logs_and_raises(tmp_path):
    """Failures are logged with the scenario and raised; a pass is logged too."""
    session = make_session(tmp_path, FakeClock(NOW))
    require(session, "failover", [], 1)
    with pytest.raises(PreconditionError) as raised:
        require(session, "failover", ["chain not advancing"], 2)
    assert raised.value.failures == ["chain not advancing"]
    kinds = [(event["kind"], event["iteration"]) for event in read_events(session.directory / "events.jsonl")]
    assert kinds == [("preconditions_passed", 1), ("precondition_failed", 2)]


def test_next_iteration_counts_started_iterations(tmp_path):
    """Numbering continues across runs in one session."""
    session = make_session(tmp_path, FakeClock(NOW))
    assert next_iteration(session) == 1
    session.events.append("iteration_start", iteration=1)
    session.events.append("iteration_start", iteration=4)
    assert next_iteration(session) == 5


def test_docker_action_logs_the_exact_command(tmp_path):
    """The event carries the command, exit code, and the issue instant as t_ms."""
    clock = FakeClock(NOW)
    commands = FakeCommands({("docker", "kill", "validator1-node"): [(0, "validator1-node\n", "")]}, clock)
    env = build_environment(FakeHttp(), commands, clock, paths_under(tmp_path), 25200)
    session = make_session(env.paths.data_dir, clock)
    docker_action(env, session, "kill", "validator1-node", "failover", 3, "fault_injected", height=1192)
    event = read_events(session.directory / "events.jsonl")[-1]
    assert (event["kind"], event["t_ms"], event["command"], event["exit_code"], event["height"]) == (
        "fault_injected", NOW, "docker kill validator1-node", 0, 1192)


def test_log_observation_variants(tmp_path):
    """Timeouts, log events, and signed heights each leave one event."""
    session = make_session(tmp_path, FakeClock(NOW))
    log_observation(session, 1, "takeover_complete", None)
    log_observation(session, 1, "takeover_complete", LogEvent("takeover_complete", "validator-2", NOW, "info", "m", "x.log:9"))
    log_observation(session, 1, "backup_first_signature", HeightRecord(144, NOW, "P", 0, {}, {}, 0))
    rows = read_events(session.directory / "events.jsonl")
    assert [row["kind"] for row in rows] == ["wait_timeout", "observed", "observed"]
    assert rows[1]["source"] == "x.log:9"
    assert rows[2]["height"] == 144


def test_record_window_tags_events_with_the_iteration(tmp_path):
    """Six primary and five backup events fall in the failback window; they are appended in time order."""
    env = ready_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    events = record_window(env, session, 7, 1791168959623, 1791168961698)
    rows = [row for row in read_events(session.directory / "events.jsonl") if row["kind"] == "syncguard"]
    assert len(rows) == len(events) == 11
    assert {row["iteration"] for row in rows} == {7}
    assert rows[0]["marker"] == "failback_considered"
    assert [row["t_ms"] for row in rows] == sorted(row["t_ms"] for row in rows)


def test_container_status(tmp_path):
    """docker inspect output, or unknown on failure."""
    clock = FakeClock(NOW)
    commands = FakeCommands({
        ("docker", "inspect", "-f", "{{.State.Status}}", "validator1-node"): [(0, "paused\n", "")],
        ("docker", "inspect", "-f", "{{.State.Status}}", "missing"): [(1, "", "No such object")],
    }, clock)
    env = build_environment(FakeHttp(), commands, clock, paths_under(tmp_path), 25200)
    assert container_status(env, "validator1-node") == "paused"
    assert container_status(env, "missing") == "unknown"


class FlakyCommits:
    """A /commit answer that fails with Unreachable for a set of heights while enabled and counts every request."""

    def __init__(self, failing: set):
        """Heights that fail while enabled; the answer signs every other height."""
        self.failing = failing
        self.enabled = True
        self.requested = []

    def __call__(self, url: str):
        """A signed commit for the height in url, or Unreachable for a failing one."""
        height = int(url.rsplit("=", 1)[1])
        self.requested.append(height)
        if self.enabled and height in self.failing:
            return Unreachable(f"commit {height} failed")
        return signed_commit_answer([TARGET_ADDRESS])(url)


def test_chunked_collection_keeps_finished_chunks_and_resumes(tmp_path):
    """A chunk that keeps failing raises after retries; earlier chunks stay stored and a rerun skips them."""
    flaky = FlakyCommits({1107})
    env = ready_env(tmp_path, extra_http={"http://localhost:26657/commit?height=*": flaky,
                                          "http://localhost:26657/status": {"result": {"sync_info": {"latest_block_height": "1200"}}}})
    session = make_session(env.paths.data_dir, env.clock)
    with pytest.raises(Unreachable):
        collect_heights_in_chunks(env, session, 1100, 1114, chunk_size=5, attempts=2, retry_wait_s=3.0)
    stored = {record.height for record in load_heights(session.directory / "heights.jsonl")}
    assert stored == {1100, 1101, 1102, 1103, 1104}
    retries = [row for row in read_events(session.directory / "events.jsonl") if row["kind"] == "heights_retry"]
    assert [row["attempt"] for row in retries] == [1, 2]
    assert 3.0 in env.clock.slept
    flaky.enabled = False
    flaky.requested.clear()
    records = collect_heights_in_chunks(env, session, 1100, 1114, chunk_size=5, attempts=2)
    assert [record.height for record in records] == list(range(1100, 1115))
    assert min(flaky.requested) == 1105


def test_chunked_collection_into_another_file_leaves_heights_jsonl_alone(tmp_path):
    """file_name names the session file the heights go to, and the collection event names it too."""
    env = ready_env(tmp_path, extra_http={
        "http://localhost:26657/status": {"result": {"sync_info": {"latest_block_height": "1200"}}}})
    session = make_session(env.paths.data_dir, env.clock)
    records = collect_heights_in_chunks(env, session, 1100, 1106, chunk_size=5, file_name="tenderduty-heights.jsonl")
    assert [record.height for record in records] == list(range(1100, 1107))
    stored = [record.height for record in load_heights(session.directory / "tenderduty-heights.jsonl")]
    assert stored == list(range(1100, 1107))
    assert not (session.directory / "heights.jsonl").exists()
    collected = [row for row in read_events(session.directory / "events.jsonl") if row["kind"] == "heights_collected"]
    assert [(row["first"], row["last"], row["file"]) for row in collected] == [(1100, 1106, "tenderduty-heights.jsonl")]


def test_abort_recorded_logs_the_error_runs_cleanup_and_reraises(tmp_path):
    """Exceptions and Ctrl-C get distinct events; a failing cleanup is logged and never hides the error."""
    session = make_session(tmp_path, FakeClock(NOW))
    ran = []
    with pytest.raises(ValueError):
        with abort_recorded(session, "failover", 3, lambda: ran.append(True)):
            raise ValueError("boom")
    with pytest.raises(KeyboardInterrupt):
        with abort_recorded(session, "failover", 3, lambda: 1 / 0):
            raise KeyboardInterrupt()
    rows = read_events(session.directory / "events.jsonl")
    assert [row["kind"] for row in rows] == ["iteration_aborted", "iteration_interrupted", "cleanup_failed"]
    assert "boom" in rows[0]["error"] and ran == [True]
    assert "ZeroDivisionError" in rows[2]["error"]


def test_abort_recorded_cleanup_survives_a_second_interrupt(tmp_path):
    """A Ctrl-C inside the cleanup is logged as cleanup_failed and the original error still propagates."""
    session = make_session(tmp_path, FakeClock(NOW))

    def interrupted_cleanup():
        """A cleanup that is itself interrupted."""
        raise KeyboardInterrupt()

    with pytest.raises(ValueError, match="original"):
        with abort_recorded(session, "failover", 1, interrupted_cleanup):
            raise ValueError("original")
    kinds = [row["kind"] for row in read_events(session.directory / "events.jsonl")]
    assert kinds == ["iteration_aborted", "cleanup_failed"]


def test_a_refused_iteration_does_not_use_up_its_number(tmp_path):
    """iteration_start is logged only after the preconditions pass, so the next run reuses the refused number."""
    from fakes import TARGET_ADDRESS, ready_env, write_key
    from harness.scenarios.failover import FailoverSettings, run_failover_iteration
    env = ready_env(tmp_path)
    write_key(env.paths.backup_key, TARGET_ADDRESS)
    session = make_session(env.paths.data_dir, env.clock)
    with pytest.raises(PreconditionError):
        run_failover_iteration(env, session, next_iteration(session), FailoverSettings(), None)
    assert next_iteration(session) == 1


def test_two_attempts_retries_once_and_reports_a_step_that_never_completes(tmp_path):
    """The shared cleanup retry: a second attempt after a failure, cleanup_failed with the step after two."""
    from harness.adapters import CommandResult
    from harness.scenarios.common import two_attempts
    session = make_session(tmp_path / "data", FakeClock(1000))
    results = {1: CommandResult([], 1, "", "no", 0, 0), 2: CommandResult([], 0, "", "", 0, 0)}
    assert two_attempts(session, "hang", 3, "kill", lambda attempt: results[attempt]).exit_code == 0
    assert two_attempts(session, "partition", 3, "reconnect", lambda attempt: results[1]) is None
    failed = [row for row in read_events(session.directory / "events.jsonl") if row["kind"] == "cleanup_failed"]
    assert [(row["scenario"], row["step"]) for row in failed] == [("partition", "reconnect")]
    assert "did not complete after two attempts" in failed[0]["error"]


PORTS_ARGV = ("docker", "inspect", "validator1-node", "--format",
              '{"bindings":{{json .HostConfig.PortBindings}},"published":{{json .NetworkSettings.Ports}}}')
RPC_BINDING = {"26657/tcp": [{"HostIp": "", "HostPort": "21657"}]}
RPC_PUBLISHED = {"26657/tcp": [{"HostIp": "0.0.0.0", "HostPort": "21657"}, {"HostIp": "::", "HostPort": "21657"}]}


def ports_env(tmp_path, answer):
    """An environment whose one scripted command is the host-port inspect of validator1-node."""
    commands = FakeCommands({PORTS_ARGV: [answer]})
    return build_environment(FakeHttp(), commands, FakeClock(NOW), paths_under(tmp_path), 25200)


def test_no_host_port_is_missing_when_docker_published_every_binding():
    """validator1-node after a normal start: 26657/tcp bound to 21657 and published on both addresses."""
    assert missing_host_ports({"bindings": RPC_BINDING, "published": RPC_PUBLISHED}) == []


def test_a_binding_without_its_published_port_is_missing():
    """S8 after the reconnect: the binding stays in HostConfig while NetworkSettings.Ports is empty or null."""
    assert missing_host_ports({"bindings": RPC_BINDING, "published": {}}) == ["26657/tcp->21657"]
    assert missing_host_ports({"bindings": RPC_BINDING, "published": None}) == ["26657/tcp->21657"]
    assert missing_host_ports({"bindings": RPC_BINDING, "published": {"26657/tcp": None}}) == ["26657/tcp->21657"]


def test_null_bindings_need_no_published_port():
    """A container with no host binding cannot miss one, whatever docker published."""
    assert missing_host_ports({"bindings": None, "published": None}) == []
    assert missing_host_ports({"bindings": None, "published": RPC_PUBLISHED}) == []
    assert missing_host_ports({"bindings": {"26657/tcp": None}, "published": {}}) == []


def test_a_published_list_without_the_bound_host_port_is_missing():
    """The port is published, but on another host port than the binding asks for."""
    other = {"26657/tcp": [{"HostIp": "0.0.0.0", "HostPort": "31657"}]}
    assert missing_host_ports({"bindings": RPC_BINDING, "published": other}) == ["26657/tcp->21657"]


def test_missing_ports_are_listed_sorted_over_several_bindings():
    """Every binding with a host port is checked; one without a host port (docker picks it) is not."""
    bindings = {"26657/tcp": [{"HostIp": "", "HostPort": "21657"}], "26656/tcp": [{"HostIp": "", "HostPort": "21656"}],
                "1317/tcp": [{"HostIp": "", "HostPort": "21317"}], "8545/tcp": [{"HostIp": "", "HostPort": ""}]}
    published = {"1317/tcp": [{"HostIp": "0.0.0.0", "HostPort": "21317"}],
                 "8545/tcp": [{"HostIp": "0.0.0.0", "HostPort": "49153"}]}
    assert missing_host_ports({"bindings": bindings, "published": published}) == ["26656/tcp->21656", "26657/tcp->21657"]


def test_container_ports_reads_bindings_and_published_ports_in_one_inspect(tmp_path):
    """One docker inspect gives both maps; docker's null for an empty map is kept for missing_host_ports."""
    env = ports_env(tmp_path, (0, json.dumps({"bindings": RPC_BINDING, "published": RPC_PUBLISHED}) + "\n", ""))
    assert container_ports(env, "validator1-node") == {"bindings": RPC_BINDING, "published": RPC_PUBLISHED}
    assert env.commands.calls == [list(PORTS_ARGV)]
    empty = ports_env(tmp_path, (0, '{"bindings":null,"published":null}\n', ""))
    assert container_ports(empty, "validator1-node") == {"bindings": None, "published": None}


@pytest.mark.parametrize("answer", [
    (1, "", "Error: No such object: validator1-node"),
    (0, "not json", ""),
    (0, "[1, 2]", ""),
    (0, '{"bindings": {}}', ""),
    (0, '{"bindings": [], "published": {}}', ""),
    (0, '{"bindings": {"26657/tcp": "21657"}, "published": {}}', ""),
    (0, '{"bindings": {"26657/tcp": ["21657"]}, "published": {}}', ""),
])
def test_container_ports_is_none_when_docker_cannot_say(tmp_path, answer):
    """A failed inspect, bad JSON, or a different shape is unreadable, never an empty port list."""
    assert container_ports(ports_env(tmp_path, answer), "validator1-node") is None
