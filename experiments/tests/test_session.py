"""Session ids, the event log, and session metadata."""

import hashlib
import json
from pathlib import Path

import pytest

from fakes import BINARY_REVISION, FakeClock, FakeCommands, go_version_output, metadata_http, metadata_script, paths_under, write_configs
from harness.environment import build_environment
from harness.session import (
    config_id,
    EventLog,
    host_description,
    next_session_id,
    open_session,
    read_events,
    start_session,
)

NOW = 1791169200000  # 2026-10-05T03:00:00.000Z, 10:00 WIB
SECRET = "test-only-shared-secret-value"


def make_env(tmp_path):
    """Fake environment with configs, genesis, and metadata answers in place."""
    paths = paths_under(tmp_path)
    write_configs(paths, SECRET)
    clock = FakeClock(NOW)
    commands = FakeCommands(metadata_script(paths.syncguard_dir, paths.localnet_dir), clock)
    return build_environment(metadata_http(), commands, clock, paths, 25200)


def test_first_new_session_is_s4(tmp_path):
    """S1-S3 are archived, so an empty data directory starts at S4."""
    assert next_session_id(tmp_path / "missing") == "S4"
    assert next_session_id(tmp_path) == "S4"


def test_next_session_id_skips_files_and_other_directories(tmp_path):
    """Only directories named S<n> count."""
    (tmp_path / "S4").mkdir()
    (tmp_path / "S7").mkdir()
    (tmp_path / "archive-sessions-1-3").mkdir()
    (tmp_path / "S9").write_text("not a directory")
    assert next_session_id(tmp_path) == "S8"


def test_event_log_appends_stamped_rows(tmp_path):
    """Each row carries session, host ms time, its UTC form, kind, and fields."""
    log = EventLog(tmp_path / "events.jsonl", "S4", FakeClock(NOW))
    log.append("fault_injected", action="kill")
    log.append("syncguard", t_ms=NOW - 5, marker="max_failures")
    rows = read_events(tmp_path / "events.jsonl")
    assert rows[0] == {"session": "S4", "t_ms": NOW, "t_utc": "2026-10-05T03:00:00.000Z",
                       "kind": "fault_injected", "action": "kill"}
    assert rows[1]["t_ms"] == NOW - 5
    assert rows[1]["marker"] == "max_failures"


def test_host_description():
    """CPU, RAM, macOS, and Docker in one line."""
    any_dir = Path("/unused")
    commands = FakeCommands(metadata_script(any_dir, any_dir))
    assert host_description(commands) == "Apple M4, 24 GB RAM, macOS 15.6.1, Docker 28.3.2 arm64"


def test_start_session_writes_metadata_and_redacted_configs(tmp_path):
    """metadata.json, two redacted configs, and a session_start event; the secret is stored nowhere."""
    env = make_env(tmp_path)
    session_id = start_session(env, "uji failover n=10")
    directory = env.paths.data_dir / session_id
    metadata = json.loads((directory / "metadata.json").read_text())
    assert session_id == "S4"
    assert metadata["session"] == "S4"
    assert metadata["purpose"] == "uji failover n=10"
    assert (metadata["start_ms"], metadata["start_utc"], metadata["start_local"]) == (
        NOW, "2026-10-05T03:00:00.000Z", "2026-10-05 10:00:00.000")
    assert metadata["harness_describe"] == "thesis-eval-1-6-g03d1187-dirty"
    assert metadata["syncguard_version"] == "thesis-eval-2"
    assert "syncguard_describe" not in metadata
    assert metadata["localnet_branch"] == "thesis-four-validators"
    assert metadata["images"] == {"story-node:localnet": "sha256:01ad91f72b07", "story-geth:localnet": "sha256:8adf069c592f"}
    assert metadata["genesis_sha256"] == hashlib.sha256(b"{}").hexdigest()
    assert metadata["monitoring_images"] == {
        "prom/prometheus:latest": "sha256:aa11bb22cc33", "grafana/grafana:latest": "sha256:dd44ee55ff66",
        "grafana/loki:latest": "sha256:1234567890ab", "grafana/promtail:latest": None}
    assert len(metadata["validators"]) == 4
    assert metadata["slashing_params"]["signed_blocks_window"] == 200
    assert metadata["evidence_params"]["max_age_num_blocks"] == 100000
    assert len(metadata["clock_offset"]["samples_ms"]) == 5
    assert "adjtimex" in metadata["clock_offset"]["method"]
    assert metadata["host"] == "Apple M4, 24 GB RAM, macOS 15.6.1, Docker 28.3.2 arm64"
    assert len(metadata["config_id"]) == 12
    fingerprints = metadata["secret_fingerprints"]
    assert fingerprints["config"] == fingerprints["config-passive"] != ""
    stored = "".join(path.read_text() for path in directory.iterdir())
    assert SECRET not in stored
    assert (directory / "config.redacted.txt").exists()
    assert read_events(directory / "events.jsonl")[0]["kind"] == "session_start"
    assert start_session(env, "kedua") == "S5"


def test_open_session_refuses_an_unknown_id(tmp_path):
    """Runners never write into a session that was not started."""
    with pytest.raises(FileNotFoundError, match="python -m harness start"):
        open_session(tmp_path, "S4", FakeClock(NOW))


def test_open_session_exposes_the_offset(tmp_path):
    """offset_ms comes from metadata clock_offset."""
    env = make_env(tmp_path)
    session_id = start_session(env, "x")
    session = open_session(env.paths.data_dir, session_id, env.clock)
    assert session.offset_ms == session.metadata["clock_offset"]["offset_ms"]
    session.events.append("note", text="ok")
    assert read_events(session.directory / "events.jsonl")[-1]["kind"] == "note"


