"""Session ids, the event log, and session metadata."""

import hashlib
import json
from pathlib import Path

import pytest

from fakes import FakeClock, FakeCommands, metadata_http, metadata_script, paths_under, write_configs
from harness.environment import build_environment
from harness.session import (
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
    assert metadata["syncguard_describe"] == "thesis-eval-1-6-g03d1187-dirty"
    assert metadata["localnet_branch"] == "thesis-four-validators"
    assert metadata["images"] == {"story-node:localnet": "sha256:01ad91f72b07", "story-geth:localnet": "sha256:8adf069c592f"}
    assert metadata["genesis_sha256"] == hashlib.sha256(b"{}").hexdigest()
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
