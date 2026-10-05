"""Wiring of adapters, RPC clients, and file locations for live runs and tests."""

from dataclasses import dataclass
from pathlib import Path

from harness.adapters import Commands, Http
from harness.clock import SystemClock, host_utc_offset_seconds
from harness.constants import (
    BACKUP_RPC_URL,
    DATA_DIR,
    LOCALNET_DIR,
    PRIMARY_RPC_URL,
    PROMETHEUS_URL,
    RPC1_URL,
    RUNBOOK,
    STORY_API_URL,
    STORY_CONFIG_DIR,
    SYNCGUARD_DIR,
)
from harness.rpc import CometRpc, Prometheus, StoryApi


@dataclass(frozen=True)
class Paths:
    """Every file location a runner reads or writes."""

    data_dir: Path
    syncguard_dir: Path
    localnet_dir: Path
    genesis_file: Path
    active_config: Path
    passive_config: Path
    active_log: Path
    passive_log: Path
    primary_key: Path
    primary_state: Path
    backup_key: Path
    backup_state: Path
    runbook: Path


def default_paths() -> Paths:
    """Locations on the evaluation host, as named in config.yaml and config-passive.yaml."""
    return Paths(
        data_dir=DATA_DIR,
        syncguard_dir=SYNCGUARD_DIR,
        localnet_dir=LOCALNET_DIR,
        genesis_file=STORY_CONFIG_DIR / "genesis-node.json",
        active_config=SYNCGUARD_DIR / "config.yaml",
        passive_config=SYNCGUARD_DIR / "config-passive.yaml",
        active_log=SYNCGUARD_DIR / "syncguard.log",
        passive_log=SYNCGUARD_DIR / "syncguard-passive.log",
        primary_key=STORY_CONFIG_DIR / "validator1" / "story" / "priv_validator_key.json",
        primary_state=STORY_CONFIG_DIR / "validator1" / "story" / "data" / "priv_validator_state.json",
        backup_key=STORY_CONFIG_DIR / "validator2" / "story" / "config" / "priv_validator_key.json",
        backup_state=STORY_CONFIG_DIR / "validator2" / "story" / "data" / "priv_validator_state.json",
        runbook=RUNBOOK,
    )


@dataclass(frozen=True)
class Environment:
    """Adapters, RPC clients, file locations, and the host UTC offset a runner works with."""

    http: object
    commands: object
    clock: object
    rpc: CometRpc
    primary_rpc: CometRpc
    backup_rpc: CometRpc
    story: StoryApi
    prometheus: Prometheus
    paths: Paths
    utc_offset_seconds: int


def build_environment(http, commands, clock, paths: Paths, utc_offset_seconds: int) -> Environment:
    """Wire the fixed localnet endpoints onto the given adapters."""
    return Environment(
        http=http,
        commands=commands,
        clock=clock,
        rpc=CometRpc(http, RPC1_URL),
        primary_rpc=CometRpc(http, PRIMARY_RPC_URL),
        backup_rpc=CometRpc(http, BACKUP_RPC_URL),
        story=StoryApi(http, STORY_API_URL),
        prometheus=Prometheus(http, PROMETHEUS_URL),
        paths=paths,
        utc_offset_seconds=utc_offset_seconds,
    )


def live_environment() -> Environment:
    """The real host: urllib, subprocess, the system clock, default paths, the host UTC offset."""
    return build_environment(Http(), Commands(), SystemClock(), default_paths(), host_utc_offset_seconds())