def test_redacted_copy_starts_with_the_session_comment(tmp_path):
    """Each stored config copy names its session on the first line."""
    env = make_env(tmp_path)
    session_id = start_session(env, "x")
    first_line = (env.paths.data_dir / session_id / "config.redacted.txt").read_text().splitlines()[0]
    assert first_line == f"# session: {session_id}"


def test_metadata_records_the_binary_hash(tmp_path):
    """The binary actually run is identified by its sha256."""
    env = make_env(tmp_path)
    session_id = start_session(env, "x")
    metadata = json.loads((env.paths.data_dir / session_id / "metadata.json").read_text())
    assert metadata["syncguard_binary_sha256"] == hashlib.sha256(b"fake-binary").hexdigest()


BUILD = {
    "binary_sha256": "aa", "vcs_revision": "rev", "localnet_commit": "bb", "images": {"story-node:localnet": "sha256:01"},
    "genesis_sha256": "cc",
}


def test_config_id_changes_with_any_build_input():
    """Binary hash, binary revision, localnet commit, images, genesis, and configs each change the id."""
    base = config_id({"config": "x"}, BUILD)
    assert config_id({"config": "x"}, BUILD) == base
    assert config_id({"config": "y"}, BUILD) != base
    for key, other in (("binary_sha256", "zz"), ("vcs_revision", "zz"), ("localnet_commit", "zz"),
                       ("images", {"story-node:localnet": "sha256:02"}), ("genesis_sha256", "zz")):
        assert config_id({"config": "x"}, {**BUILD, key: other}) != base, key


def test_metadata_records_the_binary_build_identity(tmp_path):
    """The Go build info of bin/syncguard: revision, time, modified flag, Go version; the version derives from the revision."""
    env = make_env(tmp_path)
    session_id = start_session(env, "x")
    metadata = json.loads((env.paths.data_dir / session_id / "metadata.json").read_text())
    assert metadata["syncguard_build"] == {
        "go_version": "go1.25.6", "vcs_revision": BINARY_REVISION, "vcs_time": "2026-10-05T04:15:24Z",
        "vcs_modified": False}
    assert metadata["syncguard_commit"] == BINARY_REVISION
    assert metadata["syncguard_version"] == "thesis-eval-2"


def test_the_config_id_ignores_the_harness_checkout(tmp_path):
    """A different harness describe (the harness checkout) leaves the config id of the same binary unchanged."""
    first = make_env(tmp_path / "a")
    second = make_env(tmp_path / "b")
    paths = second.paths
    second.commands.script[("git", "-C", str(paths.syncguard_dir), "describe", "--always", "--dirty")] = [
        (0, "other-describe\n", "")]
    ids = []
    for env in (first, second):
        session_id = start_session(env, "x")
        ids.append(json.loads((env.paths.data_dir / session_id / "metadata.json").read_text()))
    assert ids[0]["config_id"] == ids[1]["config_id"]
    assert ids[1]["harness_describe"] == "other-describe"


def test_start_refuses_a_binary_built_from_a_modified_tree(tmp_path):
    """vcs.modified=true means the binary does not match any commit; nothing is written."""
    env = make_env(tmp_path)
    binary = env.paths.syncguard_dir / "bin" / "syncguard"
    env.commands.script[("go", "version", "-m", str(binary))] = [(0, go_version_output("true"), "")]
    with pytest.raises(ValueError, match="vcs.modified=true"):
        start_session(env, "x")
    assert not (env.paths.data_dir / "S4").exists()


def test_start_refuses_a_binary_without_build_info(tmp_path):
    """A binary built without VCS stamping cannot be tied to a commit."""
    env = make_env(tmp_path)
    binary = env.paths.syncguard_dir / "bin" / "syncguard"
    env.commands.script[("go", "version", "-m", str(binary))] = [(0, "bin/syncguard: go1.25.6\n\tpath\tx\n", "")]
    with pytest.raises(ValueError, match="vcs.revision"):
        start_session(env, "x")


def test_refused_config_stops_the_start_before_anything_is_written(tmp_path):
    """A block-scalar secret raises and leaves no session directory behind."""
    env = make_env(tmp_path)
    env.paths.active_config.write_text("secret: |\n  fake-line\n", encoding="utf-8")
    with pytest.raises(ValueError, match="block scalar"):
        start_session(env, "x")
    assert not (env.paths.data_dir / "S4").exists()


def test_config_without_a_secret_stops_the_start(tmp_path):
    """syncguard configs always carry a secret, so a missing one means the wrong file."""
    env = make_env(tmp_path)
    env.paths.passive_config.write_text('node:\n  id: "validator-2"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="no secret"):
        start_session(env, "x")
    assert not (env.paths.data_dir / "S4").exists()


def test_monitoring_images_stay_outside_the_config_id(tmp_path):
    """The monitoring stack uses :latest tags; its image ids are recorded but never split the configurations."""
    first = make_env(tmp_path / "a")
    start_session(first, "satu")
    metadata = json.loads((first.paths.data_dir / "S4" / "metadata.json").read_text())
    from harness.session import collect_metadata
    second = make_env(tmp_path / "b")
    second.commands.script[("docker", "image", "inspect", "--format", "{{.Id}}", "grafana/grafana:latest")] = [
        (0, "sha256:ffffffffffff\n", "")]
    again, _ = collect_metadata(second, "S4", "dua")
    assert again["monitoring_images"]["grafana/grafana:latest"] == "sha256:ffffffffffff"
    assert again["config_id"] == metadata["config_id"]
