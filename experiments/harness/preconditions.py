"""Preconditions a live runner checks before it touches the localnet.

Each check returns a failure message or None. Runners refuse to start on any
failure and never repair the environment themselves.
"""

from pathlib import Path

from harness.adapters import Unreachable
from harness.constants import (
    BACKUP_NODE,
    PASSIVE_PEER_URL,
    PRIMARY_NODE,
    ROLE_CHANGE_SPACING_S,
    TARGET_VALIDATOR,
    TIP_MARGIN,
)
from harness.logparse import parse_log_file
from harness.signing import parse_commit
from harness.snapshots import key_file_fact

ROLE_CHANGE_MARKERS = ("takeover_complete", "failback_complete")
CHAIN_ADVANCE_WAIT_S = 6.0


def collect_failures(checks: list) -> list:
    """Run (name, check) pairs; an unreachable endpoint becomes a failure message, never a pass."""
    failures = []
    for name, check in checks:
        try:
            failure = check()
        except Unreachable as error:
            failure = f"{name}: {error}"
        if failure:
            failures.append(failure)
    return failures


def check_key_layout(paths, validator: str) -> str | None:
    """The primary key file must hold the validator key and the backup key file must not."""
    primary = key_file_fact(paths.primary_key).get("address")
    backup = key_file_fact(paths.backup_key).get("address")
    if primary != validator:
        return f"primary key file holds {primary}, expected {validator}"
    if backup == validator:
        return "backup key file holds the validator key; two signers would be possible"
    return None


def check_loaded_addresses(env, validator: str) -> str | None:
    """validator1-node must run the validator key and validator2-node must not."""
    primary = env.primary_rpc.loaded_address()
    backup = env.backup_rpc.loaded_address()
    if primary != validator:
        return f"validator1-node loaded {primary}, expected {validator}"
    if backup == validator:
        return "validator2-node loaded the validator key"
    return None


def startup_state(events: list) -> str:
    """State of the latest syncguard process in one log: absent, starting, started, or stopped."""
    starts = [index for index, event in enumerate(events) if event.marker == "manager_starting"]
    if not starts:
        return "absent"
    later = events[starts[-1] + 1:]
    if any(event.marker == "shutdown_signal" for event in later):
        return "stopped"
    if any(event.marker == "manager_started" for event in later):
        return "started"
    return "starting"


def _logs(env) -> tuple:
    """(path, node) of the active and the passive syncguard log."""
    return ((env.paths.active_log, PRIMARY_NODE), (env.paths.passive_log, BACKUP_NODE))


def both_logs(env) -> list:
    """Parsed events of both syncguard logs; a missing log contributes nothing."""
    events = []
    for path, node in _logs(env):
        if path.exists():
            events += parse_log_file(path, node, env.utc_offset_seconds)
    return events


def check_syncguard_started(env) -> str | None:
    """Both logs must show their latest process past 'SyncGuard failover manager started' and not stopped."""
    for path, node in _logs(env):
        if not path.exists():
            return f"{path.name} not found"
        state = startup_state(parse_log_file(path, node, env.utc_offset_seconds))
        if state != "started":
            return f"{path.name}: latest syncguard process is {state}"
    return None


def _config_argument(words: list) -> str:
    """The value after --config in a command line, or ''."""
    if "--config" not in words:
        return ""
    position = words.index("--config") + 1
    return words[position] if position < len(words) else ""


def syncguard_pids(ps_output: str) -> dict:
    """Node label to PID of every syncguard executable started with --config."""
    pids = {}
    for line in ps_output.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        pid_text, command = parts
        words = command.split()
        if not words[0].endswith("syncguard"):
            continue
        config = _config_argument(words)
        if config.endswith("config-passive.yaml"):
            pids[BACKUP_NODE] = int(pid_text)
        elif config.endswith("config.yaml"):
            pids[PRIMARY_NODE] = int(pid_text)
    return pids


def running_syncguard(env) -> dict:
    """PIDs of the running syncguard processes by node label."""
    return syncguard_pids(env.commands.run(["ps", "-axo", "pid=,command="]).stdout)


def check_syncguard_running(env) -> str | None:
    """Both syncguard processes must run."""
    pids = running_syncguard(env)
    missing = [label for label in (PRIMARY_NODE, BACKUP_NODE) if label not in pids]
    if missing:
        return f"syncguard not running for {', '.join(missing)}"
    return None


def check_syncguard_stopped(env) -> str | None:
    """No syncguard process may run (baseline drill)."""
    pids = running_syncguard(env)
    if pids:
        return f"syncguard is running ({pids}); the baseline drill measures recovery without it"
    return None


