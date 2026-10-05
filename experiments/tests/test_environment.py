"""Wiring of adapters, RPC clients, and paths."""

from fakes import FakeClock, FakeCommands, FakeHttp, paths_under
from harness.environment import build_environment, default_paths


def test_build_environment_wires_the_localnet_endpoints(tmp_path):
    """Each RPC client points at its fixed port and shares the given adapters."""
    http = FakeHttp()
    env = build_environment(http, FakeCommands(), FakeClock(0), paths_under(tmp_path), 25200)
    assert env.rpc.base_url == "http://localhost:26657"
    assert env.primary_rpc.base_url == "http://localhost:21657"
    assert env.backup_rpc.base_url == "http://localhost:22657"
    assert env.story.base_url == "http://localhost:1317"
    assert env.prometheus.base_url == "http://localhost:9090"
    assert env.rpc.http is http
    assert env.utc_offset_seconds == 25200


def test_default_paths_match_the_syncguard_configs():
    """The key and state files named in config.yaml and config-passive.yaml."""
    paths = default_paths()
    assert str(paths.primary_key).endswith("config/story/validator1/story/priv_validator_key.json")
    assert str(paths.primary_state).endswith("config/story/validator1/story/data/priv_validator_state.json")
    assert str(paths.backup_key).endswith("config/story/validator2/story/config/priv_validator_key.json")
    assert str(paths.backup_state).endswith("config/story/validator2/story/data/priv_validator_state.json")
    assert paths.active_log.name == "syncguard.log"
    assert paths.passive_log.name == "syncguard-passive.log"