def check_lock_owner(env) -> str | None:
    """The primary state lock must name the running active syncguard."""
    lock = Path(f"{env.paths.primary_state}.lock")
    if not lock.exists():
        return "primary state lock is missing"
    owner = lock.read_text(encoding="utf-8").strip()
    active_pid = running_syncguard(env).get(PRIMARY_NODE)
    if owner != str(active_pid):
        return f"primary state lock names PID {owner}, active syncguard PID is {active_pid}"
    return None


def check_target_signing(env, validator: str) -> str | None:
    """The validator must have signed the newest readable height."""
    height = env.rpc.tip_height() - TIP_MARGIN
    if validator not in parse_commit(env.rpc.commit(height)).signers:
        return f"{validator[:8]} did not sign height {height}"
    return None


def check_chain_advancing(env, wait_s: float = CHAIN_ADVANCE_WAIT_S) -> str | None:
    """The tip must rise within wait_s seconds."""
    first = env.rpc.tip_height()
    env.clock.sleep(wait_s)
    second = env.rpc.tip_height()
    if second <= first:
        return f"chain not advancing: tip stayed at {first} for {wait_s:.0f} s"
    return None


def check_role_change_spacing(env, min_seconds: int = ROLE_CHANGE_SPACING_S) -> str | None:
    """The last takeover or failback must be at least min_seconds old, so no warm-up window is open."""
    changes = [event.t_ms for event in both_logs(env) if event.marker in ROLE_CHANGE_MARKERS]
    if not changes:
        return None
    age_s = (env.clock.now_ms() - max(changes)) / 1000
    if age_s < min_seconds:
        return f"last takeover or failback was {age_s:.0f} s ago; wait until {min_seconds} s have passed"
    return None


def check_passive_health(env) -> str | None:
    """The passive syncguard peer server must answer /health with 200."""
    status = env.http.request("GET", PASSIVE_PEER_URL + "/health", timeout=5.0).status
    if status != 200:
        return f"passive syncguard /health answered {status}"
    return None


def check_runbook(env) -> str | None:
    """The manual-recovery runbook must exist."""
    if not env.paths.runbook.exists():
        return f"runbook {env.paths.runbook} not found"
    return None


def failover_preconditions(env, validator: str = TARGET_VALIDATOR) -> list:
    """Preconditions of failover_cycle, hang, and soak."""
    return collect_failures([
        ("key layout", lambda: check_key_layout(env.paths, validator)),
        ("loaded keys", lambda: check_loaded_addresses(env, validator)),
        ("syncguard startup", lambda: check_syncguard_started(env)),
        ("syncguard processes", lambda: check_syncguard_running(env)),
        ("state lock", lambda: check_lock_owner(env)),
        ("validator signing", lambda: check_target_signing(env, validator)),
        ("chain progress", lambda: check_chain_advancing(env)),
        ("role change spacing", lambda: check_role_change_spacing(env)),
    ])


def security_preconditions(env) -> list:
    """Preconditions of security_negatives."""
    return collect_failures([
        ("syncguard startup", lambda: check_syncguard_started(env)),
        ("syncguard processes", lambda: check_syncguard_running(env)),
        ("passive health", lambda: check_passive_health(env)),
        ("key layout", lambda: check_key_layout(env.paths, TARGET_VALIDATOR)),
        ("role change spacing", lambda: check_role_change_spacing(env)),
    ])


def lock_preconditions(env) -> list:
    """Preconditions of lock_negative."""
    return collect_failures([
        ("syncguard processes", lambda: check_syncguard_running(env)),
        ("state lock", lambda: check_lock_owner(env)),
    ])


def overhead_preconditions(env) -> list:
    """Preconditions of the overhead sampler."""
    return collect_failures([
        ("syncguard startup", lambda: check_syncguard_started(env)),
        ("syncguard processes", lambda: check_syncguard_running(env)),
        ("chain progress", lambda: check_chain_advancing(env)),
    ])


def drill_preconditions(env, validator: str = TARGET_VALIDATOR) -> list:
    """Preconditions of the baseline drill: syncguard stopped, layout correct, validator signing."""
    return collect_failures([
        ("syncguard stopped", lambda: check_syncguard_stopped(env)),
        ("key layout", lambda: check_key_layout(env.paths, validator)),
        ("loaded keys", lambda: check_loaded_addresses(env, validator)),
        ("validator signing", lambda: check_target_signing(env, validator)),
        ("chain progress", lambda: check_chain_advancing(env)),
        ("runbook", lambda: check_runbook(env)),
    ])
